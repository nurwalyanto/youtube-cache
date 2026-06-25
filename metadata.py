import json
import os
from threading import Lock
from config import METADATA_FILE

_lock = Lock()

def load_metadata():
    with _lock:
        if not os.path.exists(METADATA_FILE):
            return []
        with open(METADATA_FILE, "r") as f:
            try:
                return json.load(f)
            except:
                return []

def save_metadata(data):
    with _lock:
        with open(METADATA_FILE, "w") as f:
            json.dump(data, f, indent=2)

def add_video(info):
    meta = load_metadata()
    for i, v in enumerate(meta):
        if v["id"] == info["id"]:
            meta[i] = info
            save_metadata(meta)
            return
    meta.append(info)
    save_metadata(meta)

def remove_video(video_id):
    meta = load_metadata()
    meta = [v for v in meta if v["id"] != video_id]
    save_metadata(meta)

def get_video(video_id):
    meta = load_metadata()
    for v in meta:
        if v["id"] == video_id:
            return v
    return None

def find_by_title(title):
    meta = load_metadata()
    for v in meta:
        if v.get("title") == title:
            return v
    alt = title.replace('-', '/')
    if alt != title:
        for v in meta:
            if v.get("title") == alt:
                return v
    return None
