#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# NOX_KIT 2026-11-12a
"""
voicecheck_k1a.py — проверка образцов голосов по высоте тона: мужской / женский / детский.
Для каждой роли из roles.json, у которой есть образец work/voices/*.wav, считается медианная частота основного
тона (F0). Ожидание — пол/возраст роли из «Настройки озвучки.xlsx» (иначе voices_<книга>.py).
БЕРЕЖНО (с 05.11): голос, которым уже озвучены реплики, НИКОГДА не удаляется сам — только строка «проверьте на слух»;
автоматически пересоздаётся лишь новый голос без единой озвученной реплики, а прежний образец не удаляется, а
переносится в work/voices_old. Высота тона — грубая примета (весёлый тенор бывает 170 Гц), поэтому есть зона
«под вопросом» без всяких действий.
Запуск: envfast python voicecheck_k1a.py [--dry] [--force РОЛЬ ...]   (из папки проекта). Пишет work/voicecheck.txt.
  --force РОЛЬ ...  — пересоздать эти голоса, даже если ими уже озвучены реплики (их реплики переозвучатся).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import nox_audiobook as n  # noqa: E402
try:                                       # заготовки голосов этой книги (для другой книги их может не быть)
    vk = __import__(f"voices_{ROOT.name.lower()}")
except Exception:
    import re as _re
    import types as _types
    vk = _types.SimpleNamespace(GENERIC_SPLIT=_re.compile(r"^(.*) \(гл\.(\d+)\)$"),
                                CHILD=set(), FEMALE=set(), TEEN=set())

# (ок до/от, «под вопросом» до/от): мужской ок <=165, 165–200 под вопросом, выше 200 — не тот голос
LIMITS = {"male": (165.0, 200.0), "female": (165.0, 145.0), "child": (190.0, 160.0)}
MAX_TRIES = 3          # больше раз подряд не пересоздаём: значит, мешает само описание голоса


def median_f0(path: Path) -> float | None:
    import numpy as np
    import soundfile as sf
    import librosa
    y, sr = sf.read(str(path), dtype="float32")
    if y.ndim > 1:
        y = y.mean(axis=1)
    if len(y) < sr:
        return None
    y = y[: sr * 20]
    f0, voiced, _ = librosa.pyin(y, fmin=60, fmax=450, sr=sr, frame_length=2048)
    vals = f0[~np.isnan(f0)]
    if len(vals) < 20:
        return None
    return float(np.median(vals))


GENDER_MAP = {"муж": "male", "жен": "female", "юноша": "teen", "мальчик": "child", "девочка": "child"}


def expected(role: str, cfg: dict | None = None) -> str:
    g = GENDER_MAP.get(((cfg or {}).get("gender") or "").strip().lower())   # пол из «Настройки озвучки.xlsx»
    if g:
        return g
    base = vk.GENERIC_SPLIT.match(role)
    r = base.group(1) if base else role
    if r in vk.CHILD:
        return "child"
    if r in vk.FEMALE:
        return "female"
    if r in vk.TEEN:
        return "teen"          # подростки: тон переходный, не проверяем
    return "male"


def verdict(exp: str, f0: float) -> str:
    """-> "ok" | "doubt" (под вопросом, ничего не делаем) | "bad" (не тот пол/возраст)"""
    if exp == "teen":
        return "ok"
    ok_lim, bad_lim = LIMITS[exp]
    if exp == "male":
        return "ok" if f0 <= ok_lim else ("doubt" if f0 <= bad_lim else "bad")
    return "ok" if f0 >= ok_lim else ("doubt" if f0 >= bad_lim else "bad")


def main():
    dry = "--dry" in sys.argv
    force = set(sys.argv[sys.argv.index("--force") + 1:]) if "--force" in sys.argv else set()
    roles = n.load_roles()
    stems = n.voice_stems(roles)
    rows = n.load_rows() if n.CSV_PATH.exists() else []
    voiced = set()                                     # роли, у которых уже есть озвученные реплики
    for r in rows:
        rr = n.resolve_role(r["speaker"], roles)
        if rr not in voiced and n.wav_for(r, roles).exists():
            voiced.add(rr)
    lines, bad, keep_bad, doubt = [], [], [], []
    import json as _json
    tries_p = n.WORK / "voicecheck_tries.json"            # сколько раз голос роли уже пересоздавался из-за высоты тона
    try:
        tries = _json.loads(tries_p.read_text(encoding="utf-8"))
    except Exception:
        tries = {}
    report = {}
    for role, cfg in roles["roles"].items():
        if role in ("narrator", "Джек", "Хорват"):        # оставлены как есть по решению автора
            continue
        if cfg.get("same_as"):                             # звучит чужим голосом — свой образец не нужен
            continue
        if cfg.get("shared"):                              # голос из другой книги — проверен там
            continue
        wav_p, txt_p = n.voice_files(role, stems)
        if not wav_p.exists():
            continue
        f0 = median_f0(wav_p)
        exp = expected(role, cfg)
        if f0 is None:
            lines.append(f"{role:<22} {wav_p.name:<24} F0=?      ожидание {exp:<6} — не измерить")
            report[role] = {"f0": None, "expected": exp, "ok": True}
            continue
        v = "bad" if role in force else verdict(exp, f0)
        report[role] = {"f0": round(f0, 1), "expected": exp, "ok": v != "bad", "verdict": v}
        if v == "bad" and role in voiced and role not in force:
            mark = "<<< НЕ СОВПАЛ — проверьте на слух (им уже озвучены реплики, сам не трогаю)"
            keep_bad.append(role)
        elif v == "bad" and tries.get(role, 0) >= MAX_TRIES and role not in force:
            mark = f"<<< НЕ СОВПАЛ уже {tries[role]} раз — описание голоса не даёт нужного, поправьте его на листе «Роли»"
            keep_bad.append(role)
        elif v == "bad":
            mark = "<<< НЕ СОВПАЛ — пересоздаю" + (" (по --force)" if role in force else "")
            bad.append(role)
            tries[role] = tries.get(role, 0) + 1
        elif v == "doubt":
            mark = "под вопросом (ничего не делаю)"
            doubt.append(role)
        else:
            mark = "ok"
        lines.append(f"{role:<22} {wav_p.name:<24} F0={f0:5.0f} Гц  ожидание {exp:<6} {mark}")
    removed_wavs = 0
    if bad and not dry:
        import shutil
        import time as _time
        bad_set = set(bad)
        old = n.WORK / "voices_old"                   # прежние образцы не удаляются — сюда, с датой
        old.mkdir(exist_ok=True)
        stamp = _time.strftime("%Y%m%d-%H%M%S")
        for role in bad:
            wav_p, txt_p = n.voice_files(role, stems)
            for p in (wav_p, txt_p):
                if p.exists():
                    shutil.move(str(p), str(old / f"{p.stem}__{stamp}{p.suffix}"))
        for r in rows:
            if n.resolve_role(r["speaker"], roles) in bad_set:
                p = n.wav_for(r, roles)
                if p.exists():
                    p.unlink()
                    removed_wavs += 1
                    t = p.with_suffix(".txt")
                    if t.exists():
                        t.unlink()
    if not dry:
        tries_p.write_text(_json.dumps(tries, ensure_ascii=False, indent=1), encoding="utf-8")
    (n.WORK / "voicecheck.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    import json
    (n.WORK / "voicecheck.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n".join(lines))
    print(f"\nпроверено образцов: {len(lines)}; пересоздаю: {len(bad)}"
          + (f" (прежние образцы — в work\\voices_old; сброшено реплик: {removed_wavs})" if bad and not dry else "")
          + f"; под вопросом: {len(doubt)}; не совпали, но уже звучат в книге (решать вам): {len(keep_bad)}")
    if bad:
        print("  пересоздать: " + ", ".join(bad))
    if keep_bad:
        print("  ПРОВЕРЬТЕ НА СЛУХ (лист «Роли», ссылка на образец): " + ", ".join(keep_bad)
              + "\n  если голос не тот — напишите мне, или: voicecheck_k1a.py --force РОЛЬ")
    (n.WORK / "recast.txt").write_text("\n".join(bad) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
