import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import downloader
import streamer
import metadata
from config import VIDEOS_DIR


def extract_video_ids():
    ids = set()
    for f in os.listdir(VIDEOS_DIR):
        name, ext = os.path.splitext(f)
        if ext in (".jpg", ".jpeg", ".png", ".webp"):
            continue
        vid = name.split(".f")[0] if ".f" in name else name
        if vid and not vid.startswith("."):
            ids.add(vid)
    return sorted(ids)


def video_has_file(video_id):
    if downloader._find_merged_file(video_id):
        return True
    v_path, _ = streamer._get_dash_paths(video_id)
    return v_path is not None


def detect_format(video_id):
    for f in os.listdir(VIDEOS_DIR):
        name, ext = os.path.splitext(f)
        if ".f" in name and name.split(".")[0] == video_id:
            parts = name.split(".f")
            if len(parts) >= 2 and parts[-1]:
                return parts[-1]
    return ""


def main():
    video_ids = extract_video_ids()
    print(f"Found {len(video_ids)} unique video IDs")

    entries = []
    for vid in video_ids:
        if not video_has_file(vid):
            print(f"  Skipping {vid}: no valid video file")
            continue

        print(f"  Fetching info for {vid}...", end=" ", flush=True)
        try:
            info = downloader.get_video_info(vid)
        except Exception as e:
            print(f"ERROR: {e}")
            continue

        subs = streamer.list_subtitles_on_disk(vid)
        thumb_path, _ = streamer.get_thumbnail(vid)
        thumb_url = f"/thumb/{vid}" if thumb_path else ""
        format_id = detect_format(vid)

        entry = {
            "id": vid,
            "title": info.get("title", "Unknown"),
            "duration": info.get("duration", 0),
            "channel": info.get("channel", "Unknown"),
            "description": "",
            "thumbnail": thumb_url,
            "subtitles": subs,
            "format_label": "",
            "format_id": format_id,
        }
        entries.append(entry)
        title_short = (entry["title"][:50] + "..") if len(entry["title"]) > 50 else entry["title"]
        try:
            print(f"OK - {title_short}")
        except UnicodeEncodeError:
            print(f"OK - {title_short.encode('ascii', 'replace').decode()}")

    metadata.save_metadata(entries)
    print(f"\nSaved {len(entries)} entries to metadata.json")


if __name__ == "__main__":
    main()
