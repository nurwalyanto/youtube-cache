# YouTube Cache

Self-hosted YouTube video caching server with play-while-downloading, web UI, and DLNA streaming.

## Features

- **Search & download** YouTube videos from the web UI
- **Play while downloading** — watch the video as it downloads (DASH and progressive formats)
- **DASH support** — parallel video+audio download for higher quality with partial playback
- **Media Source Extensions (MSE)** — dynamically growing seek range during DASH downloads
- **DLNA/UPnP** — stream to smart TVs, consoles, and media players on your network
- **Persistent library** — downloads survive server restarts; resume where you left off
- **Subtitle support** — automatic subtitle download for common languages
- **Format selection** — choose quality, see HDR labels, codec info, and file sizes
- **Manual cache management** — delete videos you no longer need

## Requirements

- Python 3.9+
- ffmpeg (for DASH merge; must be in PATH on Windows)
- A browser with MSE support (Chrome, Firefox, Edge, Safari)

## Quick Start

### Linux / macOS

```bash
cd youtube-cache
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python3 app.py
```

### Windows (PowerShell)

```powershell
cd youtube-cache
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Open http://localhost:5000 in your browser.

## Configuration

Edit `config.py`:

| Setting | Default | Description |
|---------|---------|-------------|
| `PORT` | 5000 | Web UI port |
| `DLNA_PORT` | 5001 | DLNA server port |
| `HOST` | 0.0.0.0 | Bind address |
| `SERVER_NAME` | "YouTube Cache" | Display name |

## Avoiding YouTube 403 Errors

YouTube may block unauthenticated requests. Pass browser cookies to yt-dlp:

### Option 1: Auto-extract from browser (recommended)

In `downloader.py`, set `"cookiesfrombrowser"` to your browser:

```python
"cookiesfrombrowser": ("firefox",),      # Firefox
"cookiesfrombrowser": ("chrome",),        # Chrome/Chromium
"cookiesfrombrowser": ("brave",),         # Brave
```

The browser must be closed when the download starts (cookies file is locked while the browser runs).

### Option 2: Manual cookie file

1. Install a cookies export extension for your browser
2. Export cookies as Netscape-format `cookies.txt`
3. Place it in the project directory
4. In `downloader.py`, use:

```python
"cookiefile": "cookies.txt",
```

## How It Works

### Architecture

```
Browser  ──▶  Flask App (port 5000)
                 │
                 ├── yt-dlp ──▶ YouTube
                 │                  │
                 │            ┌─────┴─────┐
                 │            │ Video .f* │  Audio .f*
                 │            │ (fMP4)    │  (.m4a)
                 │            └─────┬─────┘
                 │                  │
                 │            ffmpeg merge
                 │                  │
                 │            ┌─────┘
                 │            │ merged .mp4
                 │            │
                 ├── Streamer ──▶ HTTP range-request video/audio
                 │
                 ├── DLNA server (port 5001) ──▶ TV / Console
                 │
                 └── Tasks ──▶ Persistent download state
```

### Play-While-Downloading

**DASH formats** (high quality, separate video+audio):
1. Two concurrent yt-dlp threads download video (fMP4) and audio (M4A)
2. Both files grow simultaneously
3. MSE (Media Source Extensions) feeds video fragments to the browser as they arrive
4. Audio plays separately via `<audio>` element, synced to video
5. The seek bar expands as the download progresses
6. On completion, ffmpeg merges video+audio into a single MP4
7. The player seamlessly switches to the merged file

**Progressive formats** (single file with audio+video):
1. The file streams directly via HTTP range requests
2. A "Refresh seek range" button lets you expand the seekable area
3. On completion, the player auto-reloads with the full file

### DLNA

Start DLNA from the web UI: the server broadcasts your library as a UPnP media server on port 5001 for TV/console discovery.

## API Endpoints

| Route | Method | Description |
|-------|--------|-------------|
| `/` | GET | Library index |
| `/search` | GET | Search page |
| `/api/search?q=<query>` | GET | JSON search results |
| `/api/formats/<id>` | GET | Available formats for a video |
| `/api/download` | POST | Start a download |
| `/api/progress/<task_id>` | GET | Download progress |
| `/api/active-tasks` | GET | All active downloads |
| `/api/active-tasks/<id>` | DELETE | Cancel a download |
| `/api/library` | GET | Library contents |
| `/api/library/<id>` | DELETE | Remove a video |
| `/api/stream-info/<id>` | GET | Streaming metadata (codecs, sizes, MSE info) |
| `/api/init-segment/<id>` | GET | MSE init segment for DASH playback |
| `/api/video-chunks/<id>` | GET | Completed fMP4 fragments for MSE |
| `/api/diag/<id>` | GET | Diagnostic info for debugging |
| `/stream/<id>` | GET | Video stream (range-request) |
| `/stream_audio/<id>` | GET | DASH audio stream |
| `/video/<id>` | GET | Player page |
| `/api/dlna/start` | POST | Start DLNA server |
| `/api/dlna/stop` | POST | Stop DLNA server |
| `/api/dlna/status` | GET | DLNA server status |

## Project Structure

```
youtube-cache/
├── app.py              # Flask application, routes
├── config.py           # Paths and server configuration
├── downloader.py       # yt-dlp wrapper, search, format listing
├── streamer.py         # HTTP streaming, MSE support, codec handling
├── metadata.py         # Video library metadata store
├── tasks.py            # Persistent download task management
├── dlna_server.py      # UPnP/DLNA media server
├── templates/
│   ├── base.html       # Layout, CSS, drawer
│   ├── index.html      # Library listing
│   ├── search.html     # Search results
│   └── player.html     # Video player with MSE support
├── static/
│   └── style.css       # Dark theme styling
├── cache/
│   ├── videos/         # Downloaded video files
│   ├── subtitles/      # Subtitle files
│   ├── thumbnails/     # Thumbnail images
│   └── active_downloads.json  # Download state
├── requirements.txt    # Python dependencies
├── opencode.json       # Opencode MCP configuration
└── README.md
```
