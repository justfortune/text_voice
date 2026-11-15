# -*- coding: utf-8 -*-
"""Regression contract for Nox Studio's editor-only safe-start mode."""
import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATH = ROOT / "kit" / "nox_studio.py"
SOURCE = SOURCE_PATH.read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


class StudioSafeStartTests(unittest.TestCase):
    def test_source_parses(self):
        self.assertTrue(TREE.body)

    def test_safe_start_cli_flag_is_present(self):
        self.assertIn(
            'ap.add_argument("--safe-start", action="store_true"',
            SOURCE,
        )

    def test_safe_mode_uses_idle_watch_instead_of_engine_tick(self):
        self.assertIn(
            "threading.Thread(target=safe_idle_watch, args=(S,), daemon=True).start()",
            SOURCE,
        )
        self.assertIn(
            "else:\n            threading.Thread(target=S.tick, daemon=True).start()",
            SOURCE,
        )

    def test_safe_mode_skips_automatic_startup_housekeeping(self):
        self.assertIn(
            "if not a.safe_start:\n            threading.Thread(target=S.startup, daemon=True).start()",
            SOURCE,
        )

    def test_safe_idle_watch_exits_after_window_close(self):
        fn = next(
            node for node in TREE.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "safe_idle_watch"
        )
        calls = [
            node for node in ast.walk(fn)
            if isinstance(node, ast.Call)
        ]
        call_names = []
        for node in calls:
            if isinstance(node.func, ast.Attribute):
                call_names.append(node.func.attr)
        self.assertIn("_exit", call_names)
        self.assertNotIn("poll_jobs", call_names)
        self.assertNotIn("engine_start", call_names)


if __name__ == "__main__":
    unittest.main()
