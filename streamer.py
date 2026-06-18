from config import VIDEOS_DIR, SUBTITLES_DIR, THUMBNAILS_DIR
from flask import request, Response, abort, jsonify
import tasks
import os
import re
import mimetypes
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
        parts = name.split(".f")
        return parts[-1]
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


def _ebml_vint_size(first_byte):
    """Return total bytes of an EBML variable-length integer given first byte."""
    if first_byte == 0:
        return 0
    mask = 0x80
    for i in range(1, 9):
        if first_byte & mask:
            return i
        mask >>= 1
    return 8

def _ebml_read_vint(f):
    buf = f.read(1)
    if not buf:
        return None, 0
    length = _ebml_vint_size(buf[0])
    if length == 1:
        return buf[0] & 0x7f, length
    extra = f.read(length - 1)
    if len(extra) < length - 1:
        return None, 0
    val = buf[0] & (0x7f >> (length - 1))
    for b in extra:
        val = (val << 8) | b
    return val, length

def _webm_init_offset(path):
    """For WebM/EBML files, find offset of first Cluster element.
    Returns the byte offset of the first Cluster ID, or None."""
    try:
        file_size = os.path.getsize(path)
    except OSError:
        return None
    if file_size < 12:
        return None
    with open(path, 'rb') as f:
        pos = 0
        while pos + 4 <= file_size:
            f.seek(pos)
            chunk = f.read(4)
            if len(chunk) < 4:
                break
            if chunk == b'\x1a\x45\xdf\xa3':
                pos += 4
                _, _ = _ebml_read_vint(f)
                continue
            if chunk == b'\x18\x53\x80\x67':
                pos += 4
                _, _ = _ebml_read_vint(f)
                continue
            if chunk == b'\x1f\x43\xb6\x75':
                return pos
            pos += 1
    return None

def _init_segment_size(path):
    """Scan fMP4 from start, accumulate box sizes until first 'moof' box.
    For WebM files, scan for first Cluster element.
    Returns offset of first moof/Cluster (init segment size), or None if incomplete."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".webm":
        offset = _webm_init_offset(path)
        if offset is not None and offset > 0:
            return offset
        return None
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
    if any(codec.startswith(p) for p in ("avc1", "hev1", "hvc1", "av01")):
        return f'video/mp4; codecs="{codec}"'
    if any(codec.startswith(p) for p in ("vp09", "vp9", "av1")):
        return f'video/webm; codecs="{codec}"'
    if codec.startswith("mp4a"):
        return f'audio/mp4; codecs="{codec}"'
    if codec.startswith("opus"):
        return f'audio/webm; codecs="{codec}"'
    return None


def stream_info(video_id):
    """Returns codec info for MSE initialization."""
    import downloader

    # Check merged file first (completed download)
    merged_path = get_video_path(video_id, exact_only=True)
    if merged_path and os.path.exists(merged_path):
        actives = tasks.get_active_tasks()
        still_downloading = any(t["video_id"] == video_id for t in actives.values())
        if not still_downloading:
            mime, _ = mimetypes.guess_type(merged_path)
            return {
                "is_dash": False,
                "has_video": True,
                "has_audio": True,
                "video_mime": mime or 'video/mp4',
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
                target = f"{video_id}.f{v_fmt}"
                v_path = None
                for f in os.listdir(VIDEOS_DIR):
                    name, ext = os.path.splitext(f)
                    if name == target and ext in VIDEO_EXTS:
                        v_path = os.path.join(VIDEOS_DIR, f)
                        break
                if v_path and os.path.getsize(v_path) > 0:
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
        fallback_is_dash = False
        # Check for exact-match regular file first (progressive download)
        exact = get_video_path(video_id, exact_only=True)
        if exact and os.path.exists(exact) and os.path.getsize(exact) > 0:
            fallback_video_path = exact
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
            fallback_is_dash = True
            if _is_audio_file(f):
                if fallback_audio_path is None:
                    fallback_audio_path = fpath
            elif fallback_video_path is None:
                fallback_video_path = fpath
        if fallback_is_dash:
            result["is_dash"] = True
        if fallback_video_path:
            result["has_video"] = True
            result["video_size"] = os.path.getsize(fallback_video_path)
            v_ext = os.path.splitext(fallback_video_path)[1]
            fmt_id = _parse_format_id(os.path.basename(fallback_video_path))
            mime, _ = mimetypes.guess_type(fallback_video_path)
            if fallback_is_dash and fmt_id:
                codec_map = {
                    "137": "avc1.640028", "136": "avc1.4d401f", "135": "avc1.4d401e",
                    "298": "avc1.4d4020", "299": "avc1.640028",
                    "247": "vp9", "248": "vp9", "302": "vp9", "303": "vp9",
                    "308": "vp9", "335": "vp9", "336": "vp9",
                }
                codec = codec_map.get(fmt_id)
                if codec:
                    result["video_mime"] = _build_mime(codec) or (mime or 'video/webm')
                else:
                    result["video_mime"] = mime or 'video/webm'
            else:
                if mime and mime.startswith("audio/"):
                    mime = "video/mp4"
                result["video_mime"] = mime or 'video/webm'
            result["init_video"] = _init_segment_size(fallback_video_path)
            dur = _mvhd_duration(fallback_video_path)
            result["duration"] = dur or 0
        if fallback_audio_path:
            result["has_audio"] = True
            result["audio_size"] = os.path.getsize(fallback_audio_path)
            fmt_id = _parse_format_id(os.path.basename(fallback_audio_path))
            if fallback_is_dash and fmt_id:
                codec_map = {
                    "139": "mp4a.40.2", "140": "mp4a.40.2", "141": "mp4a.40.2",
                    "249": "opus", "250": "opus", "251": "opus",
                    "255": "mp4a.40.2", "256": "mp4a.40.2", "258": "mp4a.40.2",
                }
                codec = codec_map.get(fmt_id, "mp4a.40.2")
                result["audio_mime"] = _build_mime(codec) or f'audio/mp4; codecs="{codec}"'
            else:
                result["audio_mime"] = 'audio/mp4; codecs="mp4a.40.2"'

    return result


def _mvhd_duration(path):
    """Parse moov box to extract mvhd duration in seconds.
    For WebM files, parse Info > Duration from EBML."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".webm":
        return _webm_duration(path)
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

def _webm_duration(path):
    """Parse WebM/EBML to extract Duration from Segment > Info."""
    try:
        file_size = os.path.getsize(path)
    except OSError:
        return None
    if file_size < 12:
        return None
    with open(path, 'rb') as f:
        pos = 0
        in_segment = False
        while pos + 4 <= file_size:
            f.seek(pos)
            chunk = f.read(4)
            if len(chunk) < 4:
                break
            if chunk == b'\x18\x53\x80\x67':
                pos += 4
                _, _ = _ebml_read_vint(f)
                in_segment = True
                continue
            if not in_segment:
                pos += 1
                continue
            if chunk == b'\x15\x49\xa9\x66':
                pos += 4
                info_size, _ = _ebml_read_vint(f)
                if info_size is None or info_size <= 0:
                    break
                info_end = f.tell() + info_size
                while f.tell() + 2 <= info_end:
                    child_id = f.read(1)[0]
                    if child_id == 0:
                        break
                    id_len = _ebml_vint_size(child_id)
                    if id_len == 2:
                        extra = f.read(1)
                        if len(extra) < 1:
                            break
                        child_id = (child_id << 8) | extra[0]
                    elif id_len != 1:
                        f.seek(f.tell() + id_len - 1)
                    if child_id == 0x4489:
                        val, val_len = _ebml_read_vint(f)
                        if val is None:
                            break
                        timescale = 1000000000
                        default_duration = val / timescale
                        return default_duration
                    else:
                        dsize, dlen = _ebml_read_vint(f)
                        if dsize is None:
                            break
                        f.seek(f.tell() + dsize)
                break
            pos += 1
    return None


def _last_complete_fragment_end(path, start_scan=0):
    """Scan from start_scan, find byte offset at end of last complete moof+mdat pair.
    For WebM files, scans for complete Cluster elements."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".webm":
        return _webm_last_cluster_end(path, start_scan)
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

def _webm_last_cluster_end(path, start_scan):
    """For WebM files, scan for complete Cluster elements.
    Returns byte offset after the last fully written Cluster."""
    try:
        file_size = os.path.getsize(path)
    except OSError:
        return start_scan
    last_good = start_scan
    with open(path, 'rb') as f:
        pos = start_scan
        while pos < file_size:
            f.seek(pos)
            chunk = f.read(4)
            if len(chunk) < 4:
                break
            if chunk == b'\x1f\x43\xb6\x75':
                f.seek(pos + 4)
                cluster_size, _ = _ebml_read_vint(f)
                if cluster_size is None or cluster_size == 0:
                    break
                cluster_end = pos + 4 + f.tell() - (pos + 4) + cluster_size
                if cluster_end > file_size:
                    break
                last_good = cluster_end
                pos = cluster_end
            else:
                pos += 1
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


def _find_init_video_file(video_id):
    """Find a DASH .f* video file for init segment, skipping audio files and too-small files."""
    for f in os.listdir(VIDEOS_DIR):
        name, ext = os.path.splitext(f)
        if ext not in VIDEO_EXTS or ".f" not in name or name.split(".")[0] != video_id:
            continue
        fpath = os.path.join(VIDEOS_DIR, f)
        if os.path.getsize(fpath) < MIN_VIDEO_SIZE or _is_audio_file(f):
            continue
        return fpath
    return None

def stream_init_segment(video_id):
    """Serve init segment directly from a DASH .f{fmt} video file,
    bypassing get_video_path() which may switch to merged file."""
    path = None
    actives = tasks.get_active_tasks()
    for t in actives.values():
        if t["video_id"] == video_id and t.get("dash_video_fmt"):
            fmt = t["dash_video_fmt"]
            target = f"{video_id}.f{fmt}"
            for f in os.listdir(VIDEOS_DIR):
                name, ext = os.path.splitext(f)
                if name == target and ext in VIDEO_EXTS:
                    path = os.path.join(VIDEOS_DIR, f)
                    break
            break
    if not path:
        path = _find_init_video_file(video_id)
    if not path or not os.path.exists(path):
        return Response(status=404)
    init_size = _init_segment_size(path)
    if init_size is None or init_size <= 0:
        return Response(status=204)
    with open(path, "rb") as f:
        data = f.read(init_size)
    mime = "video/webm" if path.endswith(".webm") else "video/mp4"
    return Response(data, status=200, mimetype=mime,
                    headers={"X-Init-Size": str(init_size),
                             "Content-Length": str(len(data))})


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
