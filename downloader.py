import os
import json
import re
import time
import threading
import shutil
import sys
import subprocess
import yt_dlp
from config import VIDEOS_DIR, SUBTITLES_DIR, THUMBNAILS_DIR, DEFAULT_SUBTITLE_LANG, FFMPEG_THREADS, FFMPEG_LOW_PRIORITY
import tasks

FFMPEG_PATH = shutil.which("ffmpeg") or "ffmpeg"

MIN_VIDEO_SIZE = 102400


def _run_ffmpeg(cmd, timeout=None, check=False):
    """Run ffmpeg with thread limit and low priority (Windows)."""
    insert = []
    if FFMPEG_THREADS:
        insert += ["-threads", str(FFMPEG_THREADS)]
    if sys.platform == "win32":
        insert += ["-fflags", "+nobuffer", "-flags", "+low_delay"]
    full_cmd = cmd[:1] + insert + cmd[1:] if insert else cmd

    if FFMPEG_LOW_PRIORITY and sys.platform == "win32":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        BELOW_NORMAL_PRIORITY_CLASS = 0x4000
        proc = subprocess.Popen(
            full_cmd,
            creationflags=subprocess.CREATE_SUSPENDED,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        kernel32.SetPriorityClass(proc._handle, BELOW_NORMAL_PRIORITY_CLASS)
        kernel32.ResumeThread(proc._handle)
        stdout, stderr = proc.communicate(timeout=timeout)
        ret = subprocess.CompletedProcess(full_cmd, proc.returncode, stdout, stderr)
    else:
        ret = subprocess.run(full_cmd, capture_output=True, timeout=timeout)

    if check and ret.returncode != 0:
        raise subprocess.CalledProcessError(ret.returncode, full_cmd, ret.stdout, ret.stderr)
    return ret

progress_listeners = {}
cancel_flags = {}
pause_flags = {}
subtitle_listeners = {}
_run_tokens = {}
_run_lock = threading.Lock()

class PauseException(Exception):
    pass


def _register_run(task_id):
    if not task_id:
        return None
    token = object()
    with _run_lock:
        _run_tokens[task_id] = token
    pause_flags.pop(task_id, None)
    cancel_flags.pop(task_id, None)
    return token


def _is_current_run(task_id, token):
    if not task_id:
        return True
    with _run_lock:
        return _run_tokens.get(task_id) is token


def _clear_run(task_id, token):
    if not task_id:
        return
    with _run_lock:
        if _run_tokens.get(task_id) is token:
            _run_tokens.pop(task_id, None)


def hdr_label(format_note):
    if not format_note:
        return "SDR"
    note = format_note.lower()
    if "dolby vision" in note:
        return "Dolby Vision"
    if "hdr10" in note:
        return "HDR10"
    if "hdr" in note:
        return "HDR"
    return "SDR"

def search_youtube(query, max_results=20):
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "force_generic_extractor": False,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(f"ytsearch{max_results}:{query}", download=False)
            if "entries" not in info:
                return []
            results = []
            for entry in info["entries"]:
                if not entry:
                    continue
                results.append({
                    "id": entry.get("id"),
                    "title": entry.get("title"),
                    "url": f"https://youtube.com/watch?v={entry.get('id')}",
                    "duration": entry.get("duration"),
                    "thumbnail": entry.get("thumbnail", f"https://i.ytimg.com/vi/{entry.get('id')}/hqdefault.jpg"),
                    "channel": entry.get("channel") or entry.get("uploader"),
                    "view_count": entry.get("view_count"),
                })
            return results
        except Exception as e:
            raise Exception(f"Search failed: {e}")

def list_formats(video_id):
    url = f"https://youtube.com/watch?v={video_id}"
    ydl_opts = {"quiet": True, "no_warnings": True}
    formats = []
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
            for f in info.get("formats", []):
                if f.get("vcodec") == "none":
                    continue
                height = f.get("height") or 0
                format_note = f.get("format_note") or ""
                formats.append({
                    "format_id": f["format_id"],
                    "ext": f.get("ext"),
                    "height": height,
                    "fps": f.get("fps"),
                    "vcodec": f.get("vcodec"),
                    "acodec": f.get("acodec"),
                    "filesize": f.get("filesize"),
                    "hdr": hdr_label(format_note),
                    "format_note": format_note,
                    "progressive": f.get("acodec") != "none",
                })
            formats.sort(key=lambda x: (x["height"], x.get("fps") or 0), reverse=True)
            return formats
        except Exception as e:
            raise Exception(f"Failed to list formats: {e}")

# Extended language list for subtitle download — covers most manually-subtitled languages.
# yt-dlp silently skips any language not available for a given video.
SUBTITLE_LANGS = [
    "en", "en-GB", "en-orig", "es", "es-419", "fr", "fr-CA", "de",
    "it", "pt", "pt-BR", "pt-PT", "ru", "ja", "ko", "zh-Hans", "zh-Hant",
    "ar", "hi", "nl", "pl", "sv", "da", "fi", "nb", "tr",
    "cs", "ro", "hu", "el", "he", "th", "vi", "uk", "id", "ms",
    "ca", "eu", "gl", "sr", "hr", "sk", "sl", "lt", "lv", "et",
    "bg", "bn", "ta", "te", "ml", "mr", "gu",
]

def get_video_info(video_id):
    url = f"https://youtube.com/watch?v={video_id}"
    ydl_opts = {"quiet": True, "no_warnings": True, "cookiesfrombrowser": ("firefox",)}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
            return {
                "id": video_id,
                "title": info.get("title", ""),
                "thumbnail": info.get("thumbnail", f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"),
                "channel": info.get("channel") or info.get("uploader", ""),
                "duration": info.get("duration", 0),
            }
        except Exception as e:
            raise Exception(f"Failed to get video info: {e}")

def list_subtitles(video_id):
    url = f"https://youtube.com/watch?v={video_id}"
    ydl_opts = {"quiet": True, "no_warnings": True, "writesubtitles": True, "writeautomaticsub": True, "cookiesfrombrowser": ("firefox",)}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
            subs = info.get("subtitles", {})
            auto_subs = info.get("automatic_captions", {})
            available = {}
            for lang in subs:
                available[lang] = "manual"
            for lang in auto_subs:
                if lang not in available:
                    available[lang] = "auto"
            return available
        except:
            return {}

VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".m4v", ".mov", ".avi", ".m4a"}
SUBTITLE_EXTS = {".vtt", ".srt", ".ass"}
THUMB_EXTS = {".jpg", ".png", ".webp"}

def _find_video_file(video_id):
    for f in os.listdir(VIDEOS_DIR):
        name, ext = os.path.splitext(f)
        if ext in VIDEO_EXTS and name == video_id:
            return os.path.join(VIDEOS_DIR, f)
    for f in os.listdir(VIDEOS_DIR):
        name, ext = os.path.splitext(f)
        if ext in VIDEO_EXTS and ".f" in name and name.split(".")[0] == video_id:
            return os.path.join(VIDEOS_DIR, f)
    return None

def _find_merged_file(video_id):
    for f in os.listdir(VIDEOS_DIR):
        name, ext = os.path.splitext(f)
        if ext in VIDEO_EXTS and name == video_id:
            return os.path.join(VIDEOS_DIR, f)
    return None


def merge_dash_to_mkv(video_id):
    """Merge DASH video + audio + subtitles into a single MKV. No re-encode."""
    from streamer import _get_dash_paths, list_subtitles_on_disk
    v_path, a_path = _get_dash_paths(video_id)
    if not v_path:
        return None
    output = os.path.join(VIDEOS_DIR, f"{video_id}.mkv")
    if os.path.exists(output):
        return output
    cmd = [FFMPEG_PATH, "-y", "-i", v_path]
    if a_path:
        cmd += ["-i", a_path]
    subs = list_subtitles_on_disk(video_id)
    sub_inputs = []
    for s in subs:
        sf = os.path.join(SUBTITLES_DIR, video_id, s["file"])
        if os.path.exists(sf):
            cmd += ["-i", sf]
            sub_inputs.append(s)
    cmd += ["-map", "0:v:0"]
    if a_path:
        cmd += ["-map", "1:a:0"]
    sub_base = 2 if a_path else 1
    for i in range(len(sub_inputs)):
        cmd += ["-map", str(sub_base + i)]
    cmd += ["-c:v", "copy", "-c:a", "copy"]
    if sub_inputs:
        cmd += ["-c:s", "webvtt"]
    cmd += ["-movflags", "+faststart", output]
    try:
        r = _run_ffmpeg(cmd, timeout=300)
        if r.returncode != 0:
            raise Exception(r.stderr[:200])
        if os.path.exists(output) and os.path.getsize(output) >= 102400:
            return output
    except Exception:
        if os.path.exists(output):
            try: os.remove(output)
            except: pass
    return None


def _find_partial_dash(video_id):
    for f in os.listdir(VIDEOS_DIR):
        name, ext = os.path.splitext(f)
        if ".f" not in name:
            continue
        if ext not in VIDEO_EXTS:
            continue
        parts = name.split(".")
        if parts[0] == video_id:
            path = os.path.join(VIDEOS_DIR, f)
            if os.path.getsize(path) > 0:
                return path
    return None

def _find_dash_video(video_id, video_format_id):
    if not video_format_id:
        return None
    target = f"{video_id}.f{video_format_id}"
    for f in os.listdir(VIDEOS_DIR):
        name, ext = os.path.splitext(f)
        if name == target and ext in VIDEO_EXTS:
            return os.path.join(VIDEOS_DIR, f)
    return None

def _find_dash_audio(video_id, audio_format_id):
    if not audio_format_id:
        return None
    target = f"{video_id}.f{audio_format_id}"
    for f in os.listdir(VIDEOS_DIR):
        name, ext = os.path.splitext(f)
        if name == target and ext in VIDEO_EXTS:
            return os.path.join(VIDEOS_DIR, f)
    return None

def partial_file_exists(video_id):
    for f in os.listdir(VIDEOS_DIR):
        name, ext = os.path.splitext(f)
        if ext not in VIDEO_EXTS:
            continue
        if name == video_id or (".f" in name and name.split(".")[0] == video_id):
            path = os.path.join(VIDEOS_DIR, f)
            if os.path.getsize(path) > 0:
                return True
    return False

def _find_thumbnail_file(video_id):
    for f in os.listdir(THUMBNAILS_DIR):
        name, ext = os.path.splitext(f)
        if name == video_id and ext in THUMB_EXTS:
            return os.path.join(THUMBNAILS_DIR, f)
    for f in os.listdir(VIDEOS_DIR):
        name, ext = os.path.splitext(f)
        if ext in THUMB_EXTS:
            if name == video_id:
                src = os.path.join(VIDEOS_DIR, f)
                dst = os.path.join(THUMBNAILS_DIR, f"{video_id}{ext}")
                try:
                    os.rename(src, dst)
                except:
                    pass
                return dst
            if ".f" in name and name.split(".")[0] == video_id:
                return os.path.join(VIDEOS_DIR, f)
    return None

def _find_subtitle_files(video_id):
    organized = _find_organized_subtitles(video_id)
    if organized:
        return [e["file"] for e in organized]
    found = []
    for f in os.listdir(SUBTITLES_DIR):
        name, ext = os.path.splitext(f)
        if name.startswith(video_id) and ext in SUBTITLE_EXTS:
            found.append(f)
    if not found:
        for f in os.listdir(VIDEOS_DIR):
            name, ext = os.path.splitext(f)
            if name.startswith(video_id) and ext in SUBTITLE_EXTS:
                src = os.path.join(VIDEOS_DIR, f)
                dst = os.path.join(SUBTITLES_DIR, f)
                try:
                    os.rename(src, dst)
                    found.append(f)
                except:
                    pass
    return found


def _organize_flat_subtitles(video_id):
    """Move flat {video_id}.{lang}.{ext} files into cache/subtitles/{video_id}/{lang}.{ext}.
    Returns list of dicts [{"lang": "en", "file": "en.vtt"}, ...]"""
    target_dir = os.path.join(SUBTITLES_DIR, video_id)
    os.makedirs(target_dir, exist_ok=True)
    moved = []
    for f in os.listdir(SUBTITLES_DIR):
        full = os.path.join(SUBTITLES_DIR, f)
        if not os.path.isfile(full):
            continue
        name, ext = os.path.splitext(f)
        # Match flat pattern: {video_id}.{lang}.{ext}
        prefix = video_id + "."
        if not name.startswith(prefix):
            continue
        lang = name[len(prefix):]
        if not lang or ext not in SUBTITLE_EXTS:
            continue
        dst = os.path.join(target_dir, f"{lang}{ext}")
        if not os.path.exists(dst):
            os.rename(full, dst)
        moved.append({"lang": lang, "file": f"{lang}{ext}"})
    return moved


def download_all_subtitles(video_id, url, task_id=None, langs=None):
    """Download subtitles into organized cache/subtitles/{id}/{lang}.vtt.
    Uses a single yt-dlp call — no pre-listing — to minimize API requests.
    If langs is provided (list of codes), only those are requested.
    If langs is None, the full SUBTITLE_LANGS list is used.
    Returns list of dicts [{"lang": "en", "file": "en.vtt"}, ...]"""
    target = langs if langs else SUBTITLE_LANGS
    sub_opts = {
        "quiet": True,
        "no_warnings": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitlesformat": "vtt",
        "subtitleslangs": target,
        "skip_download": True,
        "outtmpl": os.path.join(SUBTITLES_DIR, "%(id)s.%(ext)s"),
        "cookiesfrombrowser": ("firefox",),
    }
    try:
        with yt_dlp.YoutubeDL(sub_opts) as ydl:
            ydl.download([url])
    except Exception:
        pass
    _organize_flat_subtitles(video_id)
    return _find_organized_subtitles(video_id)


def _find_organized_subtitles(video_id):
    """Scan cache/subtitles/{video_id}/ for subtitle files.
    Returns list of dicts [{"lang": "en", "file": "en.vtt"}, ...]"""
    video_sub_dir = os.path.join(SUBTITLES_DIR, video_id)
    if not os.path.isdir(video_sub_dir):
        return []
    found = []
    for f in sorted(os.listdir(video_sub_dir)):
        name, ext = os.path.splitext(f)
        if ext in SUBTITLE_EXTS:
            found.append({"lang": name, "file": f})
    return found


def pause_download(task_id):
    """Signal a download to pause at the next progress hook."""
    pause_flags[task_id] = True


def resume_download(task_id, video_id, format_id):
    """Re-launch a paused download. Returns the new thread."""
    pause_flags.pop(task_id, None)
    cancel_flags.pop(task_id, None)
    return download_video(video_id, format_id, task_id, resume=True)


def download_video(video_id, format_id, task_id=None, resume=False):
    url = f"https://youtube.com/watch?v={video_id}"
    outtmpl = os.path.join(VIDEOS_DIR, "%(id)s.%(ext)s")
    run_token = _register_run(task_id)

    dash_video_fmt = None
    dash_audio_fmt = None

    def progress_hook(d):
        if not _is_current_run(task_id, run_token):
            raise PauseException()
        if cancel_flags.get(task_id):
            raise PauseException()
        if pause_flags.get(task_id):
            raise PauseException()
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes") or 0
            pct = (downloaded / total * 100) if total > 0 else 0
            speed = d.get("speed") or 0
            extra = {}
            if dash_video_fmt:
                extra["dash_video_fmt"] = dash_video_fmt
            if dash_audio_fmt:
                extra["dash_audio_fmt"] = dash_audio_fmt
            if task_id and task_id in progress_listeners:
                progress_listeners[task_id]({
                    "status": "downloading",
                    "percent": round(pct, 1),
                    "speed": speed,
                    "eta": d.get("eta"),
                    **extra,
                })
        elif d["status"] == "finished":
            if task_id and task_id in progress_listeners:
                progress_listeners[task_id]({"status": "processing", "percent": 100})

    ydl_opts = {
        "format": format_id,
        "outtmpl": outtmpl,
        "merge_output_format": "mp4",
        "writethumbnail": True,
        "writesubtitles": False,
        "writeautomaticsub": False,
        "ignoreerrors": True,
        "progress_hooks": [progress_hook],
        "quiet": True,
        "no_warnings": True,
        "ffmpeg_location": FFMPEG_PATH,
        "nopart": True,
        "postprocessor_args": {"ffmpeg": ["-movflags", "+faststart"]},
        "cookiesfrombrowser": ("firefox",),
    }

    def run():
        nonlocal dash_video_fmt, dash_audio_fmt
        if not _is_current_run(task_id, run_token):
            return
        if cancel_flags.get(task_id):
            return

        import metadata as meta_module

        has_partials = False

        if not resume:
            # Remove any existing merged file from a prior partial download
            # that would falsely trigger the "already done" skip below
            for f in os.listdir(VIDEOS_DIR):
                name, ext = os.path.splitext(f)
                if ext in VIDEO_EXTS and name == video_id and ".f" not in name:
                    try: os.remove(os.path.join(VIDEOS_DIR, f))
                    except: pass

            old_meta = meta_module.get_video(video_id)

            # Skip re-download if same format already has a valid merged file or DASH files
            if old_meta and old_meta.get("format_id") == format_id:
                merged = None
                for f in os.listdir(VIDEOS_DIR):
                    name, ext = os.path.splitext(f)
                    if name == video_id and ext in VIDEO_EXTS:
                        fp = os.path.join(VIDEOS_DIR, f)
                        if os.path.getsize(fp) >= 102400:
                            merged = fp
                            break
                if merged:
                    subtitle_files = _find_subtitle_files(video_id)
                    thumbnail_path = _find_thumbnail_file(video_id)
                    if task_id and task_id in progress_listeners:
                        progress_listeners[task_id]({"status": "done",
                            "video_path": merged, "subtitles": subtitle_files,
                            "thumbnail": thumbnail_path, "info": old_meta})
                    return
                dash_found = False
                for f in os.listdir(VIDEOS_DIR):
                    name, ext = os.path.splitext(f)
                    if ext not in VIDEO_EXTS:
                        continue
                    if ".f" in name and name.split(".")[0] == video_id:
                        dash_found = True
                        break
                if dash_found:
                    # Remove any small/broken merged file that might interfere
                    for f in os.listdir(VIDEOS_DIR):
                        name, ext = os.path.splitext(f)
                        if name == video_id and ext in VIDEO_EXTS:
                            fp = os.path.join(VIDEOS_DIR, f)
                            if os.path.getsize(fp) < MIN_VIDEO_SIZE:
                                try: os.remove(fp)
                                except: pass
                    has_partials = True

            # Check if partial files exist from a failed download of the same format
            # If an exact-name merged file exists, always clean slate (user is explicitly re-downloading)
            has_merged = False
            for f in os.listdir(VIDEOS_DIR):
                name, ext = os.path.splitext(f)
                if ext in VIDEO_EXTS and name == video_id:
                    has_merged = True
                    break

            if not has_merged:
                # Only preserve DASH fragments whose format_id matches the new download
                for f in os.listdir(VIDEOS_DIR):
                    name, ext = os.path.splitext(f)
                    if ext not in VIDEO_EXTS:
                        continue
                    if ".f" in name and name.split(".")[0] == video_id:
                        parts = name.split(".f")
                        if len(parts) >= 2 and parts[-1] == format_id:
                            has_partials = True
                            break

        if not resume and not has_partials:
            # Clean slate for fresh download: emit starting, delete old files, move thumbnail, remove metadata
            if task_id and task_id in progress_listeners:
                progress_listeners[task_id]({"status": "starting"})
            for f in os.listdir(VIDEOS_DIR):
                name, ext = os.path.splitext(f)
                if ext not in VIDEO_EXTS:
                    continue
                if name == video_id or (".f" in name and name.split(".")[0] == video_id):
                    try: os.remove(os.path.join(VIDEOS_DIR, f))
                    except: pass
            _find_thumbnail_file(video_id)  # moves thumbnail to THUMBNAILS_DIR
            meta_module.remove_video(video_id)

        try:
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl_pre:
                info_pre = ydl_pre.extract_info(url, download=False)
                for f in info_pre.get("formats", []):
                    if f.get("format_id") == format_id:
                        if f.get("acodec") == "none":
                            dash_video_fmt = format_id
                            best_br = 0
                            for af in info_pre.get("formats", []):
                                if af.get("vcodec") == "none" and af.get("acodec") not in (None, "none"):
                                    br = af.get("abr") or 0
                                    if br > best_br:
                                        best_br = br
                                        dash_audio_fmt = af["format_id"]
                        break

            # Extract codecs from the matched formats
            video_fmt_info = None
            audio_fmt_info = None
            video_codec = None
            audio_codec = None
            for f in info_pre.get("formats", []):
                if f.get("format_id") == format_id:
                    video_fmt_info = f
                    video_codec = f.get("vcodec")
                if dash_audio_fmt and f.get("format_id") == dash_audio_fmt:
                    audio_fmt_info = f
                    audio_codec = f.get("acodec")
            expected_video_size = None
            expected_audio_size = None
            if video_fmt_info:
                expected_video_size = video_fmt_info.get("filesize") or video_fmt_info.get("filesize_approx")
            if audio_fmt_info:
                expected_audio_size = audio_fmt_info.get("filesize") or audio_fmt_info.get("filesize_approx")
            if video_codec or audio_codec:
                tasks.update_task(task_id,
                    dash_video_codec=video_codec,
                    dash_audio_codec=audio_codec,
                    expected_video_size=expected_video_size,
                    expected_audio_size=expected_audio_size)

            if dash_video_fmt and dash_audio_fmt:
                # Parallel DASH download
                dash_outtmpl = os.path.join(VIDEOS_DIR, "%(id)s.f%(format_id)s.%(ext)s")
                shared = {
                    "v_done": 0, "a_done": 0, "v_total": None, "a_total": None,
                    "v_finished": False, "a_finished": False,
                }

                def dash_progress(track):
                    def hook(d):
                        if not _is_current_run(task_id, run_token):
                            raise PauseException()
                        if cancel_flags.get(task_id):
                            raise PauseException()
                        if pause_flags.get(task_id):
                            raise PauseException()
                        if d["status"] == "downloading":
                            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                            downloaded = d.get("downloaded_bytes") or 0
                            if track == "v":
                                shared["v_done"] = downloaded
                                shared["v_total"] = total
                            else:
                                shared["a_done"] = downloaded
                                shared["a_total"] = total
                            v_t = shared["v_total"]
                            a_t = shared["a_total"]
                            if v_t and a_t and v_t > 0 and a_t > 0:
                                combined = shared["v_done"] + shared["a_done"]
                                pct = round(combined / (v_t + a_t) * 100, 1)
                            else:
                                v_p = (shared["v_done"] / v_t * 50) if v_t and v_t > 0 else 0
                                a_p = (shared["a_done"] / a_t * 50) if a_t and a_t > 0 else 0
                                pct = round(v_p + a_p, 1)
                            if task_id and task_id in progress_listeners:
                                progress_listeners[task_id]({
                                    "status": "downloading", "percent": pct,
                                    "speed": d.get("speed"), "eta": d.get("eta"),
                                    "dash_video_fmt": dash_video_fmt, "dash_audio_fmt": dash_audio_fmt,
                                })
                        elif d["status"] == "finished":
                            if track == "v":
                                shared["v_finished"] = True
                            else:
                                shared["a_finished"] = True
                    return hook

                v_opts = {**ydl_opts, "format": dash_video_fmt, "outtmpl": dash_outtmpl,
                          "progress_hooks": [dash_progress("v")],
                          "postprocessor_args": {}, "writesubtitles": False, "writeautomaticsub": False}
                a_opts = {**ydl_opts, "format": dash_audio_fmt, "outtmpl": dash_outtmpl,
                          "progress_hooks": [dash_progress("a")],
                          "postprocessor_args": {}, "writesubtitles": False, "writeautomaticsub": False}

                paused_dash = []
                dash_errors = []

                def dash_worker(opts):
                    try:
                        yt_dlp.YoutubeDL(opts).extract_info(url, download=True)
                    except PauseException:
                        paused_dash.append(True)
                    except Exception as e:
                        if pause_flags.get(task_id):
                            paused_dash.append(True)
                        else:
                            dash_errors.append(e)

                v_thread = threading.Thread(target=dash_worker, args=(v_opts,))
                a_thread = threading.Thread(target=dash_worker, args=(a_opts,))
                v_thread.start()
                a_thread.start()
                v_thread.join()
                a_thread.join()
                if cancel_flags.get(task_id):
                    return
                if paused_dash:
                    raise PauseException()
                if dash_errors:
                    raise Exception(f"DASH download error: {dash_errors[0]}")
                if pause_flags.get(task_id):
                    raise PauseException()

                if task_id and task_id in progress_listeners:
                    progress_listeners[task_id]({"status": "processing", "percent": 100})

                v_path = _find_dash_video(video_id, dash_video_fmt)

                if not _is_current_run(task_id, run_token) or pause_flags.get(task_id):
                    raise PauseException()

                download_all_subtitles(video_id, url, langs=[DEFAULT_SUBTITLE_LANG])

                actual_id = info_pre["id"]
                subtitle_files = _find_subtitle_files(actual_id)
                thumbnail_path = _find_thumbnail_file(actual_id)

                if task_id and task_id in progress_listeners:
                    progress_listeners[task_id]({
                        "status": "done",
                        "video_path": v_path,
                        "subtitles": subtitle_files,
                        "thumbnail": thumbnail_path,
                        "info": {
                            "id": actual_id,
                            "title": info_pre.get("title"),
                            "duration": info_pre.get("duration"),
                            "channel": info_pre.get("channel") or info_pre.get("uploader"),
                            "description": info_pre.get("description", "")[:500],
                            "thumbnail": thumbnail_path,
                            "dash_video_fmt": dash_video_fmt,
                            "dash_audio_fmt": dash_audio_fmt,
                            "upload_date": info_pre.get("upload_date"),
                        },
                    })
            else:
                # Single (progressive) download
                prog_opts = {**ydl_opts, "format": format_id,
                             "writesubtitles": False, "writeautomaticsub": False}
                with yt_dlp.YoutubeDL(prog_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    if cancel_flags.get(task_id):
                        return
                    if pause_flags.get(task_id):
                        raise PauseException()

                video_path = _find_video_file(video_id)
                if not video_path:
                    time.sleep(0.5)
                    video_path = _find_video_file(video_id)
                    if not video_path:
                        dir_files = os.listdir(VIDEOS_DIR)
                        raise Exception(
                            f"Download completed but video file not found. "
                            f"Files in {VIDEOS_DIR}: {dir_files}"
                        )

                download_all_subtitles(video_id, url, langs=[DEFAULT_SUBTITLE_LANG])
                subtitle_files = _find_subtitle_files(video_id)
                thumbnail_path = _find_thumbnail_file(video_id)

                if cancel_flags.get(task_id):
                    return

                if task_id and task_id in progress_listeners:
                    progress_listeners[task_id]({
                        "status": "done",
                        "video_path": video_path,
                        "subtitles": subtitle_files,
                        "thumbnail": thumbnail_path,
                        "info": {
                            "id": video_id,
                            "title": info.get("title"),
                            "duration": info.get("duration"),
                            "channel": info.get("channel") or info.get("uploader"),
                            "description": info.get("description", "")[:500],
                            "thumbnail": thumbnail_path,
                            "upload_date": info.get("upload_date"),
                        },
                    })
        except PauseException:
            if _is_current_run(task_id, run_token) and task_id and task_id in progress_listeners:
                progress_listeners[task_id]({"status": "paused"})
            return
        except Exception as e:
            if not _is_current_run(task_id, run_token):
                return
            if cancel_flags.get(task_id):
                return
            info_fallback = locals().get("info") or locals().get("info_pre") or {}
            video_path = _find_merged_file(video_id)
            if video_path:
                subtitle_files = _find_subtitle_files(video_id)
                thumbnail_path = _find_thumbnail_file(video_id)
                if task_id and task_id in progress_listeners:
                    progress_listeners[task_id]({
                        "status": "done",
                        "video_path": video_path,
                        "subtitles": subtitle_files,
                        "thumbnail": thumbnail_path,
                        "info": {
                            "id": video_id,
                            "title": info_fallback.get("title", video_id),
                            "duration": info_fallback.get("duration", 0),
                            "channel": info_fallback.get("channel") or info_fallback.get("uploader", "Unknown"),
                            "description": "",
                            "thumbnail": thumbnail_path,
                            "upload_date": info_fallback.get("upload_date"),
                        },
                    })
            else:
                last_pct = tasks.get_task(task_id).get("percent", 0) if tasks.get_task(task_id) else 0
                err_msg = f"Download failed at {last_pct}%: {e}" if last_pct else str(e)
                if task_id and task_id in progress_listeners:
                    progress_listeners[task_id]({"status": "error", "error": err_msg})
        finally:
            _clear_run(task_id, run_token)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread
