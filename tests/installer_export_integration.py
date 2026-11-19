"""Actual Studio export integration with temporary media and a fake encoder.

The export/publish/mix orchestration, journal, disk copy and baseline acceptance
methods are real. Only PCM generation/encoding and project loading are replaced;
these tests do not claim GPU, ffmpeg or Windows acceptance.
"""
from __future__ import annotations
import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "kit"))
import nox_studio as studio
import nox_release_io as release


class ExportFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        s = studio.Studio.__new__(studio.Studio); self.s = s
        s.root=self.root; s.dir=self.root/"studio"; s.render_dir=s.dir/"render"
        s.final_parts=self.root/"gotovo/chasti"; s.final_ch=self.root/"gotovo/glavy"
        s.cache_dir=s.dir/"cache"; s.backup_dir=s.dir/"backup"
        for p in (s.dir,s.render_dir,s.final_parts,s.final_ch,s.cache_dir,s.backup_dir):p.mkdir(parents=True,exist_ok=True)
        s.lock=threading.RLock();s.rlock=threading.RLock();s._opn=0;s.ops={}
        s.title="Книга";s.author="Автор";s.fmt="mp3";s.bitrate="96k";s.speed=1.12
        s.uids=["u1","u2","u3"];s.idx_of={u:i for i,u in enumerate(s.uids)}
        s.rows=[dict(id=i+1,ch_title="Глава 1",text="Текст",para=i,chapter=1) for i in range(3)]
        s.chapters={1:(0,1,"Глава 1")};s._abs={};s.base_roles=["narrator"]*3
        s.store=s.store_defaults({"v":1,"segs":{},"parts":[{"last":"u2","export":{"file":"previous"}}],"chapters":{"1":{"export":{"file":"previous chapter"}}}})
        (self.root/"cue.wav").write_bytes(b"fixture sound")
        s.store["cues"]=[dict(id="c1",kind="sfx",uid="u1",file="cue.wav",vol=.7),dict(id="c2",kind="sfx",uid="u3",file="cue.wav",vol=.5)]
        s.cue_file=lambda c:self.root/c["file"];s.cue_ref=lambda c:c["file"]
        s.vars_on=lambda:[v for v in studio.MIX_VARS if v[0]=="all"]
        s.mix_sig=lambda a,b,meta,kinds=None:"fixture-mix"
        s._mix_base=lambda *a,**k:[.1,.2]
        self.mix_calls=[]
        def fake_mix(a,b,meta,voice_file,out,title,sub,voice,kinds=None):
            self.mix_calls.append((a,b,Path(voice_file),Path(out),sub))
            out.parent.mkdir(parents=True,exist_ok=True);out.write_bytes(b"new mixed audio")
            return dict(file=str(out),count=1,sig="fixture-mix",disk=None)
        s._export_mix=fake_mix
        s.open_part=lambda k:{"a":0,"b":1}
        s.plan=lambda:[{"n":1,"a":0,"b":1}]
        s.chapter_range=lambda c:(0,1,"Глава 1")
        s.timeline=lambda a,b,render,op=None:({"ver":"new-ver","sr":24000,"chapters":[[0,"Глава 1"]],"segs":[["u1",0.,5.],["u2",5.,10.]],"total":10.},[.1,.2])
        def encode(audio,sr,chapters,out,title,bitrate=None):out.write_bytes(b"new voice audio");return 10.
        s._encode=encode;s._times_tsv=lambda meta:"new times\n";s._tech_txt=lambda meta,title:"new tech\n"
        self.cloud=self.root/"cloud";self.disk_enabled=False
        def disk_dir(make=True):
            if not self.disk_enabled:return None
            if make:self.cloud.mkdir(parents=True,exist_ok=True)
            return str(self.cloud)
        s.disk_dir=disk_dir
        self.messages=[]
        log_patch=mock.patch.object(studio,"log",side_effect=lambda msg,lvl=None,src="студия":self.messages.append((msg,lvl,src)))
        log_patch.start();self.addCleanup(log_patch.stop)
        rms_patch=mock.patch.object(studio,"active_rms",return_value=.2);rms_patch.start();self.addCleanup(rms_patch.stop)
        self.voice=s.final_parts/"part_01.mp3";self.times=self.voice.with_suffix(".times.tsv");self.tech=self.voice.with_suffix(".tech.txt")
        self.meta=s.render_dir/"part_01.json";self.mix=s.var_dir("chasti","all")/self.voice.name
        self.chapter=s.final_ch/"01 Глава 1.mp3";self.chapter_mix=s.var_dir("glavy","all")/self.chapter.name
        for p in (self.voice,self.times,self.tech,self.meta,self.mix,self.chapter,self.chapter.with_suffix(".tech.txt"),self.chapter_mix):p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(("old:"+p.name).encode())
        (s.dir/"edits.json").write_text(studio.jd(s.store),encoding="utf-8")
        (s.dir/"fixed_parts.json").write_text('{"ver":1,"parts":[]}',encoding="utf-8")
        s._accept_published_state()

    def output_state(self):
        paths=(self.voice,self.times,self.tech,self.meta,self.mix,self.chapter,self.chapter.with_suffix(".tech.txt"),self.chapter_mix,self.s.dir/"edits.json",self.s.dir/"fixed_parts.json")
        return {p:p.read_bytes() for p in paths}

    def assert_baseline(self):
        self.assertEqual(self.s._edits_sha,release.digest(self.s.dir/"edits.json"))
        self.assertEqual(self.s._fixed_sha,release.digest(self.s.dir/"fixed_parts.json"))
        self.assertEqual(self.s._last_durable_store,self.s.store)


class ExportIntegrationTests(ExportFixture):
    def test_successful_part_publishes_voice_sidecars_mix_edits_and_one_dict_journal(self):
        ex=self.s.export_part(1)
        self.assertEqual(self.voice.read_bytes(),b"new voice audio")
        self.assertEqual(self.mix.read_bytes(),b"new mixed audio")
        self.assertEqual(self.times.read_text(),"new times\n");self.assertEqual(self.tech.read_text(),"new tech\n")
        saved=json.loads((self.s.dir/"edits.json").read_text())
        self.assertEqual(saved["parts"][0]["export"]["file"],str(self.voice))
        recs=[json.loads(x) for x in (self.s.dir/"edits.log").read_text().splitlines()]
        self.assertEqual(len(recs),1);self.assertEqual(recs[0]["file"],str(self.voice))
        self.assertEqual(ex["vars"]["all"]["file"],str(self.mix));self.assert_baseline()
        self.assertFalse(list(self.voice.parent.glob(".*.stage-*")))

    def test_mix_publish_failure_rolls_back_voice_sidecars_and_edits(self):
        before=self.output_state();old_store=copy.deepcopy(self.s.store);original=release.os.replace
        def fail_mix(src,dst):
            if Path(dst)==self.mix and "mixes" in Path(src).parts:raise OSError("injected mix publication failure")
            return original(src,dst)
        with mock.patch.object(release.os,"replace",side_effect=fail_mix):
            with self.assertRaisesRegex(OSError,"mix publication"):
                self.s.export_part(1)
        self.assertEqual(self.output_state(),before);self.assertEqual(self.s.store,old_store);self.assert_baseline()

    def test_sidecar_publish_failure_rolls_back_previously_renamed_voice(self):
        before=self.output_state();original=release.os.replace
        def fail_sidecar(src,dst):
            if Path(dst)==self.times and ".stage-" in str(src):raise OSError("injected sidecar publication failure")
            return original(src,dst)
        with mock.patch.object(release.os,"replace",side_effect=fail_sidecar):
            with self.assertRaisesRegex(OSError,"sidecar publication"):
                self.s.export_part(1)
        self.assertEqual(self.output_state(),before);self.assert_baseline()

    def test_mix_encoding_failure_never_publishes_new_voice(self):
        before=self.output_state()
        self.s._export_mix=mock.Mock(side_effect=RuntimeError("encoder failed"))
        with self.assertRaisesRegex(RuntimeError,"encoder failed"):self.s.export_part(1)
        self.assertEqual(self.output_state(),before);self.assert_baseline()

    def test_copied_audio_and_failed_edits_backup_retains_disk_path_and_warning(self):
        self.disk_enabled=True;original=studio.write_atomic
        def fail_edits_backup(path,data):
            if Path(path).name=="edits_backup.json":raise PermissionError("[WinError 32] simulated locked metadata")
            return original(path,data)
        with mock.patch.object(studio,"write_atomic",side_effect=fail_edits_backup):ex=self.s.export_part(1)
        self.assertEqual(Path(ex["disk"]).read_bytes(),b"new voice audio")
        self.assertIn("WinError 32",ex["disk_metadata_error"])
        self.assertTrue(any("аудио скопировано" in m and "копия правок" in m and lvl=="warn" for m,lvl,_ in self.messages))
        note=self.s._export_note(self.voice,10,ex["mix"],ex["disk"],ex["vars"])
        self.assertIn("аудио скопировано",note);self.assertIn("копия правок не обновлена",note)
        self.assertNotIn("папка Яндекс Диска не найдена",note);self.assert_baseline()

    def test_audio_copy_failure_preserves_valid_local_export_and_reports_copy_failure(self):
        self.disk_enabled=True
        with mock.patch.object(release,"atomic_copy",side_effect=OSError("cloud copy rejected")):ex=self.s.export_part(1)
        self.assertIsNone(ex["disk"]);self.assertEqual(self.voice.read_bytes(),b"new voice audio")
        note=self.s._export_note(self.voice,10,ex["mix"],ex["disk"],ex["vars"])
        self.assertIn("готово локально",note);self.assertIn("копирование на Яндекс Диск не завершено",note)
        self.assertNotIn("папка Яндекс Диска не найдена",note);self.assert_baseline()

    def test_successful_chapter_uses_same_transaction_and_actual_journal_contract(self):
        ex=self.s.export_chapter(1)
        self.assertEqual(self.chapter.read_bytes(),b"new voice audio")
        self.assertEqual(self.chapter_mix.read_bytes(),b"new mixed audio")
        self.assertEqual(self.s.store["chapters"]["1"]["export"]["file"],str(self.chapter))
        rec=json.loads((self.s.dir/"edits.log").read_text().strip());self.assertEqual(rec["file"],str(self.chapter))
        self.assertEqual(ex["count"],2);self.assert_baseline()

    def test_chapter_mix_failure_preserves_existing_chapter_and_metadata(self):
        before=self.output_state();original=release.os.replace
        def fail_mix(src,dst):
            if Path(dst)==self.chapter_mix and "mixes" in Path(src).parts:raise OSError("chapter mix failure")
            return original(src,dst)
        with mock.patch.object(release.os,"replace",side_effect=fail_mix):
            with self.assertRaisesRegex(OSError,"chapter mix failure"):self.s.export_chapter(1)
        self.assertEqual(self.output_state(),before);self.assert_baseline()

    def test_other_part_cue_change_does_not_invalidate_primary_cues_signature(self):
        before=self.s.cues_sig(0,1)
        self.s.store["cues"][1]["vol"]=.99;self.s.store["cues"][1]["off"]=123.
        self.assertEqual(self.s.cues_sig(0,1),before)
        self.s.store["cues"][0]["vol"]=.91
        self.assertNotEqual(self.s.cues_sig(0,1),before)

    def test_external_metadata_change_blocks_export_before_local_file_replacement(self):
        (self.s.dir/"edits.json").write_bytes(b'{"external":true}')
        before=self.output_state()
        with self.assertRaisesRegex(studio.Oops,"другим процессом"):self.s.export_part(1)
        self.assertEqual(self.output_state(),before)


if __name__=="__main__":unittest.main(verbosity=2)
