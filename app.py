#!/usr/bin/env python3
import os
import re
import json
import time
import logging
import threading
import warnings
import contextvars
from typing import Optional, Dict, Any, List
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pathlib import Path
from signalwire import AgentBase, AgentServer
from signalwire.core.function_result import SwaigFunctionResult
from signalwire.rest import RestClient
from tmdb_client import TMDBClient

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class RedactSecrets(logging.Filter):
    """
    Strip credentials out of log records.

    A failed TMDB request raises an HTTPError whose text is the whole request
    URL -- api key included -- and there are ~18 `logger.error(f"...: {e}")`
    sites that would write it out verbatim. A single 404 put the key in the
    container log three times.

    Applied as a filter on the ROOT logger rather than by editing each call
    site, so it also covers the SDK's loggers and anything added later.
    """

    _PATTERNS = [
        (re.compile(r'((?:api_key|apikey|access_token|auth_token|token)=)[^&\s"\'<>]+', re.I),
         r'\1<redacted>'),
        (re.compile(r'(https?://)[^/@\s:]+:[^/@\s]+@'), r'\1<redacted>@'),
    ]

    def filter(self, record):
        try:
            message = record.getMessage()
        except Exception:
            return True
        cleaned = message
        for pattern, replacement in self._PATTERNS:
            cleaned = pattern.sub(replacement, cleaned)
        if cleaned != message:
            record.msg = cleaned
            record.args = ()
        return True


_redactor = RedactSecrets()
logging.getLogger().addFilter(_redactor)
for _h in logging.getLogger().handlers:
    _h.addFilter(_redactor)

# Store the SWML handler info for reuse
swml_handler_info = {"id": None, "address_id": None, "address": None}

# Why registration hasn't happened yet (surfaced by /get_token so a
# misconfiguration shows up in the browser, not just the server log)
swml_setup_error = None

# Guards the lazy setup retry from /get_token
swml_setup_lock = threading.Lock()


def get_signalwire_host():
    """Get the full SignalWire host from space name."""
    space = os.getenv("SIGNALWIRE_SPACE_NAME", "")
    if not space:
        return None
    if "." in space:
        return space
    return f"{space}.signalwire.com"


def get_rest_client():
    """
    Build a SignalWire RestClient from environment configuration.

    Returns None when credentials are not configured. Credentials are passed
    explicitly because RestClient's no-arg env vars (SIGNALWIRE_API_TOKEN /
    SIGNALWIRE_SPACE) don't match this demo's SIGNALWIRE_TOKEN /
    SIGNALWIRE_SPACE_NAME convention.
    """
    sw_host = get_signalwire_host()
    project = os.getenv("SIGNALWIRE_PROJECT_ID", "")
    token = os.getenv("SIGNALWIRE_TOKEN", "")
    if not all([sw_host, project, token]):
        return None
    return RestClient(project=project, token=token, host=sw_host)


# ---------------------------------------------------------------------------
# Trailer hold
#
# The agent used to talk all the way through a trailer. The cause is not the
# trailer audio -- it is that the caller goes SILENT while watching, and the
# platform's attention_timeout (the SDK's own project template ships 15000ms)
# fires to re-engage them. So every ~15 seconds the agent pipes up over the
# film. Muting the browser mic makes it worse, because the silence becomes
# total.
#
# The fix is to put the AI on hold for the duration: hold pauses speech
# detection and the agent does not respond, so no attention timeout fires.
# The browser drives it, because only the browser knows the real runtime of
# the video (YouTube's player reports it) and when the viewer closed it early.
#
# AUTHORIZATION: the browser sends its own call id, which is an untrusted
# value. Without a check, this endpoint would let anyone hold or unhold any
# call in the project. Only call ids this agent has actually served a trailer
# to are accepted, and only for a bounded time.
#
# This dict is per-process, which is safe only because the container runs a
# single worker (see the note in the Dockerfile). If that ever changes, this
# needs to move to shared state along with the agent's other session state.
# ---------------------------------------------------------------------------
_TRAILER_CALLS = {}
_TRAILER_CALL_TTL = 6 * 3600
_TRAILER_HOLD_MAX = 900      # platform ceiling for a hold, in seconds
_TRAILER_HOLD_DEFAULT = 300  # backstop until the browser reports the real runtime


def remember_trailer_call(raw_data):
    """Record the call id of a caller that has just been sent a trailer."""
    call_id = (raw_data or {}).get("call_id")
    if not call_id:
        return None
    now = time.time()
    _TRAILER_CALLS[call_id] = now
    for known, seen in list(_TRAILER_CALLS.items()):
        if now - seen > _TRAILER_CALL_TTL:
            _TRAILER_CALLS.pop(known, None)
    return call_id


def trailer_call_known(call_id):
    seen = _TRAILER_CALLS.get(call_id)
    return bool(seen) and (time.time() - seen) <= _TRAILER_CALL_TTL


# ---------------------------------------------------------------------------
# Which commit is this instance running
#
# Three deploy paths, none of which share a mechanism:
#
#   Dokku / buildpack  SOURCE_VERSION exists during the BUILD but not at
#                      runtime, so bin/post_compile writes it to COMMIT and
#                      that file ships in the slug.
#   Docker             the Dockerfile stamps GIT_COMMIT, or .git is in the
#                      image and git can be asked directly.
#   Running from src   .git is right there.
#
# Resolved once per process: a deploy replaces the process, so the value
# cannot go stale without the thing that produced it also being replaced.
# ---------------------------------------------------------------------------
_COMMIT_CACHE = None


def resolve_commit():
    """Return {commit, short, source} for whatever this process is running."""
    global _COMMIT_CACHE
    if _COMMIT_CACHE is not None:
        return _COMMIT_CACHE

    here = Path(__file__).parent
    found, source = "", "unknown"

    for var in ("SOURCE_VERSION", "GIT_COMMIT", "COMMIT_SHA", "GIT_REV"):
        value = (os.environ.get(var) or "").strip()
        if value:
            found, source = value, f"env:{var}"
            break

    if not found:
        commit_file = here / "COMMIT"
        try:
            found = commit_file.read_text(encoding="utf-8").strip()
            source = "file:COMMIT"
        except OSError:
            pass

    if not found:
        try:
            import subprocess
            found = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=here, capture_output=True,
                text=True, timeout=5,
            ).stdout.strip()
            if found:
                source = "git"
        except Exception:
            # No git binary, no .git, or a slug that stripped it. Not an error:
            # the footer just says unknown rather than the page failing.
            pass

    # Accept only something that looks like a SHA, so a stray file or a
    # mis-set variable cannot put arbitrary text into the page footer.
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", found or ""):
        found, source = "", "unknown"

    _COMMIT_CACHE = {
        "commit": found,
        "short": found[:7],
        "source": source,
        "repo": "https://github.com/signalwire-demos/cinebot",
    }
    return _COMMIT_CACHE


# ---------------------------------------------------------------------------
# Did the CALLER ask for a video, or did the model decide on its own?
#
# Playing a trailer puts the agent on hold, so a spurious call silences it
# mid-sentence -- that is how a caller who said "the first one" got a trailer
# they never asked for and never heard the film described.
#
# The tool description asks the model not to do this, but a description is a
# request, not a control. swaig_post_conversation puts the transcript on every
# SWAIG request, so the handler can read the caller's own last turn and decide
# for itself.
# ---------------------------------------------------------------------------
_VIDEO_REQUEST = re.compile(
    r"\b("
    r"trailer|teaser|preview|clip|footage|featurette|behind[- ]the[- ]scenes"
    r"|play\s+(it|that|this|the|one)"
    r"|watch\s+(it|that|this|the)"
    r"|show\s+me\s+(it|that|the)"
    r"|roll\s+it"
    r")\b",
    re.IGNORECASE,
)


def caller_last_utterance(raw_data):
    """The caller's most recent turn, or None when no transcript was sent."""
    log = (raw_data or {}).get("call_log")
    if not isinstance(log, list):
        return None
    for entry in reversed(log):
        if not isinstance(entry, dict):
            continue
        if str(entry.get("role", "")).lower() == "user":
            content = entry.get("content")
            return content if isinstance(content, str) else None
    return None


def caller_asked_for_video(raw_data):
    """
    True when the caller actually asked to see something.

    Returns True when no transcript is available: without evidence either way,
    refusing would break playback entirely on any deployment where
    swaig_post_conversation is off. The refusal only fires on POSITIVE evidence
    that the caller asked for something else.
    """
    utterance = caller_last_utterance(raw_data)
    if utterance is None:
        logger.info("No call_log on this SWAIG request; cannot verify video intent")
        return True
    asked = bool(_VIDEO_REQUEST.search(utterance))
    logger.info(f"Video intent check on {utterance[:60]!r} -> {asked}")
    return asked


def resolve_content_id(provided, current_id, mapping):
    """
    Turn whatever the model passed for content_id into a real TMDB id.

    The model routinely answers "the trailer for the first one" by passing
    content_id=1 -- a RESULT POSITION, not a TMDB id. The old code took any
    supplied value at face value (`content_id or current`), so that became
    GET /movie/1 and a 404, and the caller was told no videos exist for a film
    that was on screen a second earlier.

    A small integer that is also a live result position is treated as a
    position; anything else is passed through untouched.
    """
    if not provided:
        return current_id
    try:
        value = int(provided)
    except (TypeError, ValueError):
        return current_id
    if 1 <= value <= 20 and mapping and value in mapping:
        mapped = (mapping.get(value) or {}).get("id")
        if mapped:
            logger.info(f"content_id={value} looks like result position {value}; using id {mapped}")
            return mapped
    return value


def find_resource_address(addresses, agent_name):
    """
    Find the resource address matching /public/{agent_name} from a list of addresses.

    When phone numbers are attached to a handler, multiple addresses exist.
    We want the resource address (e.g., /public/cinebot) not the phone number address.
    """
    expected_address = f"/public/{agent_name}"

    # First, try to find exact match for /public/{agent_name}
    for addr in addresses:
        audio_channel = addr.get("channels", {}).get("audio", "")
        if audio_channel == expected_address:
            return addr

    # Fallback: find any address that looks like a resource address (not a phone number)
    for addr in addresses:
        audio_channel = addr.get("channels", {}).get("audio", "")
        if audio_channel.startswith("/public/") and not any(c.isdigit() for c in audio_channel.split("/")[-1][:3]):
            return addr

    # Last resort: return first address
    return addresses[0] if addresses else None


def find_existing_handler(client, agent_name):
    """Find an existing SWML handler by name."""
    try:
        # List all SWML webhook handlers in the project
        handlers = client.fabric.swml_webhooks.list().get("data", [])

        for handler in handlers:
            # The name is nested in swml_webhook object
            swml_webhook = handler.get("swml_webhook", {})
            handler_name = swml_webhook.get("name") or handler.get("display_name")

            # Check if this handler matches our agent name
            if handler_name == agent_name:
                handler_id = handler.get("id")
                handler_url = swml_webhook.get("primary_request_url", "")

                # Get the address for this handler (needed for token scoping)
                addresses = client.fabric.swml_webhooks.list_addresses(handler_id).get("data", [])
                resource_addr = find_resource_address(addresses, agent_name)
                if resource_addr:
                    return {
                        "id": handler_id,
                        "name": handler_name,
                        "url": handler_url,
                        "address_id": resource_addr["id"],
                        "address": resource_addr["channels"]["audio"]
                    }
    except Exception as e:
        logger.error(f"Error finding existing handler: {e}")
    return None


def setup_swml_handler():
    """Set up SWML handler on startup."""
    global swml_setup_error

    client = get_rest_client()
    agent_name = os.getenv("AGENT_NAME", "cinebot")
    proxy_url = os.getenv("SWML_PROXY_URL_BASE", os.getenv("APP_URL", ""))
    auth_user = os.getenv("SWML_BASIC_AUTH_USER", "signalwire")
    auth_pass = os.getenv("SWML_BASIC_AUTH_PASSWORD", "")

    if client is None:
        swml_setup_error = ("SIGNALWIRE_SPACE_NAME / SIGNALWIRE_PROJECT_ID / "
                            "SIGNALWIRE_TOKEN not set")
        logger.warning(f"{swml_setup_error} - skipping SWML handler setup")
        return

    if not proxy_url:
        swml_setup_error = ("SWML_PROXY_URL_BASE (or APP_URL) not set - it must be "
                            "the public URL SignalWire can fetch SWML from "
                            "(e.g. your ngrok URL)")
        logger.warning(f"{swml_setup_error} - skipping SWML handler setup")
        return

    # Build SWML URL with basic auth credentials
    if auth_user and auth_pass and "://" in proxy_url:
        scheme, rest = proxy_url.split("://", 1)
        swml_url = f"{scheme}://{auth_user}:{auth_pass}@{rest}/cinebot"
    else:
        swml_url = proxy_url + "/cinebot"

    # Look for an existing handler by name
    existing = find_existing_handler(client, agent_name)
    if existing:
        swml_handler_info["id"] = existing["id"]
        swml_handler_info["address_id"] = existing["address_id"]
        swml_handler_info["address"] = existing["address"]
        swml_setup_error = None

        # Always update the URL to ensure credentials are current
        try:
            client.fabric.swml_webhooks.update(
                existing["id"],
                primary_request_url=swml_url,
                primary_request_method="POST"
            )
            logger.info(f"Updated SWML handler: {existing['name']}")
        except Exception as e:
            logger.error(f"Failed to update handler URL: {e}")

        logger.info(f"Call address: {existing['address']}")
    else:
        # Create a new external SWML handler with the agent name
        try:
            # A standalone dialable handler (not bound to a phone number) is
            # intentional here, so silence the SDK warning that steers
            # phone-number setups toward phone_numbers.set_swml_webhook
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                handler_resp = client.fabric.swml_webhooks.create(
                    name=agent_name,
                    used_for="calling",
                    primary_request_url=swml_url,
                    primary_request_method="POST"
                )
            handler_id = handler_resp.get("id")
            swml_handler_info["id"] = handler_id

            # Get the dialable address for this handler
            addresses = client.fabric.swml_webhooks.list_addresses(handler_id).get("data", [])
            resource_addr = find_resource_address(addresses, agent_name)
            if resource_addr:
                swml_handler_info["address_id"] = resource_addr["id"]
                swml_handler_info["address"] = resource_addr["channels"]["audio"]
                swml_setup_error = None
            else:
                swml_setup_error = f"handler '{agent_name}' created but no dialable address found"

            logger.info(f"Created SWML handler '{agent_name}' with address: {swml_handler_info.get('address')}")
        except Exception as e:
            logger.error(f"Failed to create SWML handler: {e}")
            # Retry finding existing handler (another worker may have just created it)
            time.sleep(0.5)
            existing = find_existing_handler(client, agent_name)
            if existing:
                swml_handler_info["id"] = existing["id"]
                swml_handler_info["address_id"] = existing["address_id"]
                swml_handler_info["address"] = existing["address"]
                swml_setup_error = None
                logger.info(f"Found existing SWML handler after retry: {existing['name']}")
                logger.info(f"Call address: {existing['address']}")
            else:
                swml_setup_error = f"failed to create handler '{agent_name}': {e}"


# ---------------------------------------------------------------------------
# Per-call conversation state
#
# One MovieAgent instance serves every caller. These nine values describe "what
# this caller is currently looking at", so holding them as plain instance
# attributes meant all callers shared one set: a second caller searching
# overwrote the first caller's search_result_mapping, and the first caller's
# next "tell me about number 4" resolved against the wrong list. The watchlist
# was worse -- a single global list, additionally readable by anyone over an
# unauthenticated GET /api/watchlist.
#
# They are now per-call, keyed by the call id bound in on_function_call. A
# ContextVar is what makes that safe under concurrency: each request runs in
# its own context, so two callers dispatching tools at the same time cannot see
# each other's binding.
# ---------------------------------------------------------------------------
_CURRENT_CALL_ID = contextvars.ContextVar("cinebot_current_call_id", default=None)

SESSION_DEFAULTS = {
    "current_search_results": list,
    "search_result_mapping": dict,     # position -> movie/TV details with ids
    "person_search_mapping": dict,     # position -> person details with ids
    "last_search_info": str,           # info about the last search, for the AI
    "last_person_search_info": str,
    "current_movie_id": lambda: None,
    "current_person_id": lambda: None,
    "current_tv_id": lambda: None,
    "watchlist": list,
}

# A call that has gone quiet is dropped; the cap stops an unbounded demo from
# accumulating sessions if calls are never cleanly torn down.
_SESSION_TTL = 6 * 3600
_SESSION_MAX = 500


class MovieAgent(AgentBase):
    def __init__(self):
        super().__init__(
            name="CineBot Movie Assistant",
            route="/cinebot"  # Must match server.register() route
        )
        
        # Initialize TMDB client
        self.tmdb = TMDBClient(
            api_key=os.getenv("TMDB_API_KEY"),
            redis_url=os.getenv("REDIS_URL")
        )
        
        # Per-call conversation state. There is ONE MovieAgent serving every
        # caller, so the attributes listed in SESSION_DEFAULTS used to be plain
        # instance attributes shared by everyone: two callers browsing at once
        # overwrote each other's search results, and "tell me about number 4"
        # could resolve against somebody else's list. They are now properties
        # backed by this dict, keyed by call id (see _session and the property
        # installation below the class).
        self._sessions = {}
        self._sessions_seen = {}

        # Setup agent configuration
        self._setup_agent()
        self._setup_functions()

    def _session(self):
        """
        State for the call currently being served, created on first use.

        The call id comes from a ContextVar set in on_function_call, which is
        the single point every SWAIG tool is dispatched through
        (signalwire/core/swml_service.py calls target.on_function_call). Each
        request runs in its own context, so concurrent callers cannot see each
        other's value.

        Outside a tool call the id is None and everything shares one bucket.
        That is only reached by code that is not serving a specific caller;
        no HTTP route returns it (see /api/watchlist).
        """
        call_id = _CURRENT_CALL_ID.get()
        now = time.time()

        if len(self._sessions) > _SESSION_MAX or (
            self._sessions and now - min(self._sessions_seen.values()) > _SESSION_TTL
        ):
            for known, seen in list(self._sessions_seen.items()):
                if now - seen > _SESSION_TTL:
                    self._sessions.pop(known, None)
                    self._sessions_seen.pop(known, None)

        session = self._sessions.get(call_id)
        if session is None:
            session = {name: factory() for name, factory in SESSION_DEFAULTS.items()}
            self._sessions[call_id] = session
        self._sessions_seen[call_id] = now
        return session

    def on_function_call(self, name, args, raw_data=None):
        """
        Bind the calling call id for the duration of one tool call.

        Every SWAIG tool reaches the agent through here, so this is the only
        place that needs to know about per-call state; the seventeen handlers
        and their ~120 `self.<attr>` references are unchanged.
        """
        call_id = (raw_data or {}).get("call_id")
        token = _CURRENT_CALL_ID.set(call_id)
        try:
            return super().on_function_call(name, args, raw_data)
        finally:
            _CURRENT_CALL_ID.reset(token)


    def _setup_agent(self):
        """Configure agent personality and conversation contexts"""

        # Note: video_idle_file and video_talking_file are set dynamically
        # in on_swml_request() with the full URL from get_full_url()

        # Agent personality
        # Puts the transcript on every SWAIG request so handlers can check what
        # the caller actually said. get_videos relies on it to refuse playing a
        # trailer nobody asked for; without it that check has nothing to read.
        self.set_param("swaig_post_conversation", True)

        self.set_param("voice_id", "en-US-Standard-J")
        self.set_param("voice_pitch", "-2st")
        self.set_param("voice_rate", "95%")
        
        # Greeting message
        self.set_param("greeting_text", 
            "Hello! I'm CineBot, your personal entertainment expert. "
            "I can help you discover movies and TV shows, learn about actors, "
            "find trending content, or explore different genres. "
            "What movie, TV show, or actor would you like to know about?"
        )
        
        # Set initial background display
        self.set_param("initial_background", "/background.png")
        
        # Configure voice
        self.add_language(
            name="English",
            code="en-US",
            voice="elevenlabs.adam"
        )
        
        # Add speech hints for better recognition
        self.add_hints([
            "movie", "film", "actor", "actress", "director",
            "TV", "show", "series", "season", "episode",
            "trailer", "cast", "crew", "genre", "rating",
            "search", "find", "tell", "about",
            "trending", "popular", "similar", "recommend",
            "watch", "stream", "netflix", "amazon", "disney",
            "yes", "no", "more", "details", "back"
        ])
        
        # Define conversation contexts with state machine
        contexts = self.define_contexts()
        
        default_context = contexts.add_context("default") \
            .add_section("Goal", "Help users discover and learn about movies, TV shows, actors, and entertainment.")
        
        # GREETING STATE - Entry point
        default_context.add_step("greeting") \
            .add_section("Current Task", "Welcome the user and understand what they want to explore") \
            .add_bullets("Available Actions", [
                "Search for movies by title",
                "Search for TV shows by title",
                "Search for actors or directors",
                "Show trending movies or TV shows",
                "Browse by genre",
                "Clear the display"
            ]) \
            .set_step_criteria("User has made an initial request") \
            .set_functions([
                "multi_search", "search_movie", "search_tv", "search_person",
                "get_trending", "get_trending_tv", "get_movies_by_genre",
                "get_now_playing", "discover_content", "clear_display"
            ]) \
            .set_valid_steps(["browsing", "movie_details", "tv_details", "person_details"])
        
        # BROWSING STATE - After search results
        default_context.add_step("browsing") \
            .add_section("Current Task", "User is browsing search results") \
            .add_section("CRITICAL RULE", "CHECK self.last_search_info which contains movie/TV IDs for each position! When user says 'first one', use search_position=1. ALWAYS use the ID from self.last_search_info or search_position parameter.") \
            .add_bullets("Available Actions", [
                "Get details about a specific movie or TV show",
                "Search for more movies or TV shows",
                "Search for people",
                "View trending content",
                "Browse genres",
                "Add to watchlist"
            ]) \
            .set_step_criteria("User wants to explore specific content") \
            .set_functions([
                "multi_search", "search_movie", "search_tv", "get_movie_details",
                "get_tv_details", "search_person", "get_trending", "get_trending_tv",
                "get_movies_by_genre", "discover_content", "clear_display",
                "add_to_watchlist"
            ]) \
            .set_valid_steps(["movie_details", "tv_details", "person_details", "greeting"])
        
        # MOVIE DETAILS STATE - Viewing specific movie
        default_context.add_step("movie_details") \
            .add_section("Current Task", "User is viewing movie details") \
            .add_bullets("Available Actions", [
                "Show cast and crew",
                "Find similar movies",
                "Play trailer",
                "Add to watchlist",
                "Search for other content"
            ]) \
            .set_step_criteria("User wants more information about the movie") \
            .set_functions([
                "get_cast_crew", "get_similar_content", "get_videos",
                "add_to_watchlist", "search_movie", "search_person",
                "clear_display"
            ]) \
            .set_valid_steps(["browsing", "person_details", "greeting"])
        
        # TV DETAILS STATE - Viewing specific TV show
        default_context.add_step("tv_details") \
            .add_section("Current Task", "User is viewing TV show details") \
            .add_bullets("Available Actions", [
                "Show cast and crew",
                "Explore seasons and episodes",
                "Find similar TV shows",
                "Play trailer",
                "Add to watchlist",
                "Search for other content"
            ]) \
            .set_step_criteria("User wants more information about the TV show") \
            .set_functions([
                "get_cast_crew", "get_season_details", "get_similar_content",
                "get_videos", "add_to_watchlist", "search_movie", "search_tv",
                "search_person", "clear_display"
            ]) \
            .set_valid_steps(["browsing", "person_details", "greeting"])
        
        # PERSON DETAILS STATE - Viewing actor/director
        default_context.add_step("person_details") \
            .add_section("Current Task", "User is viewing person details") \
            .add_section("CRITICAL RULE", "The person's filmography contains movie IDs. When user wants a movie from the filmography, use get_movie_details with the movie_id from the displayed films.") \
            .add_bullets("Available Actions", [
                "Get movie details from filmography (use movie IDs)",
                "Search for other people",
                "Search for movies",
                "Clear and start over"
            ]) \
            .set_step_criteria("User wants to explore other content") \
            .set_functions([
                "get_movie_details", "search_movie", "search_person",
                "clear_display"
            ]) \
            .set_valid_steps(["movie_details", "browsing", "greeting"])
        
        # Agent prompts
        self.prompt_add_section(
            "personality",
            "You are CineBot, a passionate entertainment enthusiast with encyclopedic "
            "knowledge of movies and TV shows. You're excited to share recommendations, "
            "trivia, and help users discover great content. You have a friendly, "
            "engaging personality and love discussing both films and television."
        )
        
        self.prompt_add_section(
            "instructions",
            "CRITICAL CONTENT SELECTION RULES:\n"
            "1. ALWAYS search first before calling get_movie_details or get_tv_details\n"
            "2. After search, CHECK self.last_search_info which has IDs for each position!\n"
            "3. When user selects content:\n"
            "   - 'first one' or 'number 1' → use appropriate get_details(search_position=1)\n"
            "   - By name → find it in self.last_search_info, get its ID\n"
            "   - 'the second one' → use get_details(search_position=2)\n"
            "4. Distinguish between movies and TV shows:\n"
            "   - Movies: use search_movie and get_movie_details\n"
            "   - TV shows: use search_tv and get_tv_details\n"
            "5. When user selects a person:\n"
            "   - Check self.last_person_search_info for person IDs\n"
            "   - Use search_person(search_position=N) or search_person(person_id=XXX)\n"
            "6. For multi-search results:\n"
            "   - Check the type field in search_result_mapping\n"
            "   - Use get_movie_details for movies, get_tv_details for TV, search_person for people\n"
            "7. ALWAYS use either ID or search_position parameters\n"
            "8. NEVER mention IDs to the user - they are for internal use only\n"
            "\n"
            "USER INTERACTION RULES:\n"
            "- NEVER show IDs to users in responses - keep them internal\n"
            "- Check self.last_search_info to get the correct ID\n"
            "- When presenting results, show title/name and year only\n" 
            "- If search returns no results, try searching with fewer words\n"
            "- Clear the display before showing new content\n"
            "- Mention what else is available (seasons for TV, trailers for movies), but "
            "wait to be asked before playing anything\n"
            "- When user asks for content with filters (year, genre, rating), use discover_content\n"
            "- When user asks general search without specifying type, use multi_search"
        )
        
        self.prompt_add_section(
            "error_handling",
            "If a movie or person search returns no results, suggest alternatives or "
            "ask for clarification. If an API error occurs, apologize briefly and "
            "suggest trying again or searching for something else."
        )
    
    def _setup_functions(self):
        """Register SWAIG functions for movie operations"""
        
        @self.tool(
            name="search_movie",
            description="Search for movies by title",
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The movie title to search for"
                    }
                },
                "required": ["query"]
            }
        )
        def search_movie(args, raw_data):
            query = args.get("query", "").strip()
            logger.info(f"search_movie called with query: '{query}'")
            
            if not query:
                return SwaigFunctionResult(
                    response="Please provide a movie title to search for."
                )
            
            # Parse out year from query if present.
            #
            # The year must be INTRODUCED -- "from 1990", "in 1990" or "(1990)".
            # A bare four-digit number is part of the title, not a filter: the
            # previous pattern made the prefix optional, so "Blade Runner 2049"
            # searched for "Blade Runner" released in 2049 and found nothing,
            # "1917" and "2012" searched for an empty string, and "2001: A
            # Space Odyssey" searched for ": A Space Odyssey".
            import re
            YEAR_PHRASE = r'\b(?:from|in)\s+(19\d{2}|20\d{2})\b|\((19\d{2}|20\d{2})\)'
            year_match = re.search(YEAR_PHRASE, query, re.IGNORECASE)
            search_query = query
            year_filter = None

            if year_match:
                year_filter = year_match.group(1) or year_match.group(2)
                # Strip only the matched phrase, leaving the rest of the title.
                search_query = re.sub(YEAR_PHRASE, '', query, flags=re.IGNORECASE)
                search_query = re.sub(r'\s{2,}', ' ', search_query).strip(' ,')
                logger.info(f"Parsed query: title='{search_query}', year={year_filter}")
            
            try:
                results = self.tmdb.search_movie(search_query)
                logger.info(f"TMDB returned {len(results.get('results', []))} results for '{search_query}'")
                self.current_search_results = results["results"]
                
                # If no results, try alternative search strategies
                if not results["results"] and len(search_query) > 2:
                    logger.info(f"No results for '{search_query}', trying alternative search strategies")
                    
                    # Try searching without common words
                    alt_query = re.sub(r'\b(the|a|an)\b', '', search_query, flags=re.IGNORECASE).strip()
                    if alt_query != search_query:
                        results = self.tmdb.search_movie(alt_query)
                        logger.info(f"Alternative search '{alt_query}' returned {len(results.get('results', []))} results")
                        self.current_search_results = results["results"]
                
                if results["results"]:
                    # Filter by year if specified
                    filtered_results = results["results"]
                    if year_filter:
                        filtered_results = [
                            m for m in results["results"]
                            if m.get('release_date', '').startswith(year_filter)
                        ]
                        logger.info(f"Filtered to {len(filtered_results)} results for year {year_filter}")
                    
                    if not filtered_results:
                        return SwaigFunctionResult(
                            response=f"I couldn't find '{search_query}' from {year_filter}. "
                            f"Try searching without the year or check if the year is correct."
                        )
                    
                    # Build more detailed movie list and store mapping for AI
                    movie_descriptions = []
                    self.search_result_mapping = {}  # Reset mapping
                    
                    for i, m in enumerate(filtered_results[:24], 1):  # Show more results for better matching
                        year = m.get('release_date', '')[:4] if m.get('release_date') else 'unknown year'
                        # Include ID directly in the response text for LLM to see
                        movie_descriptions.append(f"{i}. id: {m['id']} title: '{m['title']}' ({year})")
                        
                        # Store mapping for AI to use internally
                        self.search_result_mapping[i] = {
                            "id": m['id'],
                            "title": m['title'],
                            "year": year,
                            "overview": m.get('overview', '')[:100]
                        }
                    
                    # Store the filtered results for later reference
                    self.current_search_results = filtered_results
                    
                    # Create info for AI about the search results with IDs
                    self.last_search_info = f"SEARCH RESULTS WITH IDS for '{query}':\n"
                    for pos, info in self.search_result_mapping.items():
                        self.last_search_info += f"  Position {pos}: {info['title']} ({info['year']}) -> movie_id={info['id']}\n"
                    
                    # Log the mapping so we can debug
                    logger.info(f"Search mapping: {self.last_search_info}")
                    
                    result = SwaigFunctionResult(
                        response=f"I found {len(filtered_results)} movies matching '{search_query}'"
                        f"{f' from {year_filter}' if year_filter else ''}. "
                        f"Here are the results:\n{chr(10).join(movie_descriptions)}\n"
                        f"Which movie would you like to know more about?"
                    )
                else:
                    result = SwaigFunctionResult(
                        response=f"I couldn't find any movies matching '{query}'. "
                        f"Try searching with a different title or let me show you trending movies."
                    )
                
                # Send event to frontend (frontend will clear display when handling this)
                logger.info(f"Sending movie_search_results event with {len(results['results'])} movies")
                result.swml_user_event({
                    "type": "movie_search_results",
                    "data": results
                })
                
                # Transition to browsing state
                result.swml_change_step("browsing")
                logger.info("Transitioned to browsing state")
                
                return result
            except Exception as e:
                logger.error(f"Error searching movies: {e}")
                return SwaigFunctionResult(
                    response="I encountered an error searching for movies. Please try again."
                )
        
        @self.tool(
            name="get_movie_details",
            description="Get detailed information about a specific movie",
            parameters={
                "type": "object",
                "properties": {
                    "movie_title": {
                        "type": "string",
                        "description": "The title of the movie (optional if movie_id provided)"
                    },
                    "movie_id": {
                        "type": "integer",
                        "description": "The TMDB ID of the movie (preferred - use this from search results)"
                    },
                    "search_position": {
                        "type": "integer",
                        "description": "Position in search results (1-based index, e.g., 1 for first result)"
                    }
                },
                "required": []
            }
        )
        def get_movie_details(args, raw_data):
            movie_id = args.get("movie_id")
            movie_title = args.get("movie_title")
            search_position = args.get("search_position")
            logger.info(f"get_movie_details called with movie_id={movie_id}, movie_title={movie_title}, search_position={search_position}")
            
            # Priority 1: Use movie_id if provided
            if movie_id:
                logger.info(f"Using provided movie_id: {movie_id}")
            
            # Priority 2: Use search position if provided
            elif search_position and self.search_result_mapping:
                if search_position in self.search_result_mapping:
                    movie_info = self.search_result_mapping[search_position]
                    movie_id = movie_info["id"]
                    movie_title = movie_info["title"]
                    logger.info(f"Selected movie at position {search_position}: '{movie_title}' (ID: {movie_id})")
                else:
                    logger.warning(f"Position {search_position} not found in search results")
            
            # Priority 3: Try to match from current search results
            elif movie_title and self.current_search_results:
                logger.info(f"Matching '{movie_title}' from current search results")
                import re
                
                # Extract year if present in title
                year_match = re.search(r'\b(19\d{2}|20\d{2})\b', movie_title)
                requested_year = year_match.group(1) if year_match else None
                
                # Clean title for matching
                clean_title = re.sub(r'\b(19\d{2}|20\d{2})\b', '', movie_title).strip()
                clean_title = re.sub(r'[^\w\s]', '', clean_title).lower()
                
                # Find best match from current results
                best_match = None
                best_score = 0
                
                for movie in self.current_search_results:
                    score = 0
                    movie_clean = re.sub(r'[^\w\s]', '', movie["title"]).lower()
                    
                    # Exact title match
                    if movie_clean == clean_title:
                        score += 100
                    elif clean_title in movie_clean or movie_clean in clean_title:
                        score += 50
                    
                    # Year match
                    if requested_year and requested_year in movie.get("release_date", ""):
                        score += 50
                    
                    if score > best_score:
                        best_score = score
                        best_match = movie
                
                if best_match:
                    movie_id = best_match["id"]
                    logger.info(f"Best match from search results: '{best_match['title']}' (ID: {movie_id}, score: {best_score})")
            
            # Priority 4: Do a fresh search if we still don't have an ID
            if not movie_id and movie_title:
                logger.info(f"No movie_id provided, searching for '{movie_title}'")
                import re
                
                # ALWAYS do a fresh search to ensure we have the right data
                # Extract year if present
                year_match = re.search(r'\b(19\d{2}|20\d{2})\b', movie_title)
                requested_year = year_match.group(1) if year_match else None
                
                # Clean title for searching
                clean_title = re.sub(r'\b(19\d{2}|20\d{2})\b', '', movie_title)
                clean_title = re.sub(r'\b(with|starring|julia roberts|richard gere|julia|roberts|gere|from|the|one)\b', '', clean_title, flags=re.IGNORECASE)
                clean_title = clean_title.strip()
                
                logger.info(f"Searching for clean title: '{clean_title}', requested year: {requested_year}")
                
                # Always search fresh to get consistent results
                search_results = self.tmdb.search_movie(clean_title)
                
                if search_results["results"]:
                    # Special handling for Pretty Woman - ALWAYS get the 1990 version unless specified otherwise
                    if "pretty woman" in clean_title.lower():
                        # Default to 1990 version
                        for movie in search_results["results"]:
                            if "1990" in movie.get("release_date", ""):
                                movie_id = movie["id"]
                                logger.info(f"Selected Pretty Woman 1990 (default) with ID {movie_id}")
                                break
                        # Only use a different version if year is explicitly different
                        if requested_year and requested_year != "1990":
                            for movie in search_results["results"]:
                                if requested_year in movie.get("release_date", ""):
                                    movie_id = movie["id"]
                                    logger.info(f"Selected Pretty Woman {requested_year} (requested) with ID {movie_id}")
                                    break
                    else:
                        # For other movies, use simple scoring
                        best_match = None
                        best_score = 0
                        
                        for movie in search_results["results"]:
                            score = 0
                            
                            # Title match
                            if movie["title"].lower() == clean_title.lower():
                                score += 20
                            elif clean_title.lower() in movie["title"].lower():
                                score += 10
                            
                            # Year match is most important if specified
                            if requested_year and movie.get("release_date"):
                                if requested_year in movie["release_date"]:
                                    score += 50  # Heavy weight for year match
                            
                            # Use popularity as tiebreaker only
                            if score > 0:
                                score += min(movie.get("popularity", 0) / 100, 2)
                            
                            if score > best_score:
                                best_score = score
                                best_match = movie
                        
                        if best_match:
                            movie_id = best_match["id"]
                            logger.info(f"Selected {best_match['title']} ({best_match.get('release_date', 'N/A')[:4]}) with ID {movie_id} (score: {best_score})")
                        elif search_results["results"]:
                            # Fallback to first result if no good match
                            movie_id = search_results["results"][0]["id"]
                            logger.info(f"No good match, using first result: {search_results['results'][0]['title']}")
            
            if not movie_id:
                result = SwaigFunctionResult(
                    response="Please specify which movie you'd like details about."
                )
                return result
            
            try:
                details = self.tmdb.get_movie_details(movie_id)
                self.current_movie_id = movie_id
                
                # Build response
                genres = ", ".join(details["genres"][:3])
                runtime_hours = details["runtime"] // 60
                runtime_mins = details["runtime"] % 60
                
                response = f"Here's {details['title']} from {details['release_date'][:4] if details['release_date'] else 'unknown year'}. "
                
                if details["tagline"]:
                    response += f"\"{details['tagline']}\". "
                
                response += f"It's a {genres} film that runs {runtime_hours} hours and {runtime_mins} minutes. "
                response += f"The movie has a rating of {details['vote_average']:.1f} out of 10. "
                
                if details["overview"]:
                    response += f"Here's what it's about: {details['overview'][:200]}... "
                
                # Check if trailer is available before offering it
                has_trailer = False
                if details.get("videos"):
                    has_trailer = any(v["type"] == "Trailer" for v in details["videos"])
                
                # Build options based on available content
                options = []
                if has_trailer:
                    options.append("play the trailer")
                options.append("find similar movies")
                options.append("tell you about the cast members shown on screen")
                
                response += f"You can ask me to {', or '.join(options)}."
                
                result = SwaigFunctionResult(response=response)
                
                # Get watch provider information and add to details
                try:
                    providers = self.tmdb.get_watch_providers(movie_id)
                    if providers:
                        details["watch_providers"] = providers
                        logger.info(f"Added {len(providers.get('providers', []))} watch providers to details")
                except Exception as e:
                    logger.error(f"Error getting watch providers: {e}")
                    details["watch_providers"] = None
                
                # Send event to frontend with all details including providers (frontend will clear display)
                event_data = {
                    "type": "movie_details",
                    "data": details
                }
                logger.info(f"Sending movie_details event for '{details['title']}'")
                result.swml_user_event(event_data)
                
                # Transition to movie_details state
                result.swml_change_step("movie_details")
                logger.info("Transitioned to movie_details state")
                
                return result
                
            except Exception as e:
                logger.error(f"Error getting movie details: {e}")
                result = SwaigFunctionResult(
                    response="I couldn't fetch the movie details. Please try again."
                )
                return result
        
        @self.tool(
            name="get_cast_crew",
            description="Get cast and crew information for a movie or TV show",
            parameters={
                "type": "object",
                "properties": {
                    "content_id": {
                        "type": "integer",
                        "description": "TMDB id of the movie or TV show. Omit it to use whatever is currently on screen. This is NOT a result position -- never pass 1, 2, 3 here."
                    },
                    "content_type": {
                        "type": "string",
                        "description": "Type of content",
                        "enum": ["movie", "tv"]
                    }
                },
                "required": []
            }
        )
        def get_cast_crew(args, raw_data):
            # Determine content type and ID
            content_type = args.get("content_type")
            content_id = resolve_content_id(
                args.get("content_id"),
                None,
                self.search_result_mapping,
            )
            
            # Auto-detect based on current state if not provided
            if not content_type:
                if self.current_movie_id:
                    content_type = "movie"
                    content_id = content_id or self.current_movie_id
                elif self.current_tv_id:
                    content_type = "tv"
                    content_id = content_id or self.current_tv_id
                else:
                    return SwaigFunctionResult(
                        response="Please select a movie or TV show first to see its cast and crew."
                    )
            else:
                if content_type == "movie":
                    content_id = content_id or self.current_movie_id
                else:
                    content_id = content_id or self.current_tv_id
            
            if not content_id:
                result = SwaigFunctionResult(
                    response=f"Please select a {'movie' if content_type == 'movie' else 'TV show'} first to see its cast and crew."
                )
                return result
            
            try:
                # Get details based on content type
                if content_type == "movie":
                    details = self.tmdb.get_movie_details(content_id)
                else:
                    details = self.tmdb.get_tv_details(content_id)
                
                cast_crew = {
                    "cast": details.get("cast", []),
                    "crew": details.get("crew", [])
                }
                
                # Build response - store cast IDs for voice navigation
                top_cast = cast_crew["cast"][:5]
                cast_descriptions = []
                self.person_search_mapping = {}  # Reset person mapping for voice selection
                
                for i, actor in enumerate(top_cast, 1):
                    if content_type == "movie":
                        cast_descriptions.append(f"{actor['name']} as {actor.get('character', 'Unknown')}")
                    else:
                        # TV shows might have roles instead of character
                        cast_descriptions.append(f"{actor['name']} as {actor.get('character', actor.get('roles', [{}])[0].get('character', 'Unknown'))}")
                    
                    # Store mapping for voice selection
                    self.person_search_mapping[i] = {
                        "id": actor["id"],
                        "name": actor["name"],
                        "character": actor.get("character", "")
                    }
                
                # Find key crew members
                if content_type == "movie":
                    director = next((c for c in cast_crew["crew"] if c["job"] == "Director"), None)
                    producer = next((c for c in cast_crew["crew"] if c["job"] == "Producer"), None)
                    writer = next((c for c in cast_crew["crew"] if "Writer" in c.get("job", "") or "Screenplay" in c.get("job", "")), None)
                else:
                    # TV shows have creators instead of directors
                    creators = details.get("created_by", [])
                    executive_producers = [c for c in cast_crew["crew"] if c["job"] == "Executive Producer"][:2]
                
                # Build voice-friendly response
                response = f"Here's the cast and crew for {details.get('title', details.get('name', 'this content'))}. "
                
                if cast_descriptions:
                    response += f"The main cast includes: {', '.join(cast_descriptions[:3])}. "
                    if len(cast_descriptions) > 3:
                        response += f"Also featuring {', '.join([actor['name'] for actor in top_cast[3:5]])}. "
                
                if content_type == "movie":
                    if director:
                        response += f"Directed by {director['name']}. "
                    if writer:
                        response += f"Written by {writer['name']}. "
                    if producer:
                        response += f"Produced by {producer['name']}. "
                else:
                    if creators:
                        response += f"Created by {', '.join([c['name'] for c in creators[:2]])}. "
                    if executive_producers:
                        response += f"Executive produced by {', '.join([p['name'] for p in executive_producers])}. "
                
                response += "Would you like to know more about any of these people?"
                
                # Update last person search info for AI
                self.last_person_search_info = f"CAST MEMBERS WITH IDS:\n"
                for pos, info in self.person_search_mapping.items():
                    self.last_person_search_info += f"  Position {pos}: {info['name']} -> person_id={info['id']}\n"
                
                logger.info(f"Cast mapping: {self.last_person_search_info}")
                
                result = SwaigFunctionResult(response=response)
                
                # Check if this is a different movie/show than what's currently displayed
                # If so, send full details instead of just cast
                should_update_full_display = False
                if content_type == "movie" and content_id != self.current_movie_id:
                    should_update_full_display = True
                    self.current_movie_id = content_id
                    self.current_tv_id = None
                elif content_type == "tv" and content_id != self.current_tv_id:
                    should_update_full_display = True
                    self.current_tv_id = content_id
                    self.current_movie_id = None
                
                if should_update_full_display:
                    # Send full movie/TV details event to update entire display
                    event_type = "movie_details" if content_type == "movie" else "tv_details"
                    result.swml_user_event({
                        "type": event_type,
                        "data": details
                    })
                    
                    # Also change state
                    if content_type == "movie":
                        result.swml_change_step("movie_details")
                    else:
                        result.swml_change_step("tv_details")
                else:
                    # Just update cast section if it's the same content
                    result.swml_user_event({
                        "type": "cast_crew_display",
                        "data": cast_crew
                    })
                
                return result
                
            except Exception as e:
                logger.error(f"Error getting cast/crew: {e}")
                result = SwaigFunctionResult(
                    response="I couldn't fetch the cast information. Please try again."
                )
                return result
        
        @self.tool(
            name="get_now_playing",
            description="Get movies currently playing in theaters",
            parameters={
                "type": "object",
                "properties": {
                    "region": {
                        "type": "string",
                        "description": "Country code (e.g., 'US', 'UK', 'CA')",
                        "default": "US"
                    }
                },
                "required": []
            }
        )
        def get_now_playing(args, raw_data):
            region = args.get("region", "US")
            logger.info(f"get_now_playing called for region: {region}")
            
            try:
                results = self.tmdb.get_now_playing(region=region)
                
                if results["results"]:
                    movie_list = []
                    self.search_result_mapping = {}  # Use same mapping as search
                    
                    for i, m in enumerate(results["results"][:24], 1):
                        year = m.get('release_date', '')[:4] if m.get('release_date') else ''
                        movie_list.append(f"{i}. '{m['title']}' ({year}) - Rating: {m['vote_average']:.1f}/10")
                        
                        # Store mapping for AI
                        self.search_result_mapping[i] = {
                            "id": m['id'],
                            "title": m['title'],
                            "year": year
                        }
                    
                    response = f"Here are the movies currently playing in theaters"
                    if region != "US":
                        response += f" in {region}"
                    response += f":\n{chr(10).join(movie_list)}\n"
                    response += "Which movie would you like to know more about?"
                    
                    result = SwaigFunctionResult(response=response)
                    
                    # Send event to frontend
                    result.swml_user_event({
                        "type": "now_playing",
                        "data": results
                    })
                    
                    # Transition to browsing state
                    result.swml_change_step("browsing")
                    
                    return result
                else:
                    return SwaigFunctionResult(
                        response="I couldn't find any movies currently playing in theaters. Try checking trending movies instead."
                    )
                    
            except Exception as e:
                logger.error(f"Error getting now playing: {e}")
                return SwaigFunctionResult(
                    response="I had trouble getting current theater listings. Let me show you trending movies instead."
                )
        
        @self.tool(
            name="get_similar_content", 
            description="Find similar movies or TV shows to the current one",
            parameters={
                "type": "object",
                "properties": {
                    "content_id": {
                        "type": "integer",
                        "description": "TMDB id of the movie or TV show. Omit it to use whatever is currently on screen. This is NOT a result position -- never pass 1, 2, 3 here."
                    },
                    "content_type": {
                        "type": "string",
                        "description": "Type of content",
                        "enum": ["movie", "tv"]
                    }
                },
                "required": []
            }
        )
        def get_similar_content(args, raw_data):
            # Determine content type and ID
            content_type = args.get("content_type")
            content_id = resolve_content_id(
                args.get("content_id"),
                None,
                self.search_result_mapping,
            )
            
            # Auto-detect based on current state if not provided
            if not content_type:
                if self.current_movie_id:
                    content_type = "movie"
                    content_id = content_id or self.current_movie_id
                elif self.current_tv_id:
                    content_type = "tv"
                    content_id = content_id or self.current_tv_id
                else:
                    return SwaigFunctionResult(
                        response="Please select a movie or TV show first to find similar content."
                    )
            else:
                if content_type == "movie":
                    content_id = content_id or self.current_movie_id
                else:
                    content_id = content_id or self.current_tv_id
            
            if not content_id:
                result = SwaigFunctionResult(
                    response=f"Please select a {'movie' if content_type == 'movie' else 'TV show'} first to find similar ones."
                )
                return result
            
            try:
                # Use the new recommendations endpoint (ML-based, better than similar)
                recommendations = self.tmdb.get_recommendations(content_id, content_type)
                
                # Fallback to similar if no recommendations
                if not recommendations.get("results"):
                    if content_type == "movie":
                        details = self.tmdb.get_movie_details(content_id)
                        similar = details.get("similar", [])
                        content_name = details['title']
                    else:
                        details = self.tmdb.get_tv_details(content_id)
                        similar = details.get("similar", [])
                        content_name = details['name']
                else:
                    similar = recommendations["results"]
                    # Get content name for response
                    if content_type == "movie":
                        details = self.tmdb.get_movie_details(content_id)
                        content_name = details['title']
                    else:
                        details = self.tmdb.get_tv_details(content_id)
                        content_name = details['name']
                
                if similar:
                    # Build descriptions and store mapping for voice selection
                    descriptions = []
                    self.search_result_mapping = {}
                    
                    for i, item in enumerate(similar[:8], 1):  # Show more results for voice
                        if content_type == "movie":
                            year = item.get('release_date', '')[:4] if item.get('release_date') else ''
                            title = item['title']
                            rating = item.get('vote_average', 0)
                            descriptions.append(f"{i}. {title} ({year}) - {rating:.1f}⭐")
                            
                            self.search_result_mapping[i] = {
                                "type": "movie",
                                "id": item['id'],
                                "title": title,
                                "year": year
                            }
                        else:
                            year = item.get('first_air_date', '')[:4] if item.get('first_air_date') else ''
                            name = item['name']
                            rating = item.get('vote_average', 0)
                            descriptions.append(f"{i}. {name} ({year}) - {rating:.1f}⭐")
                            
                            self.search_result_mapping[i] = {
                                "type": "tv",
                                "id": item['id'],
                                "name": name,
                                "year": year
                            }
                    
                    response = f"Based on {content_name}, you might enjoy these similar {'movies' if content_type == 'movie' else 'TV shows'}:\n"
                    response += "\n".join(descriptions[:6]) + "\n"  # Voice-friendly, not too many
                    response += f"Which one would you like to know more about?"
                    
                    # Update last search info for AI voice navigation
                    self.last_search_info = f"SIMILAR CONTENT WITH IDS:\n"
                    for pos, info in self.search_result_mapping.items():
                        if info['type'] == 'movie':
                            self.last_search_info += f"  Position {pos}: {info['title']} ({info['year']}) -> movie_id={info['id']}\n"
                        else:
                            self.last_search_info += f"  Position {pos}: {info['name']} ({info['year']}) -> tv_id={info['id']}\n"
                    
                    logger.info(f"Similar content mapping: {self.last_search_info}")
                else:
                    response = f"I couldn't find similar {'movies' if content_type == 'movie' else 'TV shows'} for this title."
                
                result = SwaigFunctionResult(response=response)
                
                # Send event to frontend
                result.swml_user_event({
                    "type": f"similar_{content_type}",
                    "data": {"items": similar}
                })
                
                # Transition to browsing state for selection
                if similar:
                    result.swml_change_step("browsing")
                
                return result
                
            except Exception as e:
                logger.error(f"Error getting similar content: {e}")
                result = SwaigFunctionResult(
                    response="I couldn't fetch similar content. Please try again."
                )
                return result
        
        @self.tool(
            name="get_videos",
            description=(
                "Play a trailer or clip on the caller's screen. ONLY call this when "
                "the caller has explicitly asked to see or play a video. Choosing a "
                "title, or asking about it, is NOT a request for its trailer. The "
                "agent is put on hold while a video plays, so calling this unasked "
                "cuts off whatever you were about to say."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "content_id": {
                        "type": "integer",
                        "description": "TMDB id of the movie or TV show. Omit it to use whatever is currently on screen. This is NOT a result position -- never pass 1, 2, 3 here."
                    },
                    "content_type": {
                        "type": "string",
                        "description": "Type of content",
                        "enum": ["movie", "tv"]
                    },
                    "video_type": {
                        "type": "string",
                        "description": "Type of video to show",
                        "enum": ["trailer", "teaser", "clip", "behind_the_scenes", "all"]
                    }
                },
                "required": []
            }
        )
        def get_videos(args, raw_data):
            # Refuse to play anything the caller did not ask for. The model
            # calls this off its own bat after a selection; the hold that comes
            # with playback then cuts off whatever it was saying. Checked here
            # rather than trusted to the tool description.
            if not caller_asked_for_video(raw_data):
                logger.info("get_videos called without a caller request; not playing")
                return SwaigFunctionResult(
                    response=("Do not play a video now - the caller has not asked for one. "
                              "Carry on with what you were saying, and mention a trailer is "
                              "available if it is worth offering.")
                )

            # Authorize this caller to drive /trailer/hold and /trailer/unhold.
            # Registering here, rather than on every tool call, keeps the window
            # to callers who have actually reached a trailer.
            remember_trailer_call(raw_data)

            # Determine content type and ID
            content_type = args.get("content_type")
            content_id = resolve_content_id(
                args.get("content_id"),
                None,
                self.search_result_mapping,
            )
            video_type = args.get("video_type", "trailer")
            
            # Auto-detect based on current state if not provided
            if not content_type:
                if self.current_movie_id:
                    content_type = "movie"
                    content_id = content_id or self.current_movie_id
                elif self.current_tv_id:
                    content_type = "tv"
                    content_id = content_id or self.current_tv_id
                else:
                    return SwaigFunctionResult(
                        response="Please select a movie or TV show first to see its videos."
                    )
            else:
                if content_type == "movie":
                    content_id = content_id or self.current_movie_id
                else:
                    content_id = content_id or self.current_tv_id
            
            if not content_id:
                result = SwaigFunctionResult(
                    response=f"Please select a {'movie' if content_type == 'movie' else 'TV show'} first to see its videos."
                )
                return result
            
            try:
                # Get details with videos
                if content_type == "movie":
                    details = self.tmdb.get_movie_details(content_id)
                    content_name = details['title']
                else:
                    details = self.tmdb.get_tv_details(content_id)
                    content_name = details['name']
                
                videos = details.get("videos", [])
                
                # Filter by type if specified
                if video_type == "all":
                    filtered_videos = videos
                elif video_type == "trailer":
                    filtered_videos = [v for v in videos if v["type"] == "Trailer"]
                elif video_type == "teaser":
                    filtered_videos = [v for v in videos if v["type"] == "Teaser"]
                elif video_type == "clip":
                    filtered_videos = [v for v in videos if v["type"] in ["Clip", "Featurette"]]
                elif video_type == "behind_the_scenes":
                    filtered_videos = [v for v in videos if v["type"] in ["Behind the Scenes", "Featurette"]]
                else:
                    filtered_videos = [v for v in videos if v["type"] == "Trailer"]
                
                if filtered_videos:
                    # Play straight away when the caller asked for one KIND of
                    # video: "play the trailer" wants the trailer, not a menu.
                    # The client ranks trailers first, official and newest
                    # ahead of the rest, so index 0 is the one to play.
                    #
                    # This used to key off there being exactly one match, which
                    # only held because the client truncated the video list to
                    # three and usually cut the trailers off. With the full list
                    # most films have several, and the demo started reading a
                    # list out instead of playing anything.
                    single_kind = video_type in ("trailer", "teaser")
                    if len(filtered_videos) == 1 or single_kind:
                        video = filtered_videos[0]
                        # Terse on purpose. The browser opens the trailer the
                        # moment this event arrives, and hold does not cut off
                        # speech already in flight -- it only stops the agent
                        # taking NEW turns. Anything said here lands over the
                        # opening seconds of the film, so there is nothing to
                        # gain from narrating what the caller can already see.
                        response = "Playing it now."

                        # Send single video
                        result = SwaigFunctionResult(response=response)
                        result.swml_user_event({
                            "type": "video_available",
                            "data": {"video": video, "all_videos": filtered_videos}
                        })
                        # Hold here, with the tool result, instead of waiting
                        # for the browser to come back over /trailer/hold. That
                        # round trip was another second of talking, and holding
                        # atomically lands the action before the model takes a
                        # speaking turn.
                        #
                        # No prompt argument on purpose: passing one sets
                        # post_process and buys the model one more turn to
                        # speak, which is the thing being avoided. The timeout
                        # is only a backstop -- the browser still refines it to
                        # the real runtime and releases on close.
                        #
                        # Note the int: this is the SWML hold ACTION, which
                        # takes seconds as an integer. The REST command
                        # calling.ai_hold used by /trailer/hold wants the same
                        # value as a STRING and silently 400s on an int. Same
                        # concept, two transports, two types.
                        result.hold(_TRAILER_HOLD_DEFAULT)
                    else:
                        # Multiple videos - let user choose via voice
                        video_list = []
                        for i, v in enumerate(filtered_videos[:5], 1):  # Limit for voice
                            video_list.append(f"{i}. {v['type']}: {v['name']}")
                        
                        response = f"I found {len(filtered_videos)} videos for {content_name}:\n"
                        response += "\n".join(video_list)
                        response += "\nThe first one is playing now. Would you like to see a different one?"
                        
                        # Send all videos, play first
                        result = SwaigFunctionResult(response=response)
                        result.swml_user_event({
                            "type": "videos_available",
                            "data": {"videos": filtered_videos, "playing": filtered_videos[0]}
                        })
                else:
                    # No videos found
                    if video_type == "all":
                        response = f"Unfortunately, no videos are available for {content_name}."
                    else:
                        response = f"Unfortunately, no {video_type} is available for {content_name}."
                        
                        # Check if other types exist
                        if videos:
                            available_types = list(set([v["type"] for v in videos]))
                            if available_types:
                                response += f" However, I found: {', '.join(available_types).lower()}. "
                                response += "Would you like to see those instead?"
                    
                    result = SwaigFunctionResult(response=response)
                
                return result
                    
            except Exception as e:
                logger.error(f"Error getting videos: {e}")
                result = SwaigFunctionResult(
                    response="I couldn't fetch the videos. Please try again."
                )
                return result
        
        @self.tool(
            name="search_person",
            description="Search for actors, directors, or other film personalities",
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The name of the person to search for (optional if person_id provided)"
                    },
                    "person_id": {
                        "type": "integer",
                        "description": "The TMDB ID of the person (use this from search results)"
                    },
                    "search_position": {
                        "type": "integer",
                        "description": "Position in search results (1-based index)"
                    }
                },
                "required": []
            }
        )
        def search_person(args, raw_data):
            query = args.get("query", "").strip()
            person_id = args.get("person_id")
            search_position = args.get("search_position")
            logger.info(f"search_person called with query='{query}', person_id={person_id}, search_position={search_position}")
            
            # Priority 1: Use person_id if provided
            if person_id:
                logger.info(f"Using provided person_id: {person_id}")
            
            # Priority 2: Use search position if provided
            elif search_position and self.person_search_mapping:
                if search_position in self.person_search_mapping:
                    person_info = self.person_search_mapping[search_position]
                    person_id = person_info["id"]
                    logger.info(f"Selected person at position {search_position}: '{person_info['name']}' (ID: {person_id})")
            
            try:
                if person_id:
                    details = self.tmdb.get_person_details(person_id)
                    self.current_person_id = person_id
                    
                    # Get total count and top films
                    total_movies = details.get("total_movie_count", 0)
                    films = details.get("filmography", [])
                    recent_films = films[:5]  # Top 5 most recent
                    known_for = [f["title"] for f in recent_films]
                    
                    # Log filmography IDs for AI reference
                    filmography_info = f"FILMOGRAPHY for {details['name']} with IDs:\n"
                    for i, film in enumerate(films[:24], 1):  # Show top 20 for AI
                        year = film.get('release_date', '')[:4] if film.get('release_date') else ''
                        filmography_info += f"  {i}. {film['title']} ({year}) -> movie_id={film['id']}\n"
                    logger.info(f"Person filmography: {filmography_info}")
                    
                    response = f"Here's {details['name']}, "
                    if details.get("known_for_department"):
                        response += f"known for {details['known_for_department'].lower()}. "
                    
                    if total_movies:
                        response += f"They've appeared in {total_movies} movies! "
                    
                    if known_for:
                        response += f"Recent films include: {', '.join(known_for)}. "
                    
                    if details.get("biography"):
                        bio_snippet = details["biography"][:150]
                        response += f"{bio_snippet}... "
                    
                    response += f"I'm showing all {total_movies} movies on your screen."
                    
                    result = SwaigFunctionResult(response=response)
                    
                    # Send event to frontend
                    result.swml_user_event({
                        "type": "person_details",
                        "data": details
                    })
                    
                    # Transition to person_details state
                    result.swml_change_step("person_details")
                    
                    return result
                    
                elif query:
                    results = self.tmdb.search_person(query)
                    
                    if results["results"]:
                        # If only one result, get details directly
                        if len(results["results"]) == 1:
                            person = results["results"][0]
                            details = self.tmdb.get_person_details(person["id"])
                            self.current_person_id = person["id"]
                            
                            total_movies = details.get("total_movie_count", 0)
                            
                            # Log filmography IDs for AI reference
                            films = details.get("filmography", [])
                            filmography_info = f"FILMOGRAPHY for {details['name']} with IDs:\n"
                            for i, film in enumerate(films[:24], 1):  # Show top 20 for AI
                                year = film.get('release_date', '')[:4] if film.get('release_date') else ''
                                filmography_info += f"  {i}. {film['title']} ({year}) -> movie_id={film['id']}\n"
                            logger.info(f"Person filmography: {filmography_info}")
                            
                            response = f"I found {details['name']}. "
                            if total_movies:
                                response += f"They've appeared in {total_movies} movies. "
                            response += f"I'm displaying their complete filmography on your screen."
                            
                            result = SwaigFunctionResult(response=response)
                            
                            # Send person details event
                            result.swml_user_event({
                                "type": "person_details",
                                "data": details
                            })
                            
                            # Transition to person_details state
                            result.swml_change_step("person_details")
                        else:
                            # Multiple results - let user choose
                            people = results["results"][:5]
                            person_descriptions = []
                            self.person_search_mapping = {}  # Reset mapping
                            
                            for i, p in enumerate(people, 1):
                                dept = p.get("known_for_department", "")
                                known_for = p.get("known_for", [])
                                known_for_titles = [item.get("title", item.get("name", "")) for item in known_for[:2]]
                                
                                # Include ID directly in the response text for LLM to see
                                desc = f"{i}. id: {p['id']} name: {p['name']} ({dept})"
                                if known_for_titles:
                                    desc += f" - Known for: {', '.join(known_for_titles)}"
                                person_descriptions.append(desc)
                                
                                # Store mapping for AI
                                self.person_search_mapping[i] = {
                                    "id": p["id"],
                                    "name": p["name"],
                                    "department": dept
                                }
                            
                            # Create info for AI about the person results with IDs
                            self.last_person_search_info = f"PERSON SEARCH RESULTS WITH IDS for '{query}':\n"
                            for pos, info in self.person_search_mapping.items():
                                self.last_person_search_info += f"  Position {pos}: {info['name']} ({info['department']}) -> person_id={info['id']}\n"
                            
                            logger.info(f"Person search mapping: {self.last_person_search_info}")
                            
                            response = f"I found several people matching '{query}':\n"
                            response += "\n".join(person_descriptions) + "\n"
                            response += "Which person would you like to know more about?"
                            
                            result = SwaigFunctionResult(response=response)
                            
                            # Send search results event
                            result.swml_user_event({
                                "type": "person_search_results",
                                "data": results
                            })
                    else:
                        response = f"I couldn't find anyone matching '{query}'."
                        result = SwaigFunctionResult(response=response)
                    
                    return result
                    
                else:
                    result = SwaigFunctionResult(
                        response="Please provide a name to search for."
                    )
                    return result
                    
            except Exception as e:
                logger.error(f"Error searching person: {e}")
                result = SwaigFunctionResult(
                    response="I couldn't search for that person. Please try again."
                )
                return result
        
        @self.tool(
            name="get_trending",
            description="Get trending movies for the day or week",
            parameters={
                "type": "object",
                "properties": {
                    "time_window": {
                        "type": "string",
                        "description": "The time window for trending movies (day or week)",
                        "enum": ["day", "week"]
                    }
                },
                "required": []
            }
        )
        def get_trending(args, raw_data):
            time_window = args.get("time_window", "week")
            logger.info(f"get_trending called with time_window: {time_window}")
            
            try:
                results = self.tmdb.get_trending(time_window=time_window)
                
                top_movies = results["results"][:24]
                movie_list = []
                self.search_result_mapping = {}  # Use same mapping as search
                
                for i, m in enumerate(top_movies, 1):
                    year = m.get('release_date', '')[:4] if m.get('release_date') else ''
                    movie_list.append(f"{i}. id: {m['id']} title: '{m['title']}' ({year})")
                    
                    # Store mapping for AI
                    self.search_result_mapping[i] = {
                        "id": m['id'],
                        "title": m['title'],
                        "year": year
                    }
                
                # Update last search info for AI
                self.last_search_info = f"TRENDING MOVIES WITH IDS:\n"
                for pos, info in self.search_result_mapping.items():
                    self.last_search_info += f"  Position {pos}: {info['title']} ({info['year']}) -> movie_id={info['id']}\n"
                
                logger.info(f"Trending mapping: {self.last_search_info}")
                
                response = f"Here are this {time_window}'s trending movies:\n"
                response += "\n".join(movie_list) + "\n"
                response += "They're all displayed on your screen. Which one interests you?"
                
                result = SwaigFunctionResult(response=response)
                
                # Send event to frontend (frontend will clear display when handling this)
                event_data = {
                    "type": "trending_movies",
                    "data": results
                }
                logger.info(f"Sending trending_movies event with {len(results['results'])} movies")
                result.swml_user_event(event_data)
                
                # Transition to browsing state
                result.swml_change_step("browsing")
                
                return result
                
            except Exception as e:
                logger.error(f"Error getting trending: {e}")
                return SwaigFunctionResult(
                    response="I couldn't fetch trending movies. Please try again."
                )
        
        @self.tool(
            name="get_movies_by_genre",
            description="Browse movies by genre like action, comedy, horror, or drama",
            parameters={
                "type": "object",
                "properties": {
                    "genre_name": {
                        "type": "string",
                        "description": "The genre name (e.g., action, comedy, horror, drama, sci-fi, romance)"
                    }
                },
                "required": ["genre_name"]
            }
        )
        def get_movies_by_genre(args, raw_data):
            genre_name = args.get("genre_name", "").lower()
            logger.info(f"get_movies_by_genre called with genre_name='{genre_name}'")
            
            if not genre_name:
                result = SwaigFunctionResult(
                    response="Please specify a genre like action, comedy, horror, or drama."
                )
                return result
            
            try:
                # Get genre mapping
                genres_data = self.tmdb.get_genres()
                genres = {g["name"].lower(): g["id"] for g in genres_data["genres"]}
                
                if genre_name not in genres:
                    available = ", ".join(list(genres.keys())[:24])
                    result = SwaigFunctionResult(
                        response=f"I don't recognize '{genre_name}'. "
                        f"Try genres like: {available}"
                    )
                    return result
                
                genre_id = genres[genre_name]
                results = self.tmdb.discover_by_genre([genre_id])
                
                top_movies = results["results"][:24]
                movie_list = []
                self.search_result_mapping = {}  # Use same mapping as search
                
                for i, m in enumerate(top_movies, 1):
                    year = m.get('release_date', '')[:4] if m.get('release_date') else ''
                    movie_list.append(f"{i}. id: {m['id']} title: '{m['title']}' ({year})")
                    
                    # Store mapping for AI
                    self.search_result_mapping[i] = {
                        "id": m['id'],
                        "title": m['title'],
                        "year": year
                    }
                
                # Update last search info for AI
                self.last_search_info = f"GENRE MOVIES WITH IDS for {genre_name}:\n"
                for pos, info in self.search_result_mapping.items():
                    self.last_search_info += f"  Position {pos}: {info['title']} ({info['year']}) -> movie_id={info['id']}\n"
                
                logger.info(f"Genre mapping: {self.last_search_info}")
                
                response = f"Here are popular {genre_name} movies:\n"
                response += "\n".join(movie_list) + "\n"
                response += "Which movie would you like to explore?"
                
                result = SwaigFunctionResult(response=response)
                
                # Send event to frontend
                result.swml_user_event({
                    "type": "genre_movies",
                    "data": {
                        "genre": genre_name.title(),
                        "movies": results["results"]
                    }
                })
                
                # Transition to browsing state
                result.swml_change_step("browsing")
                
                return result
                
            except Exception as e:
                logger.error(f"Error getting movies by genre: {e}")
                result = SwaigFunctionResult(
                    response="I couldn't fetch movies for that genre. Please try again."
                )
                return result
        
        @self.tool(name="add_to_watchlist", description="Add a movie to the user's watchlist")
        def add_to_watchlist(args, raw_data):
            movie_id = args.get("movie_id", self.current_movie_id)
            
            if not movie_id:
                result = SwaigFunctionResult(
                    response="Please select a movie to add to your watchlist."
                )
                return result
            
            try:
                # Check if already in watchlist
                if any(m["id"] == movie_id for m in self.watchlist):
                    result = SwaigFunctionResult(
                        response="This movie is already in your watchlist."
                    )
                    return result
                
                details = self.tmdb.get_movie_details(movie_id)
                
                # Add to watchlist
                self.watchlist.append({
                    "id": movie_id,
                    "title": details["title"],
                    "poster_path": details["poster_path"]
                })
                
                result = SwaigFunctionResult(
                    response=f"I've added '{details['title']}' to your watchlist. "
                    f"You now have {len(self.watchlist)} movies saved."
                )
                
                # Send event to frontend
                result.swml_user_event({
                    "type": "watchlist_updated",
                    "data": {"watchlist": self.watchlist}
                })
                
                return result
                
            except Exception as e:
                logger.error(f"Error adding to watchlist: {e}")
                result = SwaigFunctionResult(
                    response="I couldn't add that movie to your watchlist. Please try again."
                )
                return result
        
        @self.tool(
            name="clear_display",
            description="Clear the current display for a new search",
            parameters={
                "type": "object",
                "properties": {},
                "required": []
            }
        )
        def clear_display(args, raw_data):
            # Reset state
            self.current_search_results = []
            self.current_movie_id = None
            self.current_person_id = None
            self.current_tv_id = None
            
            result = SwaigFunctionResult(
                response="I've cleared the display. What would you like to search for next?"
            )
            
            # Send event to frontend with background image
            result.swml_user_event({
                "type": "clear_display",
                "data": {
                    "background_image": "/background.png"
                }
            })
            
            # Transition to greeting state
            result.swml_change_step("greeting")
            
            return result
        
        @self.tool(
            name="search_tv",
            description="Search for TV shows by title",
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The TV show title to search for"
                    }
                },
                "required": ["query"]
            }
        )
        def search_tv(args, raw_data):
            query = args.get("query", "").strip()
            logger.info(f"search_tv called with query: '{query}'")
            
            if not query:
                return SwaigFunctionResult(
                    response="Please provide a TV show title to search for."
                )
            
            try:
                results = self.tmdb.search_tv(query)
                logger.info(f"TMDB returned {len(results.get('results', []))} TV shows for '{query}'")
                self.current_search_results = results["results"]
                
                if results["results"]:
                    # Build TV show list and store mapping for AI
                    show_descriptions = []
                    self.search_result_mapping = {}  # Reset mapping
                    
                    for i, show in enumerate(results["results"], 1):
                        year = show.get('first_air_date', '')[:4] if show.get('first_air_date') else 'unknown year'
                        show_descriptions.append(f"{i}. id: {show['id']} title: '{show['name']}' ({year})")
                        
                        # Store mapping for AI to use internally
                        self.search_result_mapping[i] = {
                            "id": show['id'],
                            "name": show['name'],
                            "year": year,
                            "overview": show.get('overview', '')[:100]
                        }
                    
                    # Create info for AI about the search results with IDs
                    self.last_search_info = f"TV SHOW SEARCH RESULTS WITH IDS for '{query}':\n"
                    for pos, info in self.search_result_mapping.items():
                        self.last_search_info += f"  Position {pos}: {info['name']} ({info['year']}) -> tv_id={info['id']}\n"
                    
                    logger.info(f"TV search mapping: {self.last_search_info}")
                    
                    result = SwaigFunctionResult(
                        response=f"I found {len(results['results'])} TV shows matching '{query}'. "
                        f"Here are the results:\n{chr(10).join(show_descriptions)}\n"
                        f"Which show would you like to know more about?"
                    )
                else:
                    result = SwaigFunctionResult(
                        response=f"I couldn't find any TV shows matching '{query}'. "
                        f"Try searching with a different title or let me show you trending TV shows."
                    )
                
                # Send event to frontend
                logger.info(f"Sending tv_search_results event with {len(results['results'])} shows")
                result.swml_user_event({
                    "type": "tv_search_results",
                    "data": results
                })
                
                # Transition to browsing state
                result.swml_change_step("browsing")
                logger.info("Transitioned to browsing state")
                
                return result
            except Exception as e:
                logger.error(f"Error searching TV shows: {e}")
                return SwaigFunctionResult(
                    response="I encountered an error searching for TV shows. Please try again."
                )
        
        @self.tool(
            name="get_tv_details",
            description="Get detailed information about a specific TV show",
            parameters={
                "type": "object",
                "properties": {
                    "tv_title": {
                        "type": "string",
                        "description": "The title of the TV show (optional if tv_id provided)"
                    },
                    "tv_id": {
                        "type": "integer",
                        "description": "The TMDB ID of the TV show (preferred - use this from search results)"
                    },
                    "search_position": {
                        "type": "integer",
                        "description": "Position in search results (1-based index)"
                    }
                },
                "required": []
            }
        )
        def get_tv_details(args, raw_data):
            tv_id = args.get("tv_id")
            tv_title = args.get("tv_title")
            search_position = args.get("search_position")
            logger.info(f"get_tv_details called with tv_id={tv_id}, tv_title={tv_title}, search_position={search_position}")
            
            # Priority 1: Use tv_id if provided
            if tv_id:
                logger.info(f"Using provided tv_id: {tv_id}")
            
            # Priority 2: Use search position if provided
            elif search_position and self.search_result_mapping:
                if search_position in self.search_result_mapping:
                    show_info = self.search_result_mapping[search_position]
                    tv_id = show_info["id"]
                    tv_title = show_info.get("name", show_info.get("title"))
                    logger.info(f"Selected TV show at position {search_position}: '{tv_title}' (ID: {tv_id})")
                else:
                    logger.warning(f"Position {search_position} not found in search results")
            
            # Priority 3: Do a fresh search if we still don't have an ID
            if not tv_id and tv_title:
                logger.info(f"No tv_id provided, searching for '{tv_title}'")
                search_results = self.tmdb.search_tv(tv_title)
                
                if search_results["results"]:
                    tv_id = search_results["results"][0]["id"]
                    logger.info(f"Using first search result: ID {tv_id}")
            
            if not tv_id:
                result = SwaigFunctionResult(
                    response="Please specify which TV show you'd like details about."
                )
                return result
            
            try:
                details = self.tmdb.get_tv_details(tv_id)
                self.current_tv_id = tv_id
                
                # Build response
                genres = ", ".join(details["genres"][:3])
                seasons = details["number_of_seasons"]
                episodes = details["number_of_episodes"]
                
                response = f"Here's {details['name']}"
                if details['first_air_date']:
                    response += f" which premiered in {details['first_air_date'][:4]}"
                response += ". "
                
                if details["tagline"]:
                    response += f"\"{details['tagline']}\". "
                
                response += f"It's a {genres} series with {seasons} season{'s' if seasons != 1 else ''} "
                response += f"and {episodes} episodes. "
                
                if details["networks"]:
                    response += f"It airs on {', '.join(details['networks'][:2])}. "
                
                response += f"The show has a rating of {details['vote_average']:.1f} out of 10. "
                
                if details["overview"]:
                    response += f"Here's what it's about: {details['overview'][:200]}... "
                
                # Check if trailer is available before offering it
                has_trailer = False
                if details.get("videos"):
                    has_trailer = any(v["type"] == "Trailer" for v in details["videos"])
                
                # Build options based on available content
                options = []
                if has_trailer:
                    options.append("watch the trailer")
                options.append("ask about specific seasons")
                options.append("find similar shows")
                options.append("explore the cast members shown on screen")
                
                response += f"You can {', or '.join(options)}."
                
                result = SwaigFunctionResult(response=response)
                
                # Send event to frontend with all details
                event_data = {
                    "type": "tv_details",
                    "data": details
                }
                logger.info(f"Sending tv_details event for '{details['name']}' with {len(details.get('seasons', []))} seasons")
                logger.debug(f"Seasons data: {details.get('seasons', [])[:3]}")  # Log first 3 seasons for debugging
                result.swml_user_event(event_data)
                
                # Transition to tv_details state (we'll need to add this)
                result.swml_change_step("tv_details")
                logger.info("Transitioned to tv_details state")
                
                return result
                
            except Exception as e:
                logger.error(f"Error getting TV show details: {e}")
                result = SwaigFunctionResult(
                    response="I couldn't fetch the TV show details. Please try again."
                )
                return result
        
        @self.tool(
            name="get_season_details",
            description="Get details about a specific season of a TV show",
            parameters={
                "type": "object",
                "properties": {
                    "tv_id": {
                        "type": "integer",
                        "description": "The TMDB ID of the TV show (uses current if not provided)"
                    },
                    "season_number": {
                        "type": "integer",
                        "description": "The season number to get details for"
                    }
                },
                "required": ["season_number"]
            }
        )
        def get_season_details(args, raw_data):
            tv_id = args.get("tv_id", self.current_tv_id)
            season_number = args.get("season_number")
            
            if not tv_id:
                result = SwaigFunctionResult(
                    response="Please select a TV show first to see its seasons."
                )
                return result
            
            try:
                season = self.tmdb.get_tv_season(tv_id, season_number)
                
                episode_count = len(season["episodes"])
                
                response = f"Season {season_number}: {season['name']}. "
                response += f"This season has {episode_count} episodes. "
                
                if season["air_date"]:
                    response += f"It premiered on {season['air_date']}. "
                
                if season["overview"]:
                    response += f"{season['overview'][:150]}... "
                
                # List first few episodes
                if season["episodes"]:
                    response += "The first few episodes are: "
                    episode_list = []
                    for ep in season["episodes"][:3]:
                        episode_list.append(f"Episode {ep['episode_number']}: {ep['name']}")
                    response += ", ".join(episode_list) + ". "
                
                response += "I'm showing the full episode list on your screen."
                
                result = SwaigFunctionResult(response=response)
                
                # Send event to frontend
                result.swml_user_event({
                    "type": "season_details",
                    "data": season
                })
                
                return result
                
            except Exception as e:
                logger.error(f"Error getting season details: {e}")
                result = SwaigFunctionResult(
                    response="I couldn't fetch the season details. Please try again."
                )
                return result
        
        @self.tool(
            name="multi_search",
            description="Search for movies, TV shows, and people all at once",
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query (can be movie, TV show, or person name)"
                    }
                },
                "required": ["query"]
            }
        )
        def multi_search(args, raw_data):
            query = args.get("query", "").strip()
            logger.info(f"multi_search called with query: '{query}'")
            
            if not query:
                return SwaigFunctionResult(
                    response="Please provide something to search for."
                )
            
            try:
                results = self.tmdb.multi_search(query)
                logger.info(f"Multi-search returned {len(results.get('results', []))} results")
                
                if results["results"]:
                    # Organize results by type
                    movies = []
                    tv_shows = []
                    people = []
                    self.search_result_mapping = {}
                    
                    position = 1
                    for item in results["results"]:
                        media_type = item.get("media_type")
                        
                        if media_type == "movie":
                            year = item.get('release_date', '')[:4] if item.get('release_date') else ''
                            movies.append(f"🎬 Movie: '{item['title']}' ({year})")
                            self.search_result_mapping[position] = {
                                "type": "movie",
                                "id": item['id'],
                                "title": item['title'],
                                "year": year
                            }
                        elif media_type == "tv":
                            year = item.get('first_air_date', '')[:4] if item.get('first_air_date') else ''
                            tv_shows.append(f"📺 TV: '{item['name']}' ({year})")
                            self.search_result_mapping[position] = {
                                "type": "tv",
                                "id": item['id'],
                                "name": item['name'],
                                "year": year
                            }
                        elif media_type == "person":
                            dept = item.get('known_for_department', '')
                            people.append(f"👤 Person: {item['name']} ({dept})")
                            self.search_result_mapping[position] = {
                                "type": "person",
                                "id": item['id'],
                                "name": item['name'],
                                "department": dept
                            }
                        position += 1
                    
                    # Build response
                    response = f"I found {len(results['results'])} results for '{query}':\n\n"
                    
                    all_results = []
                    pos = 1
                    
                    if movies:
                        for movie in movies:
                            all_results.append(f"{pos}. {movie}")
                            pos += 1
                    
                    if tv_shows:
                        for show in tv_shows:
                            all_results.append(f"{pos}. {show}")
                            pos += 1
                    
                    if people:
                        for person in people:
                            all_results.append(f"{pos}. {person}")
                            pos += 1
                    
                    response += "\n".join(all_results)
                    response += "\n\nWhich one would you like to know more about?"
                    
                    # Update last search info for AI
                    self.last_search_info = f"MULTI-SEARCH RESULTS WITH IDS for '{query}':\n"
                    for pos, info in self.search_result_mapping.items():
                        if info['type'] == 'movie':
                            self.last_search_info += f"  Position {pos}: Movie - {info['title']} ({info['year']}) -> movie_id={info['id']}\n"
                        elif info['type'] == 'tv':
                            self.last_search_info += f"  Position {pos}: TV - {info['name']} ({info['year']}) -> tv_id={info['id']}\n"
                        else:
                            self.last_search_info += f"  Position {pos}: Person - {info['name']} -> person_id={info['id']}\n"
                    
                    logger.info(f"Multi-search mapping: {self.last_search_info}")
                    
                    result = SwaigFunctionResult(response=response)
                else:
                    result = SwaigFunctionResult(
                        response=f"I couldn't find anything matching '{query}'. Try a different search term."
                    )
                
                # Send event to frontend
                result.swml_user_event({
                    "type": "multi_search_results",
                    "data": results
                })
                
                # Transition to browsing state
                result.swml_change_step("browsing")
                
                return result
            except Exception as e:
                logger.error(f"Error in multi-search: {e}")
                return SwaigFunctionResult(
                    response="I encountered an error searching. Please try again."
                )
        
        @self.tool(
            name="discover_content",
            description="Discover movies or TV shows with advanced filters",
            parameters={
                "type": "object",
                "properties": {
                    "content_type": {
                        "type": "string",
                        "description": "Type of content to discover",
                        "enum": ["movie", "tv"]
                    },
                    "year": {
                        "type": "integer",
                        "description": "Specific year of release"
                    },
                    "decade": {
                        "type": "string",
                        "description": "Decade (e.g., '1980s', '2000s')"
                    },
                    "genre": {
                        "type": "string",
                        "description": "Genre name (e.g., 'action', 'comedy', 'horror')"
                    },
                    "min_rating": {
                        "type": "number",
                        "description": "Minimum rating (0-10)"
                    },
                    "certification": {
                        "type": "string",
                        "description": "MPAA rating (G, PG, PG-13, R)"
                    },
                    "sort_by": {
                        "type": "string",
                        "description": "Sort order",
                        "enum": ["popularity", "rating", "release_date", "title"]
                    }
                },
                "required": ["content_type"]
            }
        )
        def discover_content(args, raw_data):
            content_type = args.get("content_type", "movie")
            logger.info(f"discover_content called with type={content_type}, filters={args}")
            
            try:
                filters = {}
                
                # Process year/decade
                if "year" in args:
                    if content_type == "movie":
                        filters["year"] = args["year"]
                    else:
                        filters["first_air_year"] = args["year"]
                elif "decade" in args:
                    decade_str = args["decade"].replace("s", "")
                    decade = int(decade_str)
                    if content_type == "movie":
                        filters["year_gte"] = decade
                        filters["year_lte"] = decade + 9
                    else:
                        filters["air_date_gte"] = f"{decade}-01-01"
                        filters["air_date_lte"] = f"{decade + 9}-12-31"
                
                # Process genre
                if "genre" in args:
                    genre_name = args["genre"].lower()
                    if content_type == "movie":
                        genres_data = self.tmdb.get_genres()
                    else:
                        genres_data = self.tmdb.get_tv_genres()
                    
                    genres = {g["name"].lower(): g["id"] for g in genres_data["genres"]}
                    if genre_name in genres:
                        filters["genre_ids"] = [genres[genre_name]]
                
                # Process rating
                if "min_rating" in args:
                    filters["vote_average_gte"] = args["min_rating"]
                
                # Process certification
                if "certification" in args and content_type == "movie":
                    filters["certification"] = args["certification"]
                
                # Process sort
                sort_map = {
                    "popularity": "popularity.desc",
                    "rating": "vote_average.desc",
                    "release_date": "primary_release_date.desc" if content_type == "movie" else "first_air_date.desc",
                    "title": "original_title.asc" if content_type == "movie" else "name.asc"
                }
                filters["sort_by"] = sort_map.get(args.get("sort_by", "popularity"), "popularity.desc")
                
                # Call appropriate discover method
                if content_type == "movie":
                    results = self.tmdb.discover_movies_advanced(filters)
                else:
                    results = self.tmdb.discover_tv_advanced(filters)
                
                if results["results"]:
                    # Build response
                    item_list = []
                    self.search_result_mapping = {}
                    
                    for i, item in enumerate(results["results"][:15], 1):
                        if content_type == "movie":
                            year = item.get('release_date', '')[:4] if item.get('release_date') else ''
                            title = item['title']
                            item_list.append(f"{i}. '{title}' ({year}) - {item['vote_average']:.1f}⭐")
                            self.search_result_mapping[i] = {
                                "type": "movie",
                                "id": item['id'],
                                "title": title,
                                "year": year
                            }
                        else:
                            year = item.get('first_air_date', '')[:4] if item.get('first_air_date') else ''
                            name = item['name']
                            item_list.append(f"{i}. '{name}' ({year}) - {item['vote_average']:.1f}⭐")
                            self.search_result_mapping[i] = {
                                "type": "tv",
                                "id": item['id'],
                                "name": name,
                                "year": year
                            }
                    
                    # Build filter description
                    filter_desc = []
                    if "year" in args:
                        filter_desc.append(f"from {args['year']}")
                    elif "decade" in args:
                        filter_desc.append(f"from the {args['decade']}")
                    if "genre" in args:
                        filter_desc.append(args['genre'])
                    if "min_rating" in args:
                        filter_desc.append(f"rated {args['min_rating']}+")
                    if "certification" in args:
                        filter_desc.append(f"rated {args['certification']}")
                    
                    filter_str = " ".join(filter_desc) if filter_desc else "matching your criteria"
                    
                    response = f"I found {len(results['results'])} {'movies' if content_type == 'movie' else 'TV shows'} {filter_str}:\n\n"
                    response += "\n".join(item_list)
                    response += "\n\nWhich one would you like to explore?"
                    
                    result = SwaigFunctionResult(response=response)
                    
                    # Send event to frontend
                    result.swml_user_event({
                        "type": f"discover_{content_type}_results",
                        "data": results
                    })
                    
                    # Transition to browsing state
                    result.swml_change_step("browsing")
                else:
                    result = SwaigFunctionResult(
                        response=f"I couldn't find any {'movies' if content_type == 'movie' else 'TV shows'} matching those criteria. Try adjusting your filters."
                    )
                
                return result
                
            except Exception as e:
                logger.error(f"Error discovering content: {e}")
                return SwaigFunctionResult(
                    response="I encountered an error while searching. Please try again."
                )
        
        @self.tool(
            name="get_trending_tv",
            description="Get trending TV shows for the day or week",
            parameters={
                "type": "object",
                "properties": {
                    "time_window": {
                        "type": "string",
                        "description": "The time window for trending shows (day or week)",
                        "enum": ["day", "week"]
                    }
                },
                "required": []
            }
        )
        def get_trending_tv(args, raw_data):
            time_window = args.get("time_window", "week")
            logger.info(f"get_trending_tv called with time_window: {time_window}")
            
            try:
                results = self.tmdb.get_trending_tv(time_window=time_window)
                
                top_shows = results["results"][:24]
                show_list = []
                self.search_result_mapping = {}  # Use same mapping as search
                
                for i, show in enumerate(top_shows, 1):
                    year = show.get('first_air_date', '')[:4] if show.get('first_air_date') else ''
                    show_list.append(f"{i}. id: {show['id']} title: '{show['name']}' ({year})")
                    
                    # Store mapping for AI
                    self.search_result_mapping[i] = {
                        "id": show['id'],
                        "name": show['name'],
                        "year": year
                    }
                
                # Update last search info for AI
                self.last_search_info = f"TRENDING TV SHOWS WITH IDS:\n"
                for pos, info in self.search_result_mapping.items():
                    self.last_search_info += f"  Position {pos}: {info['name']} ({info['year']}) -> tv_id={info['id']}\n"
                
                logger.info(f"Trending TV mapping: {self.last_search_info}")
                
                response = f"Here are this {time_window}'s trending TV shows:\n"
                response += "\n".join(show_list) + "\n"
                response += "They're all displayed on your screen. Which one interests you?"
                
                result = SwaigFunctionResult(response=response)
                
                # Send event to frontend
                event_data = {
                    "type": "trending_tv",
                    "data": results
                }
                logger.info(f"Sending trending_tv event with {len(results['results'])} shows")
                result.swml_user_event(event_data)
                
                # Transition to browsing state
                result.swml_change_step("browsing")
                
                return result
                
            except Exception as e:
                logger.error(f"Error getting trending TV shows: {e}")
                return SwaigFunctionResult(
                    response="I couldn't fetch trending TV shows. Please try again."
                )
    
    def on_swml_request(self, request_data, callback_path, request=None):
        """Handle incoming SWML requests and configure the AI agent"""
        # Get the base URL using SDK's auto-detection from X-Forwarded headers
        # Falls back to SWML_PROXY_URL_BASE or APP_URL if needed
        base_url = self.get_full_url(include_auth=False)

        # Set video URLs using set_param (this is what makes video work!)
        if base_url:
            self.set_param("video_idle_file", f"{base_url}/cinebot_idle.mp4")
            self.set_param("video_talking_file", f"{base_url}/cinebot_talking.mp4")
            print(f"Set video URLs to use host: {base_url}")

            # Silence instead of the platform's default hold music.
            #
            # The AI is held for the length of a trailer (see /trailer/hold),
            # and the default hold music played straight over the film. There
            # is no "no hold music" switch -- hold_music takes a URL -- so we
            # serve ten seconds of digital silence and point it at that.
            #
            # web/silence.mp3 is generated, not recorded:
            #   ffmpeg -f lavfi -i anullsrc=r=44100:cl=mono -t 10 \
            #          -c:a libmp3lame -b:a 32k web/silence.mp3
            self.set_param("hold_music", f"{base_url}/silence.mp3")

        # Optional post-prompt URL from environment
        post_prompt_url = os.environ.get("POST_PROMPT_URL")
        if post_prompt_url:
            self.set_post_prompt("Summarize the conversation including movies/shows discussed, user preferences, and any recommendations made.")
            self.set_post_prompt_url(post_prompt_url)


# Install the per-call state as properties, so the seventeen tool handlers keep
# reading and writing `self.search_result_mapping` and friends exactly as
# before and the change stays confined to this file's plumbing. Done after the
# class body because it is generated from SESSION_DEFAULTS rather than written
# out nine times.
def _install_session_properties(cls, names):
    def make(name):
        def getter(self):
            return self._session()[name]

        def setter(self, value):
            self._session()[name] = value

        return property(getter, setter, doc=f"Per-call {name} (see MovieAgent._session)")

    for name in names:
        setattr(cls, name, make(name))


_install_session_properties(MovieAgent, SESSION_DEFAULTS)


HOST = "0.0.0.0"
PORT = int(os.environ.get('PORT', 3030))


def create_server(port=None):
    """Create AgentServer with static file mounting and API endpoints."""
    server = AgentServer(host=HOST, port=port or PORT)
    agent = MovieAgent()
    server.register(agent, "/cinebot")

    # Serve static files
    web_dir = Path(__file__).parent / "web"
    if web_dir.exists():
        server.serve_static_files(str(web_dir))

    # Add API endpoints
    @server.app.get("/api/version")
    async def get_version():
        """Which commit this instance is actually running."""
        return JSONResponse(content=resolve_commit())

    @server.app.get("/api/menu")
    async def get_menu():
        """Return available genres for the UI"""
        try:
            genres = agent.tmdb.get_genres()
            return JSONResponse(content=genres)
        except:
            return JSONResponse(content={"genres": []})

    @server.app.get("/api/watchlist")
    async def get_watchlist(call_id: str = ""):
        """
        Return one caller's watchlist.

        This used to return `agent.watchlist`: a single process-global list that
        every caller appended to, served over an unauthenticated GET. Anyone who
        knew the hostname could read what callers had been adding, and callers
        saw each other's entries.

        It is now scoped to a call id, which is a UUID the caller's own browser
        holds. Unknown ids get an empty list rather than a distinguishing error,
        so this cannot be used to probe which calls are live.
        """
        session = agent._sessions.get(call_id) if call_id else None
        return JSONResponse(content={"watchlist": (session or {}).get("watchlist", [])})

    @server.app.post("/trailer/hold")
    async def trailer_hold(request: Request):
        """
        Put the AI on hold for the length of a trailer.

        Called by the browser once the YouTube player reports its duration, so
        the hold matches the actual runtime rather than a guess. /trailer/unhold
        releases it the moment the viewer closes the trailer; this timeout is
        only the backstop for a browser that goes away mid-video.
        """
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)

        call_id = (body or {}).get("call_id")
        seconds = (body or {}).get("seconds")

        if not call_id or not trailer_call_known(call_id):
            # Deliberately the same response for unknown and not-yet-served, so
            # this cannot be used to probe which call ids are live.
            return JSONResponse({"error": "unknown call"}, status_code=403)

        try:
            seconds = int(seconds)
        except (TypeError, ValueError):
            seconds = 300
        seconds = max(5, min(_TRAILER_HOLD_MAX, seconds))

        client = get_rest_client()
        if client is None:
            return JSONResponse({"error": "SignalWire credentials not configured"}, status_code=500)

        try:
            # timeout MUST go over the wire as a string. The SDK types this
            # parameter `int | None` (calling_resources_generated.ai_hold), but
            # the platform's own schema for calling.ai_hold is `timeout: str`
            # (relay/protocol_types_generated.CallingAiHoldParams). Passing the
            # int the signature asks for is accepted by the REST call and then
            # fails asynchronously: the call log shows `calling.ai_hold`
            # executed, followed one second later by
            #   calling_error 400 "timeout error"
            # and the agent is never actually held. Nothing raises here, so the
            # only evidence is the platform event log.
            resp = client.calling.ai_hold(call_id, timeout=str(seconds))
            logger.info(f"Trailer hold: call {call_id} held for {seconds}s -> {resp}")
            return {"held": True, "seconds": seconds}
        except Exception as e:
            logger.warning(f"Trailer hold failed for {call_id}: {e}")
            return JSONResponse({"error": "hold failed"}, status_code=502)

    @server.app.post("/trailer/unhold")
    async def trailer_unhold(request: Request):
        """Release the hold as soon as the viewer closes the trailer."""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)

        call_id = (body or {}).get("call_id")
        if not call_id or not trailer_call_known(call_id):
            return JSONResponse({"error": "unknown call"}, status_code=403)

        client = get_rest_client()
        if client is None:
            return JSONResponse({"error": "SignalWire credentials not configured"}, status_code=500)

        try:
            client.calling.ai_unhold(call_id)
            logger.info(f"Trailer unhold: call {call_id} released")
            return {"held": False}
        except Exception as e:
            logger.warning(f"Trailer unhold failed for {call_id}: {e}")
            return JSONResponse({"error": "unhold failed"}, status_code=502)

    @server.app.get("/get_token")
    def get_token():
        """Get a guest token for the web client to call the agent."""
        client = get_rest_client()

        if client is None:
            return JSONResponse({"error": "SignalWire credentials not configured (SIGNALWIRE_SPACE_NAME / SIGNALWIRE_PROJECT_ID / SIGNALWIRE_TOKEN)"}, status_code=500)

        # Registration happens at startup, but retry lazily here so a
        # transient failure (or an extra worker) heals itself
        if not swml_handler_info.get("address_id"):
            with swml_setup_lock:
                if not swml_handler_info.get("address_id"):
                    setup_swml_handler()

        if not swml_handler_info.get("address_id"):
            reason = swml_setup_error or "unknown error - check server logs"
            return JSONResponse({"error": f"SWML handler not registered: {reason}"}, status_code=500)

        try:
            # Create a guest token with access to this address
            expire_at = int(time.time()) + 3600 * 24  # 24 hours

            guest = client.fabric.tokens.create_guest_token(
                allowed_addresses=[swml_handler_info["address_id"]],
                expire_at=expire_at
            )
            guest_token = guest.get("token", "")

            return {
                "token": guest_token,
                "address": swml_handler_info["address"]
            }

        except Exception as e:
            logger.error(f"Token request failed: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    @server.app.get("/get_resource_info")
    async def get_resource_info():
        """Return SWML handler info for debugging."""
        return JSONResponse(content=swml_handler_info)

    @server.app.on_event("startup")
    async def on_startup():
        """Register SWML handler on startup."""
        setup_swml_handler()

    return server


# Create server and expose app for gunicorn
server = create_server()
app = server.app


if __name__ == "__main__":
    server.run()
