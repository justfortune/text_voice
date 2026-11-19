import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "kit"))
import nox_file_transactions as tx


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "studio"
        self.root.mkdir()
        self.manager = tx.FileTransactions(self.root)
        self.wav = self.root / "takes/s1/s1_t001.wav"
        self.wav.parent.mkdir(parents=True)
        self.wav.write_bytes(b"RIFF-audio-original")
        self.meta = self.wav.with_suffix(".json")
        self.meta.write_bytes(b'{"tid":"t001"}')
        (self.root / "edits.json").write_bytes(b'{"before":true}')
        (self.root / "fixed_parts.json").write_bytes(b'{"parts":[]}')
        self.before = {p: p.read_bytes() for p in (self.wav, self.meta, self.root / "edits.json", self.root / "fixed_parts.json")}
        self.moves = [(self.wav, self.root / "trash/wav"), (self.meta, self.root / "trash/meta")]
        self.docs = {"edits.json": b'{"after":true}', "fixed_parts.json": b'{"parts":[1]}'}
        self.expected = {name: tx.digest(self.root / name) for name in self.docs}

    def assert_original(self):
        for path, content in self.before.items():
            self.assertEqual(path.read_bytes(), content)
        for _, target in self.moves:
            self.assertFalse(target.exists())

    def commit(self):
        return self.manager.commit(self.docs, self.moves, expected=self.expected, undoable=True, uids=["u"])

    def test_success_and_durable_reverse(self):
        op = self.commit()
        self.assertEqual(op["state"], "committed")
        self.assertFalse(self.wav.exists())
        reverse = self.manager.reverse_moves(op["id"])
        docs = {rel: self.before[self.root / rel] for rel in self.docs}
        self.manager.commit(docs, reverse, expected={rel: tx.digest(self.root / rel) for rel in docs}, undo_of=op["id"])
        self.assert_original()
        self.assertEqual(self.manager.pending_undo(), [])

    def test_second_move_error_restores_complete_bundle_and_documents(self):
        original = tx.os.rename
        calls = []
        def fail_second(source, target):
            calls.append((source, target))
            if len(calls) == 2:
                raise PermissionError("locked JSON")
            return original(source, target)
        with patch.object(tx.os, "rename", side_effect=fail_second):
            with self.assertRaises(tx.TransactionError):
                self.commit()
        self.assert_original()

    def test_hash_read_error_after_rename_is_rolled_back(self):
        original = tx.digest
        fault = [False]
        def flaky(path):
            if Path(path) == self.moves[0][1] and Path(path).exists() and not fault[0]:
                fault[0] = True
                raise OSError("transient read failure")
            return original(path)
        with patch.object(tx, "digest", side_effect=flaky):
            with self.assertRaises(tx.TransactionError):
                self.commit()
        self.assert_original()

    def test_restart_recovers_crash_after_first_rename(self):
        original = tx.os.rename
        def crash(source, target):
            original(source, target)
            raise SystemExit("simulated process death")
        with patch.object(tx.os, "rename", side_effect=crash):
            with self.assertRaises(SystemExit):
                self.commit()
        recovered = tx.FileTransactions(self.root).recover()
        self.assertEqual(len(recovered), 1)
        self.assert_original()

    def test_restart_recovers_crash_after_document_replace(self):
        original = tx.atomic_bytes
        crashed = [False]
        def crash(path, data):
            original(path, data)
            if Path(path) == self.root / "edits.json" and not crashed[0]:
                crashed[0] = True
                raise SystemExit("simulated process death")
        with patch.object(tx, "atomic_bytes", side_effect=crash):
            with self.assertRaises(SystemExit):
                self.commit()
        tx.FileTransactions(self.root).recover()
        self.assert_original()

    def test_external_edits_change_refuses_before_moving_media(self):
        (self.root / "edits.json").write_bytes(b"external update")
        with self.assertRaises(tx.TransactionError):
            self.commit()
        self.assertEqual(self.wav.read_bytes(), self.before[self.wav])
        self.assertEqual((self.root / "edits.json").read_bytes(), b"external update")

    def test_reverse_never_overwrites_new_source(self):
        op = self.commit()
        self.wav.write_bytes(b"new recording")
        with self.assertRaises(tx.TransactionError):
            self.manager.commit({"edits.json": self.before[self.root / "edits.json"]}, self.manager.reverse_moves(op["id"]))
        self.assertEqual(self.wav.read_bytes(), b"new recording")
        self.assertEqual(self.moves[0][1].read_bytes(), self.before[self.wav])

    def test_invalid_paths_and_operation_ids_are_rejected(self):
        for op_id in ("../escape", "/tmp/escape", "", "abc"):
            with self.subTest(op_id=op_id):
                if not op_id:  # empty requests use a generated ID by contract
                    continue
                with self.assertRaises(tx.TransactionError):
                    self.manager.commit({}, op_id=op_id)
        with self.assertRaises(tx.TransactionError):
            self.manager.commit({}, [(self.wav, self.root.parent / "outside.wav")])
        self.assertTrue(self.wav.exists())


if __name__ == "__main__":
    unittest.main()
