"""Preflight must explain all stable failures and avoid a live project snapshot."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import sys
import unittest
from unittest import mock
import zipfile

KIT = Path(__file__).resolve().parents[1] / 'kit'
sys.path.insert(0, str(KIT))
import nox_freeze
import nox_update as update
from installer_release_safety import Fixture, inventory


class PreflightCompatibility(Fixture):
    def invoke(self):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            code = update.main(['--root', str(self.book), '--home', str(self.home),
                                '--bundle', str(self.bundle), '--output', str(self.root / 'diagnostics')])
        return code, output.getvalue()

    def test_active_writer_stops_before_book_validation_and_diagnostic_snapshot(self):
        before = inventory(self.book)
        with mock.patch.object(update, 'running_writers', return_value=[{'pid': 321, 'name': 'pythonw.exe'}]), \
                mock.patch.object(update, 'check_project', side_effect=AssertionError('must not inspect live state')):
            code, output = self.invoke()
        self.assertEqual(code, 2)
        self.assertIn('321 pythonw.exe', output)
        self.assertIn('закройте окно Studio', output)
        self.assertEqual(inventory(self.book), before)
        self.assertFalse((self.root / 'diagnostics').exists())

    def test_unknown_code_and_bad_protection_are_both_reported_without_writes(self):
        (self.book / 'a.py').write_text('unknown local change\n', encoding='utf-8')
        (self.book / 'studio/edits.json').write_text('{"parts":{},"segs":{}}', encoding='utf-8')
        before_book, before_home = inventory(self.book), inventory(self.home)
        code, output = self.invoke()
        self.assertEqual(code, 2)
        self.assertIn('неизвестные локальные версии', output)
        self.assertIn('состояние закреплений требует разбора', output)
        self.assertEqual(inventory(self.book), before_book)
        self.assertEqual(inventory(self.home), before_home)
        paths = list((self.root / 'diagnostics').glob('*.zip'))
        self.assertEqual(len(paths), 1)
        with zipfile.ZipFile(paths[0]) as archive:
            report = json.loads(archive.read('preflight.json'))
            self.assertEqual(len(report['issues']), 2)
            self.assertIn('book/a.py', archive.namelist())

    def test_metadata_changed_during_validation_is_not_a_stable_project(self):
        original = nox_freeze.read_protection
        def concurrently_changed(*args, **kwargs):
            state = original(*args, **kwargs)
            (self.book / 'work/settings.json').write_text('{"tempo_pct":123}', encoding='utf-8')
            return state
        before_code = (self.book / 'a.py').read_bytes()
        with mock.patch.object(nox_freeze, 'read_protection', side_effect=concurrently_changed):
            with self.assertRaisesRegex(update.UpdateError, 'изменилось во время проверки'):
                update.check_project(self.book)
        self.assertEqual((self.book / 'a.py').read_bytes(), before_code)
        self.assertFalse((self.home / 'update_backups').exists())


if __name__ == '__main__':
    unittest.main()
