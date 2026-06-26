"""Scan for videos with DASH fragments but no merged file, and add them to the download queue."""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import VIDEOS_DIR
import tasks
import metadata

AUDIO_FORMAT_IDS = {"139", "140", "141", "249", "250", "251", "255", "256", "258"}
VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".m4v", ".m4a"}
MIN_SIZE = 102400


def find_fragments():
    fragments = {}
    for f in os.listdir(VIDEOS_DIR):
        name, ext = os.path.splitext(f)
        if ext not in VIDEO_EXTS:
            continue
        if ".f" not in name:
            continue
        parts = name.split(".f")
        video_id = parts[0]
        fmt_id = parts[-1]
        full = os.path.join(VIDEOS_DIR, f)
        if os.path.getsize(full) < MIN_SIZE:
            continue
        if video_id not in fragments:
            fragments[video_id] = {"formats": set()}
        fragments[video_id]["formats"].add(fmt_id)
    return fragments


def has_merged(video_id):
    for f in os.listdir(VIDEOS_DIR):
        name, ext = os.path.splitext(f)
        if ext in VIDEO_EXTS and name == video_id:
            if os.path.getsize(os.path.join(VIDEOS_DIR, f)) >= MIN_SIZE:
                return True
    return False


def main():
    fragments = find_fragments()
    if not fragments:
        print("No DASH fragments found.")
        return

    unfinished = []
    for vid in fragments:
        if has_merged(vid):
            print(f"  OK   {vid} — merged file exists")
            continue
        meta = metadata.get_video(vid)
        fmt_id = None
        if meta:
            fmt_id = meta.get("format_id")
        if not fmt_id:
            fmt_id = max(fragments[vid]["formats"])
        unfinished.append({
            "video_id": vid,
            "format_id": fmt_id,
            "title": meta.get("title", vid) if meta else vid,
            "thumbnail": meta.get("thumbnail", "") if meta else "",
            "format_label": meta.get("format_label", fmt_id) if meta else fmt_id,
        })

    if not unfinished:
        print("\nAll videos have merged output. Nothing to queue.")
        return

    print(f"\nFound {len(unfinished)} unfinished video(s):")
    for item in unfinished:
        print(f"  {item['video_id']} (format {item['format_id']}) — {item['title'][:60]}")

    # Create tasks and queue them
    queued = 0
    for item in unfinished:
        vid = item["video_id"]
        task_id = f"{vid}_{int(time.time())}"
        tasks.create_task(task_id, vid, item["title"], item["thumbnail"],
                          item["format_id"], item["format_label"])
        tasks.update_task(task_id, status="queued")
        tasks.queue_task(task_id)
        print(f"  QUEUED {vid} (task {task_id})")
        queued += 1
        time.sleep(0.01)

    print(f"\nDone. {queued} task(s) queued.")
    print("Start the server and the downloads will begin automatically.")


if __name__ == "__main__":
    main()
