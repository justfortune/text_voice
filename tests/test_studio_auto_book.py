# -*- coding: utf-8 -*-
"""Regression tests for the book-wide automatic speech state machine."""
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "kit"))
import nox_studio as studio


def make_completed_book(pending_chapters=()):
    s = studio.Studio.__new__(studio.Studio)
    s.lock = threading.RLock()
    s.rows = [{"id": 1, "kind": "text", "text": "sample"}]
    s.store = {
        "parts": [{"locked": "2026-11-13T00:00:00"}],
        "chapters": {},
        "mix": {},
    }
    s.auto = dict(studio.Studio.AUTO_DEF)
    s.auto.update(state="run", part=1, step="finalizing", secs=0.0)
    s.chapters = {1: (0, 0, "Chapter 1")}
    s._auto_t0 = None
    s._auto_rows0 = 0
    s.ch_busy = None
    s.final_ch = Path("unused-final-chapters")
    s.final_ch_mix = Path("unused-final-chapters-mix")
    s.plan = lambda: [{"n": 1, "a": 0, "b": 0, "opened": True}]
    s.load_rows = lambda: None
    s.files_missing = lambda _export: []
    s.export_stale = lambda *_args: False
    s.chapters_due = lambda: list(pending_chapters)
    s._auto_chapters = lambda final=False: None
    s.disk_sync = lambda: 0
    s._auto_acc = lambda: None
    s.auto_save = lambda: None
    s.kit_score = lambda: None
    s.disk_dir = lambda *_args: None
    s._locked_rows = lambda _pl: 1
    return s


class AutoBookCompletionTests(unittest.TestCase):
    def test_view_is_not_done_when_final_chapter_export_is_missing(self):
        s = make_completed_book([1])
        view = s.auto_view()
        self.assertTrue(view["parts_done"])
        self.assertFalse(view["done"])

    def test_view_is_done_only_when_all_final_chapters_are_current(self):
        s = make_completed_book([])
        self.assertTrue(s.auto_view()["done"])

    def test_step_does_not_finish_when_chapter_export_remains_pending(self):
        s = make_completed_book([1])
        sync_calls = []
        s.disk_sync = lambda: sync_calls.append(True) or 0

        with patch.object(studio, "log"):
            with self.assertRaisesRegex(studio.Oops, "главы не собраны или устарели"):
                s._auto_step()

        self.assertEqual("run", s.auto["state"])
        self.assertEqual([], sync_calls)

    def test_pause_during_final_chapter_build_is_not_overwritten_by_done(self):
        s = make_completed_book([])
        sync_calls = []

        def pause_during_build(final=False):
            self.assertTrue(final)
            s.auto["state"] = "pause"

        s._auto_chapters = pause_during_build
        s.disk_sync = lambda: sync_calls.append(True) or 0

        with patch.object(studio, "log"):
            self.assertFalse(s._auto_step())

        self.assertEqual("pause", s.auto["state"])
        self.assertEqual([], sync_calls)

    def test_successful_finalization_marks_done_after_chapters_are_current(self):
        s = make_completed_book([])
        with patch.object(studio, "log"):
            self.assertFalse(s._auto_step())

        self.assertEqual("done", s.auto["state"])
        self.assertEqual("вся книга озвучена, закреплена и собрана по главам", s.auto["step"])


if __name__ == "__main__":
    unittest.main()
