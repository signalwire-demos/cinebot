# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

CineBot is a voice-driven movie/TV discovery assistant powered by SignalWire AI Agents and The Movie Database (TMDB). Users interact via WebRTC video/audio to have natural conversations about movies, TV shows, and actors.

## Development Commands

```bash
# Activate virtual environment
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Run locally (development)
python app.py

# Run with gunicorn (production-like)
gunicorn app:app --bind 0.0.0.0:3030 --workers 2 --worker-class uvicorn.workers.UvicornWorker

# Test TMDB connection
python -c "from tmdb_client import TMDBClient; client = TMDBClient('your_key'); print(client.search_movie('Star Wars'))"
```

## Required Environment Variables

- `TMDB_API_KEY` - TMDB API key (required)
- `SIGNALWIRE_SPACE_NAME` - SignalWire space (e.g., 'myspace' or 'myspace.signalwire.com')
- `SIGNALWIRE_PROJECT_ID` - SignalWire project ID
- `SIGNALWIRE_TOKEN` - SignalWire API token
- `SWML_PROXY_URL_BASE` - Public URL base for SWML callbacks
- `REDIS_URL` - Optional Redis URL for caching
- `PORT` - Server port (default: 3030)

## Architecture

### Core Components

**app.py** - Main application containing:
- `MovieAgent(AgentBase)` - SignalWire AI Agent with conversation state machine and SWAIG function tools
- `create_server()` - Creates AgentServer with static file mounting and API endpoints
- `setup_swml_handler()` - Registers/updates SWML handler with SignalWire Fabric on startup

**tmdb_client.py** - TMDB API wrapper with Redis caching. All TMDB operations go through this client.

**web/** - Frontend static files (index.html, app.js, styles.css) served by FastAPI

### SignalWire Integration Pattern

The agent uses SignalWire's SWAIG (SignalWire AI Gateway) pattern:
1. Agent registers as external SWML handler on startup
2. Frontend gets guest token via `/get_token` endpoint
3. WebRTC call connects to the agent's address
4. Agent responds with SWML and uses `swml_user_event()` to send UI updates to frontend

### State Machine

The agent uses a context-based state machine with states:
- `greeting` - Initial state
- `browsing` - Viewing search results or lists
- `movie_details` - Viewing specific movie
- `tv_details` - Viewing specific TV show
- `person_details` - Viewing actor/director info

State transitions are managed via `result.swml_change_step("state_name")`.

### SWAIG Functions (Tools)

Functions are defined with `@self.tool()` decorator in `_setup_functions()`. Key patterns:
- Functions receive `(args, raw_data)` parameters
- Return `SwaigFunctionResult(response="...")` for voice response
- Use `result.swml_user_event({...})` to send data to frontend
- Use `result.swml_change_step("...")` for state transitions

Search result mappings (`self.search_result_mapping`, `self.person_search_mapping`) track IDs for voice navigation (e.g., "tell me about the first one").

### Frontend Event System

Events flow one-way from backend to frontend:
- `movie_search_results`, `tv_search_results` - Search results
- `movie_details`, `tv_details` - Content details
- `person_details` - Person information
- `trending_movies`, `trending_tv` - Trending content
- `video_available`, `videos_available` - Trailer/clip playback
- `clear_display` - Clear UI

## Deployment

Deployed via Dokku with GitHub Actions (`.github/workflows/deploy.yml`). Configuration in `.dokku/config.yml` and `.dokku/services.yml`.
