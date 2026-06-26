import os
import shutil
import platform

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CACHE_DIR = os.path.join(BASE_DIR, "cache")
VIDEOS_DIR = os.path.join(CACHE_DIR, "videos")
SUBTITLES_DIR = os.path.join(CACHE_DIR, "subtitles")
THUMBNAILS_DIR = os.path.join(CACHE_DIR, "thumbnails")
METADATA_FILE = os.path.join(CACHE_DIR, "metadata.json")

HOST = "0.0.0.0"
PORT = 5000
DLNA_PORT = 5001
SERVER_NAME = "YouTube Cache"
SUBTITLE_URL_PREFIX = "/subtitle"
DEFAULT_SUBTITLE_LANG = "en"

# HLS cache — on Windows always use disk; on Linux prefer tmpfs
if platform.system() == "Windows":
    HLS_DIR = os.path.join(CACHE_DIR, "hls")
else:
    HLS_DIR = os.environ.get("YT_HLS_DIR") or "/run/shm/yt-cache"
    try:
        os.makedirs(HLS_DIR, exist_ok=True)
        test_file = os.path.join(HLS_DIR, ".write_test")
        open(test_file, "w").close()
        os.remove(test_file)
    except (OSError, PermissionError):
        HLS_DIR = os.path.join(CACHE_DIR, "hls")
os.makedirs(HLS_DIR, exist_ok=True)

FFMPEG_PATH = shutil.which("ffmpeg") or "ffmpeg"
FFMPEG_THREADS = int(os.environ.get("FFMPEG_THREADS", "2"))
FFMPEG_LOW_PRIORITY = os.environ.get("FFMPEG_LOW_PRIORITY", "1") == "1"
HLS_SEGMENT_DURATION = 6
