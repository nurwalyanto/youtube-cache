import mimetypes
import os
import re
from flask import request, Response, abort, send_file

from config import VIDEOS_DIR, SUBTITLES_DIR, THUMBNAILS_DIR, HLS_SEGMENT_DURATION
import tasks
import hls_manager

mimetypes.add_type('image/webp', '.webp')

VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".m4v", ".m4a"}
MIN_VIDEO_SIZE = 102400
AUDIO_FORMAT_IDS = {"139", "140", "141", "249", "250", "251", "255", "256", "258"}

def _is_audio_file(filename):
    name, ext = os.path.splitext(filename)
    if ext == ".m4a":
        return True
    if ".f" in name:
        parts = name.split(".f")
        if len(parts) >= 2 and parts[-1] in AUDIO_FORMAT_IDS:
            return True
    return False

def _parse_format_id(filename):
    name, ext = os.path.splitext(filename)
    if ".f" in name:
        return name.split(".f")[-1]
    return None

def get_video_path(video_id, exact_only=False, prefer_dash=False):
    if prefer_dash:
        actives = tasks.get_active_tasks()
        dash_fmt = None
        for t in actives.values():
            if t["video_id"] == video_id and t.get("dash_video_fmt"):
                dash_fmt = t["dash_video_fmt"]
                break
        if dash_fmt:
            candidate = os.path.join(VIDEOS_DIR, f"{video_id}.f{dash_fmt}.mp4")
            if os.path.exists(candidate) and os.path.getsize(candidate) >= MIN_VIDEO_SIZE:
                return candidate
    for f in os.listdir(VIDEOS_DIR):
        name, ext = os.path.splitext(f)
        fpath = os.path.join(VIDEOS_DIR, f)
        if name == video_id and ext in VIDEO_EXTS:
            if os.path.getsize(fpath) >= MIN_VIDEO_SIZE:
                return fpath
    if not exact_only:
        best = None
        for f in os.listdir(VIDEOS_DIR):
            name, ext = os.path.splitext(f)
            if ext not in VIDEO_EXTS or ".f" not in name or name.split(".")[0] != video_id:
                continue
            if _is_audio_file(f):
                continue
            fpath = os.path.join(VIDEOS_DIR, f)
            if os.path.getsize(fpath) < MIN_VIDEO_SIZE:
                continue
            best = fpath
        if best:
            return best
    return None

def _get_dash_paths(video_id):
    """Return (video_path, audio_path) for DASH files, checking active task first then scanning disk."""
    actives = tasks.get_active_tasks()
    for t in actives.values():
        if t["video_id"] != video_id:
            continue
        v_fmt = t.get("dash_video_fmt")
        a_fmt = t.get("dash_audio_fmt")
        v_path = a_path = None
        if v_fmt:
            target = f"{video_id}.f{v_fmt}"
            for f in os.listdir(VIDEOS_DIR):
                name, ext = os.path.splitext(f)
                if name == target and ext in VIDEO_EXTS:
                    full = os.path.join(VIDEOS_DIR, f)
                    if os.path.getsize(full) >= MIN_VIDEO_SIZE:
                        v_path = full
                    break
        if a_fmt:
            target = f"{video_id}.f{a_fmt}"
            for f in os.listdir(VIDEOS_DIR):
                name, ext = os.path.splitext(f)
                if name == target and ext in VIDEO_EXTS:
                    full = os.path.join(VIDEOS_DIR, f)
                    if os.path.getsize(full) >= MIN_VIDEO_SIZE:
                        a_path = full
                    break
        return v_path, a_path
    # Fallback: scan for .fXXX files even without active task (completed DASH)
    v_path = a_path = None
    for f in os.listdir(VIDEOS_DIR):
        name, ext = os.path.splitext(f)
        if ext not in VIDEO_EXTS:
            continue
        if ".f" not in name or name.split(".")[0] != video_id:
            continue
        full = os.path.join(VIDEOS_DIR, f)
        if os.path.getsize(full) < MIN_VIDEO_SIZE:
            continue
        if _is_audio_file(f):
            if not a_path:
                a_path = full
        elif not v_path:
            v_path = full
    return v_path, a_path

def _is_download_complete(video_id):
    """Check if there is no active task and the video is fully on disk."""
    actives = tasks.get_active_tasks()
    still_downloading = any(t["video_id"] == video_id for t in actives.values())
    if still_downloading:
        return False
    merged = get_video_path(video_id, exact_only=True)
    if merged and os.path.getsize(merged) >= MIN_VIDEO_SIZE:
        return True
    v_path, a_path = _get_dash_paths(video_id)
    if v_path and os.path.getsize(v_path) >= MIN_VIDEO_SIZE:
        return True
    return False


# ─── HLS (FFmpeg-backed) ───────────────────────────────

def _ensure_hls_init(video_id):
    """Ensure HLS metadata exists for DASH video. Returns (v_path, a_path) or (None, None)."""
    v_path, a_path = _get_dash_paths(video_id)
    if not v_path:
        return None, None
    try:
        hls_manager.initialize(video_id, v_path, a_path)
    except Exception:
        return None, None
    return v_path, a_path


def _seed_segments(video_id, v_path, a_path):
    """Generate new segments sequentially until one is empty.
    On first call (no existing segments), seeds up to 10.
    On subsequent calls, generates at most 1 new segment
    to keep playlist poll responses fast (~1-2s)."""
    existing = hls_manager.segment_count(video_id)
    max_new = 10 if existing == 0 else 1
    new_count = 0
    n = 0
    while n < 1000:
        try:
            path, dur, st = hls_manager.get_or_create_segment(video_id, v_path, a_path, n)
        except Exception:
            break
        if path is None:
            break
        if n >= existing:
            new_count += 1
        if dur < HLS_SEGMENT_DURATION * 0.3:
            break
        if new_count >= max_new:
            break
        n += 1
    return new_count


def hls_master_playlist(video_id):
    """Build m3u8 playlist from HLS cache.
    Returns (body, content_type) or (None, None) if not available."""
    if get_video_path(video_id, exact_only=True):
        return None, None
    v_path, a_path = _ensure_hls_init(video_id)
    if not v_path:
        return None, None
    _seed_segments(video_id, v_path, a_path)
    playlist = hls_manager.build_playlist(
        video_id,
        include_endlist=hls_manager.is_playlist_complete(video_id),
    )
    if playlist is None:
        return None, None
    return playlist, "application/vnd.apple.mpegurl"


def hls_serve_file(video_id, filename):
    """Serve a file from HLS cache. Generates on-demand if needed."""
    if filename == "master.m3u8":
        try:
            v_path, a_path = _ensure_hls_init(video_id)
            if not v_path:
                abort(404)
            _seed_segments(video_id, v_path, a_path)
        except Exception:
            abort(404)
        playlist = hls_manager.build_playlist(
            video_id,
            include_endlist=hls_manager.is_playlist_complete(video_id),
        )
        if not playlist:
            abort(404)
        return Response(playlist, mimetype="application/vnd.apple.mpegurl",
                        headers={"Cache-Control": "no-cache"})

    v_path, a_path = _get_dash_paths(video_id)
    if not v_path:
        abort(404)

    if filename == "init.mp4":
        try:
            ok = hls_manager.ensure_init(video_id, v_path, a_path)
        except Exception:
            abort(404)
        if not ok:
            abort(404)
        path = os.path.join(hls_manager.video_dir(video_id), "init.mp4")
        if not os.path.exists(path):
            abort(404)
        return send_file(path, mimetype="video/mp4")

    m = re.match(r"seg_(\d+)\.(ts|m4s)$", filename)
    if not m:
        abort(404)
    n = int(m.group(1))

    try:
        path, duration, seg_type = hls_manager.get_or_create_segment(video_id, v_path, a_path, n)
    except Exception:
        abort(404)
    if path is None:
        abort(404)

    mime = "video/mp4" if m.group(2) == "m4s" else "video/MP2T"
    return send_file(path, mimetype=mime)


def stream_video(video_id):
    path = get_video_path(video_id, prefer_dash=True)
    if not path:
        abort(404)

    is_dash_partial = ".f" in os.path.basename(path)
    file_size = os.path.getsize(path)
    mime_type, _ = mimetypes.guess_type(path)
    if not mime_type:
        mime_type = "video/mp4"

    range_header = request.headers.get("Range", None)
    if not range_header:
        def generate_full():
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    yield chunk
        resp = Response(generate_full(), mimetype=mime_type)
        resp.headers["Content-Length"] = file_size
        resp.headers["Accept-Ranges"] = "bytes"
        resp.headers["X-Stream-Type"] = "dash-partial" if is_dash_partial else "complete"
        return resp

    start, end = 0, file_size - 1
    match = re.match(r"bytes=(\d+)-(\d*)", range_header)
    if match:
        start = int(match.group(1))
        if match.group(2):
            end = min(int(match.group(2)), file_size - 1)

    if start >= file_size:
        return Response(status=416)

    length = end - start + 1

    def generate():
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk_size = min(8192, remaining)
                data = f.read(chunk_size)
                if not data:
                    break
                remaining -= len(data)
                yield data

    resp = Response(generate(), status=206, mimetype=mime_type)
    resp.headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
    resp.headers["Content-Length"] = length
    resp.headers["Accept-Ranges"] = "bytes"
    resp.headers["X-Stream-Type"] = "dash-partial" if is_dash_partial else "complete"
    return resp


def _find_audio_path(video_id):
    """Find audio file for a video, checking active task first then scanning disk."""
    actives = tasks.get_active_tasks()
    audio_fmt = None
    for t in actives.values():
        if t["video_id"] == video_id:
            audio_fmt = t.get("dash_audio_fmt")
            break
    if audio_fmt:
        target = f"{video_id}.f{audio_fmt}"
        for f in os.listdir(VIDEOS_DIR):
            name, ext = os.path.splitext(f)
            if name == target and ext in VIDEO_EXTS:
                return os.path.join(VIDEOS_DIR, f)
    for f in os.listdir(VIDEOS_DIR):
        name, ext = os.path.splitext(f)
        if ".f" in name and name.split(".")[0] == video_id and ext == ".m4a":
            return os.path.join(VIDEOS_DIR, f)
    for f in os.listdir(VIDEOS_DIR):
        name, ext = os.path.splitext(f)
        if ".f" in name and name.split(".")[0] == video_id and ext in VIDEO_EXTS and ext != ".mp4":
            return os.path.join(VIDEOS_DIR, f)
    return None


def stream_audio_track(video_id):
    path = _find_audio_path(video_id)
    if not path:
        abort(404)

    ext = os.path.splitext(path)[1]
    file_size = os.path.getsize(path)
    mime_type = "audio/mp4" if ext == ".m4a" else "audio/webm" if ext == ".webm" else "audio/mp4"

    range_header = request.headers.get("Range", None)
    if not range_header:
        def generate_audio_full():
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    yield chunk
        resp = Response(generate_audio_full(), mimetype=mime_type)
        resp.headers["Content-Length"] = file_size
        resp.headers["Accept-Ranges"] = "bytes"
        return resp

    start, end = 0, file_size - 1
    match = re.match(r"bytes=(\d+)-(\d*)", range_header)
    if match:
        start = int(match.group(1))
        if match.group(2):
            end = min(int(match.group(2)), file_size - 1)
    if start >= file_size:
        return Response(status=416)
    length = end - start + 1

    def generate():
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk_size = min(8192, remaining)
                data = f.read(chunk_size)
                if not data:
                    break
                remaining -= len(data)
                yield data

    resp = Response(generate(), status=206, mimetype=mime_type)
    resp.headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
    resp.headers["Content-Length"] = length
    resp.headers["Accept-Ranges"] = "bytes"
    return resp


def stream_status(video_id):
    """Status endpoint for frontend polling."""
    is_done = _is_download_complete(video_id)
    has_merged = get_video_path(video_id, exact_only=True) is not None
    v_path, a_path = _get_dash_paths(video_id)
    has_hls = hls_manager.build_playlist(video_id) is not None
    result = {
        "is_done": is_done,
        "is_downloading": any(t["video_id"] == video_id for t in tasks.get_active_tasks().values()),
        "has_merged": has_merged,
        "has_video": bool(v_path) or has_merged,
        "has_audio": bool(a_path) or has_merged,
        "hls_ready": has_hls,
        "duration": 0,
    }
    if not v_path and not has_merged:
        any_path = get_video_path(video_id)
        if any_path:
            result["has_video"] = True
    return result


def list_subtitles_on_disk(video_id):
    """Scan cache/subtitles/{video_id}/ for subtitle files.
    Returns list of dicts [{"lang": "en", "file": "en.vtt"}, ...]"""
    video_sub_dir = os.path.join(SUBTITLES_DIR, video_id)
    if not os.path.isdir(video_sub_dir):
        return []
    found = []
    for f in sorted(os.listdir(video_sub_dir)):
        name, ext = os.path.splitext(f)
        if ext in (".vtt", ".srt", ".ass"):
            found.append({"lang": name, "file": f})
    return found


def get_subtitle(video_id, lang):
    # Organized dir first: cache/subtitles/{video_id}/{lang}.{ext}
    video_sub_dir = os.path.join(SUBTITLES_DIR, video_id)
    if os.path.isdir(video_sub_dir):
        for f in os.listdir(video_sub_dir):
            name, ext = os.path.splitext(f)
            if name == lang and ext in (".vtt", ".srt", ".ass"):
                path = os.path.join(video_sub_dir, f)
                with open(path, "r", encoding="utf-8") as fh:
                    content = fh.read()
                if ext == ".srt":
                    content = srt_to_vtt(content)
                return content, "text/vtt"
    # Fallback to flat files: cache/subtitles/{video_id}_{lang}.{ext}
    for f in os.listdir(SUBTITLES_DIR):
        name, ext = os.path.splitext(f)
        expected = f"{video_id}_{lang}"
        if name == expected and ext in (".vtt", ".srt", ".ass"):
            path = os.path.join(SUBTITLES_DIR, f)
            with open(path, "r", encoding="utf-8") as fh:
                content = fh.read()
            if ext == ".srt":
                content = srt_to_vtt(content)
            return content, "text/vtt"
    return None, None


def srt_to_vtt(srt_text):
    lines = srt_text.split("\n")
    cleaned = []
    for line in lines:
        line = line.replace(",", ".")
        cleaned.append(line)
    return "WEBVTT\n\n" + "\n".join(cleaned)


def get_thumbnail(video_id):
    for f in os.listdir(THUMBNAILS_DIR):
        name, _ = os.path.splitext(f)
        if name == video_id:
            path = os.path.join(THUMBNAILS_DIR, f)
            mime, _ = mimetypes.guess_type(path)
            return path, mime or "image/jpeg"
    for f in os.listdir(VIDEOS_DIR):
        name, ext = os.path.splitext(f)
        if ".f" in name and name.split(".")[0] == video_id and ext in (".jpg", ".jpeg", ".png", ".webp"):
            mime, _ = mimetypes.guess_type(f)
            return os.path.join(VIDEOS_DIR, f), mime or "image/jpeg"
    return None, None
