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
- ffmpeg (for HLS segment generation; must be in PATH)
- A browser with HLS support via hls.js (Chrome, Firefox, Edge) or native (Safari, iOS)

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
                  │            │ (webm/mp4)│  (.m4a)
                  │            └─────┬─────┘
                  │                  │
                  │            ┌─────┘
                  │            ▼
                  │      FFmpeg (on-demand, per-segment)
                  │      -c copy (no re-encode)
                  │            │
                  │            ▼
                  │      /run/shm/yt-cache/{id}/
                  │      init.mp4 + seg_0000.m4s + ...
                  │            │
                  ├── HLS serve ──▶ hls.js / ExoPlayer (single stream)
                  │
                  ├── HTTP range ──▶ progressive files / DLNA
                  │
                  ├── DLNA server (port 5001) ──▶ TV / Console
                  │
                  └── Tasks ──▶ Persistent download state
```

### Play-While-Downloading

**DASH formats** (high quality, separate video+audio):
1. Two concurrent yt-dlp threads download video (WebM/fMP4) and audio (M4A)
2. Both files grow simultaneously
3. On first viewer request, FFmpeg generates muxed HLS segments on-demand
4. hls.js feeds segments to the browser as they become available
5. Single `<video>` element — no separate audio tracking
6. Segments are cached in tmpfs for fast subsequent access
7. On completion, final segments are generated and playlist is finalized

**Progressive formats** (single file with audio+video):
1. The file streams directly via HTTP range requests
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
| `/api/stream-info/<id>` | GET | Streaming status (is_done, hls_ready, has_merged, etc.) |
| `/api/hls/<id>/master.m3u8` | GET | HLS master playlist |
| `/api/hls/<id>/init.mp4` | GET | HLS init segment (for fMP4 mode) |
| `/api/hls/<id>/seg_NNNN.m4s` | GET | Individual HLS media segment (generated on demand) |
| `/stream/<id>` | GET | Direct video stream (range-request, for progressive files) |
| `/stream_audio/<id>` | GET | DASH audio stream (fallback only) |
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
├── streamer.py         # HTTP streaming, HLS serving, subtitle/thumbnail
├── hls_manager.py      # FFmpeg HLS segment generation and cache management
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
