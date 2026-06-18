import os
import json
import threading
import time
from flask import Flask, render_template, request, jsonify, Response, send_file

from config import (
    VIDEOS_DIR, SUBTITLES_DIR, THUMBNAILS_DIR, PORT, DLNA_PORT, HOST, SERVER_NAME
)
import metadata
import downloader
import tasks
from streamer import stream_video, stream_audio_track, stream_video_chunks, stream_info, stream_init_segment, stream_diag, get_subtitle, get_thumbnail

app = Flask(__name__)
dlna_server = None

tasks.mark_interrupted()
tasks.cleanup_orphaned()

@app.route("/")
def index():
    vids = metadata.load_metadata()
    actives = tasks.get_active_tasks()
    for t in actives.values():
        t["has_partial"] = downloader.partial_file_exists(t["video_id"])
    return render_template("index.html", videos=vids, active_tasks=actives, hostname=SERVER_NAME)

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
        return jsonify({"formats": formats, "subtitles": subs})
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

    task_id = f"{video_id}_{int(time.time())}"
    tasks.create_task(task_id, video_id, title, thumbnail, format_id, format_label)
    tasks.update_task(task_id, status="queued")

    def listener(evt):
        status = evt.get("status")
        if status == "downloading":
            dash_video = evt.get("dash_video_fmt")
            dash_audio = evt.get("dash_audio_fmt")
            tasks.update_task(task_id, status="downloading", percent=evt.get("percent"), speed=evt.get("speed"), eta=evt.get("eta"),
                              dash_video_fmt=dash_video, dash_audio_fmt=dash_audio)
        elif status == "processing":
            tasks.update_task(task_id, status="processing", percent=100)
        elif status == "done":
            info = evt.get("info", {})
            subtitle_list = []
            for f_name in evt.get("subtitles", []):
                lang = os.path.splitext(f_name)[0].rsplit("_", 1)[-1]
                if "_" not in f_name:
                    stem = f_name.rsplit(".", 2)
                    lang = stem[1] if len(stem) >= 3 else lang
                subtitle_list.append({"lang": lang, "file": f_name})
            thumb_url = f"/thumb/{info['id']}" if evt.get("thumbnail") else ""
            metadata.add_video({
                "id": info["id"],
                "title": info.get("title", "Unknown"),
                "duration": info.get("duration", 0),
                "channel": info.get("channel", "Unknown"),
                "description": info.get("description", ""),
                "thumbnail": thumb_url,
                "subtitles": subtitle_list,
            })
            tasks.complete_task(task_id)
        elif status == "error":
            tasks.fail_task(task_id, evt.get("error", "Unknown error"))

    downloader.progress_listeners[task_id] = listener
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
    result = []
    for t in actives.values():
        t["has_partial"] = downloader.partial_file_exists(t["video_id"])
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
    for f in os.listdir(SUBTITLES_DIR):
        if os.path.splitext(f)[0] == video_id:
            os.remove(os.path.join(SUBTITLES_DIR, f))
    for f in os.listdir(THUMBNAILS_DIR):
        if os.path.splitext(f)[0] == video_id:
            os.remove(os.path.join(THUMBNAILS_DIR, f))
    metadata.remove_video(video_id)
    return jsonify({"ok": True})

@app.route("/video/<video_id>")
def player(video_id):
    from streamer import get_video_path
    from config import VIDEOS_DIR
    vid = metadata.get_video(video_id)
    if vid:
        return render_template("player.html", video=vid, is_partial=False)
    active = tasks.get_active_tasks()
    is_downloading = any(t["video_id"] == video_id for t in active.values())
    if is_downloading and downloader.partial_file_exists(video_id):
        # Prefer DASH intermediate file when an active DASH download exists
        dash_path = None
        for t in active.values():
            if t["video_id"] == video_id and t.get("dash_video_fmt"):
                c = os.path.join(VIDEOS_DIR, f"{video_id}.f{t['dash_video_fmt']}.mp4")
                if os.path.exists(c):
                    dash_path = c
                    break
        if dash_path:
            is_partial = True
        else:
            stream_path = get_video_path(video_id)
            is_partial = stream_path and ".f" in os.path.basename(stream_path)
        return render_template("player.html", video={
            "id": video_id,
            "title": "Download in Progress",
            "channel": "",
            "duration": 0,
            "description": "",
            "subtitles": [],
        }, is_partial=is_partial)
    return "Video not found", 404

@app.route("/stream/<video_id>")
def video_stream(video_id):
    return stream_video(video_id)

@app.route("/stream_audio/<video_id>")
def audio_stream(video_id):
    return stream_audio_track(video_id)

@app.route("/api/video-chunks/<video_id>")
def api_video_chunks(video_id):
    return stream_video_chunks(video_id)

@app.route("/api/stream-info/<video_id>")
def api_stream_info(video_id):
    return jsonify(stream_info(video_id))

@app.route("/api/init-segment/<video_id>")
def api_init_segment(video_id):
    return stream_init_segment(video_id)

@app.route("/api/diag/<video_id>")
def api_diag(video_id):
    return stream_diag(video_id)

@app.route("/sub/<video_id>/<lang>")
def sub(video_id, lang):
    content, mime = get_subtitle(video_id, lang)
    if content is None:
        return "Not found", 404
    return Response(content, mimetype=mime)

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

if __name__ == "__main__":
    app.run(host=HOST, port=PORT, debug=True, threaded=True)
