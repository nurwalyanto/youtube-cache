from config import VIDEOS_DIR, SUBTITLES_DIR, THUMBNAILS_DIR
from flask import request, Response, abort, jsonify
import tasks
import os
import re
import mimetypes
mimetypes.add_type('image/webp', '.webp')


VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".m4v", ".m4a"}


def get_video_path(video_id, exact_only=False, prefer_dash=False):
    if prefer_dash:
        # Return DASH intermediate .f{fmt}.mp4 file if it exists (partial download)
        actives = tasks.get_active_tasks()
        dash_fmt = None
        for t in actives.values():
            if t["video_id"] == video_id and t.get("dash_video_fmt"):
                dash_fmt = t["dash_video_fmt"]
                break
        if dash_fmt:
            candidate = os.path.join(VIDEOS_DIR, f"{video_id}.f{dash_fmt}.mp4")
            if os.path.exists(candidate):
                return candidate
    for f in os.listdir(VIDEOS_DIR):
        name, ext = os.path.splitext(f)
        if name == video_id and ext in VIDEO_EXTS:
            return os.path.join(VIDEOS_DIR, f)
    if not exact_only:
        for f in os.listdir(VIDEOS_DIR):
            name, ext = os.path.splitext(f)
            if ext in VIDEO_EXTS and ".f" in name and name.split(".")[0] == video_id:
                return os.path.join(VIDEOS_DIR, f)
    return None


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


def get_subtitle(video_id, lang):
    for f in os.listdir(SUBTITLES_DIR):
        name, ext = os.path.splitext(f)
        expected = f"{video_id}_{lang}"
        if name == expected and ext in (".vtt", ".srt", ".ass"):
            path = os.path.join(SUBTITLES_DIR, f)
            with open(path, "r", encoding="utf-8") as fh:
                content = fh.read()
            mime = "text/vtt"
            if ext == ".srt":
                content = srt_to_vtt(content)
                mime = "text/vtt"
            return content, mime
    return None, None


def srt_to_vtt(srt_text):
    lines = srt_text.split("\n")
    cleaned = []
    for line in lines:
        line = line.replace(",", ".")
        cleaned.append(line)
    return "WEBVTT\n\n" + "\n".join(cleaned)


def stream_audio_track(video_id):
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
                path = os.path.join(VIDEOS_DIR, f)
                break
        else:
            abort(404)
    else:
        # No active task with audio format - scan disk for any .f* audio file
        audio_path = None
        for f in os.listdir(VIDEOS_DIR):
            name, ext = os.path.splitext(f)
            if ".f" in name and name.split(".")[0] == video_id and ext == ".m4a":
                audio_path = os.path.join(VIDEOS_DIR, f)
                break
        if not audio_path:
            for f in os.listdir(VIDEOS_DIR):
                name, ext = os.path.splitext(f)
                if ".f" in name and name.split(".")[0] == video_id and ext in VIDEO_EXTS and ext != ".mp4":
                    audio_path = os.path.join(VIDEOS_DIR, f)
                    break
        if not audio_path:
            abort(404)
        path = audio_path

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


def _init_segment_size(path):
    """Scan fMP4 from start, accumulate box sizes until first 'moof' box.
    Returns offset of first moof (init segment size), or None if incomplete."""
    try:
        file_size = os.path.getsize(path)
    except OSError:
        return None
    if file_size < 8:
        return None
    total = 0
    with open(path, 'rb') as f:
        while True:
            pos = f.tell()
            if pos + 8 > file_size:
                return None
            buf = f.read(8)
            if len(buf) < 8:
                return None
            size = int.from_bytes(buf[:4], 'big')
            box_type = buf[4:8].decode('ascii', errors='replace')
            if box_type == 'moof':
                return total
            if size < 8:
                return None
            if pos + size > file_size:
                return None
            f.seek(pos + size)
            total += size


def _build_mime(codec):
    if not codec:
        return None
    if any(codec.startswith(p) for p in ("avc1", "hev1", "hvc1", "av01", "vp09", "vp9")):
        return f'video/mp4; codecs="{codec}"'
    if codec.startswith("av1"):
        return f'video/webm; codecs="{codec}"'
    if any(codec.startswith(p) for p in ("mp4a", "opus")):
        return f'audio/mp4; codecs="{codec}"'
    return None


def stream_info(video_id):
    """Returns codec info for MSE initialization."""
    import downloader

    # Check merged file first (completed download)
    merged_path = get_video_path(video_id, exact_only=True)
    if merged_path and os.path.exists(merged_path):
        return {
            "is_dash": False,
            "has_video": True,
            "has_audio": True,
            "video_mime": 'video/mp4; codecs="avc1.64002a"',
            "audio_mime": None,
            "video_size": os.path.getsize(merged_path),
            "audio_size": None,
            "init_video": None,
            "init_audio": None,
            "duration": _mvhd_duration(merged_path) or 0,
            "is_done": True,
        }

    actives = tasks.get_active_tasks()
    result = {
        "is_dash": False,
        "has_video": False,
        "has_audio": False,
        "video_mime": None,
        "audio_mime": None,
        "video_size": None,
        "audio_size": None,
        "init_video": None,
        "init_audio": None,
        "is_done": False,
    }

    for t in actives.values():
        if t["video_id"] == video_id:
            v_fmt = t.get("dash_video_fmt")
            a_fmt = t.get("dash_audio_fmt")
            v_codec = t.get("dash_video_codec")
            a_codec = t.get("dash_audio_codec")

            if v_fmt and a_fmt:
                result["is_dash"] = True
                v_path = os.path.join(VIDEOS_DIR, f"{video_id}.f{v_fmt}.mp4")
                if os.path.exists(v_path) and os.path.getsize(v_path) > 0:
                    init_video = _init_segment_size(v_path)
                    if init_video is not None:
                        result["has_video"] = True
                        result["video_mime"] = _build_mime(
                            v_codec) or 'video/mp4; codecs="avc1.64002a"'
                        result["video_size"] = os.path.getsize(v_path)
                        result["init_video"] = init_video
                        result["duration"] = _mvhd_duration(v_path) or 0

                a_path = downloader._find_dash_audio(video_id, a_fmt)
                if a_path and os.path.exists(a_path):
                    result["has_audio"] = True
                    result["audio_mime"] = _build_mime(
                        a_codec) or 'audio/mp4; codecs="mp4a.40.2"'
                    result["audio_size"] = os.path.getsize(a_path)
                    result["init_audio"] = _init_segment_size(a_path)
            else:
                # Active task but not DASH (progressive download) - check on disk
                any_path = get_video_path(video_id)
                if any_path and os.path.exists(any_path):
                    result["has_video"] = True
                    result["video_size"] = os.path.getsize(any_path)
                    mime, _ = mimetypes.guess_type(any_path)
                    result["video_mime"] = mime or 'video/mp4'
                    dur = _mvhd_duration(any_path)
                    result["duration"] = dur or 0
            break

    # Fallback: no active task, no merged file, but files exist on disk
    if not result["has_video"] and not result["has_audio"]:
        fallback_video_path = None
        fallback_audio_path = None
        # Check for exact-match regular file first (progressive download)
        exact = get_video_path(video_id, exact_only=True)
        if exact and os.path.exists(exact) and os.path.getsize(exact) > 0:
            fallback_video_path = exact
            # Progressive files contain both video and audio
            result["has_audio"] = True
        # Scan for DASH .f* files
        for f in os.listdir(VIDEOS_DIR):
            name, ext = os.path.splitext(f)
            if ".f" not in name or name.split(".")[0] != video_id:
                continue
            if ext not in VIDEO_EXTS:
                continue
            fpath = os.path.join(VIDEOS_DIR, f)
            if os.path.getsize(fpath) <= 0:
                continue
            if ext == ".m4a":
                if fallback_audio_path is None:
                    fallback_audio_path = fpath
            elif fallback_video_path is None:
                fallback_video_path = fpath
        if fallback_video_path:
            result["has_video"] = True
            result["video_size"] = os.path.getsize(fallback_video_path)
            mime, _ = mimetypes.guess_type(fallback_video_path)
            if mime and mime.startswith("audio/"):
                mime = "video/mp4"
            result["video_mime"] = mime or 'video/mp4'
            dur = _mvhd_duration(fallback_video_path)
            result["duration"] = dur or 0
        if fallback_audio_path:
            result["has_audio"] = True
            result["audio_mime"] = 'audio/mp4; codecs="mp4a.40.2"'
            result["audio_size"] = os.path.getsize(fallback_audio_path)

    return result


def _mvhd_duration(path):
    """Parse moov box to extract mvhd duration in seconds."""
    with open(path, 'rb') as f:
        f.seek(0, 2)
        file_size = f.tell()
        pos = 0
        while pos + 8 <= file_size:
            f.seek(pos)
            buf = f.read(8)
            if len(buf) < 8:
                break
            size = int.from_bytes(buf[:4], 'big')
            box_type = buf[4:8].decode('ascii', errors='replace')
            if size < 8:
                break
            if box_type == 'moov':
                end = pos + size
                child_pos = pos + 8
                while child_pos + 8 <= end:
                    f.seek(child_pos)
                    cbuf = f.read(8)
                    if len(cbuf) < 8:
                        break
                    csize = int.from_bytes(cbuf[:4], 'big')
                    ctype = cbuf[4:8].decode('ascii', errors='replace')
                    if csize < 8 or child_pos + csize > end:
                        break
                    if ctype == 'mvhd':
                        f.seek(child_pos + 8)
                        data = f.read(csize - 8)
                        if len(data) < 4:
                            return None
                        version = data[0]
                        if version == 0:
                            if len(data) < 20:
                                return None
                            timescale = int.from_bytes(data[12:16], 'big')
                            duration = int.from_bytes(data[16:20], 'big')
                        else:
                            if len(data) < 36:
                                return None
                            timescale = int.from_bytes(data[20:28], 'big')
                            duration = int.from_bytes(data[28:36], 'big')
                        if timescale > 0:
                            return duration / timescale
                        return None
                    child_pos += csize
                return None
            pos += size
    return None


def _last_complete_fragment_end(path, start_scan=0):
    """Scan from start_scan, find byte offset at end of last complete moof+mdat pair."""
    with open(path, 'rb') as f:
        f.seek(0, 2)
        file_size = f.tell()
        f.seek(start_scan)
        last_good = start_scan
        while True:
            pos = f.tell()
            buf = f.read(8)
            if len(buf) < 8:
                break
            box_size = int.from_bytes(buf[:4], 'big')
            box_type = buf[4:8].decode('ascii', errors='replace')
            if box_size < 8:
                break
            box_end = pos + box_size
            if box_end > file_size:
                break
            if box_type == 'moof':
                f.seek(box_end)
                buf2 = f.read(8)
                if len(buf2) < 8:
                    break
                mdat_size = int.from_bytes(buf2[:4], 'big')
                mdat_type = buf2[4:8].decode('ascii', errors='replace')
                mdat_end = box_end + mdat_size
                if mdat_type == 'mdat' and mdat_end <= file_size:
                    last_good = mdat_end
                    f.seek(mdat_end)
                else:
                    break
            else:
                f.seek(box_end)
        return last_good


def stream_video_chunks(video_id):
    after = request.args.get("after", 0, type=int)
    path = get_video_path(video_id, prefer_dash=True)
    if not path:
        abort(404)
    file_size = os.path.getsize(path)
    if after >= file_size:
        return Response(status=204)
    safe_end = _last_complete_fragment_end(path, start_scan=after)
    if safe_end <= after:
        return Response(status=204)
    length = safe_end - after
    mime_type, _ = mimetypes.guess_type(path)

    def generate():
        with open(path, "rb") as f:
            f.seek(after)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    resp = Response(generate(), status=200, mimetype=mime_type or "video/mp4")
    resp.headers["Content-Length"] = length
    resp.headers["X-Chunk-Start"] = str(after)
    resp.headers["X-Chunk-End"] = str(safe_end)
    return resp


def stream_diag(video_id):
    result = {
        "video_id": video_id,
        "files": [],
        "merged_exists": False,
        "merged_size": None,
        "active_tasks": [],
        "dashboard_url": f"/video/{video_id}",
    }
    for f in sorted(os.listdir(VIDEOS_DIR)):
        name, ext = os.path.splitext(f)
        if name == video_id or (".f" in name and name.split(".")[0] == video_id):
            fpath = os.path.join(VIDEOS_DIR, f)
            if ext in VIDEO_EXTS:
                result["files"].append({
                    "name": f,
                    "size": os.path.getsize(fpath),
                    "ext": ext,
                })
                if name == video_id:
                    result["merged_exists"] = True
                    result["merged_size"] = os.path.getsize(fpath)
    for tid, t in tasks.get_active_tasks().items():
        if t["video_id"] == video_id:
            result["active_tasks"].append({
                "task_id": tid,
                "status": t.get("status"),
                "dash_video_fmt": t.get("dash_video_fmt"),
                "dash_audio_fmt": t.get("dash_audio_fmt"),
            })
    return jsonify(result)


def stream_init_segment(video_id):
    """Serve init segment directly from the DASH .f{fmt}.mp4 file,
    bypassing get_video_path() which may switch to merged file."""
    actives = tasks.get_active_tasks()
    for t in actives.values():
        if t["video_id"] == video_id and t.get("dash_video_fmt"):
            fmt = t["dash_video_fmt"]
            path = os.path.join(VIDEOS_DIR, f"{video_id}.f{fmt}.mp4")
            if not os.path.exists(path):
                return Response(status=404)
            init_size = _init_segment_size(path)
            if init_size is None or init_size <= 0:
                return Response(status=204)
            with open(path, "rb") as f:
                data = f.read(init_size)
            return Response(data, status=200, mimetype="video/mp4",
                            headers={"X-Init-Size": str(init_size),
                                     "Content-Length": str(len(data))})
    return Response(status=404)


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
