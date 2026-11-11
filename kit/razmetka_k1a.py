#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# NOX_KIT 2026-11-12a
"""
razmetka_k1a.py — РАЗМЕТКА КНИГИ ТЕГАМИ + КАРТА «КТО ЗА КЕМ ГОВОРИТ» + КАРТА ВРЕМЕНИ + ОТЧЁТ О ГОЛОСАХ.

Что делает (видеокарта не нужна, ~10–30 с; ничего в озвучке не меняет):
 1. Разметка_книги.txt (в папке книги) — вся книга по порядку, каждая реплика в парных тегах, как в html:
        ⟦voice id="92" role="narrator" voice="narrator" part="01" t="03:12.4-03:25.0" state="проверено"⟧текст⟦/voice⟧
    Скобки тегов ⟦ ⟧ выбираются из знаков, которых в книге нет (проверяется при каждом запуске; если вдруг
    появились — берётся следующая пара: ⟪⟫, 〚〛, ⦃⦄). Главы — ⟦chapter …⟧…⟦/chapter⟧.
    Звуковой слой (если найдена партитура cues_book1.json — в work, в D:\\nox_audiobook\\zvuk или zip в Загрузках)
    ставится точечными тегами там, где он звучит:  ⟦music cue="C009" action="start" track="WANDER1"/⟧,
    ⟦amb cue="C010" action="bed" loops="…"/⟧, ⟦sfx cue="C008" name="HumanMaleEatApple" when="после фразы"/⟧.
    Музыка играет от тега start/change до следующего тега music. Громкости дорожек — не в тегах, а в настройках
    сведения (голос 1, эффекты 1/3, музыка 1/10 голоса), чтобы менять их одним числом на всю книгу.
 2. work/role_map.tsv — машинная карта: порядок, id, глава, роль, образец голоса и его отпечаток, кто говорит ДО и
    ПОСЛЕ (роль и id в обе стороны), имя записи, часть и секунды в ней, состояние проверки, высота голоса к образцу.
 3. work/role_map_changes.txt — что изменилось с прошлого запуска: у какой реплики сменились роль, голос или текст
    (ошибки распознавания ролей и «наследство» прежних запусков видны сразу).
 4. work/voice_report.txt — где голос не похож на образец своей роли (по проверке и сторожу голоса), с частью и
    временем, — что послушать; сводка по ролям; реплики со скобками (до этапа 18 движок на них сбивался).
 5. work/cues_check.txt — отметки партитуры, чей якорь не нашёлся в тексте книги.
 6. work/roles_check.txt — ПРОВЕРКА РОЛЕЙ: абзацы, где разбор «речь — ремарка» по тире, скорее всего, ошибся (после тире
    не ремарка, а продолжение речи; реплику читает рассказчик; ремарка к цитате внутри реплики; ремарка называет другого
    говорящего). Абзацы с явной разметкой в сценарии («=», проверены вручную) не проверяются.

Запуск: go делает это сам перед озвучкой; освежить в любой момент — go с ключом -Map (ничего не останавливает).
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
import nox_audiobook as n  # noqa: E402

PAIRS = [("\u27e6", "\u27e7"), ("\u27ea", "\u27eb"), ("\u301a", "\u301b"), ("\u2983", "\u2984")]
OUT_MARKUP = ROOT / "Разметка_книги.txt"
MAP = n.WORK / "role_map.tsv"
CHANGES = n.WORK / "role_map_changes.txt"
REPORT = n.WORK / "voice_report.txt"
ROLES_CHECK = n.WORK / "roles_check.txt"
CUES_CHECK = n.WORK / "cues_check.txt"
OKV = "\tv4"
LISTEN_RATIO = 1.35          # голос выше/ниже образца больше чем в 1,35 раза — в список «послушать»
_ORD = sorted({"одиннадц": 11, "двенадц": 12, "перв": 1, "втор": 2, "трет": 3, "четв": 4, "пят": 5, "шест": 6,
               "седьм": 7, "восьм": 8, "девят": 9, "десят": 10}.items(), key=lambda kv: -len(kv[0]))
MAP_COLS = ["n", "id", "chapter", "label", "para", "kind", "speaker", "role", "voice", "vsig", "prev_role", "prev_id",
            "next_role", "next_id", "wav", "part", "start_s", "end_s", "state", "voice_x", "alien", "method", "text"]


def pick_brackets(texts):
    for a, b in PAIRS:
        if not any(a in t or b in t for t in texts):
            return a, b
    sys.exit("все пары скобок для тегов встречаются в тексте книги — разметка не создана (пришлите вывод в чат)")


def attr(v) -> str:
    return str(v).replace('"', "\u2033").replace("\n", " ")


def tag(L, R, _tname, close=False, selfclose=False, **kw):
    a = "".join(f' {k}="{attr(v)}"' for k, v in kw.items() if v is not None and v != "")
    return f"{L}{'/' if close else ''}{_tname}{a}{'/' if selfclose else ''}{R}"


def ch_label(title: str):
    t = (title or "").strip()
    low = t.lower()
    if low.startswith("пролог"):
        return "Пролог"
    if low.startswith("эпилог"):
        return "Эпилог"
    m = re.match(r"глава\s+([IVXLC]+)\b", t, re.I)
    return m.group(1).upper() if m else None


def part_no(text: str):
    m = re.match(r"\s*часть\s+([а-яё]+)", text or "", re.I)
    if not m:
        return None
    w = m.group(1).lower()
    for k, v in _ORD:
        if w.startswith(k):
            return v
    return None


def norm_map(s: str):
    """Текст для поиска якорей: только буквы и цифры (регистр, ё/е, знаки, тире реплик не важны), с картой позиций
    в исходный текст. Якорь партитуры может переходить из авторской речи в реплику («адрес: — Зо. Ча.»), а в
    разметке реплики разделены и тире реплики нет — поэтому знаки при поиске не учитываются."""
    out, idx = [], []
    for i, ch in enumerate(s):
        c = ch.lower().replace("ё", "е")
        if not c.isalnum():
            if not out or out[-1] == " ":
                continue
            c = " "
        out.append(c)
        idx.append(i)
    while out and out[-1] == " ":
        out.pop()
        idx.pop()
    return "".join(out), idx


def norm(s: str) -> str:
    return norm_map(s)[0]


def find_cues():
    """Партитура звука: work/cues_book1.json, D:\\nox_audiobook\\zvuk\\**, папка книги, zip в Загрузках."""
    cands = [n.WORK / "cues_book1.json", ROOT / "cues_book1.json"]
    for base in (Path(r"D:\nox_audiobook\zvuk"), ROOT / "zvuk"):
        if base.is_dir():
            cands += sorted(base.rglob("cues_book1.json"))
    for p in cands:
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8")), str(p)
            except Exception:
                pass
    dl = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Downloads"
    for z in sorted(dl.glob("Nox_*.zip"), key=lambda p: p.stat().st_mtime, reverse=True) if dl.is_dir() else []:
        try:
            with zipfile.ZipFile(z) as zf:
                for i in zf.infolist():
                    if i.filename.endswith("cues_book1.json"):
                        return json.loads(zf.read(i).decode("utf-8")), f"{z.name}!{i.filename}"
        except Exception:
            continue
    return None, None


def read_times():
    """parts/part_NN.times.tsv -> id -> (часть, начало с, конец с)."""
    def sec(v):
        v = v.strip()
        if ":" in v:
            mm, ss = v.split(":", 1)
            return int(mm) * 60 + float(ss)
        return float(v)
    out = {}
    if not n.PARTS.is_dir():
        return out
    for f in sorted(n.PARTS.glob("part_*.times.tsv")):
        m = re.match(r"part_(\d+)\.times", f.name)
        if not m:
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            p = line.split("\t")
            if len(p) < 3:
                continue
            try:
                a, b = (float(p[6]), float(p[7])) if len(p) >= 8 else (sec(p[0]), sec(p[1]))
            except ValueError:
                continue
            out[p[2]] = (m.group(1), a, b)
    return out


def mmss(v):
    return f"{int(v // 60):02d}:{v % 60:04.1f}"


def seg_state(p: Path):
    """-> (состояние, высота голоса к образцу или None, чужих кусков у сторожа, способ)."""
    vx, alien, method = None, 0, ""
    try:
        st_ = p.stat()
    except OSError:
        if any(p.parent.glob(p.stem + ".rej*.wav")):
            return "на переозвучке", None, 0, ""
        return "нет записи", None, 0, ""
    state = "озвучено, не проверено"
    try:
        okt = p.with_suffix(".ok").read_text(encoding="utf-8")
        if okt.split("\t")[0] == f"{st_.st_size}:{st_.st_mtime_ns}":
            state = "проверено" if OKV in okt else "проверено прежней проверкой"
            if "лучшая из" in okt:
                state += " (лучшая из попыток)"
            m = re.search(r"голос ×(\d+(?:\.\d+)?)", okt)
            if m:
                vx = float(m.group(1))
    except OSError:
        pass
    try:
        meta = json.loads(p.with_suffix(".meta").read_text(encoding="utf-8"))
        alien = int(meta.get("alien") or 0)
        method = str(meta.get("method") or "")
    except Exception:
        method = "нет .meta (запись старой схемы)"
    return state, vx, alien, method


def place_cues(rows, cues, L, R):
    """Отметки партитуры -> вставки тегов {индекс реплики: [(позиция в тексте, тег)]} + список ненайденных."""
    by_ch, heads = {}, {}
    for i, r in enumerate(rows):
        lab = ch_label(r["ch_title"])
        if lab:
            by_ch.setdefault(lab, []).append(i)
        if r["kind"] == "head":
            k = part_no(r["text"])
            if k:
                heads[f"ЧАСТЬ {k}"] = i
    cache, ins, missed, cursor = {}, {}, [], {}

    def chapter_text(lab):
        if lab not in cache:
            parts, offs, pos = [], [], 0
            for i in by_ch.get(lab, []):
                t, mp = norm_map(rows[i]["text"])
                offs.append((pos, i, mp, len(t)))
                parts.append(t)
                pos += len(t) + 1
            cache[lab] = (" ".join(parts), offs)
        return cache[lab]

    def to_raw(offs, pos):
        for o, i, mp, ln in reversed(offs):
            if pos >= o:
                k = min(max(pos - o, 0), ln)
                return i, (mp[k] if k < ln else (mp[-1] + 1 if mp else 0))
        return offs[0][1], 0

    for c in cues:
        ch = c.get("chapter", "")
        name = {"M": "music", "A": "amb", "S": "sfx"}.get(c.get("layer"), "cue")
        kw = dict(cue=c.get("id"))
        if name == "music":
            kw.update(action=c.get("action"), track=c.get("target"))
        elif name == "amb":
            kw.update(action=c.get("action"), loops=c.get("target"))
        else:
            kw.update(name=c.get("target"))
        when = (c.get("when") or "") + " " + (c.get("note") or "")
        if name == "sfx":
            kw["when"] = "до фразы" if "ДО" in when else ("после слога" if "слог" in when else "после фразы")
        kw["basis"] = c.get("basis")
        if ch.startswith("ЧАСТЬ"):
            i = heads.get(ch)
            if i is None:
                missed.append(f"{c.get('id')}\t{ch}\tнет заголовка части в тексте\t{c.get('anchor', '')[:70]}")
                continue
            ins.setdefault(i, []).append((0, tag(L, R, name, selfclose=True, **kw)))
            continue
        text, offs = chapter_text(ch)
        if not offs:
            missed.append(f"{c.get('id')}\t{ch}\tглавы нет в тексте\t{c.get('anchor', '')[:70]}")
            continue
        key = norm(c.get("anchor", ""))
        cur = cursor.get(ch, 0)
        p = text.find(key[:60], cur)
        if p < 0:
            p = text.find(key[:30], cur)
        if p < 0 or not key:
            missed.append(f"{c.get('id')}\t{ch}\tне найден якорь\t{c.get('anchor', '')[:70]}")
            continue
        cursor[ch] = p
        if name != "sfx" or kw["when"] == "до фразы":
            i, rp = to_raw(offs, p)
        else:
            i, rp = to_raw(offs, min(p + len(key) - 1, len(text) - 1))
            rp += 1                                       # сразу после последней буквы якоря
            if kw["when"] == "после фразы":               # конец предложения, в котором кончается якорь
                m = re.search(r"[.!?…]+[»\"”)]*(?=\s|$)", rows[i]["text"][rp - 1:])
                rp = rp - 1 + m.end() if m else len(rows[i]["text"])
        ins.setdefault(i, []).append((rp, tag(L, R, name, selfclose=True, **kw)))
    return ins, missed


def roles_check(rows, roles):
    """Подозрительные абзацы с репликами -> [(глава, абзац, [замечания], [(вид, роль, текст)])]."""
    marked = set()
    ovr = n.WORK / "speaker_overrides.tsv"
    if ovr.exists():
        for line in ovr.read_text(encoding="utf-8").splitlines():
            p = line.split("\t")
            if len(p) >= 3 and p[1].strip() == "=":
                marked.add(p[0].strip())
    R, AL = roles["roles"], roles.get("aliases", {})
    gen = lambda r: {"муж": "m", "юноша": "m", "мальчик": "m", "жен": "f", "девушка": "f", "девочка": "f"}.get(
        (R.get(r) or {}).get("gender"))
    verb = getattr(n, "SPEECH_VERB_RE", None) or re.compile(r"$^")
    stems = getattr(n, "SPEECH_VERB_STEMS", "сказа")
    past = re.compile(r"\b[а-яё]+(?:л|ла|ло|ли|лся|лась|лось|лись)\b")
    proper = {r for r in R if re.fullmatch(r"[А-ЯЁ][а-яё]+", r)} | set(AL)
    me = getattr(n, "ME", "Джек")
    by = {}
    for r in rows:
        by.setdefault((r["chapter"], r["para"]), []).append(r)
    out = []
    for key, seg in by.items():
        if key[1] in marked or not any(s["kind"] == "speech" for s in seg):
            continue
        chars = [s["speaker"] for s in seg if s["kind"] == "speech" and s["speaker"] != n.NARRATOR]
        why = []
        for i, s in enumerate(seg):
            t = s["text"].strip()
            prev = seg[i - 1] if i else None
            if s["kind"] == "speech" and s["speaker"] == n.NARRATOR and chars:
                why.append(f"реплику читает рассказчик: «{t[:50]}»")
            if s["kind"] != "narr" or prev is None or prev["kind"] != "speech":
                continue
            first = " ".join(re.findall(r"[А-Яа-яЁё]+", t)[:5]).lower()
            sent1 = re.split(r"(?<=[.!?…])\s", t, maxsplit=1)[0].lower()
            if not (verb.search(first) or past.search(first) or (t[:1].isupper() and (past.search(sent1) or verb.search(sent1)))):
                why.append(f"после тире не ремарка — похоже, речь читает рассказчик: «{t[:50]}»")
            if re.search(r"»[,!?…]?\s*$", prev["text"]) and verb.search(first):
                why.append(f"ремарка к цитате внутри реплики: «{prev['text'][-30:]}» | «{t[:30]}»")
            m = re.match(r"^(?:\w+\s+){0,1}?(?:" + stems + r")[а-яё]*\s+(?:\w+\s+)?([А-ЯЁ][а-яё]+|я|он|она)\b", t)
            if m:
                w, sp = m.group(1), AL.get(prev["speaker"], prev["speaker"])
                exp = me if w == "я" else (AL.get(w, w) if w in proper else None)
                if exp and exp != sp and sp != n.NARRATOR:
                    why.append(f"ремарка «{t[:30]}» — говорит {exp}, а реплика у роли «{sp}»")
                if w in ("он", "она") and gen(sp) and ((w == "она") != (gen(sp) == "f")):
                    why.append(f"«{t[:25]}», а роль «{sp}» — {'мужская' if gen(sp) == 'm' else 'женская'}")
        if why:
            out.append((key[0], key[1], why, [(s["kind"], s["speaker"], s["text"]) for s in seg]))
    return out


def main():
    t0 = time.time()
    rows = n.load_rows()
    roles = n.load_roles()
    stems = n.voice_stems(roles)
    L, R = pick_brackets([r["text"] for r in rows] + [r["ch_title"] for r in rows])
    times = read_times()
    nf = None
    try:
        import nox_fast as nf                       # высота голоса образцов (те же функции, что у сторожа)
        if not hasattr(nf, "ref_f0"):
            nf = None
    except Exception:
        nf = None
    cues, cues_src = find_cues()

    # прежняя карта — для списка изменений
    old = {}
    if MAP.exists():
        try:
            with MAP.open(encoding="utf-8") as f:
                for r in csv.DictReader(f, delimiter="\t"):
                    old[r["id"]] = r
        except Exception:
            old = {}

    resolved = [n.resolve_role(r["speaker"], roles) for r in rows]
    out_rows = []
    for k, r in enumerate(rows):
        role = resolved[k]
        p = n.wav_for(r, roles)
        state, vx, alien, method = seg_state(p)
        part, a, b = times.get(r["id"], ("", None, None))
        wav_p, _ = n.voice_files(role, stems)
        out_rows.append(dict(
            n=k + 1, id=r["id"], chapter=r["chapter"], label=ch_label(r["ch_title"]) or "", para=r.get("para", ""),
            kind=r.get("kind", ""), speaker=r["speaker"], role=role, voice=wav_p.stem,
            vsig=n.voice_sig(role, roles) if hasattr(n, "voice_sig") else "",
            prev_role=resolved[k - 1] if k else "", prev_id=rows[k - 1]["id"] if k else "",
            next_role=resolved[k + 1] if k + 1 < len(rows) else "",
            next_id=rows[k + 1]["id"] if k + 1 < len(rows) else "",
            wav=p.stem, part=part, start_s="" if a is None else f"{a:.2f}", end_s="" if b is None else f"{b:.2f}",
            state=state, voice_x="" if vx is None else f"{vx:.2f}", alien=alien or "", method=method,
            text=r["text"].replace("\t", " ").replace("\n", " ")))

    # 1. разметка
    ins, missed = place_cues(rows, cues, L, R) if cues else ({}, [])
    lines = [f"# Разметка книги тегами. Скобки тегов: {L} {R} (в тексте книги не встречаются). "
             f"Создано {time.strftime('%Y-%m-%d %H:%M')}.",
             "# voice — реплика (роль, образец голоса, часть и время, состояние проверки); music/amb/sfx — звуковой слой "
             "(музыка, фон местности, эффекты) точно там, где звучит; chapter — глава.",
             tag(L, R, "book", title=getattr(n, "SETTINGS", {}).get("title", "") if hasattr(n, "SETTINGS") else "")]
    cur_ch = None
    for k, (r, o) in enumerate(zip(rows, out_rows)):
        if r["chapter"] != cur_ch:
            if cur_ch is not None:
                lines.append(tag(L, R, "chapter", close=True))
            cur_ch = r["chapter"]
            lines.append(tag(L, R, "chapter", n=r["chapter"], label=o["label"], title=r["ch_title"]))
        txt = r["text"]
        for _, pos, tg in sorted(((j, pos, tg) for j, (pos, tg) in enumerate(ins.get(k, []))),
                                 key=lambda x: (x[1], x[0]), reverse=True):   # одно место — в порядке партитуры
            txt = txt[:pos] + tg + txt[pos:]
        t = f"{mmss(float(o['start_s']))}-{mmss(float(o['end_s']))}" if o["start_s"] else None
        lines.append(tag(L, R, "voice", id=r["id"], role=o["role"], voice=o["voice"],
                         speaker=r["speaker"] if r["speaker"] != o["role"] else None, part=o["part"] or None, t=t,
                         state=o["state"], x=o["voice_x"] or None) + txt + tag(L, R, "voice", close=True))
    if cur_ch is not None:
        lines.append(tag(L, R, "chapter", close=True))
    lines.append(tag(L, R, "book", close=True))
    tmp = OUT_MARKUP.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, OUT_MARKUP)

    # 2. карта
    tmp = MAP.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=MAP_COLS, delimiter="\t", lineterminator="\n")
        w.writeheader()
        w.writerows(out_rows)
    os.replace(tmp, MAP)

    # 3. изменения с прошлого раза
    ch_lines = []
    if old:
        sha = lambda t: hashlib.sha1(t.encode("utf-8")).hexdigest()[:10]
        for o in out_rows:
            pr = old.get(o["id"])
            if pr is None:
                ch_lines.append(f"{o['id']}\tновая реплика\t{o['role']}\t{o['text'][:80]}")
                continue
            what = []
            if pr.get("role") != o["role"]:
                what.append(f"роль {pr.get('role')} -> {o['role']}")
            elif pr.get("vsig") and o["vsig"] and pr.get("vsig") != o["vsig"]:
                what.append(f"образец голоса роли {o['role']} сменился")
            if sha(pr.get("text", "")) != sha(o["text"]):
                what.append("текст изменился")
            if what:
                ch_lines.append(f"{o['id']}\t{'; '.join(what)}\t{o['text'][:80]}")
        gone = set(old) - {o["id"] for o in out_rows}
        ch_lines += [f"{i}\tреплики больше нет\t{old[i].get('role', '')}\t{old[i].get('text', '')[:80]}" for i in sorted(gone)]
    CHANGES.write_text((f"изменений с прошлой карты: {len(ch_lines)}\n" if old else "прежней карты не было\n")
                       + "\n".join(ch_lines) + "\n", encoding="utf-8")

    # 4. отчёт о голосах
    rep = [f"Отчёт о голосах {time.strftime('%Y-%m-%d %H:%M')}. «×» — высота голоса записи к образцу её роли "
           f"(1.00 = как образец; чужой голос — больше {getattr(nf, 'VOICE_TOL', 1.5)} или меньше "
           f"{1 / getattr(nf, 'VOICE_TOL', 1.5):.2f}).", ""]
    listen = []
    for o in out_rows:
        vx = float(o["voice_x"]) if o["voice_x"] else None
        if (vx and (vx >= LISTEN_RATIO or vx <= 1 / LISTEN_RATIO)) or o["alien"]:
            listen.append((abs(math.log(vx)) if vx else 9.0, o))
    listen.sort(key=lambda x: -x[0])
    rep.append(f"== ПОСЛУШАТЬ: голос не похож на образец своей роли ({len(listen)}) ==")
    for _, o in listen[:400]:
        where = f"часть {o['part']} {mmss(float(o['start_s']))}" if o["start_s"] else "часть ещё не собрана"
        rep.append(f"{o['id']}\t{where}\t{o['role']}\t×{o['voice_x'] or '?'}"
                   + (f"\tсторож: чужих предл. {o['alien']}" if o["alien"] else "") + f"\t{o['state']}\t{o['text'][:80]}")
    rep += ["", "== РОЛИ: реплик / озвучено / проверено / высота образца, Гц / пол / чужих =="]
    agg = {}
    for o in out_rows:
        a = agg.setdefault(o["role"], [0, 0, 0, 0, o["voice"]])
        a[0] += 1
        a[1] += int(o["state"] not in ("нет записи", "на переозвучке"))
        a[2] += int(o["state"].startswith("проверено"))
        a[3] += int(bool(o["alien"]) or (bool(o["voice_x"]) and not (1 / 1.5 <= float(o["voice_x"]) <= 1.5)))
    for role, a in sorted(agg.items(), key=lambda kv: -kv[1][0]):
        hz = None
        if nf is not None:
            try:
                hz = nf.ref_f0(n.voice_files(role, stems)[0])
            except Exception:
                hz = None
        g = (roles["roles"].get(role, {}) or {}).get("gender", "")
        rep.append(f"{role}\t{a[0]}\t{a[1]}\t{a[2]}\t{'' if hz is None else round(hz)}\t{g}\t{a[3]}")
    par = [o for o in out_rows if "(" in o["text"] or ")" in o["text"]]
    rep += ["", f"== РЕПЛИКИ СО СКОБКАМИ ({len(par)}): до этапа 18 движок на скобках сбивался (чужой голос); "
            f"с этапа 18 скобки читаются как паузы, эти реплики переозвучиваются ==" ]
    for o in par:
        where = f"часть {o['part']} {mmss(float(o['start_s']))}" if o["start_s"] else ""
        rep.append(f"{o['id']}\t{where}\t{o['role']}\t{o['text'][:90]}")
    trip = n.WORK / "voice_trip.txt"
    if trip.exists():
        rep += ["", "== роли, у которых сторож голоса выключился (образец не похож на речь роли) ==",
                trip.read_text(encoding="utf-8").strip()]
    REPORT.write_text("\n".join(rep) + "\n", encoding="utf-8")

    # 6. проверка ролей (разбор «речь — ремарка»)
    rc = roles_check(rows, roles)
    rl = [f"Проверка ролей {time.strftime('%Y-%m-%d %H:%M')}: подозрительных абзацев {len(rc)}. Абзацы с явной разметкой "
          f"в сценарии («=») не проверяются. Исправление — строкой «абзац<TAB>=<TAB>⟦Роль⟧текст⟦narrator⟧текст» в сценарии.", ""]
    for ch, para, why, segs in rc:
        rl.append(f"гл.{ch} ¶{para}:")
        rl += [f"   ! {w}" for w in why]
        rl += [f"      {'Р' if k == 'narr' else sp:16.16} | {t[:120]}" for k, sp, t in segs]
    ROLES_CHECK.write_text("\n".join(rl) + "\n", encoding="utf-8")

    # 5. партитура
    if cues:
        CUES_CHECK.write_text(f"партитура: {cues_src}; отметок {len(cues)}, не нашлось {len(missed)}\n"
                              + "\n".join(missed) + "\n", encoding="utf-8")
    voiced = sum(1 for o in out_rows if o["state"] not in ("нет записи", "на переозвучке"))
    print(f"  разметка: {OUT_MARKUP}  (реплик {len(rows)}, скобки тегов {L} {R})")
    print(f"  карта ролей и времени: {MAP}; изменений с прошлого раза: {len(ch_lines) if old else 'первая карта'}")
    print(f"  отчёт о голосах: {REPORT}  (послушать: {len(listen)}; реплик со скобками: {len(par)}; озвучено {voiced})")
    print(f"  проверка ролей (кто читает речь и ремарки): подозрительных абзацев {len(rc)} ({ROLES_CHECK.name})")
    if cues:
        print(f"  звуковой слой: отметок {len(cues)}, поставлено {len(cues) - len(missed)}, не нашлось якоря "
              f"{len(missed)} ({CUES_CHECK.name})")
    else:
        print("  партитура звука (cues_book1.json) не найдена — теги звука не поставлены")
    print(f"  готово за {time.time() - t0:.0f} с")


if __name__ == "__main__":
    main()
