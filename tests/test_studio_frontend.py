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
        scripts = re.findall(r"<script(?:\s[^>]*)?>([\s\S]*?)</script>", cls.html, re.I)
        cls.scripts = [x for x in scripts if x.strip()]

    def test_inline_javascript_parses(self):
        node = shutil.which("node")
        if not node:
            raise unittest.SkipTest("Node.js не установлен; CI устанавливает его отдельно")
        self.assertTrue(self.scripts, "в HTML не найден встроенный JavaScript")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "studio.js"
            path.write_text("\n".join(self.scripts), encoding="utf-8")
            p = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
            self.assertEqual(0, p.returncode, p.stdout + p.stderr)

    def test_microphone_capture_uses_existing_take_pipeline(self):
        for token in (
            "navigator.mediaDevices.getUserMedia",
            "new MediaRecorder",
            "rec.start(500)",
            "rec.stop()",
            "micCancel()",
            "await ownRecord(uid,file)",
            r"const SND_RE = /\.(wav|webm|mp3",
            'on("#ownMic", "click", () => recordOwn(s.uid))',
        ):
            self.assertIn(token, self.html)

    def test_microphone_has_level_meter_and_preview_before_upload(self):
        for token in (
            "createMediaStreamSource(stream)",
            "getByteTimeDomainData(data)",
            'id="micMeter"',
            'id="micLevel"',
            "Прослушайте запись",
            "micReviewClear()",
            "Сохранить дубль",
            "Перезаписать",
        ):
            self.assertIn(token, self.html)
        self.assertLess(self.html.index("Прослушайте запись"), self.html.index("await ownRecord(uid,file)"))

    def test_microphone_dialog_has_bounded_duration_and_cancel_path(self):
        self.assertIn("sec>=180", self.html)
        self.assertIn("Запись микрофона отменена", self.html)
        self.assertIn("NotAllowedError", self.html)
        self.assertIn("NotFoundError", self.html)
        self.assertIn("audioCtx.close()", self.html)

    def test_paragraph_recording_is_not_exposed_until_transactional_backend_exists(self):
        self.assertNotIn('recordOwn(s.uid, "para")', self.html)
        self.assertNotIn("ownParagraphRecord(", self.html)


if __name__ == "__main__":
    unittest.main()
