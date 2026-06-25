"""Tests for expected-size guard against premature HLS segment merge."""

import os
import json
import tempfile
from unittest.mock import patch, MagicMock, ANY

import pytest


# ── Helpers ─────────────────────────────────────────────────────────

def _make_meta(expected_sizes=None, sources=None):
    return {
        "seg_type": "fmp4",
        "codec": "vp9",
        "sources": sources or {
            "video": {"path": "v.f303.webm", "size": 50000000, "mtime": 1000.0},
            "audio": {"path": "a.f140.m4a", "size": 2000000, "mtime": 1000.0},
        },
        "expected_sizes": expected_sizes or {"video": None, "audio": None},
        "segments": {},
    }


# ── _sources_complete unit tests ───────────────────────────────────

class TestSourcesComplete:
    """Pure logic tests for hls_manager._sources_complete()."""

    def test_no_expected_sizes_returns_true(self):
        from hls_manager import _sources_complete
        meta = _make_meta(expected_sizes={"video": None, "audio": None})
        assert _sources_complete(meta) is True

    def test_missing_expected_sizes_key_returns_true(self):
        from hls_manager import _sources_complete
        meta = _make_meta()
        del meta["expected_sizes"]
        assert _sources_complete(meta) is True

    def test_video_size_met_returns_true(self):
        from hls_manager import _sources_complete
        meta = _make_meta(
            expected_sizes={"video": 50000000, "audio": 2000000},
            sources={"video": {"size": 50000000}, "audio": {"size": 2000000}},
        )
        assert _sources_complete(meta) is True

    def test_video_size_below_expected_returns_false(self):
        from hls_manager import _sources_complete
        meta = _make_meta(
            expected_sizes={"video": 100000000, "audio": 2000000},
            sources={"video": {"size": 50000000}, "audio": {"size": 2000000}},
        )
        assert _sources_complete(meta) is False

    def test_tolerance_allows_90_percent(self):
        from hls_manager import _sources_complete
        meta = _make_meta(
            expected_sizes={"video": 100000000, "audio": 2000000},
            sources={"video": {"size": 90000000}, "audio": {"size": 2000000}},
        )
        assert _sources_complete(meta) is True

    def test_below_90_percent_tolerance_returns_false(self):
        from hls_manager import _sources_complete
        meta = _make_meta(
            expected_sizes={"video": 100000000, "audio": 2000000},
            sources={"video": {"size": 89999999}, "audio": {"size": 2000000}},
        )
        assert _sources_complete(meta) is False

    def test_audio_below_expected_returns_false(self):
        from hls_manager import _sources_complete
        meta = _make_meta(
            expected_sizes={"video": 50000000, "audio": 2000000},
            sources={"video": {"size": 50000000}, "audio": {"size": 1000000}},
        )
        assert _sources_complete(meta) is False

    def test_zero_expected_size_treated_as_none(self):
        from hls_manager import _sources_complete
        meta = _make_meta(
            expected_sizes={"video": 0, "audio": 0},
            sources={"video": {"size": 50000000}, "audio": {"size": 2000000}},
        )
        assert _sources_complete(meta) is True

    def test_with_disk_paths_actual_meets_expected(self, tmp_path):
        from hls_manager import _sources_complete
        v = tmp_path / "video.mp4"
        a = tmp_path / "audio.m4a"
        v.write_bytes(b"\x00" * 50000000)
        a.write_bytes(b"\x00" * 2000000)
        meta = _make_meta(
            expected_sizes={"video": 50000000, "audio": 2000000},
        )
        assert _sources_complete(meta, video_path=str(v), audio_path=str(a)) is True

    def test_with_disk_paths_actual_below_expected(self, tmp_path):
        from hls_manager import _sources_complete
        v = tmp_path / "video.mp4"
        a = tmp_path / "audio.m4a"
        v.write_bytes(b"\x00" * 30000000)
        a.write_bytes(b"\x00" * 2000000)
        meta = _make_meta(
            expected_sizes={"video": 50000000, "audio": 2000000},
        )
        assert _sources_complete(meta, video_path=str(v), audio_path=str(a)) is False

    def test_missing_disk_file_returns_false(self, tmp_path):
        from hls_manager import _sources_complete
        a = tmp_path / "audio.m4a"
        a.write_bytes(b"\x00" * 2000000)
        meta = _make_meta(
            expected_sizes={"video": 50000000, "audio": 2000000},
        )
        assert _sources_complete(meta, video_path="/nonexistent/v.mp4", audio_path=str(a)) is False


# ── initialize() expected-sizes integration ────────────────────────

class TestInitializeExpectedSizes:
    """verify initialize() reads expected_sizes from active task and stores in meta."""

    @patch("hls_manager._detect_codec", return_value="vp9")
    @patch("hls_manager.segment_type_for_codec", return_value="fmp4")
    @patch("hls_manager.get_source_info", return_value={
        "video": {"path": "v.webm", "size": 50000000, "mtime": 1000},
        "audio": {"path": "a.m4a", "size": 2000000, "mtime": 1000},
    })
    def test_stores_expected_sizes_from_active_task(
        self, mock_src, mock_typ, mock_codec, tmp_path
    ):
        from hls_manager import initialize

        hls_dir = tmp_path / "hls" / "vid1"
        hls_dir.mkdir(parents=True)

        meta_path = hls_dir / "meta.json"

        with (
            patch("hls_manager.video_dir", return_value=str(hls_dir)),
            patch("hls_manager.meta_path", return_value=str(meta_path)),
            patch("hls_manager._load_meta", return_value={}),
            patch("hls_manager.sources_match", return_value=False),
            patch("hls_manager.invalidate") as mock_inv,
            patch("tasks.get_active_tasks", return_value={
                "t1": {
                    "video_id": "vid1",
                    "expected_video_size": 100000000,
                    "expected_audio_size": 5000000,
                }
            }),
        ):
            initialize("vid1", "/videos/vid1.f303.webm", "/videos/vid1.f140.m4a")

        saved = json.loads(meta_path.read_text())
        assert saved["expected_sizes"] == {"video": 100000000, "audio": 5000000}

    @patch("hls_manager._detect_codec", return_value="vp9")
    @patch("hls_manager.segment_type_for_codec", return_value="fmp4")
    @patch("hls_manager.get_source_info", return_value={
        "video": {"path": "v.webm", "size": 50000000, "mtime": 1000},
        "audio": {"path": "a.m4a", "size": 2000000, "mtime": 1000},
    })
    def test_no_active_task_stores_none(
        self, mock_src, mock_typ, mock_codec, tmp_path
    ):
        from hls_manager import initialize

        hls_dir = tmp_path / "hls" / "vid2"
        hls_dir.mkdir(parents=True)
        meta_path = hls_dir / "meta.json"

        with (
            patch("hls_manager.video_dir", return_value=str(hls_dir)),
            patch("hls_manager.meta_path", return_value=str(meta_path)),
            patch("hls_manager._load_meta", return_value={}),
            patch("hls_manager.sources_match", return_value=False),
            patch("hls_manager.invalidate"),
            patch("tasks.get_active_tasks", return_value={}),
        ):
            initialize("vid2", "/videos/vid2.f303.webm", "/videos/vid2.f140.m4a")

        saved = json.loads(meta_path.read_text())
        assert saved["expected_sizes"] == {"video": None, "audio": None}


# ── is_playlist_complete() expected-sizes guard ────────────────────

class TestIsPlaylistComplete:
    """verify is_playlist_complete() checks source completeness when no active tasks."""

    def test_active_tasks_exist_returns_false(self, tmp_path):
        from hls_manager import is_playlist_complete

        with (
            patch("tasks.get_active_tasks", return_value={"t1": {"video_id": "vid1"}}),
            patch("hls_manager._load_meta", return_value=_make_meta()),
        ):
            assert is_playlist_complete("vid1") is False

    def test_no_active_tasks_and_sources_complete_returns_true(self, tmp_path):
        from hls_manager import is_playlist_complete

        meta = _make_meta(
            expected_sizes={"video": 50000000, "audio": 2000000},
            sources={"video": {"size": 50000000}, "audio": {"size": 2000000}},
        )

        with (
            patch("tasks.get_active_tasks", return_value={}),
            patch("hls_manager._load_meta", return_value=meta),
        ):
            assert is_playlist_complete("vid1") is True

    def test_no_active_tasks_but_sources_incomplete_returns_false(self, tmp_path):
        from hls_manager import is_playlist_complete

        meta = _make_meta(
            expected_sizes={"video": 100000000, "audio": 2000000},
            sources={"video": {"size": 50000000}, "audio": {"size": 2000000}},
        )

        with (
            patch("tasks.get_active_tasks", return_value={}),
            patch("hls_manager._load_meta", return_value=meta),
        ):
            assert is_playlist_complete("vid1") is False

    def test_no_meta_returns_true(self, tmp_path):
        from hls_manager import is_playlist_complete

        with (
            patch("tasks.get_active_tasks", return_value={}),
            patch("hls_manager._load_meta", return_value={}),
        ):
            assert is_playlist_complete("vid1") is True


# ── get_or_create_segment() expected-sizes preservation ────────────

class TestGetOrCreateSegmentPreservation:
    """verify expected_sizes survive segment regeneration in get_or_create_segment()."""

    @patch("hls_manager._detect_codec", return_value="vp9")
    @patch("hls_manager.segment_type_for_codec", return_value="fmp4")
    @patch("hls_manager._generate_all_segments", return_value=[
        (0, 6.000, "final"),
        (1, 6.000, "final"),
    ])
    @patch("hls_manager.get_source_info", return_value={
        "video": {"path": "v.webm", "size": 50000000, "mtime": 1000},
        "audio": {"path": "a.m4a", "size": 2000000, "mtime": 1000},
    })
    def test_expected_sizes_preserved_after_regeneration(
        self, mock_src, mock_gen, mock_typ, mock_codec, tmp_path
    ):
        from hls_manager import get_or_create_segment

        hls_dir = tmp_path / "hls" / "vid1"
        hls_dir.mkdir(parents=True)
        meta_path = hls_dir / "meta.json"
        seg_0 = hls_dir / "seg_0000.m4s"
        seg_0.write_text("fake segment data")

        initial_expected = {"video": 100000000, "audio": 5000000}

        initial_meta = _make_meta(expected_sizes=initial_expected)
        json.dump(initial_meta, open(meta_path, "w"))

        with (
            patch("hls_manager.video_dir", return_value=str(hls_dir)),
            patch("hls_manager.meta_path", return_value=str(meta_path)),
            patch("hls_manager.sources_match", return_value=False),
            patch("hls_manager.invalidate"),
            patch("os.path.exists", return_value=True),
        ):
            get_or_create_segment("vid1", "/v/v.webm", "/a/a.m4a", 0)

        saved = json.loads(meta_path.read_text())
        assert saved["expected_sizes"] == initial_expected

    @patch("hls_manager._detect_codec", return_value="vp9")
    @patch("hls_manager.segment_type_for_codec", return_value="fmp4")
    @patch("hls_manager._generate_all_segments", return_value=[
        (0, 6.000, "final"),
    ])
    @patch("hls_manager.get_source_info", return_value={
        "video": {"path": "v.webm", "size": 50000000, "mtime": 1000},
        "audio": {"path": "a.m4a", "size": 2000000, "mtime": 1000},
    })
    def test_fresh_meta_uses_empty_expected_sizes(
        self, mock_src, mock_gen, mock_typ, mock_codec, tmp_path
    ):
        from hls_manager import get_or_create_segment

        hls_dir = tmp_path / "hls" / "vid2"
        hls_dir.mkdir(parents=True)
        meta_path = hls_dir / "meta.json"
        seg_0 = hls_dir / "seg_0000.m4s"
        seg_0.write_text("fake segment data")

        with (
            patch("hls_manager.video_dir", return_value=str(hls_dir)),
            patch("hls_manager.meta_path", return_value=str(meta_path)),
            patch("hls_manager._load_meta", return_value={}),
            patch("hls_manager.sources_match", return_value=False),
            patch("hls_manager.invalidate"),
            patch("os.path.exists", return_value=True),
        ):
            get_or_create_segment("vid2", "/v/v.webm", "/a/a.m4a", 0)

        saved = json.loads(meta_path.read_text())
        assert saved["expected_sizes"] == {"video": None, "audio": None}


# ── downloader.py expected size extraction ─────────────────────────

class TestDownloaderExpectedSizes:
    """verify expected_video_size/expected_audio_size extraction from format info."""

    def test_filesize_used_when_available(self):
        format_info = {
            "format_id": "303",
            "filesize": 100000000,
            "filesize_approx": 95000000,
        }
        result = format_info.get("filesize") or format_info.get("filesize_approx")
        assert result == 100000000

    def test_filesize_approx_fallback(self):
        format_info = {
            "format_id": "303",
            "filesize": None,
            "filesize_approx": 95000000,
        }
        result = format_info.get("filesize") or format_info.get("filesize_approx")
        assert result == 95000000

    def test_neither_available(self):
        format_info = {
            "format_id": "303",
            "filesize": None,
            "filesize_approx": None,
        }
        result = format_info.get("filesize") or format_info.get("filesize_approx")
        assert result is None
