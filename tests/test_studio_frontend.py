# -*- coding: utf-8 -*-
"""Frontend regression contracts and JavaScript syntax validation."""
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "kit" / "nox_studio.html"


class StudioFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = HTML.read_text(encoding="utf-8")
        scripts = re.findall(r"<script(?:\\s[^>]*)?>([\\s\\S]*?)</script>", cls.html, re.I)
        cls.scripts = [x for x in scripts if x.strip()]

    def test_inline_javascript_parses(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js не установлен; CI устанавливает его отдельно")
        self.assertTrue(self.scripts, "в HTML не найден встроенный JavaScript")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "studio.js"
            path.write_text("\\n".join(self.scripts), encoding="utf-8")
            p = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
            self.assertEqual(0, p.returncode, p.stdout + p.stderr)

    def test_microphone_recording_uses_browser_capture_and_existing_take_pipeline(self):
        for token in (
            "navigator.mediaDevices.getUserMedia",
            "new MediaRecorder",
            "rec.start(500)",
            "rec.stop()",
            "micCancel()",
            "await ownRecord(uid,file)",
            'const SND_RE = /\\.(wav|webm|mp3',
            'on("#ownMic", "click", () => recordOwn(s.uid, "phrase"))',
        ):
            self.assertIn(token, self.html)

    def test_paragraph_recording_is_explicit_and_rolls_back_partial_join(self):
        for token in (
            'scope === "para"',
            'api("/api/join",{uid:headUid})',
            'api("/api/unjoin",{uid:headUid})',
            "не удалось автоматически отменить соединение абзаца",
            "Исходные записи фраз сохранены",
        ):
            self.assertIn(token, self.html)

    def test_microphone_dialog_has_bounded_duration_and_cancel_path(self):
        self.assertIn("sec>=180", self.html)
        self.assertIn("Запись микрофона отменена", self.html)
        self.assertIn("NotAllowedError", self.html)
        self.assertIn("NotFoundError", self.html)


if __name__ == "__main__":
    unittest.main()
