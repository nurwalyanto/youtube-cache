import os
import json
import time
from threading import Lock
from config import CACHE_DIR

TASKS_FILE = os.path.join(CACHE_DIR, "active_downloads.json")

_lock = Lock()
_tasks_cache = None

def _load():
    global _tasks_cache
    if _tasks_cache is not None:
        return _tasks_cache
    if not os.path.exists(TASKS_FILE):
        _tasks_cache = {}
        return _tasks_cache
    with open(TASKS_FILE, "r") as f:
        try:
            _tasks_cache = json.load(f)
        except:
            _tasks_cache = {}
    return _tasks_cache

def _save():
    global _tasks_cache
    with _lock:
        with open(TASKS_FILE, "w") as f:
            json.dump(_tasks_cache, f, indent=2)

def create_task(task_id, video_id, title, thumbnail, format_id, format_label):
    tasks = _load()
    tasks[task_id] = {
        "task_id": task_id,
        "video_id": video_id,
        "title": title,
        "thumbnail": thumbnail,
        "format_id": format_id,
        "format_label": format_label,
        "status": "queued",
        "percent": 0,
        "speed": None,
        "eta": None,
        "error": None,
        "started_at": time.time(),
        "completed_at": None,
    }
    _tasks_cache = tasks
    _save()

def update_task(task_id, **kwargs):
    tasks = _load()
    if task_id not in tasks:
        return
    for k, v in kwargs.items():
        tasks[task_id][k] = v
    _tasks_cache = tasks
    _save()

def get_task(task_id):
    tasks = _load()
    return tasks.get(task_id)

def get_active_tasks():
    tasks = _load()
    return {k: v for k, v in tasks.items() if v.get("status") not in ("done", "removed")}

def complete_task(task_id):
    tasks = _load()
    if task_id in tasks:
        tasks[task_id]["status"] = "done"
        tasks[task_id]["completed_at"] = time.time()
        _tasks_cache = tasks
        _save()

def fail_task(task_id, error):
    tasks = _load()
    if task_id in tasks:
        tasks[task_id]["status"] = "error"
        tasks[task_id]["error"] = str(error)
        tasks[task_id]["completed_at"] = time.time()
        _tasks_cache = tasks
        _save()

def remove_task(task_id):
    tasks = _load()
    tasks.pop(task_id, None)
    _tasks_cache = tasks
    _save()

def mark_interrupted():
    """Mark active tasks as interrupted (app crash). Preserve percent for resume display."""
    tasks = _load()
    changed = False
    for tid, t in tasks.items():
        if t.get("status") in ("downloading", "queued", "processing", "starting"):
            t["status"] = "interrupted"
            changed = True
    if changed:
        _tasks_cache = tasks
        _save()

def cleanup_orphaned():
    tasks = _load()
    to_remove = []
    for tid, t in tasks.items():
        if t.get("status") in ("done", "error", "removed"):
            age = time.time() - t.get("completed_at", 0) if t.get("completed_at") else 0
            if age > 86400:
                to_remove.append(tid)
    for tid in to_remove:
        tasks.pop(tid, None)
    _tasks_cache = tasks
    _save()
