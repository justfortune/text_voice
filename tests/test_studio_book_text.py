# -*- coding: utf-8 -*-
"""Regression tests for the pre-audio manuscript editor API."""
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "kit"))
import nox_studio as studio


def make_book():
    s = studio.Studio.__new__(studio.Studio)
    s.lock = threading.RLock()
    s.title = "Посох Забвения"
    s.author = "Автор"
    s.rows = [
        {"id": "1", "para": 1, "chapter": 1, "ch_title": "Глава I", "kind": "head",
         "speaker": "narrator", "conf": "high", "text": "Глава I"},
        {"id": "2", "para": 2, "chapter": 1, "ch_title": "Глава I", "kind": "speech",
         "speaker": "голос", "conf": "low", "text": "Первая реплика"},
        {"id": "3", "para": 3, "chapter": 1, "ch_title": "Глава I", "kind": "speech",
         "speaker": "Голос", "conf": "none", "text": "Вторая реплика"},
        {"id": "4", "para": 4, "chapter": 2, "ch_title": "Глава II", "kind": "narr",
         "speaker": "narrator", "conf": "mid", "text": "Третья строка"},
    ]
    s.uids = ["u1", "u2", "u3", "u4"]
    s.base_roles = ["narrator", "голос", "Голос", "narrator"]
    s.chapters = {1: (0, 2, "Глава I"), 2: (3, 3, "Глава II")}
    s.roles = {"roles": {"narrator": {}, "голос": {}, "Голос": {}}}
    s._effective = [
        {"role": "narrator", "tech": "Глава I", "tech_src": "base", "locked": False},
        {"role": "голос", "tech": "Первая реплика", "tech_src": "base", "locked": False},
        {"role": "Голос", "tech": "Вторая реплика", "tech_src": "manual", "locked": False},
        {"role": "narrator", "tech": "Третья строка", "tech_src": "base", "locked": False},
    ]
    s.load_rows = lambda: False
    s.eff = lambda i: dict(s._effective[i])
    s.row_text = lambda i: s.rows[i]["text"]
    return s


class BookTextViewTests(unittest.TestCase):
    def test_whole_book_paginates_without_creating_audio_parts(self):
        s = make_book()
        result = studio.Studio.book_text_view(s, limit=2)
        self.assertEqual(4, result["total_rows"])
        self.assertEqual(4, result["result_count"])
        self.assertEqual(["u1", "u2"], [r["uid"] for r in result["rows"]])
        self.assertTrue(result["text_only"])
        self.assertTrue(result["editable"])
        self.assertNotIn("parts", result)
        self.assertEqual(4, len(s.rows))

    def test_page_offset_is_clamped_and_chapter_filter_is_respected(self):
        s = make_book()
        result = studio.Studio.book_text_view(s, chapter=1, offset=80, limit=2)
        self.assertEqual(1, result["chapter"])
        self.assertEqual(3, result["result_count"])
        self.assertEqual(["u3"], [r["uid"] for r in result["rows"]])
        self.assertEqual(2, result["offset"])

    def test_role_filter_is_case_sensitive(self):
        s = make_book()
        lower = studio.Studio.book_text_view(s, role_filter="голос")
        upper = studio.Studio.book_text_view(s, role_filter="Голос")
        self.assertEqual(["u2"], [r["uid"] for r in lower["rows"]])
        self.assertEqual(["u3"], [r["uid"] for r in upper["rows"]])
        self.assertIn("голос", lower["roles"])
        self.assertIn("Голос", lower["roles"])

    def test_confidence_and_text_search_filters_compose(self):
        s = make_book()
        result = studio.Studio.book_text_view(s, query="реплика", confidence="low")
        self.assertEqual(["u2"], [r["uid"] for r in result["rows"]])

    def test_search_matches_text_and_is_case_insensitive(self):
        s = make_book()
        result = studio.Studio.book_text_view(s, query="ВТОРАЯ РЕПЛИКА")
        self.assertEqual(["u3"], [r["uid"] for r in result["rows"]])

    def test_invalid_filters_are_rejected(self):
        s = make_book()
        with self.assertRaises(studio.Oops):
            studio.Studio.book_text_view(s, chapter=99)
        with self.assertRaises(studio.Oops):
            studio.Studio.book_text_view(s, confidence="critical")
        with self.assertRaises(studio.Oops):
            studio.Studio.book_text_view(s, role_filter="missing")

    def test_view_does_not_mutate_roles_or_source_rows(self):
        s = make_book()
        before_rows = [dict(r) for r in s.rows]
        before_roles = {k: dict(v) for k, v in s.roles["roles"].items()}
        studio.Studio.book_text_view(s)
        self.assertEqual(before_rows, s.rows)
        self.assertEqual(before_roles, s.roles["roles"])


if __name__ == "__main__":
    unittest.main()
