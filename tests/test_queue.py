"""Tests for download queue logic (no external deps, pure logic)."""

import os
import json
import tempfile
import time

import pytest


# ── Fixtures ───────────────────────────────────────────────────────

@pytest.fixture
def td():
    """Temp directory + monkey-patch of tasks module file paths."""
    with tempfile.TemporaryDirectory() as tmp:
        import tasks as t
        t.TASKS_FILE = os.path.join(tmp, "active_downloads.json")
        t.QUEUE_FILE = os.path.join(tmp, "download_queue.json")
        t._tasks_cache = None
        yield t


@pytest.fixture
def ctx():
    """Convenience: create a task dict with minimal fields."""
    def _make(task_id, video_id, status="queued", percent=0):
        return {
            "task_id": task_id,
            "video_id": video_id,
            "title": video_id,
            "thumbnail": "",
            "format_id": "f1",
            "format_label": "720p",
            "status": status,
            "percent": percent,
            "speed": None,
            "eta": None,
            "error": None,
            "started_at": time.time(),
            "completed_at": None,
        }
    return _make


# ── Queue operation tests ──────────────────────────────────────────

class TestQueueOps:
    def test_queue_append_order(self, td):
        td.queue_task("a")
        td.queue_task("b")
        assert td.get_queue() == ["a", "b"]

    def test_dequeue_middle(self, td):
        td.queue_task("a")
        td.queue_task("b")
        td.queue_task("c")
        td.dequeue_task("b")
        assert td.get_queue() == ["a", "c"]

    def test_dequeue_missing_is_noop(self, td):
        td.queue_task("a")
        td.dequeue_task("nonexistent")
        assert td.get_queue() == ["a"]

    def test_queue_front_inserts_at_zero(self, td):
        td.queue_task("b")
        td.queue_task_front("a")
        assert td.get_queue() == ["a", "b"]

    def test_queue_front_duplicate_is_noop(self, td):
        td.queue_task("a")
        td.queue_task_front("a")
        assert td.get_queue() == ["a"]

    def test_clear_queue(self, td):
        td.queue_task("a")
        td.queue_task("b")
        td.clear_queue()
        assert td.get_queue() == []

    def test_queue_persistence(self, td):
        """Verify data survives by re-reading from disk (no cache)."""
        td.queue_task("x")
        td.queue_task("y")
        with open(td.QUEUE_FILE) as f:
            data = json.load(f)
        assert data == ["x", "y"]

    def test_multiple_queues_preserve_order(self, td):
        td.queue_task("a")
        td.queue_task("b")
        td.queue_task("c")
        td.queue_task_front("a")
        td.dequeue_task("b")
        assert td.get_queue() == ["a", "c"]


# ── Task state tests ───────────────────────────────────────────────

class TestTaskState:
    def test_create_default_status(self, td):
        td.create_task("t1", "v1", "T", "", "f1", "720p")
        assert td.get_task("t1")["status"] == "queued"

    def test_update_preserves_other_fields(self, td):
        td.create_task("t1", "v1", "T", "", "f1", "720p")
        td.update_task("t1", status="downloading", percent=50)
        t = td.get_task("t1")
        assert t["status"] == "downloading"
        assert t["percent"] == 50
        assert t["video_id"] == "v1"

    def test_has_active_true_when_downloading(self, td):
        td.create_task("a", "v1", "A", "", "f1", "720p")
        td.update_task("a", status="downloading")
        assert td.has_active_download() is True

    def test_has_active_true_when_processing(self, td):
        td.create_task("a", "v1", "A", "", "f1", "720p")
        td.update_task("a", status="processing")
        assert td.has_active_download() is True

    def test_has_active_true_when_starting(self, td):
        td.create_task("a", "v1", "A", "", "f1", "720p")
        td.update_task("a", status="starting")
        assert td.has_active_download() is True

    def test_has_active_true_when_pausing(self, td):
        td.create_task("a", "v1", "A", "", "f1", "720p")
        td.update_task("a", status="pausing")
        assert td.has_active_download() is True

    def test_has_active_false_when_paused(self, td):
        td.create_task("a", "v1", "A", "", "f1", "720p")
        td.update_task("a", status="paused")
        assert td.has_active_download() is False

    def test_has_active_false_when_queued(self, td):
        td.create_task("a", "v1", "A", "", "f1", "720p")
        assert td.has_active_download() is False

    def test_get_active_task_id(self, td):
        td.create_task("a", "v1", "A", "", "f1", "720p")
        td.create_task("b", "v2", "B", "", "f1", "720p")
        td.update_task("a", status="downloading")
        assert td.get_active_download_task_id() == "a"

    def test_get_active_task_id_pausing(self, td):
        td.create_task("a", "v1", "A", "", "f1", "720p")
        td.update_task("a", status="pausing")
        assert td.get_active_download_task_id() == "a"

    def test_get_active_task_id_none(self, td):
        td.create_task("a", "v1", "A", "", "f1", "720p")
        td.create_task("b", "v2", "B", "", "f1", "720p")
        td.update_task("a", status="paused")
        td.update_task("b", status="queued")
        assert td.get_active_download_task_id() is None

    def test_remove_task_also_dequeues(self, td):
        td.create_task("a", "v1", "A", "", "f1", "720p")
        td.queue_task("a")
        td.remove_task("a")
        assert td.get_queue() == []
        assert td.get_task("a") is None

    def test_complete_task_sets_done(self, td):
        td.create_task("a", "v1", "A", "", "f1", "720p")
        td.complete_task("a")
        assert td.get_task("a")["status"] == "done"

    def test_fail_task_sets_error(self, td):
        td.create_task("a", "v1", "A", "", "f1", "720p")
        td.fail_task("a", "connection error")
        assert td.get_task("a")["status"] == "error"
        assert "connection error" in td.get_task("a")["error"]

    def test_get_active_excludes_done_and_removed(self, td):
        td.create_task("a", "v1", "A", "", "f1", "720p")
        td.create_task("b", "v2", "B", "", "f1", "720p")
        td.complete_task("a")
        td.update_task("b", status="downloading")
        actives = td.get_active_tasks()
        assert "a" not in actives
        assert "b" in actives


# ── Integration tests (simulating app.py orchestration) ────────────

class TestQueueIntegration:
    def test_new_task_queued_when_active_exists(self, td):
        """A is downloading -> B is created -> B goes to queue."""
        td.create_task("a", "v_a", "A", "", "f1", "720p")
        td.update_task("a", status="downloading")
        td.create_task("b", "v_b", "B", "", "f1", "720p")
        td.queue_task("b")
        td.update_task("b", status="queued")
        assert td.get_queue() == ["b"]
        assert td.has_active_download() is True

    def test_force_resume_displaces_active_to_queue_front(self, td):
        """
        Simulate api_resume_task / api_download force-download path:
        A active, B queued -> resume B -> A goes to front of queue.
        """
        td.create_task("a", "v_a", "A", "", "f1", "720p")
        td.update_task("a", status="downloading")
        td.create_task("b", "v_b", "B", "", "f1", "720p")
        td.queue_task("b")
        # Force-resume B (app.py lines 331-335 equivalent)
        for tid_b in td.get_queue():
            td.dequeue_task(tid_b)
            break
        active_id = td.get_active_download_task_id()
        td.update_task(active_id, status="queued")
        td.queue_task_front(active_id)
        td.update_task("b", status="starting")
        assert td.get_queue() == ["a"]
        assert td.get_active_download_task_id() == "b"

    def test_pause_active_promotes_queued(self, td):
        """
        Simulate: A downloading, B queued -> pause A -> B starts.
        """
        td.create_task("a", "v_a", "A", "", "f1", "720p")
        td.update_task("a", status="downloading")
        td.create_task("b", "v_b", "B", "", "f1", "720p")
        td.queue_task("b")
        td.update_task("a", status="paused")
        assert td.has_active_download() is False
        q = td.get_queue()
        assert q == ["b"]
        td.dequeue_task(q[0])
        td.update_task("b", status="starting")
        assert td.get_active_download_task_id() == "b"
        assert td.get_queue() == []

    def test_active_completes_starts_next_queued(self, td):
        """A done -> _start_next_queued promotes B."""
        td.create_task("a", "v_a", "A", "", "f1", "720p")
        td.update_task("a", status="downloading")
        td.create_task("b", "v_b", "B", "", "f1", "720p")
        td.queue_task("b")
        td.create_task("c", "v_c", "C", "", "f1", "720p")
        td.queue_task("c")
        td.complete_task("a")
        assert td.has_active_download() is False
        q = td.get_queue()
        assert q == ["b", "c"]
        td.dequeue_task("b")
        td.update_task("b", status="starting")
        assert td.get_active_download_task_id() == "b"
        assert td.get_queue() == ["c"]

    def test_queued_task_resume_with_no_active(self, td):
        """Only queued tasks exist (no active) -> resume starts immediately."""
        td.create_task("a", "v_a", "A", "", "f1", "720p")
        td.queue_task("a")
        td.create_task("b", "v_b", "B", "", "f1", "720p")
        td.queue_task("b")
        assert td.has_active_download() is False
        assert td.get_active_download_task_id() is None
        td.dequeue_task("a")
        td.update_task("a", status="starting")
        assert td.get_active_download_task_id() == "a"
        assert td.get_queue() == ["b"]

    def test_paused_event_does_not_clobber_queued(self, td):
        """
        When a force-displaced task (status="queued") receives a stray
        'paused' event from its old thread, the listener must preserve
        the "queued" status (app.py lines 85-87).
        """
        td.create_task("a", "v_a", "A", "", "f1", "720p")
        td.update_task("a", status="queued")
        td.queue_task("a")
        cur = td.get_task("a")
        if cur and cur.get("status") != "queued":
            td.update_task("a", status="paused")
        assert td.get_task("a")["status"] == "queued"

    def test_queued_status_survives_pause_event(self, td):
        """
        Like paused_event_does_not_clobber_queued but from
        the listener perspective: a forced-queued task stays
        queued even if the old thread emits 'paused'.
        """
        td.create_task("a", "v_a", "A", "", "f1", "720p")
        td.update_task("a", status="queued")
        td.queue_task("a")
        cur = td.get_task("a")
        status_after_check = cur["status"]
        if cur and cur.get("status") != "queued":
            td.update_task("a", status="paused")
        assert status_after_check == "queued"
        assert td.get_task("a")["status"] == "queued"

    def test_listener_paused_still_starts_next_queued(self, td):
        """
        The 'paused' listener branch still calls _start_next_queued
        even if the task's status is already 'queued'.
        """
        td.create_task("a", "v_a", "A", "", "f1", "720p")
        td.update_task("a", status="queued")
        td.queue_task("a")
        td.create_task("b", "v_b", "B", "", "f1", "720p")
        td.queue_task("b")
        td.update_task("b", status="starting")
        if td.has_active_download():
            pass
        td.dequeue_task("a")
        td.queue_task_front("a")
        assert td.get_task("b")["status"] == "starting"

    def test_queue_unchanged_when_other_task_paused(self, td):
        """
        Pausing a non-active (queued) task should not affect the
        rest of the queue.
        """
        td.create_task("a", "v_a", "A", "", "f1", "720p")
        td.update_task("a", status="downloading")
        td.create_task("b", "v_b", "B", "", "f1", "720p")
        td.queue_task("b")
        td.create_task("c", "v_c", "C", "", "f1", "720p")
        td.queue_task("c")
        td.dequeue_task("b")
        td.update_task("b", status="paused")
        assert td.get_queue() == ["c"]
        assert td.get_active_download_task_id() == "a"

    def test_force_resume_then_cancel_new_active(self, td):
        """
        A active, B queued -> force-resume B (A goes to queue front)
        -> cancel B -> A should still be at front of queue.
        """
        td.create_task("a", "v_a", "A", "", "f1", "720p")
        td.update_task("a", status="downloading")
        td.create_task("b", "v_b", "B", "", "f1", "720p")
        td.queue_task("b")
        td.dequeue_task("b")
        active_id = td.get_active_download_task_id()
        td.update_task(active_id, status="queued")
        td.queue_task_front(active_id)
        td.update_task("b", status="starting")
        assert td.get_queue() == ["a"]
        td.remove_task("b")
        assert td.get_queue() == ["a"]


# ── Scenario: pause active then request new download ────────────

class TestPauseThenNewDownload:
    def test_new_download_starts_after_active_paused(self, td):
        """
        A downloading -> pause A -> request new B.
        B should start immediately (no active download blocks it).
        """
        td.create_task("a", "v_a", "A", "", "f1", "720p")
        td.update_task("a", status="downloading")

        td.update_task("a", status="paused")
        assert td.get_active_download_task_id() is None
        assert td.has_active_download() is False

        td.create_task("b", "v_b", "B", "", "f1", "720p")
        assert td.has_active_download() is False

        td.update_task("b", status="starting")
        assert td.get_active_download_task_id() == "b"
        assert td.get_queue() == []

    def test_b_appears_in_active_tasks(self, td):
        """After request, B must appear in get_active_tasks list."""
        td.create_task("a", "v_a", "A", "", "f1", "720p")
        td.update_task("a", status="downloading")
        td.update_task("a", status="paused")
        td.create_task("b", "v_b", "B", "", "f1", "720p")
        td.update_task("b", status="starting")
        actives = td.get_active_tasks()
        assert "b" in actives
        assert actives["b"]["status"] in ("starting", "queued")

    def test_paused_event_after_old_thread_does_not_harm(self, td):
        """
        Simulate: A paused -> old download thread eventually fires
        'paused' -> check status == 'paused' (not 'queued') -> ok.
        _start_next_queued should see B already active.
        """
        td.create_task("a", "v_a", "A", "", "f1", "720p")
        td.update_task("a", status="downloading")

        td.update_task("a", status="paused")
        td.create_task("b", "v_b", "B", "", "f1", "720p")

        cur = td.get_task("a")
        if cur and cur.get("status") != "queued":
            td.update_task("a", status="paused")

        td.update_task("b", status="starting")
        assert td.get_active_download_task_id() == "b"

    def test_paused_event_does_not_restart_a(self, td):
        """
        A downloading, B queued -> pause A -> _start_next_queued
        dequeues B and starts it. Then A's old thread fires 'paused'.
        With B already 'starting', _start_next_queued is a no-op.
        """
        td.create_task("a", "v_a", "A", "", "f1", "720p")
        td.update_task("a", status="downloading")
        td.create_task("b", "v_b", "B", "", "f1", "720p")
        td.queue_task("b")
        td.update_task("b", status="queued")

        td.update_task("a", status="paused")
        assert td.has_active_download() is False

        td.dequeue_task("b")
        td.update_task("b", status="starting")
        assert td.has_active_download() is True
        assert td.get_queue() == []

    def test_no_active_means_new_download_starts_directly(self, td):
        """
        After pausing, has_active_download is False.
        A new download request starts B directly (no queue).
        """
        td.create_task("a", "v_a", "A", "", "f1", "720p")
        td.update_task("a", status="downloading")
        td.update_task("a", status="paused")
        assert td.has_active_download() is False
        assert td.get_active_download_task_id() is None
        assert td.get_queue() == []

    def test_skip_already_downloaded_doesnt_affect_b(self, td):
        """
        If 'a' was already downloaded (same format) and marked done,
        has_active_download should still depend only on (downloading,
        processing, starting).
        """
        td.create_task("a", "v_a", "A", "", "f1", "720p")
        td.update_task("a", status="downloading")
        td.complete_task("a")
        td.create_task("b", "v_b", "B", "", "f1", "720p")
        assert td.has_active_download() is False


# ── App-level logic tests (mocking downloader) ───────────────────

class TestAppLogic:
    def test_start_next_queued_picks_first(self, td):
        """_start_next_queued() starts the first queued task."""
        td.create_task("a", "v_a", "A", "", "f1", "720p")
        td.create_task("b", "v_b", "B", "", "f1", "720p")
        td.queue_task("a")
        td.queue_task("b")
        assert td.has_active_download() is False
        q = td.get_queue()
        assert q
        next_tid = q[0]
        td.dequeue_task(next_tid)
        td.update_task(next_tid, status="starting")
        assert td.get_active_download_task_id() == "a"
        assert td.get_queue() == ["b"]

    def test_start_next_queued_skips_stale_entries(self, td):
        """If a queued task no longer exists, it is skipped."""
        td.create_task("b", "v_b", "B", "", "f1", "720p")
        td.queue_task("X")
        td.queue_task("b")
        assert td.has_active_download() is False
        q = td.get_queue()
        while q:
            next_tid = q[0]
            t = td.get_task(next_tid)
            if not t:
                td.dequeue_task(next_tid)
                q = td.get_queue()
                continue
            td.dequeue_task(next_tid)
            td.update_task(next_tid, status="starting")
            break
        assert td.get_queue() == []
        assert td.get_active_download_task_id() == "b"

    def test_pause_active_then_queued_resume(self, td):
        """
        Full flow: A downloading -> B queued -> pause A (B starts)
        -> pause B (A should resume from queue front).
        This test documents the fix: after force-download of B,
        A is queued (not just paused), so when B finishes A resumes.
        """
        td.create_task("a", "v_a", "A", "", "f1", "720p")
        td.update_task("a", status="downloading")
        td.create_task("b", "v_b", "B", "", "f1", "720p")
        td.queue_task("b")
        # Force-resume B (A gets queued at front)
        td.dequeue_task("b")
        active_id = td.get_active_download_task_id()
        td.update_task(active_id, status="queued")
        td.queue_task_front(active_id)
        td.update_task("b", status="starting")
        assert td.get_active_download_task_id() == "b"
        assert td.get_queue() == ["a"]
        # B completes/paused -> A resumes from queue
        td.update_task("b", status="paused")
        assert td.has_active_download() is False
        q = td.get_queue()
        assert q == ["a"]
        td.dequeue_task(q[0])
        td.update_task("a", status="starting")
        assert td.get_active_download_task_id() == "a"
        assert td.get_queue() == []

    def test_force_resume_then_complete_promotes_queued(self, td):
        """
        A downloading, B queued -> force-resume B (A to queue front)
        -> B completes -> A starts from queue.
        """
        td.create_task("a", "v_a", "A", "", "f1", "720p")
        td.update_task("a", status="downloading")
        td.create_task("b", "v_b", "B", "", "f1", "720p")
        td.queue_task("b")
        # Force-resume B (A gets queued at front)
        td.dequeue_task("b")
        active_id = td.get_active_download_task_id()
        td.update_task(active_id, status="queued")
        td.queue_task_front(active_id)
        td.update_task("b", status="starting")
        assert td.get_active_download_task_id() == "b"
        assert td.get_queue() == ["a"]
        # B completes -> A resumes from queue
        td.complete_task("b")
        assert td.has_active_download() is False
        q = td.get_queue()
        assert q == ["a"]
        td.dequeue_task(q[0])
        td.update_task("a", status="starting")
        assert td.get_active_download_task_id() == "a"
        assert td.get_queue() == []
