import os

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
