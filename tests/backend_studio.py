import copy
import csv
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import wave

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "kit"))
import nox_studio as studio
import nox_file_transactions as ft
from nox_freeze import row_uids
from nox_studio_18d import cut_audio, trim_spans

studio.np = np


def row(index, text):
    return dict(id=index + 1, chapter=1, ch_title="Глава", para=index, kind="narr", speaker="narrator",
                conf="high", pause_before_ms=300, text=text)


def wav(path, seconds=1., value=6000):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1); handle.setsampwidth(2); handle.setframerate(24000)
        handle.writeframes(np.full(int(seconds * 24000), value, dtype="<i2").tobytes())


def read_audio(path):
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        audio = np.frombuffer(handle.readframes(handle.getnframes()), dtype="<i2").astype("float32") / 32768.
    return audio, rate


class StudioFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        s = studio.Studio.__new__(studio.Studio)
        self.s = s
        s.root=root; s.dir=root/"studio"; s.takes_dir=s.dir/"takes"; s.backup_dir=s.dir/"backup"; s.sounds_dir=s.dir/"sounds"
        for d in (s.takes_dir,s.backup_dir,s.sounds_dir,root/"work/wav"):
            d.mkdir(parents=True)
        s.rows=[row(0,"Первая фраза."),row(1,"Следующая фраза."),row(2,"Третья фраза.")]
        s.sidx=[0,1,2]; s.pidx=[0,1,2]; s.uids=row_uids(s.rows); s.idx_of={u:i for i,u in enumerate(s.uids)}
        s.keys=[u.split(".")[0] for u in s.uids]; s.base_roles=["narrator"]*3
        s.roles={"roles":{"narrator":{}}}; s.wavs=[root/"work/wav"/f"draft{i}.wav" for i in range(3)]
        s.lock=threading.RLock(); s.rlock=threading.Lock(); s.jobs={}; s.undo=[]; s._takes={}; s._abs={}; s.nsave=0
        s._parts_cache=None; s._oldv=None
        s.store=studio.Studio.store_defaults({"v":1,"segs":{},"parts":[],"seq":1})
        s.store["segs"][s.uids[0]]=dict(sid="s000001",take="t001",tseq=2,ctx=s.ctx_of(0))
        s.eff=lambda i:dict(role="narrator",tech=s.rows[i]["text"],tempo=1.,pause=300,after=0.)
        s.auto_tech=lambda i:(s.rows[i]["text"],[])
        s.read_audio=read_audio
        s.auto_guard=lambda what:None
        s.housekeeping=lambda **kw:dict(freed=0)
        s.part_of=lambda i:1
        s.part_range=lambda k:list(range(3))
        s.project_calls=[]
        s.project_save=lambda **kw:s.project_calls.append(kw) or {"name":"snapshot.noxproj"}
        self.add_take("s000001","t001",s.rows[0]["text"])
        self.add_take("s000001","t002",s.rows[0]["text"])
        self.persist_fixture()

    def add_take(self,sid,tid,text,role="narrator",src="synth"):
        path=self.s.take_path(sid,tid); wav(path,seconds=1.)
        path.with_suffix(".json").write_text(json.dumps(dict(tid=tid,raw=text,tech=text,role=role,dur=1.,src=src,orig="voice.wav" if src=="import" else None)),encoding="utf-8")
        self.s._takes.clear()
        return path

    def persist_fixture(self):
        s=self.s
        (s.dir/"edits.json").write_text(json.dumps(s.store,ensure_ascii=False),encoding="utf-8")
        (s.dir/"fixed_parts.json").write_text('{"ver":1,"parts":[]}',encoding="utf-8")
        s._tx=ft.FileTransactions(s.dir)
        s._edits_sha=ft.digest(s.dir/"edits.json"); s._fixed_sha=ft.digest(s.dir/"fixed_parts.json")
        s._last_durable_store=copy.deepcopy(s.store)


class MediaUndoTests(StudioFixture):
    def test_delete_selected_then_undo_restores_real_pair_and_choice(self):
        s=self.s; uid=s.uids[0]; p=s.take_path("s000001","t001")
        before={x:x.read_bytes() for x in (p,p.with_suffix(".json"))}
        s.delete_take(uid,"t001")
        self.assertFalse(p.exists()); self.assertNotIn("take",s.store["segs"][uid])
        s.undo_last()
        self.assertEqual(s.store["segs"][uid]["take"],"t001")
        self.assertEqual(s.src(0)["kind"],"take")
        for path,data in before.items():self.assertEqual(path.read_bytes(),data)

    def test_wipe_then_undo_restores_both_takes_and_edits(self):
        s=self.s; uid=s.uids[0]; s.store["segs"][uid]["tech"]="Правка";self.persist_fixture()
        result=s.wipe_seg(uid)
        self.assertEqual(result["takes"],2)
        self.assertFalse(s.take_path("s000001","t001").exists())
        s.undo_last()
        self.assertEqual(len(s.takes_of(s.store["segs"][uid])),2)
        self.assertEqual(s.store["segs"][uid]["tech"],"Правка")

    def test_delete_io_failure_never_unlinks_or_clears_choice(self):
        s=self.s; uid=s.uids[0]; original=ft.os.rename; count=[0]
        def fail_second(a,b):
            count[0]+=1
            if count[0]==2:raise PermissionError("locked sidecar")
            return original(a,b)
        with patch.object(ft.os,"rename",side_effect=fail_second):
            with self.assertRaises(studio.Oops):s.delete_take(uid,"t001")
        self.assertEqual(s.src(0)["kind"],"take")
        self.assertEqual(s.store["segs"][uid]["take"],"t001")

    def test_file_undo_is_rehydrated_after_restart(self):
        s=self.s; uid=s.uids[0]
        s.delete_take(uid,"t001");s.undo.clear()
        s.store=json.loads((s.dir/"edits.json").read_text())
        s._refresh_file_undo();s.undo_last()
        self.assertEqual(s.src(0)["kind"],"take")

    def test_clean_both_modes_preserve_undo_references(self):
        s=self.s;uid=s.uids[0]
        s.choose_take(uid,"t002")
        for backup in (False,True):
            result=s.clean_takes(backup)
            self.assertTrue(result["reversible"])
            self.assertTrue(s.take_path("s000001","t001").exists())
        self.assertEqual(len(s.project_calls),1)
        s.undo_last();self.assertEqual(s.src(0)["tid"],"t001")

    def test_clean_unused_take_is_reversible_in_both_modes(self):
        s=self.s
        for backup in (False,True):
            result=s.clean_takes(backup)
            self.assertEqual(result["takes"],1)
            self.assertEqual(result["freed"],0)
            self.assertGreater(result["quarantined_bytes"],0)
            self.assertFalse(s.take_path("s000001","t002").exists())
            s.undo_last()
            self.assertTrue(s.take_path("s000001","t002").exists())

    def test_incomplete_pair_and_locked_state_block_removal(self):
        s=self.s;uid=s.uids[0]
        s.take_path("s000001","t002").with_suffix(".json").unlink()
        with self.assertRaises(studio.Oops):s.delete_take(uid,"t002")
        with self.assertRaises(studio.Oops):s.wipe_seg(uid)
        self.assertTrue(s.take_path("s000001","t002").exists())
        s.store["segs"][uid]["locked"]={"at":"now","take":"t001"}
        with self.assertRaises(studio.Oops):s.delete_take(uid,"t001")

    def test_corrupt_edits_is_preserved_and_not_replaced_by_empty(self):
        s=self.s;path=s.dir/"edits.json";path.write_bytes(b'{broken')
        fixed=(s.dir/"fixed_parts.json").read_bytes()
        with self.assertRaises(studio.Oops):s.load_store()
        self.assertEqual(path.read_bytes(),b'{broken')
        self.assertEqual((s.dir/"fixed_parts.json").read_bytes(),fixed)

    def test_prepare_marker_blocks_mutation_before_files_move(self):
        s=self.s;(s.root/"work/.nox_prepare.lock").write_text("preparing")
        with self.assertRaises(studio.Oops):s.delete_take(s.uids[0],"t001")
        self.assertEqual(s.src(0)["kind"],"take")


class TrimTests(StudioFixture):
    def test_single_retained_span_and_multiple_spans_persist(self):
        s=self.s;uid=s.uids[0]
        s.take_trim(uid,"t001",spans=[[.2,1.]])
        self.assertEqual(s.store["segs"][uid]["trims"]["t001"],[.2,1.])
        s.take_trim(uid,"t001",spans=[[0,.2],[.5,1.]])
        self.assertEqual(json.loads((s.dir/"edits.json").read_text())["segs"][uid]["trims"]["t001"],[[0.,.2],[.5,1.]])
        audio,sr=read_audio(s.take_path("s000001","t001"))
        rendered=cut_audio(audio,sr,s.store["segs"][uid]["trims"]["t001"])
        self.assertAlmostEqual(len(rendered)/sr,.688,places=3)

    def test_empty_and_nonfinite_spans_do_not_change_state(self):
        s=self.s;uid=s.uids[0];before=(s.dir/"edits.json").read_bytes()
        for spans in ([],[[0,float("inf")]],[[float("nan"),1]],[[.9,.95]]):
            with self.subTest(spans=spans),self.assertRaises(studio.Oops):s.take_trim(uid,"t001",spans=spans)
            self.assertEqual((s.dir/"edits.json").read_bytes(),before)

    def test_next_uses_end_of_last_retained_span(self):
        s=self.s;uid=s.uids[0]
        self.add_take("s000001","t001",s.row_text(0),src="import")
        result=s.take_next(uid,"t001",0,.5,spans=[[0,.2],[.3,.5]])
        e=s.store["segs"][s.uids[1]]
        self.assertEqual(e["trims"][result["tid"]],[.5,1.])
        self.assertEqual(s.store["segs"][uid]["trims"]["t001"],[[0.,.2],[.3,.5]])


class OldVoiceTests(StudioFixture):
    def orphan(self,sid,text,role="narrator"):
        self.add_take(sid,"t001",text,role)
        self.s.store["orphans"].append(dict(uid="old"+sid,e={"sid":sid},at="old"))

    def test_ambiguous_and_unknown_role_never_auto_choose(self):
        s=self.s;s.store["segs"]={};self.persist_fixture()
        self.orphan("s10",s.row_text(1));self.orphan("s11",s.row_text(1));self.orphan("s12",s.row_text(2),None)
        self.persist_fixture()
        self.assertTrue(s.old_voice_for(s.uids[1])[0]["ambiguous"])
        self.assertEqual(s.old_voice_auto(1)["got"],0)
        self.assertNotIn(s.uids[1],s.store["segs"])

    def test_unique_exact_known_role_can_auto_adopt(self):
        s=self.s;s.store["segs"]={};self.orphan("s10",s.row_text(1));self.persist_fixture()
        result=s.old_voice_auto(1)
        self.assertEqual(result["got"],1)
        self.assertEqual(s.src(1)["kind"],"take")

    def test_manual_adoption_preserves_source_text_and_original_bytes(self):
        s=self.s;self.orphan("s10","Прежний текст.");self.persist_fixture()
        original=s.take_path("s10","t001").read_bytes()
        result=s.old_voice_adopt(s.uids[1],"s10","t001",choose=True)
        e=s.store["segs"][s.uids[1]];p=s.take_path(e["sid"],result["tid"])
        meta=json.loads(p.with_suffix(".json").read_text())
        self.assertEqual(meta["raw"],"Прежний текст.")
        self.assertEqual(meta["source_raw"],"Прежний текст.")
        self.assertEqual(meta["target_raw"],s.row_text(1))
        self.assertEqual(p.read_bytes(),original)
        self.assertEqual(s.take_path("s10","t001").read_bytes(),original)

    def test_nested_metadata_change_is_visible(self):
        s=self.s;self.orphan("s10","Первоначальный текст.")
        self.assertEqual(s.old_voices()[0]["raw"],"Первоначальный текст.")
        p=s.take_path("s10","t001").with_suffix(".json");meta=json.loads(p.read_text());meta["raw"]="Исправленный текст.";p.write_text(json.dumps(meta))
        self.assertEqual(s.old_voices()[0]["raw"],"Исправленный текст.")


class CatalogTests(StudioFixture):
    def test_external_refresh_and_path_confinement(self):
        s=self.s;folder=s.root/"game";folder.mkdir()
        result=s.sound_root_add(str(folder),"Игра")
        self.assertEqual(s.ext_catalog(),[])
        wav(folder/"WolfAttack.wav")
        self.assertEqual(s.ext_catalog(),[])  # cached until explicit refresh/expiry
        s.ext_catalog(fresh=True)
        self.assertEqual(len(s.ext_catalog()),1)
        self.assertIn("Волк",s.ext_catalog()[0]["title"])
        self.assertIsNone(s.sound_path(f"ext:{result['id']}/../../outside.wav"))
        self.assertIsNotNone(s.sound_path(s.ext_catalog()[0]["file"]))

    def test_enumeration_is_bounded_before_full_walk(self):
        s=self.s;folder=s.root/"game";folder.mkdir();s.EXT_CAP=1
        for i in range(20):wav(folder/f"f{i}.wav")
        s.sound_root_add(str(folder))
        self.assertEqual(len(s.ext_catalog()),1)
        self.assertLess(s._ext[3]["visited"],20)
        self.assertTrue(s._ext[3]["limited"])


if __name__ == "__main__":
    unittest.main()
