import os
import json
import threading
import time
import shutil
import xml.etree.ElementTree as ET
import mimetypes
from email import utils
from urllib.parse import quote
from flask import Flask, render_template, request, jsonify, Response, send_file, abort, redirect

from config import (
    VIDEOS_DIR, SUBTITLES_DIR, THUMBNAILS_DIR, PORT, DLNA_PORT, HOST, SERVER_NAME
)

os.makedirs(VIDEOS_DIR, exist_ok=True)
os.makedirs(SUBTITLES_DIR, exist_ok=True)
os.makedirs(THUMBNAILS_DIR, exist_ok=True)

import metadata
import downloader
import tasks


def _migrate_flat_subtitles():
    """Move flat {video_id}_{lang}.{ext} files into organized cache/subtitles/{video_id}/{lang}.{ext}."""
    import re
    for f in os.listdir(SUBTITLES_DIR):
        full = os.path.join(SUBTITLES_DIR, f)
        if not os.path.isfile(full):
            continue
        name, ext = os.path.splitext(f)
        if ext not in (".vtt", ".srt", ".ass"):
            continue
        # Match {video_id}_{lang} pattern (last underscore separates lang)
        m = re.match(r"^(.+?)_([a-z]{2}(?:-[A-Za-z]+)?)$", name)
        if not m:
            continue
        vid, lang = m.group(1), m.group(2)
        target_dir = os.path.join(SUBTITLES_DIR, vid)
        os.makedirs(target_dir, exist_ok=True)
        dst = os.path.join(target_dir, f"{lang}{ext}")
        if not os.path.exists(dst):
            os.rename(full, dst)


_migrate_flat_subtitles()
from streamer import (
    stream_video, stream_audio_track, get_subtitle, get_thumbnail,
    hls_master_playlist, hls_serve_file,
    stream_status, get_video_path, list_subtitles_on_disk,
    hls_simple_master_playlist,
)
import hls_manager


def _clean_webvtt(content):
    """Strip complex WebVTT formatting (cue span tags, embedded timestamps, positioning)
    that Kodi's HLS player may not support."""
    import re
    content = re.sub(r'</?c[^>]*>', '', content)
    content = re.sub(r'<\d{2}:\d{2}:\d{2}\.\d{3}>', '', content)
    content = re.sub(r' align:start position:\d+%', '', content)
    return content


app = Flask(__name__)
dlna_server = None

tasks.mark_interrupted()
tasks.cleanup_orphaned()


def _start_next_queued():
    """Start the next queued download, if any."""
    if tasks.has_active_download():
        return
    queue = tasks.get_queue()
    while queue:
        next_tid = queue[0]
        t = tasks.get_task(next_tid)
        if not t:
            tasks.dequeue_task(next_tid)
            queue = tasks.get_queue()
            continue
        tasks.dequeue_task(next_tid)
        vid = t["video_id"]
        fmt = t["format_id"]
        title = t.get("title", vid)
        thumb = t.get("thumbnail", "")
        fmt_label = t.get("format_label", fmt)
        listener = _make_listener(next_tid, vid, title, thumb, fmt_label, fmt)
        downloader.progress_listeners[next_tid] = listener
        downloader.download_video(vid, fmt, next_tid)
        break


def _make_listener(task_id, video_id, title, thumbnail, format_label, format_id):
    """Build a progress listener closure for a download task."""
    def listener(evt):
        status = evt.get("status")
        if status == "starting":
            metadata.add_video({
                "id": video_id, "title": title, "duration": 0, "channel": "",
                "description": "", "thumbnail": thumbnail if thumbnail.startswith("/") else thumbnail,
                "subtitles": [], "format_label": format_label, "format_id": format_id,
            })
            tasks.update_task(task_id, status="starting")
        elif status == "downloading":
            dash_video = evt.get("dash_video_fmt")
            dash_audio = evt.get("dash_audio_fmt")
            tasks.update_task(task_id, status="downloading", percent=evt.get("percent"),
                              speed=evt.get("speed"), eta=evt.get("eta"),
                              dash_video_fmt=dash_video, dash_audio_fmt=dash_audio)
            metadata.add_video({
                "id": video_id, "title": title, "duration": 0, "channel": "",
                "description": "", "thumbnail": thumbnail if thumbnail.startswith("/") else thumbnail,
                "subtitles": [], "format_label": format_label, "format_id": format_id,
            })
        elif status == "processing":
            tasks.update_task(task_id, status="processing", percent=100)
        elif status == "paused":
            cur = tasks.get_task(task_id)
            if cur and cur.get("status") != "queued":
                tasks.update_task(task_id, status="paused")
                _start_next_queued()
        elif status == "done":
            info = evt.get("info", {})
            subtitle_list = []
            for f_name in evt.get("subtitles", []):
                stem = os.path.splitext(f_name)[0]
                if stem.startswith(video_id):
                    lang = stem[len(video_id):].lstrip("._-")
                else:
                    lang = stem.rsplit("_", 1)[-1]
                subtitle_list.append({"lang": lang, "file": f_name})
            thumb_url = f"/thumb/{info['id']}" if evt.get("thumbnail") else ""
            actual_id = info.get("id", video_id)
            metadata.add_video({
                "id": actual_id, "title": info.get("title", "Unknown"),
                "duration": info.get("duration", 0), "channel": info.get("channel", "Unknown"),
                "description": info.get("description", ""), "thumbnail": thumb_url,
                "subtitles": subtitle_list, "format_label": format_label, "format_id": format_id,
                "upload_date": info.get("upload_date"),
            })
            tasks.complete_task(task_id)
            _start_next_queued()
            if info.get("dash_video_fmt"):
                threading.Thread(
                    target=downloader.merge_dash,
                    args=(actual_id,), daemon=True
                ).start()
        elif status == "error":
            tasks.fail_task(task_id, evt.get("error", "Unknown error"))
            metadata.add_video({
                "id": video_id, "title": title, "duration": 0, "channel": "",
                "description": "", "thumbnail": thumbnail if thumbnail.startswith("/") else thumbnail,
                "subtitles": [], "format_label": format_label, "format_id": format_id,
                "error": evt.get("error", "Unknown error"),
            })
            _start_next_queued()
    return listener


def _make_subtitle_listener(task_id, video_id):
    """Build a progress listener for standalone subtitle download."""
    def listener(evt):
        status = evt.get("status")
        if status == "starting":
            tasks.update_task(task_id, status="starting", percent=0)
        elif status == "downloading":
            tasks.update_task(task_id, status="downloading", percent=evt.get("percent", 50))
        elif status == "processing":
            tasks.update_task(task_id, status="processing", percent=100)
        elif status == "done":
            subs = evt.get("subtitles", [])
            vid = metadata.get_video(video_id)
            if vid:
                subtitle_list = [{"lang": s["lang"], "file": s["file"]} for s in subs]
                vid["subtitles"] = subtitle_list
                metadata.add_video(vid)
            tasks.complete_task(task_id)
            _start_next_queued()
        elif status == "error":
            tasks.fail_task(task_id, evt.get("error", "Subtitle download failed"))
            _start_next_queued()
    return listener


def resume_pending_tasks():
    """Re-launch interrupted tasks: start first, queue the rest."""
    tasks.clear_queue()
    interrupted = []
    for tid, t in list(tasks.get_active_tasks().items()):
        if t.get("status") == "interrupted":
            vid = t["video_id"]
            fmt = t.get("format_id")
            if not vid or not fmt:
                continue
            interrupted.append((tid, vid, fmt, t.get("title", vid),
                                t.get("thumbnail", ""), t.get("format_label", fmt)))

    if not interrupted:
        return

    # Start the first one immediately
    tid, vid, fmt, title, thumb, fmt_label = interrupted[0]
    listener = _make_listener(tid, vid, title, thumb, fmt_label, fmt)
    downloader.progress_listeners[tid] = listener
    downloader.download_video(vid, fmt, tid, resume=True)

    # Queue the rest
    for tid, vid, fmt, title, thumb, fmt_label in interrupted[1:]:
        tasks.update_task(tid, status="queued")
        tasks.queue_task(tid)
        listener = _make_listener(tid, vid, title, thumb, fmt_label, fmt)
        downloader.progress_listeners[tid] = listener


@app.route("/")
def index():
    vids = metadata.load_metadata()
    actives = tasks.get_active_tasks()
    paused = {}
    downloading = {}
    for tid, t in actives.items():
        t["has_partial"] = downloader.partial_file_exists(t["video_id"])
        if t.get("status") == "paused":
            paused[tid] = t
        else:
            downloading[tid] = t
    return render_template("index.html", videos=vids, active_tasks=downloading,
                           paused_tasks=paused, hostname=SERVER_NAME)

@app.route("/search")
def search():
    return render_template("search.html")

@app.route("/api/search")
def api_search():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify([])
    try:
        results = downloader.search_youtube(query)
        lib_vids = {v["id"] for v in metadata.load_metadata()}
        active_vids = {t["video_id"] for t in tasks.get_active_tasks().values()}
        for r in results:
            r["downloaded"] = r["id"] in lib_vids
            r["downloading"] = r["id"] in active_vids
        return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/formats/<video_id>")
def api_formats(video_id):
    try:
        formats = downloader.list_formats(video_id)
        subs = downloader.list_subtitles(video_id)
        vid = metadata.get_video(video_id)
        current_fmt = vid.get("format_id") if vid else None
        return jsonify({"formats": formats, "subtitles": subs, "current_format_id": current_fmt})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/video-info/<video_id>")
def api_video_info(video_id):
    try:
        info = downloader.get_video_info(video_id)
        return jsonify(info)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/download", methods=["POST"])
def api_download():
    data = request.get_json()
    video_id = data.get("video_id")
    format_id = data.get("format_id")
    title = data.get("title", video_id)
    thumbnail = data.get("thumbnail", "")
    format_label = data.get("format_label", format_id)
    if not video_id or not format_id:
        return jsonify({"error": "Missing video_id or format_id"}), 400

    # Check for existing non-error task for this video
    existing_tid = None
    existing_task = None
    for tid, t in tasks.get_active_tasks().items():
        if t["video_id"] == video_id and t.get("status") != "error":
            existing_tid = tid
            existing_task = t
            break

    if existing_task:
        status = existing_task.get("status")
        tid = existing_tid

        # downloading/processing -> request pause; queued work starts only after the worker confirms.
        if status in ("starting", "downloading", "processing"):
            tasks.update_task(tid, status="pausing")
            downloader.pause_download(tid)
            return jsonify({"task_id": tid, "status": "pausing"})

        # queued -> promote to the front. Displace active task to queue front so it auto-resumes later.
        if status == "queued":
            tasks.dequeue_task(tid)
            active_tid = tasks.get_active_download_task_id()
            if active_tid:
                tasks.update_task(active_tid, status="queued")
                tasks.queue_task_front(active_tid)
                downloader.pause_download(active_tid)
                tasks.queue_task_front(tid)
                tasks.update_task(tid, status="queued")
                _start_next_queued()
                return jsonify({"task_id": tid, "status": "queued"})
            listener = _make_listener(tid, video_id, title, thumbnail, format_label, format_id)
            downloader.progress_listeners[tid] = listener
            downloader.download_video(video_id, format_id, tid)
            return jsonify({"task_id": tid, "status": "started"})

        # paused → queue
        if status == "paused":
            tasks.update_task(tid, status="queued")
            tasks.queue_task(tid)
            listener = _make_listener(tid, video_id, title, thumbnail, format_label, format_id)
            downloader.progress_listeners[tid] = listener
            return jsonify({"task_id": tid, "status": "queued"})

    # No existing task — create new one
    task_id = f"{video_id}_{int(time.time())}"
    tasks.create_task(task_id, video_id, title, thumbnail, format_id, format_label)
    listener = _make_listener(task_id, video_id, title, thumbnail, format_label, format_id)
    downloader.progress_listeners[task_id] = listener

    if tasks.has_active_download():
        tasks.queue_task(task_id)
        tasks.update_task(task_id, status="queued")
        return jsonify({"task_id": task_id, "status": "queued"})
    else:
        downloader.download_video(video_id, format_id, task_id)
        return jsonify({"task_id": task_id, "status": "started"})

@app.route("/api/progress/<task_id>")
def api_progress(task_id):
    t = tasks.get_task(task_id)
    if not t:
        t = {"status": "not_found"}
    return jsonify(t)

@app.route("/api/active-tasks")
def api_active_tasks():
    actives = tasks.get_active_tasks()
    queue_list = tasks.get_queue()
    result = []
    for t in actives.values():
        t["has_partial"] = downloader.partial_file_exists(t["video_id"])
        if t["task_id"] in queue_list:
            t["queue_position"] = queue_list.index(t["task_id"]) + 1
        result.append(t)
    return jsonify(result)

@app.route("/api/active-tasks/<task_id>", methods=["DELETE"])
def api_cancel_task(task_id):
    t = tasks.get_task(task_id)
    if t:
        tasks.remove_task(task_id)
    downloader.cancel_flags[task_id] = True
    downloader.progress_listeners.pop(task_id, None)
    return jsonify({"ok": True})

@app.route("/api/active-tasks/<task_id>/pause", methods=["POST"])
def api_pause_task(task_id):
    t = tasks.get_task(task_id)
    if not t:
        return jsonify({"error": "Task not found"}), 404
    status = t.get("status")
    if status in ("starting", "downloading", "processing"):
        tasks.update_task(task_id, status="pausing")
        downloader.pause_download(task_id)
    elif status == "queued":
        tasks.dequeue_task(task_id)
        tasks.update_task(task_id, status="paused")
        _start_next_queued()
    return jsonify({"ok": True, "status": "pausing"})

@app.route("/api/active-tasks/<task_id>/resume", methods=["POST"])
def api_resume_task(task_id):
    t = tasks.get_task(task_id)
    if not t:
        return jsonify({"error": "Task not found"}), 404
    vid = t["video_id"]
    fmt = t["format_id"]
    title = t.get("title", vid)
    thumb = t.get("thumbnail", "")
    fmt_label = t.get("format_label", fmt)
    if t.get("status") == "queued":
        tasks.dequeue_task(task_id)
        active_tid = tasks.get_active_download_task_id()
        if active_tid:
            tasks.update_task(active_tid, status="queued")
            tasks.queue_task_front(active_tid)
            downloader.pause_download(active_tid)
            tasks.queue_task_front(task_id)
            tasks.update_task(task_id, status="queued")
            _start_next_queued()
            return jsonify({"ok": True, "status": "queued"})
        listener = _make_listener(task_id, vid, title, thumb, fmt_label, fmt)
        downloader.progress_listeners[task_id] = listener
        downloader.download_video(vid, fmt, task_id)
        return jsonify({"ok": True, "status": "started"})

    if tasks.has_active_download():
        tasks.queue_task(task_id)
        tasks.update_task(task_id, status="queued")
        listener = _make_listener(task_id, vid, title, thumb, fmt_label, fmt)
        downloader.progress_listeners[task_id] = listener
        return jsonify({"ok": True, "status": "queued"})

    listener = _make_listener(task_id, vid, title, thumb, fmt_label, fmt)
    downloader.progress_listeners[task_id] = listener
    downloader.resume_download(task_id, vid, fmt)
    return jsonify({"ok": True, "status": "resuming"})

@app.route("/api/library")
def api_library():
    vids = metadata.load_metadata()
    enriched = []
    for v in vids:
        video_path = None
        for f in os.listdir(VIDEOS_DIR):
            if os.path.splitext(f)[0] == v["id"]:
                video_path = f
                break
        thumbnail_found = None
        for f in os.listdir(THUMBNAILS_DIR):
            if os.path.splitext(f)[0] == v["id"]:
                thumbnail_found = True
                break
        enriched.append({
            **v,
            "has_video": video_path is not None,
            "video_file": video_path,
            "has_thumbnail": thumbnail_found or False,
        })
    return jsonify(enriched)

@app.route("/api/library/<video_id>", methods=["DELETE"])
def api_delete(video_id):
    for f in os.listdir(VIDEOS_DIR):
        if os.path.splitext(f)[0] == video_id:
            os.remove(os.path.join(VIDEOS_DIR, f))
    # Remove organized subtitle directory if it exists
    sub_dir = os.path.join(SUBTITLES_DIR, video_id)
    if os.path.isdir(sub_dir):
        shutil.rmtree(sub_dir)
    # Remove flat subtitle files (legacy)
    for f in os.listdir(SUBTITLES_DIR):
        name, ext = os.path.splitext(f)
        if name.startswith(video_id) and ext in (".vtt", ".srt", ".ass"):
            os.remove(os.path.join(SUBTITLES_DIR, f))
    for f in os.listdir(THUMBNAILS_DIR):
        if os.path.splitext(f)[0] == video_id:
            os.remove(os.path.join(THUMBNAILS_DIR, f))
    hls_manager.clear(video_id)
    metadata.remove_video(video_id)
    return jsonify({"ok": True})

@app.route("/video/<video_id>")
def player(video_id):
    active = tasks.get_active_tasks()
    is_downloading = any(t["video_id"] == video_id for t in active.values())
    vid = metadata.get_video(video_id)
    if vid:
        has_partial = downloader.partial_file_exists(video_id)
        return render_template("player.html", video=vid, is_partial=has_partial,
                               is_downloading=is_downloading)
    if is_downloading:
        has_partial = downloader.partial_file_exists(video_id)
        return render_template("player.html", video={
            "id": video_id,
            "title": "Download in Progress",
            "channel": "",
            "duration": 0,
            "description": "",
            "subtitles": [],
        }, is_partial=has_partial, is_downloading=True)
    return "Video not found", 404

@app.route("/stream/<video_id>")
def video_stream(video_id):
    return stream_video(video_id)

@app.route("/stream_audio/<video_id>")
def audio_stream(video_id):
    return stream_audio_track(video_id)

@app.route("/api/hls/<video_id>/master.m3u8")
def api_hls_master(video_id):
    body, content_type = hls_master_playlist(video_id)
    if body is None:
        return Response(status=204)
    return Response(body, mimetype=content_type or "application/vnd.apple.mpegurl",
                    headers={"Cache-Control": "no-cache"})

@app.route("/api/hls/<video_id>/<path:filename>")
def api_hls_file(video_id, filename):
    return hls_serve_file(video_id, filename)


@app.route("/api/hls-simple/<video_id>/<path:filename>")
def api_hls_simple(video_id, filename):
    if filename == "master.m3u8":
        content = hls_simple_master_playlist(video_id, request.host_url)
        if content is None:
            return Response(status=204)
        return Response(content, mimetype="application/vnd.apple.mpegurl",
                        headers={"Cache-Control": "no-cache"})
    if filename.startswith("subs_"):
        for ext in (".vtt", ".srt"):
            if filename.endswith(ext):
                lang = filename[5:-len(ext)]
                content, mime = get_subtitle(video_id, lang)
                if content is None:
                    return abort(404)
                content = _clean_webvtt(content)
                return Response(content, mimetype="text/vtt",
                                headers={"Access-Control-Allow-Origin": "*",
                                         "Cache-Control": "no-cache"})
    return abort(404)


@app.route("/api/stream-info/<video_id>")
def api_stream_info(video_id):
    return jsonify(stream_status(video_id))


@app.route("/rss")
def rss_feed():
    ET.register_namespace("atom", "http://www.w3.org/2005/Atom")
    ET.register_namespace("media", "http://search.yahoo.com/mrss/")
    rss = ET.Element("rss", version="2.0")
    ch = ET.SubElement(rss, "channel")
    ET.SubElement(ch, "title").text = "YouTube Cache"
    ET.SubElement(ch, "link").text = request.host_url
    ET.SubElement(ch, "description").text = "Downloaded YouTube videos"

    atom_link = ET.SubElement(ch, "{http://www.w3.org/2005/Atom}link")
    atom_link.set("href", request.host_url.rstrip("/") + "/rss")
    atom_link.set("rel", "self")
    atom_link.set("type", "application/rss+xml")

    for v in metadata.load_metadata():
        vid = v["id"]
        path = get_video_path(vid, exact_only=False)
        if not path:
            continue
        mime, _ = mimetypes.guess_type(path)
        size = os.path.getsize(path)
        mtime = os.path.getmtime(path)

        item = ET.SubElement(ch, "item")
        ET.SubElement(item, "title").text = v.get("title", "Unknown")
        ET.SubElement(item, "link").text = request.host_url.rstrip("/") + f"/video/{vid}"
        ET.SubElement(item, "guid").text = vid
        ET.SubElement(item, "pubDate").text = utils.formatdate(mtime, usegmt=True)

        desc = v.get("description", "") or v.get("channel", "")
        ET.SubElement(item, "description").text = desc

        enc = ET.SubElement(item, "enclosure")
        enc.set("url", request.host_url.rstrip("/") + f"/stream/{vid}")
        enc.set("type", mime or "video/mp4")
        enc.set("length", str(size))

        mc = ET.SubElement(item, "{http://search.yahoo.com/mrss/}content")
        mc.set("url", request.host_url.rstrip("/") + f"/api/hls-simple/{vid}/master.m3u8")
        mc.set("type", "application/vnd.apple.mpegurl")
        for s in list_subtitles_on_disk(vid):
            sub = ET.SubElement(mc, "{http://search.yahoo.com/mrss/}subTitle")
            sub.set("type", "text/vtt")
            sub.set("lang", s["lang"])

        thumb = ET.SubElement(item, "{http://search.yahoo.com/mrss/}thumbnail")
        thumb.set("url", request.host_url.rstrip("/") + f"/thumb/{vid}")

    return Response(
        ET.tostring(rss, encoding="unicode", xml_declaration=True),
        mimetype="application/rss+xml",
    )

def _fmt_size(n):
    for unit in ("B", "K", "M", "G"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}T"


def _fmt_time(ts):
    import time
    return time.strftime("%d-%b-%Y %H:%M", time.gmtime(ts))


def _apache_line(display_name, href, date, size, title=None):
    """Apache-style listing line."""
    extra = f' title="{title}"' if title else ""
    if not date and not size:
        return f'<a href="{href}"{extra}>{display_name}</a>'
    pad = " " * max(1, 50 - len(display_name))
    sz = size if size else "-"
    return f'<a href="{href}"{extra}>{display_name}</a>{pad}{date}    {sz:>8}'


def _apache_page(title, entries):
    """Build a full Apache-style HTML page."""
    head = (
        '<html>\n'
        f'<head><title>Index of {title}</title></head>\n'
        '<body>\n'
        f'<h1>Index of {title}</h1><hr><pre>'
    )
    return head + "\n".join(entries) + "\n</pre><hr></body>\n</html>\n"


def _resolve_slug(slug):
    """Resolve a URL slug to video metadata. Slug may be a video_id or a title."""
    v = metadata.get_video(slug)
    if v:
        return v
    return metadata.find_by_title(slug)


def _slug_for_video(v):
    title = v.get("title", v["id"])
    safe = title.replace("/", "-")
    return quote(safe, safe='')


@app.route("/browse")
@app.route("/browse/")
def browse_root():
    items = []
    for v in metadata.load_metadata():
        path = get_video_path(v["id"], exact_only=False)
        if not path:
            continue
        items.append(v)
    if not items:
        return Response(_apache_page("/browse/", ['<a href="../">../</a>']), mimetype="text/html")
    entries = ['<a href="../">../</a>']
    for v in items:
        title = v.get("title", v["id"])
        href = _slug_for_video(v) + "/"
        display = title + "/"
        path = get_video_path(v["id"], exact_only=False)
        mt = _fmt_time(os.path.getmtime(path))
        entries.append(_apache_line(display, href, mt, "-", title=title))
    return Response(_apache_page("/browse/", entries), mimetype="text/html")


@app.route("/browse/<slug>")
@app.route("/browse/<slug>/")
def browse_video(slug):
    import os
    meta = _resolve_slug(slug)
    if not meta:
        return abort(404)
    video_id = meta["id"]
    items = []
    merged = downloader._find_merged_file(video_id)
    if merged:
        base = os.path.basename(merged)
        items.append((base, _fmt_size(os.path.getsize(merged)), os.path.getmtime(merged)))
    for s in list_subtitles_on_disk(video_id):
        sp = os.path.join(SUBTITLES_DIR, video_id, s["file"])
        if os.path.exists(sp):
            items.append((s["file"], _fmt_size(os.path.getsize(sp)), os.path.getmtime(sp)))
    entries = ['<a href="../">../</a>']
    for name, size_str, mtime in items:
        entries.append(_apache_line(name, name, _fmt_time(mtime), size_str))
    title_hdr = meta.get("title", slug)
    return Response(_apache_page(f"/browse/{title_hdr}/", entries), mimetype="text/html")


@app.route("/browse/<slug>/<path:filename>")
def browse_video_file(slug, filename):
    import os, mimetypes
    meta = _resolve_slug(slug)
    if not meta:
        return abort(404)
    video_id = meta["id"]
    sub_path = os.path.join(SUBTITLES_DIR, video_id, filename)
    if os.path.exists(sub_path) and os.path.isfile(sub_path):
        raw = open(sub_path, "rb").read()
        mime, _ = mimetypes.guess_type(sub_path)
        if filename.endswith(".vtt"):
            raw = _clean_webvtt(raw.decode("utf-8")).encode("utf-8")
        return Response(raw, mimetype=mime or "text/plain")
    merged = downloader._find_merged_file(video_id)
    if merged and os.path.basename(merged) == filename:
        mime, _ = mimetypes.guess_type(merged)
        return send_file(merged, mimetype=mime, conditional=True)
    return abort(404)


@app.route("/api/subtitles/<video_id>", methods=["GET"])
def api_subtitles_list(video_id):
    on_disk = list_subtitles_on_disk(video_id)
    available = downloader.list_subtitles(video_id)
    return jsonify({"on_disk": on_disk, "available": available})


@app.route("/api/subtitles/<video_id>", methods=["POST"])
def api_subtitles_download(video_id):
    data = request.get_json(silent=True) or {}
    langs = data.get("langs") if data else None
    task_id = f"sub_{video_id}_{int(time.time())}"
    tasks.create_task(task_id, video_id, f"Subtitles for {video_id}", "", "", "")
    url = f"https://youtube.com/watch?v={video_id}"

    def run():
        if task_id in downloader.subtitle_listeners:
            downloader.subtitle_listeners[task_id]({"status": "starting"})
        subs = downloader.download_all_subtitles(video_id, url, langs=langs)
        if task_id in downloader.subtitle_listeners:
            downloader.subtitle_listeners[task_id]({"status": "done", "subtitles": subs})

    listener = _make_subtitle_listener(task_id, video_id)
    downloader.subtitle_listeners[task_id] = listener
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return jsonify({"task_id": task_id, "status": "started"})


@app.route("/subtitle/<video_id>/<lang>")
def subtitle_serve(video_id, lang):
    content, mime = get_subtitle(video_id, lang)
    if content is None:
        return "Not found", 404
    return Response(content, mimetype=mime)


@app.route("/sub/<video_id>/<lang>")
def sub_redirect(video_id, lang):
    return redirect(f"/subtitle/{video_id}/{lang}", 301)


@app.route("/thumb/<video_id>")
def thumb(video_id):
    path, mime = get_thumbnail(video_id)
    if not path:
        return "Not found", 404
    return send_file(path, mimetype=mime)

@app.route("/api/dlna/start", methods=["POST"])
def dlna_start():
    global dlna_server
    if dlna_server:
        return jsonify({"status": "already_running"})
    from dlna_server import DLMAServer
    dlna_server = DLMAServer()
    dlna_server.start()
    return jsonify({"status": "started", "port": DLNA_PORT})

@app.route("/api/dlna/stop", methods=["POST"])
def dlna_stop():
    global dlna_server
    if dlna_server:
        dlna_server.stop()
        dlna_server = None
        return jsonify({"status": "stopped"})
    return jsonify({"status": "not_running"})

@app.route("/api/dlna/status")
def dlna_status():
    return jsonify({
        "running": dlna_server is not None,
        "port": DLNA_PORT,
        "ip": None,
    })

if not app.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
    resume_pending_tasks()

if __name__ == "__main__":
    app.run(host=HOST, port=PORT, debug=True)
