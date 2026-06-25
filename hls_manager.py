import os
import json
import subprocess
import shutil
from config import VIDEOS_DIR, HLS_DIR, FFMPEG_PATH, HLS_SEGMENT_DURATION


# ─── Path helpers ──────────────────────────────────────

def video_dir(video_id):
    return os.path.join(HLS_DIR, video_id)

def meta_path(video_id):
    return os.path.join(video_dir(video_id), "meta.json")

def init_path(video_id):
    return os.path.join(video_dir(video_id), "init.mp4")

def seg_path(video_id, n, ext="m4s"):
    return os.path.join(video_dir(video_id), f"seg_{n:04d}.{ext}")

def seg_number(filename):
    stem, _ = os.path.splitext(filename)
    if stem.startswith("seg_"):
        return int(stem.split("_")[1])
    return None


# ─── Source tracking ───────────────────────────────────

def get_source_info(video_path, audio_path):
    def _info(p):
        if p and os.path.exists(p):
            return {"path": os.path.basename(p), "size": os.path.getsize(p),
                    "mtime": os.path.getmtime(p)}
        return {"path": None, "size": 0, "mtime": 0}
    return {"video": _info(video_path), "audio": _info(audio_path)}

def sources_match(meta, video_path, audio_path):
    current = get_source_info(video_path, audio_path)
    saved = meta.get("sources", {})
    for key in ("video", "audio"):
        c, s = current.get(key, {}), saved.get(key, {})
        if c["path"] != s["path"] or c["size"] != s["size"] or c["mtime"] != s["mtime"]:
            return False
    return True


# ─── Codec detection ───────────────────────────────────

def _detect_codec(path):
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=10,
        )
        codec = result.stdout.strip().lower()
        if not codec:
            return None
        return codec
    except:
        return None

def segment_type_for_codec(codec):
    """MPEG-TS for H.264/H.265, fMP4 for others (VP9, AV1, etc)."""
    if codec in ("h264", "h265", "hevc"):
        return "mpegts"
    return "fmp4"

def seg_ext(seg_type):
    return "ts" if seg_type == "mpegts" else "m4s"


# ─── Init segment ──────────────────────────────────────

def ensure_init(video_id, video_path, audio_path):
    """Generate init.mp4 once per video (only needed for fMP4). Returns True if ready."""
    init = init_path(video_id)
    if os.path.exists(init) and os.path.getsize(init) > 0:
        return True
    os.makedirs(video_dir(video_id), exist_ok=True)
    tmpdir = video_dir(video_id)
    cmd = [FFMPEG_PATH, "-y", "-i", video_path]
    if audio_path:
        cmd += ["-i", audio_path]
    cmd += [
        "-c", "copy",
        "-f", "hls",
        "-hls_segment_type", "fmp4",
        "-hls_list_size", "0",
        "-hls_time", "9999",
        "-hls_segment_filename", os.path.join(tmpdir, ".init_seg_%d.m4s"),
        os.path.join(tmpdir, ".init_pl.m3u8"),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=120)
        if not os.path.exists(init) or os.path.getsize(init) == 0:
            return False
        for name in list(os.listdir(tmpdir)):
            if name.startswith(".init_"):
                try:
                    os.remove(os.path.join(tmpdir, name))
                except (OSError, PermissionError):
                    pass
        return True
    except:
        return False


# ─── Segment generation ────────────────────────────────

def _ffprobe_duration(path):
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=10,
        )
        return float(result.stdout.strip())
    except:
        return 0.0

def _generate_all_segments(video_id, video_path, audio_path, seg_type):
    """Generate all segments in batch using FFmpeg HLS muxer.
    Produces proper A/V sync (fixes per-segment input-seeking drift).
    Returns list of (n, duration, status) tuples, or empty list on failure."""
    vdir = video_dir(video_id)
    os.makedirs(vdir, exist_ok=True)
    ext = seg_ext(seg_type)
    pattern = os.path.join(vdir, f"seg_%04d.{ext}")
    # Temp files for HLS muxer output.
    # hls_fmp4_init_filename is relative to the output playlist's directory.
    tmppl = os.path.join(vdir, ".hls_playlist.m3u8")
    tmpinit = ".hls_init.mp4"  # relative to vdir (same dir as tmppl)

    # Remove old temp files and stale segments
    for name in list(os.listdir(vdir)):
        if name.startswith(".") or name.endswith(".m3u8"):
            continue
        path = os.path.join(vdir, name)
        if name.startswith("seg_") and name.endswith(f".{ext}"):
            try:
                os.remove(path)
            except (OSError, PermissionError):
                pass

    fmt = "fmp4" if seg_type == "fmp4" else "mpegts"
    cmd = [FFMPEG_PATH, "-y"]
    cmd += ["-i", video_path]
    if audio_path:
        cmd += ["-i", audio_path]
    cmd += ["-map", "0:v"]
    if audio_path:
        cmd += ["-map", "1:a"]
    cmd += ["-c:v", "copy"]
    if audio_path:
        cmd += ["-c:a", "copy"]
    cmd += ["-shortest"]
    cmd += [
        "-f", "hls",
        "-hls_segment_type", fmt,
        "-hls_list_size", "0",
        "-hls_time", str(HLS_SEGMENT_DURATION),
        "-hls_fmp4_init_filename", tmpinit,
        "-hls_segment_filename", pattern,
        tmppl,
    ]

    try:
        subprocess.run(cmd, capture_output=True, timeout=600)
    except:
        return []

    # Move new init.mp4 into place (replaces the one from ensure_init)
    tmpinit_path = os.path.join(vdir, tmpinit) if not os.path.isabs(tmpinit) else tmpinit
    if seg_type == "fmp4" and os.path.exists(tmpinit_path) and os.path.getsize(tmpinit_path) > 0:
        dest_init = init_path(video_id)
        shutil.move(tmpinit_path, dest_init)

    # Parse segment durations from the HLS muxer's temp playlist.
    # The muxer computes EXTINF consistent with how hls.js reads
    # each segment's timeline (tfdt), so use those values directly.
    segments = []
    if os.path.exists(tmppl):
        with open(tmppl) as f:
            lines = f.readlines()
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if line.startswith("#EXTINF:"):
                dur = float(line[len("#EXTINF:"):-1])
                if i + 1 < len(lines):
                    seg_name = lines[i + 1].strip()
                    n = seg_number(seg_name)
                    if n is not None:
                        status = "final" if dur >= HLS_SEGMENT_DURATION * 0.8 else "partial"
                        segments.append((n, round(dur, 3), status))
                i += 2
            else:
                i += 1

    # Cleanup temp playlist
    if os.path.exists(tmppl):
        os.remove(tmppl)

    segments.sort()
    return segments


# ─── Meta management ───────────────────────────────────

def _load_meta(video_id):
    path = meta_path(video_id)
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except:
        return {}

def _save_meta(video_id, meta):
    os.makedirs(video_dir(video_id), exist_ok=True)
    with open(meta_path(video_id), "w") as f:
        json.dump(meta, f, indent=2)


def _sources_complete(meta, video_path=None, audio_path=None):
    """Check if source files have reached their expected sizes.
    Uses actual file sizes from disk if paths provided, else meta sources.
    Returns True if no expected sizes recorded or actual meets expected."""
    expected = meta.get("expected_sizes", {})
    for key in ("video", "audio"):
        exp = expected.get(key)
        if exp is None or exp <= 0:
            continue
        if video_path is not None and audio_path is not None:
            path = video_path if key == "video" else audio_path
            if not path or not os.path.exists(path):
                return False
            actual = os.path.getsize(path)
        else:
            actual = meta.get("sources", {}).get(key, {}).get("size", 0)
        if actual < exp * 0.9:
            return False
    return True


def initialize(video_id, video_path, audio_path):
    """Create meta.json from source files without generating segments.
    Returns True if metadata is ready (newly created or already existed)."""
    meta = _load_meta(video_id)
    if meta and sources_match(meta, video_path, audio_path):
        return True
    invalidate(video_id)
    codec = _detect_codec(video_path)
    seg_type = segment_type_for_codec(codec)
    import tasks
    expected_video_size = None
    expected_audio_size = None
    for t in tasks.get_active_tasks().values():
        if t["video_id"] == video_id:
            expected_video_size = t.get("expected_video_size")
            expected_audio_size = t.get("expected_audio_size")
            break
    meta = {
        "seg_type": seg_type,
        "codec": codec,
        "sources": get_source_info(video_path, audio_path),
        "expected_sizes": {"video": expected_video_size, "audio": expected_audio_size},
        "segments": {},
    }
    _save_meta(video_id, meta)
    return True


# ─── Public API ────────────────────────────────────────

def get_or_create_segment(video_id, video_path, audio_path, n):
    """Get cached segment or trigger batch generation.

    Returns (path_or_None, duration_seconds, seg_type_or_None).
    """
    meta = _load_meta(video_id)
    sources_ok = meta and sources_match(meta, video_path, audio_path)

    # Preserve expected_sizes across cache invalidations
    prev_expected = meta.get("expected_sizes") if meta else None

    # Fast path: segment already cached and sources unchanged
    if sources_ok:
        seg_type = meta["seg_type"]
        ext = seg_ext(seg_type)
        dest = seg_path(video_id, n, ext)
        seg_key = f"seg_{n:04d}"
        seg_info = meta.get("segments", {}).get(seg_key, {})
        if os.path.exists(dest) and seg_info.get("status") in ("final", "partial"):
            return dest, seg_info["duration"], seg_type
        # If no segments exist yet, batch generation hasn't run — proceed below
        if meta.get("segments"):
            return None, 0, None

    # Sources changed, no meta, or batch not yet run — generate all segments
    if meta and not sources_ok:
        invalidate(video_id)
        meta = {}

    codec = _detect_codec(video_path)
    seg_type = segment_type_for_codec(codec)

    new_segs = _generate_all_segments(video_id, video_path, audio_path, seg_type)
    if not new_segs:
        return None, 0, None

    seg_map = {}
    for sn, sdur, sst in new_segs:
        seg_map[f"seg_{sn:04d}"] = {"duration": sdur, "status": sst}

    meta = {
        "seg_type": seg_type,
        "codec": codec,
        "sources": get_source_info(video_path, audio_path),
        "expected_sizes": prev_expected or {"video": None, "audio": None},
        "segments": seg_map,
    }
    _save_meta(video_id, meta)

    ext = seg_ext(seg_type)
    dest = seg_path(video_id, n, ext)
    seg_key = f"seg_{n:04d}"
    if os.path.exists(dest):
        info = seg_map.get(seg_key, {})
        return dest, info.get("duration", 0), seg_type

    return None, 0, None


def segment_count(video_id):
    """Return number of non-empty segments known."""
    meta = _load_meta(video_id)
    count = 0
    for key, info in meta.get("segments", {}).items():
        if key.startswith("seg_") and info.get("status") not in ("empty",):
            count += 1
    return count


def build_playlist(video_id, include_endlist=True):
    """Build m3u8 string from known segments. Returns string or None."""
    meta = _load_meta(video_id)
    if not meta:
        return None
    seg_type = meta.get("seg_type", "fmp4")
    ext = seg_ext(seg_type)

    segments = []
    for key, info in meta.get("segments", {}).items():
        if key.startswith("seg_"):
            n = int(key.split("_")[1])
            segments.append((n, info.get("duration", 0), info.get("status", "final")))
    segments.sort()

    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:7",
        f"#EXT-X-TARGETDURATION:{HLS_SEGMENT_DURATION}",
        "#EXT-X-MEDIA-SEQUENCE:0",
    ]
    if seg_type == "fmp4":
        lines.append('#EXT-X-MAP:URI="init.mp4"')

    for n, dur, status in segments:
        if status == "empty":
            continue
        lines.append(f"#EXTINF:{dur:.3f},")
        lines.append(f"seg_{n:04d}.{ext}")

    if include_endlist:
        lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def is_playlist_complete(video_id):
    """Check if ENDLIST should be included (no active downloads, sources complete)."""
    import tasks
    if any(t["video_id"] == video_id
           for t in tasks.get_active_tasks().values()):
        return False
    meta = _load_meta(video_id)
    if not meta:
        return True
    return _sources_complete(meta)


# ─── Cache lifecycle ───────────────────────────────────

def invalidate(video_id):
    """Clear segments + meta when source files change. Keep init.mp4."""
    vdir = video_dir(video_id)
    if not os.path.isdir(vdir):
        return
    for name in os.listdir(vdir):
        if name.startswith("seg_") or name in ("meta.json", "master.m3u8"):
            try:
                os.remove(os.path.join(vdir, name))
            except (OSError, PermissionError):
                pass


def clear(video_id):
    """Delete entire HLS cache for a video."""
    vdir = video_dir(video_id)
    if os.path.isdir(vdir):
        shutil.rmtree(vdir, ignore_errors=True)
