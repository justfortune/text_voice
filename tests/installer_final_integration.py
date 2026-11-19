"""Final integration regressions found by independent review of release paths."""
from __future__ import annotations

from pathlib import Path
import json
import sys
import unittest

KIT = Path(__file__).resolve().parents[1] / "kit"
sys.path.insert(0, str(KIT))
import nox_audio_archive as archive
import nox_update as update
from nox_release_io import Publication
from installer_release_safety import Fixture, inventory


class FinalIntegration(Fixture):
    def app_manifest(self):
        manifest = dict(self.manifest)
        manifest["targets"] = [dict(t, scope="app") for t in self.manifest["targets"]]
        return manifest

    def test_archive_rejects_symlinked_wav_parent_before_moving_external_audio(self):
        outside = self.root / "external_audio"
        outside.mkdir()
        (outside / "a.wav").write_bytes(b"outside-owned-wave")
        (outside / "a.txt").write_text("outside-owned source text", encoding="utf-8")
        try:
            (self.book / "work/wav").symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("symlink creation unavailable")
        before = inventory(outside)
        with self.assertRaises((RuntimeError, ValueError, OSError)):
            archive.archive_stem(self.book, "a", "must not reach external audio")
        self.assertEqual(inventory(outside), before)

    def test_missing_or_changed_immutable_manifest_stops_before_release_records_change(self):
        self.install()
        manifest_path = self.home / "kits/test18d/release_manifest.json"
        for bad in (None, b'{"format":1,"changed":true}'):
            with self.subTest(manifest=bad):
                if bad is None:
                    manifest_path.unlink()
                else:
                    manifest_path.write_bytes(bad)
                before_book, before_home = inventory(self.book), inventory(self.home)
                with self.assertRaises(update.UpdateError):
                    self.install()
                self.assertEqual(inventory(self.book), before_book)
                self.assertEqual(inventory(self.home), before_home)

    def test_source_changed_after_manifest_validation_is_not_accepted_by_bootstrap(self):
        manifest = self.app_manifest()
        (self.bundle / "release_manifest.json").write_text(json.dumps(manifest))
        checked = update.read_manifest(self.bundle)
        (self.bundle / "kit/a.py").write_bytes(b"unverified replacement after manifest check\n")
        before = inventory(self.home / "studio")
        fresh_book = self.root / "fresh_book"
        with self.assertRaises(update.UpdateError):
            update.install_new_app(self.bundle, checked, fresh_book, self.home)
        self.assertEqual(inventory(self.home / "studio"), before)
        self.assertFalse(fresh_book.exists())

    def test_bootstrap_recovers_previous_published_transaction_before_installing(self):
        # A completed publish without commit is the durable state left by a
        # killed process at that boundary, not a mocked helper implementation.
        stage = self.home / "new_app_staging/interrupted"
        stage.mkdir(parents=True)
        prior_source = stage / "a.py"
        prior_source.write_bytes(self.new["a.py"])
        previous = Publication(self.home / "new_app_transactions",
                               [(prior_source, self.home / "studio/a.py")], allowed_root=self.home)
        previous.publish()
        self.assertEqual(json.loads(previous.path.read_text())["state"], "published")
        fresh_book = self.root / "fresh_book"
        manifest = self.app_manifest()
        update.install_new_app(self.bundle, manifest, fresh_book, self.home)
        self.assertEqual(json.loads(previous.path.read_text())["state"], "rolled_back")
        for name, data in self.new.items():
            self.assertEqual((self.home / "studio" / name).read_bytes(), data)
        self.assertTrue((self.home / "studio/nox_release.json").is_file())
        self.assertEqual(update.unfinished_updates(self.home), [])
        update.verify_installed(self.bundle, manifest, fresh_book, self.home, "app")
        self.assertFalse(fresh_book.exists())


if __name__ == "__main__":
    unittest.main()
