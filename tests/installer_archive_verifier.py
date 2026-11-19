"""Retention/recovery and actual verifier publication block, without speech models."""
import ast
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

KIT = Path(__file__).resolve().parents[1] / 'kit'
sys.path.insert(0, str(KIT))
import nox_audio_archive as archive
import nox_file_transactions as ft
import nox_freeze as freeze
import nox_release_io as release
import nox_update as update


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.wavs = self.root / 'work/wav'
        self.wavs.mkdir(parents=True)
        self.before = {'abc.wav': b'old audio', 'abc.txt': b'old technical text', 'abc.json': b'{"source":"old"}'}
        for name, data in self.before.items():
            (self.wavs / name).write_bytes(data)

    def test_bundle_is_retained_with_original_text_and_hashes(self):
        target = archive.archive_stem(self.root, 'abc', 'new edition', target_row={'text': 'new text'})
        doc = json.loads((target / 'manifest.json').read_text())
        self.assertEqual(doc['source_tech'], 'old technical text')
        self.assertEqual(doc['state'], 'archived')
        for item in doc['files']:
            self.assertEqual(release.digest(target / item['name']), item['sha256'])
            self.assertFalse((self.wavs / item['name']).exists())

    def test_row_locked_before_archive_is_left_untouched(self):
        self.assertIsNone(archive.archive_stem(self.root, 'abc', 'new', precondition=lambda: False))
        self.assertEqual({p.name: p.read_bytes() for p in self.wavs.iterdir()}, self.before)

    def interrupted(self):
        dest = archive.archive_stem(self.root, 'abc', 'test')
        jp = dest / 'manifest.json'
        doc = json.loads(jp.read_text())
        doc['state'] = 'prepared'
        jp.write_text(json.dumps(doc))
        os.replace(dest / 'abc.json', self.wavs / 'abc.json')
        return dest, jp

    def test_recover_partial_move_restores_entire_bundle(self):
        dest, jp = self.interrupted()
        self.assertEqual(archive.recover_archives(self.root), 1)
        self.assertEqual({p.name: p.read_bytes() for p in self.wavs.iterdir()}, self.before)
        self.assertEqual(json.loads(jp.read_text())['state'], 'rolled_back')
        self.assertEqual(archive.recover_archives(self.root), 0)

    def test_corrupt_pending_bundle_restores_nothing(self):
        dest, jp = self.interrupted()
        (dest / 'abc.wav').write_bytes(b'corrupt')
        before = {p.name: p.read_bytes() for p in self.wavs.iterdir()}
        with self.assertRaises(RuntimeError):
            archive.recover_archives(self.root)
        self.assertEqual({p.name: p.read_bytes() for p in self.wavs.iterdir()}, before)
        self.assertTrue((dest / 'abc.txt').exists())

    def test_unfinished_code_update_is_detected_without_repair(self):
        journal = self.root / 'update_backups/rev/update.json'
        journal.parent.mkdir(parents=True)
        journal.write_text('{"kind":"nox_code_update","state":"installing"}')
        before = journal.read_bytes()
        self.assertEqual(update.unfinished_updates(self.root), [journal])
        self.assertEqual(journal.read_bytes(), before)


class VerifierPublicationTests(unittest.TestCase):
    """Execute the unchanged AST of the real post-Whisper publication section.

    The inference itself is outside this probe. Every WAV/sidecar operation,
    common process lock and archival call is the implementation under test.
    """
    @classmethod
    def setUpClass(cls):
        tree = ast.parse((KIT / 'verify_k1a.py').read_text())
        defs = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in ('sig_of', '_put_gaps')]
        candidates = [node for node in ast.walk(tree) if isinstance(node, ast.With) and
                      'directory_lock' in ast.unparse(node.items[0].context_expr) and
                      any(isinstance(child, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'current_window' for t in child.targets) for child in node.body)]
        assert len(candidates) == 1
        loop = ast.For(target=ast.Name(id='r', ctx=ast.Store()), iter=ast.List(elts=[ast.Name(id='row', ctx=ast.Load())], ctx=ast.Load()), body=[copy.deepcopy(candidates[0])], orelse=[])
        cls.code = compile(ast.fix_missing_locations(ast.Module(body=defs + [loop], type_ignores=[])), 'actual_verifier_publication', 'exec')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); (self.root / 'work/wav').mkdir(parents=True)
        self.p = self.root / 'work/wav/key.wav'; self.p.write_bytes(b'ORIGINAL-WAV')
        self.p.with_suffix('.txt').write_text('Original text')
        self.row = {'id': '1', 'chapter': '1', 'speaker': 'narrator', 'text': 'Original text'}
        n = types.SimpleNamespace(load_rows=lambda: [dict(self.row)], load_roles=lambda: {},
                                  fixed_set=lambda rows: set(), by_parts=lambda: True,
                                  work_window=lambda rows, roles: (0, 0, 1, False))
        stat = self.p.stat()
        self.env = dict(Path=Path, json=json, os=os, n=n, ROOT=self.root, _i=0, row=self.row, p=self.p,
                        sg=f'{stat.st_size}:{stat.st_mtime_ns}', score=0.1, gaps=[], good=False,
                        why='too short', expected='Original text', heard='', checked=0, did=0,
                        rejected=0, accepted_best=0, max_tries=3, SUSP=self.root / 'work/suspicious.txt',
                        okf=self.p.with_suffix('.ok'), OKV='v6', side=self.p.with_suffix('.txt'),
                        ok_cache={}, log=lambda text: None, preserve_pipeline_bundle=freeze.preserve_pipeline_bundle,
                        directory_lock=ft.directory_lock, atomic_copy=release.atomic_copy, atomic_write=release.atomic_write)

    def run_publication(self):
        exec(self.code, self.env)

    def test_new_lock_after_inference_prevents_rejection(self):
        self.env['n'].fixed_set = lambda rows: {0}
        self.run_publication()
        self.assertEqual(self.p.read_bytes(), b'ORIGINAL-WAV')
        self.assertEqual(self.env['rejected'], 0)
        self.assertFalse(list(self.p.parent.glob('*.rej*')))

    def test_closed_part_after_inference_prevents_rejection(self):
        self.env['n'].work_window = lambda rows, roles: (0, 0, 1, True)
        self.run_publication()
        self.assertTrue(self.p.is_file()); self.assertEqual(self.env['checked'], 0)

    def test_changed_recording_after_inference_is_never_rejected(self):
        self.p.write_bytes(b'NEW-GOOD-RECORDING')
        self.run_publication()
        self.assertEqual(self.p.read_bytes(), b'NEW-GOOD-RECORDING')
        self.assertEqual(self.env['checked'], 0)

    def test_rejection_keeps_original_wav_and_technical_text_in_vault(self):
        self.run_publication()
        self.assertFalse(self.p.exists())
        old = self.root / 'work/retained_audio' / hashlib.sha256(b'ORIGINAL-WAV').hexdigest()
        self.assertEqual((old / 'key.wav').read_bytes(), b'ORIGINAL-WAV')
        side = list((old / 'sidecar_versions').glob('*/key.txt'))
        self.assertEqual(len(side), 1); self.assertEqual(side[0].read_text(), 'Original text')
        self.assertEqual(self.env['rejected'], 1)

    def test_best_retry_replacement_keeps_the_displaced_original(self):
        retry = self.p.with_name('key.rej1.wav'); retry.write_bytes(b'BETTER-WAV')
        retry.with_suffix('.json').write_text('{"score":0.9,"gaps":[]}')
        self.env['max_tries'] = 2
        self.run_publication()
        self.assertEqual(self.p.read_bytes(), b'BETTER-WAV')
        old = self.root / 'work/retained_audio' / hashlib.sha256(b'ORIGINAL-WAV').hexdigest()
        self.assertEqual((old / 'key.wav').read_bytes(), b'ORIGINAL-WAV')
        self.assertEqual(self.env['accepted_best'], 1)

    def test_failed_preservation_cannot_remove_current_audio(self):
        self.env['preserve_pipeline_bundle'] = mock.Mock(side_effect=OSError('disk full'))
        with self.assertRaises(OSError): self.run_publication()
        self.assertEqual(self.p.read_bytes(), b'ORIGINAL-WAV')
        self.assertTrue(self.p.with_suffix('.txt').is_file())


if __name__ == '__main__':
    unittest.main()
