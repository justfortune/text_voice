#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
nox_audiobook.py — из .docx делаем аудиокнигу ПО РОЛЯМ локально (Qwen3-TTS, Apache-2.0).

Этапы (запускаются по очереди, каждый можно перезапускать — всё возобновляемое):

  python nox_audiobook.py segment book.docx            # CSV с сегментами и ролями + roles.json (GPU не нужен)
  python nox_audiobook.py cast                         # голоса по текстовому описанию (VoiceDesign) -> work/voices/*.wav
  python nox_audiobook.py synth [--batch 16] [--char-budget 3200] [--chapter 2]   # озвучка клонированием (Base)
  python nox_audiobook.py build --title "Посох Забвения" --author "Д."   # сборка глав -> book.m4b

synth ведёт один общий конвейер: реплики ВСЕХ ролей сортируются по длине и идут смешанными батчами (у каждой свой
голос), до --batch штук, длинные — меньшими пачками (реплик x длина самой длинной <= --char-budget); при нехватке
VRAM батч уменьшается сам. Готовые wav пропускаются, Ctrl+C безопасен.
API сверено с qwen-tts 0.1.1 (https://github.com/QwenLM/Qwen3-TTS).

Зависимости:
  pip install -U qwen-tts python-docx soundfile numpy
  (опционально)  pip install num2words ruaccent tqdm ;  flash-attn (ускорение/память)
  ffmpeg в PATH или в папке tools рядом со скриптом (для build)
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter, OrderedDict
from pathlib import Path

HERE = Path(__file__).resolve().parent
# модели лежат рядом со скриптом (D:\nox_audiobook\hf), а не в C:\Users\...\.cache — на C: мало места
if "HF_HOME" not in os.environ and (HERE / "hf").is_dir():
    os.environ["HF_HOME"] = str(HERE / "hf")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

WORK = HERE / "work"
CSV_PATH = WORK / "segments.csv"
ROLES_PATH = WORK / "roles.json"
VOICES = WORK / "voices"
WAVS = WORK / "wav"
CHAPTERS = WORK / "chapters"
PARTS = HERE / "parts"
PARTS_INDEX = WORK / "parts.json"
DUR_CACHE = WORK / "durations.json"
LOCK_STALE_SEC = 45 * 60   # «замок» старше этого без готового wav считается брошенным (упавший воркер)

NARRATOR = "narrator"
ME = "Джек"            # рассказчик говорит от первого лица — его реплики («сказал я») идут этой роли
UNKNOWN = "UNKNOWN"

MAX_CHARS = 300        # максимальная длина одного сегмента для TTS (дальше режем по предложениям)
FRAME_RATE = 12.5      # кадров кодека в секунду у Qwen3-TTS-12Hz (24000 Гц / 1920)

# паузы, мс
PAUSE = dict(chunk=120, para=550, dialog=320, poem_line=250, heading=1200, chapter_start=1500, chapter_end=2000)
# NOX_PT_V10: настройки книги (пишет nastroyki_ozvuchki.py из «Настройки озвучки.xlsx»)
try:
    SETTINGS = json.loads((WORK / "settings.json").read_text(encoding="utf-8"))
except Exception:
    SETTINGS = {}
PAUSE.setdefault("sentence", 420)   # NOX_PT_V13: кусок абзаца кончился точкой -> пауза предложения
PAUSE.setdefault("turn", 260)       # NOX_PT_V13: смена голоса внутри абзаца («— Стой! — крикнул он»)
PAUSE.update({k: int(v) for k, v in (SETTINGS.get("pause") or {}).items() if k in PAUSE})
ME = str(SETTINGS.get("me") or ME)   # NOX_PT_V14: кто говорит «я»

FIELDS = ["id", "chapter", "ch_title", "para", "kind", "speaker", "conf", "pause_before_ms", "text"]

# ----------------------------------------------------------------------------------------------------------------------
# 1. SEGMENT — разбор docx на сегменты с ролями
# ----------------------------------------------------------------------------------------------------------------------

HEAD_RE = re.compile(r"^(Пролог|Эпилог|Часть|Глава|Интерлюдия|Послесловие|Предисловие)\b")
DASHES = "—–-"
# «речь — ремарка»: тире-разделитель только после знака препинания (чтобы не резать «жизнь — это…»)
SPLIT_RE = re.compile(r"(?<=[,.!?…;:»”\"])\s+[—–]\s+")
SENT_RE = re.compile(r"(?<=[.!?…])\s+(?=[«\"“А-ЯЁA-Z—–(\[])")

SPEECH_VERB_STEMS = (
    "сказа|ответи|спроси|проговори|пробормота|произн|крикну|прошепта|отозва|замети|добави|буркну|возрази|усмехну|вздохну|"
    "переби|повтори|объясни|поинтересова|кивну|продолжа|продолжи|подтверди|согласи|проворча|рявкну|выдохну|хмыкну|откликну|"
    "уточни|протяну|поясни|заяви|прибави|промолви|молви|броси|отреза|фыркну|осведоми|процеди|прохрипе|проскрипе|рассмея|"
    "засмея|поправи|пробурча|шепну|прошипе|пропыхте|прокашля|призна|завери|напомни|веле|приказа|распоряди|позва|окликну|"
    "нача|законч|подума|говори|переспроси|подхвати|отмахну|пожа|покача|прошамка|проскуле|промычи|простона|прокрича|"
    "воскликну|вскрича|выпали|отвеча|спрашива|обеща|пообеща|пожалова|посетова|изуми|удиви|ахну|охну|пропе|затяну|"
    "перечисли|подытожи|резюмирова|заключи|предложи|посоветова|предупреди|потребова|попроси|умоля|взмоли|огрызну|"
    "отозвал|обернул|повернул|оберну|поверну|взгляну|посмотре|глядел|гляну|сощури|прищури|наклони|выпрями|встал|подня|"
    "сел|присел|усел|махну|развёл|разве|ткну|указа|постуча|хлопну|щёлкну|открыл|закрыл|помолча|помедли|подожда|"
    "представи|познакоми|обрати|поклони|сплюну|плюну|почеса|потёр|потер|потрепа|хлебну|отхлебну|глотну|жевал|прожева"
)
SPEECH_VERB_RE = re.compile(r"\b(?:" + SPEECH_VERB_STEMS + r")[а-яё]*\b")
# глаголы 1-го лица настоящего времени в ремарках («говорю», «спрашиваю») -> рассказчик
FIRST_PERSON_VERBS = {"говорю", "отвечаю", "спрашиваю", "думаю", "повторяю", "признаюсь", "киваю", "пожимаю", "соглашаюсь",
                      "возражаю", "перебиваю", "уточняю", "добавляю", "бормочу", "шепчу", "кричу", "молчу", "вздыхаю"}

PRONOUN_M = {"он"}
PRONOUN_F = {"она"}
NOUN_WHITELIST = {
    # нарицательные, которыми в тексте часто обозначают говорящего
    "старик", "старуха", "старичок", "девушка", "женщина", "мужчина", "парень", "мальчик", "девочка", "капитан",
    "волшебник", "архивариус", "лорд", "король", "королева", "голос", "незнакомец", "незнакомка", "стражник", "стражница",
    "хозяин", "хозяйка", "трактирщик", "торговец", "торговка", "дама", "господин", "госпожа", "учитель", "ученик",
    "гробовщик", "монах", "жрец", "жрица", "ведьма", "колдун", "некромант", "рыцарь", "воин", "солдат", "оруженосец",
    "привратник", "писарь", "библиотекарь", "хранитель", "командир", "сержант", "офицер", "лекарь", "целитель", "аптекарь",
    "кузнец", "плотник", "рыбак", "крестьянин", "пастух", "охотник", "мастер", "подмастерье", "гость", "гостья", "спутник",
    "спутница", "проводник", "вожак", "вождь", "великан", "огр", "гоблин", "урчин", "гном", "эльф", "тролль", "призрак",
    "тень", "существо", "тварь", "зверь", "чудовище", "мертвец", "скелет", "дух", "эхо",
}
FEM_NOUNS = {"старуха", "девушка", "женщина", "девочка", "королева", "незнакомка", "стражница", "хозяйка", "торговка",
             "дама", "госпожа", "жрица", "ведьма", "гостья", "спутница", "тень", "тварь"}

ROMAN = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}
ORD_F = ["", "первая", "вторая", "третья", "четвёртая", "пятая", "шестая", "седьмая", "восьмая", "девятая", "десятая",
         "одиннадцатая", "двенадцатая", "тринадцатая", "четырнадцатая", "пятнадцатая", "шестнадцатая", "семнадцатая",
         "восемнадцатая", "девятнадцатая", "двадцатая"]
ORD_F_TENS = {20: "двадцать", 30: "тридцать", 40: "сорок"}


def roman_to_int(s: str) -> int:
    total, prev = 0, 0
    for ch in reversed(s):
        v = ROMAN[ch]
        total = total - v if v < prev else total + v
        prev = max(prev, v)
    return total


def ordinal_f(n: int) -> str:
    if n < len(ORD_F):
        return ORD_F[n]
    if (n % 10 == 0 or n >= 50) and 1 <= n <= 99 and "_ord_fem" in globals():   # NOX_PT_V14
        return _ord_fem(n)
    tens, ones = n // 10 * 10, n % 10
    return (ORD_F_TENS.get(tens, "") + " " + (ORD_F[ones] if ones else "")).strip() if tens in ORD_F_TENS else str(n)


def normalize_heading(txt: str) -> str:
    """«Глава XII. Лес» -> «Глава двенадцатая. Лес» (чтобы TTS не читал римские цифры по-английски)."""
    m = re.match(r"^(Глава)\s+([IVXLC]+)\b(.*)$", txt)
    if m:
        return f"{m.group(1)} {ordinal_f(roman_to_int(m.group(2)))}{m.group(3)}"
    return txt


def read_docx(path: Path):
    """-> список (text, is_heading, is_block). Строки внутри абзаца (soft break) сохраняем через '\n'."""
    try:
        import docx  # python-docx
    except ImportError:
        sys.exit("pip install python-docx")
    d = docx.Document(str(path))
    out = []
    for p in d.paragraphs:
        txt = p.text.replace("\u00a0", " ").strip()
        if not txt:
            continue
        style = (p.style.name or "") if p.style is not None else ""
        low = style.lower()
        if txt.rstrip(".:").strip().lower() in ("содержание", "оглавление"):   # NOX_PT_V14
            out.append(("<<<SCENE>>>", False, False))
            continue
        if "toc" in low or "оглавлен" in low or "contents" in low:
            continue                                    # строки оглавления не озвучиваем
        if "scene" in low or not any(ch.isalnum() for ch in txt):
            out.append(("<<<SCENE>>>", False, False))   # разрыв сцены (❦, ***) -> пауза, не текст
            continue
        is_head = style.startswith("Heading") or style.startswith("Заголовок") or bool(HEAD_RE.match(txt))
        if is_head and "\n" in txt:
            txt = ". ".join(x.strip() for x in txt.split("\n") if x.strip())
        is_block = "Block" in style or "Quote" in style or "Цитата" in style
        out.append((txt, is_head, is_block))
    return out


def split_sentences(text: str):
    parts = [s.strip() for s in SENT_RE.split(text) if s.strip()]
    return parts or [text]


def chunk_text(text: str, max_chars: int = MAX_CHARS):
    """Режем длинный текст по предложениям в куски <= max_chars; очень длинные предложения — по запятым."""
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 1:
        raise ValueError("max_chars должен быть положительным целым числом")
    chunks, cur = [], ""
    for s in split_sentences(text):
        if len(s) > max_chars * 1.5:
            # режем по запятым/точкам с запятой (NOX_CHUNK_ORDER: сначала сбросить накопленное, иначе порядок фраз ломается)
            if cur:
                chunks.append(cur)
                cur = ""
            piece = ""
            for frag in re.split(r"(?<=[,;:])\s+", s):
                if piece and len(piece) + len(frag) + 1 > max_chars:
                    chunks.append(piece)
                    piece = frag
                else:
                    piece = (piece + " " + frag).strip()
            if piece:
                cur = piece
            continue
        if cur and len(cur) + len(s) + 1 > max_chars:
            chunks.append(cur)
            cur = s
        else:
            cur = (cur + " " + s).strip()
    if cur:
        chunks.append(cur)
    # Страховка: кусок без запятых и длиннее max_chars*1.5 дробим по словам (обычные куски не трогаем).
    limit = int(max_chars * 1.5)
    safe = []
    for c in chunks:
        if len(c) <= limit:
            safe.append(c)
            continue
        cur2 = ""
        for w in re.findall(r"\S+", c):
            while len(w) > limit:
                if cur2:
                    safe.append(cur2)
                    cur2 = ""
                safe.append(w[:limit])
                w = w[limit:]
            if cur2 and len(cur2) + 1 + len(w) > max_chars:
                safe.append(cur2)
                cur2 = w
            else:
                cur2 = (cur2 + " " + w).strip()
        if cur2:
            safe.append(cur2)
    return safe


def verb_gender(part: str):
    """Пол говорящего по окончанию глагола ремарки: сказала -> f, сказал -> m."""
    m = SPEECH_VERB_RE.search(part)
    if not m:
        return None
    v = m.group(0).lower()
    if v.endswith(("ла", "лась")):
        return "f"
    if v.endswith(("ли", "лись")):
        return "pl"
    if v.endswith(("л", "лся", "нул", "нулся", "ёл", "ел", "ёс", "ес")):
        return "m"
    return None


class SpeakerTracker:
    """Кто говорит: по ремарке (имя / местоимение / «я»), иначе — чередование собеседников."""

    def __init__(self, names: set[str]):
        self.names = names
        self.gender: dict[str, str] = {ME: "m"}
        self.recent: list[str] = []          # недавние говорящие, последний — в конце
        self.mentioned: list[str] = []       # персонажи, упомянутые в прозе («вылетел старик в белом»)
        self.pending = None                  # герой короткого абзаца-«жеста» перед репликой («Он подошёл ближе.»)
        self.prev_line_speaker = None

    def reset_scene(self):
        self.recent = []
        self.mentioned = []
        self.pending = None
        self.prev_line_speaker = None

    def note_beat(self, text: str):
        """Короткий абзац прозы между репликами, начинающийся с подлежащего («Он подошёл…», «Хорват покачал головой»),
        почти всегда предваряет реплику этого же персонажа."""
        self.pending = None
        if len(text) > 250:
            return
        m = re.match(r"([А-ЯЁ][а-яё\-]+)", text)
        if not m:
            return
        t = m.group(1)
        tl = t.lower()
        if tl == "я":
            self.pending = ME
        elif tl in PRONOUN_M or tl in PRONOUN_F:
            want = "m" if tl in PRONOUN_M else "f"
            prev = self.prev_line_speaker
            if prev and prev != ME and not prev.startswith(UNKNOWN) and self.gender.get(prev, want) == want:
                self.pending = prev
            else:
                self.pending = self._resolve_pronoun(want)
        elif t in self.names or tl in NOUN_WHITELIST:
            self.pending = t if t in self.names else tl

    def _push(self, who):
        if who is None or who.startswith(UNKNOWN):
            return
        if who in self.recent:
            self.recent.remove(who)
        self.recent.append(who)

    def note_narration(self, text: str):
        """Запоминаем, кто упомянут в прозе, — чтобы разрешать «он/она» в начале сцены."""
        for t in re.findall(r"[А-ЯЁа-яё\-]+", text):
            tl = t.lower()
            who = t if t in self.names else (tl if tl in NOUN_WHITELIST else None)
            if who is None:
                continue
            if tl in FEM_NOUNS:
                self.gender.setdefault(who, "f")
            elif tl in NOUN_WHITELIST:
                self.gender.setdefault(who, "m")
            if who in self.mentioned:
                self.mentioned.remove(who)
            self.mentioned.append(who)
        self.mentioned = self.mentioned[-6:]

    def _resolve_pronoun(self, want: str):
        for pool in (
            [s for s in reversed(self.recent) if s != ME and s != self.prev_line_speaker],
            [s for s in reversed(self.recent) if s != ME],
            [s for s in reversed(self.mentioned) if s != ME and s != self.prev_line_speaker],
            [s for s in reversed(self.mentioned) if s != ME],
        ):
            cands = [s for s in pool if self.gender.get(s, want) == want]
            if cands:
                return cands[0]
        return None

    def from_attribution(self, part: str):
        """-> (speaker, conf) или (None, None), если ремарка ничего не говорит о говорящем."""
        toks = re.findall(r"[А-ЯЁа-яёA-Za-z\-]+", part)
        g = verb_gender(part)
        has_verb = bool(SPEECH_VERB_RE.search(part))
        for i, t in enumerate(toks[:12]):
            tl = t.lower()
            if tl == "я" or tl in FIRST_PERSON_VERBS:
                return ME, "high"
            if tl in PRONOUN_M or tl in PRONOUN_F:
                want = "m" if tl in PRONOUN_M else "f"
                who = self._resolve_pronoun(want)
                if who:
                    return who, "mid"
                return f"{UNKNOWN}({tl})", "low"
            if t in self.names or (tl in NOUN_WHITELIST):
                who = t if t in self.names else tl
                if g in ("m", "f"):
                    self.gender.setdefault(who, g)
                elif tl in FEM_NOUNS:
                    self.gender.setdefault(who, "f")
                return who, "high" if has_verb else "mid"
        return None, None

    def by_alternation(self):
        """Реплика без ремарки: отвечает «другой» собеседник. В записках от первого лица, если другого
        собеседника ещё не видели, а предыдущая реплика не рассказчика, — отвечает рассказчик."""
        if self.pending:
            return self.pending, "mid"
        if self.prev_line_speaker is not None:
            others = [s for s in reversed(self.recent) if s != self.prev_line_speaker]
            if others:
                return others[0], "low"
            if self.prev_line_speaker != ME:
                return ME, "low"
            if self.mentioned:
                return self.mentioned[-1], "low"
        elif self.mentioned:
            return self.mentioned[-1], "low"
        return UNKNOWN, "none"

    def commit(self, who):
        self._push(who)
        self.prev_line_speaker = who
        self.pending = None


def collect_names(paras) -> set[str]:
    """Имена = слова с заглавной, стоящие сразу после глагола речи («сказал Хорват»). Считаем частоты,
    отбрасываем косвенные формы (Хорвата/Хорвату/Хорватом -> Хорват), оставляем встречающиеся >= 2 раз."""
    cnt = Counter()
    for txt, is_head, _ in paras:
        if is_head:
            continue
        for m in re.finditer(SPEECH_VERB_RE.pattern + r"\s+(?:(?:ему|ей|им|мне|нам|вам|тихо|громко|наконец|вдруг|"
                             r"снова|опять|только|было|же|уже)\s+)*([А-ЯЁ][а-яё\-]+)", txt):
            cnt[m.group(1)] += 1
    names = set()
    for n, c in cnt.most_common():
        base = None
        for suf, repl in (("ом", ""), ("ем", ""), ("у", ""), ("ю", "я"), ("а", ""), ("я", ""), ("е", "а"), ("ы", "а"), ("и", "я")):
            if n.endswith(suf) and len(n) > len(suf) + 2:
                cand = n[: -len(suf)] + repl
                if cand in cnt and cnt[cand] >= c:
                    base = cand
                    break
        if base is None and c >= 2:
            names.add(n)
    return names


from nox_freeze import (guard_segment as _nox_guard_segment,
    merge_prepared_rows as _nox_merge_prepared_rows, commit_prepared_rows as _nox_commit_prepared_rows,
    read_protection as _nox_read_protection, edition_write_lock as _nox_write_lock,
    preserve_pipeline_bundle as _nox_preserve_bundle, guard_metadata as _nox_guard_metadata)
NOX_PROTECTION_VERSION = "18d"

@_nox_guard_segment
def segment(docx_paths, max_chars: int):
    WORK.mkdir(exist_ok=True)
    # 18d: подготовка не перемещает исходные WAV; прежняя редакция проверяется до разбивки.
    paras = []
    for dp in docx_paths:
        part = read_docx(dp)
        paras += part
        print(f"  + {dp.name}: {len(part)} абзацев")
    names = collect_names(paras)
    tracker = SpeakerTracker(names)
    rows = []
    chapter, ch_title = 0, "Начало"
    narr_run = 0            # подряд идущая проза без диалога (символов) — для сброса сцены
    seg_id = 0
    extra_pause = 0         # добавочная тишина после разрыва сцены
    SPEAKER_OVERRIDES = {}  # para -> [(prefix|'*', role), ...]
    OVR_FP = {}             # para -> отпечаток текста абзаца (NOX_PT_V9)
    ovr_path = WORK / "speaker_overrides.tsv"
    if ovr_path.exists():
        for line in ovr_path.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 3:
                continue
            try:
                SPEAKER_OVERRIDES.setdefault(int(parts[0]), []).append((parts[1].strip(), parts[2].strip()))
                if len(parts) > 3 and parts[3].strip():
                    OVR_FP[int(parts[0])] = parts[3].strip()
            except ValueError:
                continue
        print(f"  сценарий по ролям: {len(SPEAKER_OVERRIDES)} абзацев из {ovr_path.name}")
    ovr_last = {}
    if SPEAKER_OVERRIDES and OVR_FP:   # NOX_PT_V9: сверка номеров абзацев с отпечатками текста
        def _fp(t):
            return re.sub(r"[^А-Яа-яЁёA-Za-z0-9]", "", t).lower()[:24]
        fps = [_fp(p_[0]) for p_ in paras]
        W = 150
        cand = {p0: [i for i in range(max(0, p0 - W), min(len(fps), p0 + W)) if fps[i] == fp]
                for p0, fp in OVR_FP.items()}
        # сдвиг определяем по однозначным строкам, неоднозначные («Да.», «Хм.») берут сдвиг ближайшей однозначной
        offs = {p0: c[0] - p0 for p0, c in cand.items() if len(c) == 1}
        known = sorted(offs)
        import bisect
        def _local_off(p0):
            if not known:
                return 0
            k = bisect.bisect_left(known, p0)
            near = min(known[max(0, k - 1):k + 1], key=lambda q: abs(q - p0))
            return offs[near]
        remap, lost = {}, []
        for p0, c in cand.items():
            if not c:
                lost.append(p0)
                continue
            want = p0 + _local_off(p0)
            tgt = want if want in c else min(c, key=lambda i: abs(i - want))
            if tgt != p0:
                remap[p0] = tgt
        if remap or lost:
            new_ovr = {}
            for p0, lst in SPEAKER_OVERRIDES.items():
                if p0 in lost:
                    continue
                new_ovr.setdefault(remap.get(p0, p0), []).extend(lst)
            SPEAKER_OVERRIDES.clear()
            SPEAKER_OVERRIDES.update(new_ovr)
            print(f"  сценарий: книга правилась — строк переехало к другим абзацам: {len(remap)}, не найдено: {len(lost)}")
            if lost:
                (WORK / "speaker_overrides_lost.txt").write_text("\n".join(str(x) for x in lost), encoding="utf-8")

    def _norm(t):
        return re.sub(r"[^А-Яа-яЁёA-Za-z0-9]", "", t).lower()

    def add(kind, speaker, conf, pause, text, para_idx):
        nonlocal seg_id, extra_pause
        if int(pause) == PAUSE["chunk"] and rows and int(rows[-1]["para"]) == para_idx:   # NOX_PT_V13
            _prev = rows[-1]
            if re.search(r"[.!?…][\"»”)]*\s*$", _prev["text"]) and \
                    not (_prev["kind"] == "speech" and kind == "narr" and not text[:1].isupper()):
                pause = PAUSE["sentence"]      # конец предложения (кроме «— Стой! — крикнул он»)
            elif _prev["kind"] != kind:
                pause = PAUSE["turn"]
        pause = int(pause) + extra_pause
        extra_pause = 0
        if kind == "speech" and para_idx in SPEAKER_OVERRIDES and \
                not any(p_ == "=" for p_, _ in SPEAKER_OVERRIDES[para_idx]):   # NOX_PT_V19: роль уже задана разметкой
            role = None
            nt = _norm(text)
            for pref, rl in SPEAKER_OVERRIDES[para_idx]:
                if pref == "*":
                    role = rl
                    break
                np_ = _norm(pref)
                if np_ and (np_ in nt or (len(nt) >= 12 and nt in np_)):   # начало реплики внутри куска
                    role = rl
                    break
            if role is None:
                role = ovr_last.get(para_idx)        # продолжение той же реплики (разрезана по длине)
            if role:
                speaker, conf = role, "script"
                ovr_last[para_idx] = role
        seg_id += 1
        rows.append(dict(id=seg_id, chapter=chapter, ch_title=ch_title, para=para_idx, kind=kind, speaker=speaker,
                         conf=conf, pause_before_ms=pause, text=text))

    for pi, (txt, is_head, is_block) in enumerate(paras):
        if txt == "<<<SCENE>>>":
            tracker.reset_scene()
            narr_run = 0
            extra_pause = max(extra_pause, PAUSE["heading"])
            continue
        if is_head:
            next_is_head = pi + 1 < len(paras) and paras[pi + 1][1]
            if not next_is_head:
                # «Часть первая…» прямо перед «Глава I…» не становится отдельной главой — читается в начале главы
                chapter += 1
                ch_title = txt
            tracker.reset_scene()
            narr_run = 0
            if next_is_head:
                chapter += 1
                ch_title = paras[pi + 1][0]
                add("head", NARRATOR, "high", PAUSE["chapter_start"], normalize_heading(txt), pi)
                chapter -= 1  # следующий заголовок сам сделает += 1 и получит тот же номер
                continue
            add("head", NARRATOR, "high", PAUSE["chapter_start"] if pi == 0 or not paras[pi - 1][1] else PAUSE["heading"],
                normalize_heading(txt), pi)
            continue

        lines = [l.strip() for l in txt.split("\n") if l.strip()]
        _eq = [rl for pref, rl in SPEAKER_OVERRIDES.get(pi, []) if pref == "="]        # NOX_PT_V19: явная разметка
        if _eq:
            _parts = [(m.group(1).strip(), m.group(2).strip())
                      for m in re.finditer("\u27e6([^\u27e7]+)\u27e7([^\u27e6]*)", _eq[-1])]
            _flat = re.sub(r"[^А-Яа-яЁёA-Za-z0-9]", "", "".join(t_ for _, t_ in _parts)).lower()
            _orig = re.sub(r"[^А-Яа-яЁёA-Za-z0-9]", "", txt).lower()
            if _parts and _flat == _orig:
                narr_run = 0
                _main = next((r_ for r_, _ in _parts if r_ != NARRATOR), None)
                if _main:
                    tracker.commit(_main)
                for k_, (r_, t_) in enumerate(_parts):
                    if not t_:
                        continue
                    kind_ = "narr" if r_ == NARRATOR else "speech"
                    if kind_ == "narr":
                        tracker.note_narration(t_)
                    for j, ch in enumerate(chunk_text(t_, max_chars)):
                        pause = PAUSE["dialog"] if (k_ == 0 and j == 0) else PAUSE["chunk"]
                        add(kind_, r_, "script" if kind_ == "speech" else "high", pause, ch, pi)
                continue
            print(f"  разметка абзаца {pi} не совпала с текстом книги (книга правилась?) — абзац разобран по тире", flush=True)
        is_dialog = txt[0] in DASHES
        # NOX_PT_V8: принудительный вид абзаца из сценария («!» — весь абзац речь Роли; «—» — разобрать как реплику)
        force_kind, force_role = None, None
        for pref, rl in SPEAKER_OVERRIDES.get(pi, []):
            if pref in ("!", "—", "–", "-") and rl:
                force_kind, force_role = pref, rl
        if force_kind == "!":
            narr_run = 0
            tracker.note_narration(txt)
            for li, line in enumerate(lines):
                pause = PAUSE["dialog"] if li == 0 else PAUSE["poem_line"]
                for j, ch in enumerate(chunk_text(line, max_chars)):
                    add("speech", force_role, "script", pause if j == 0 else PAUSE["chunk"], ch, pi)
            continue
        if force_kind is not None:
            is_dialog = True

        if not is_dialog:
            # проза / стихи
            narr_run += len(txt)
            if narr_run > 1500:
                tracker.reset_scene()
            tracker.note_narration(txt)
            tracker.note_beat(txt)
            # стихи/посвящение (soft break внутри абзаца): каждая строка — отдельный сегмент с короткой паузой
            for li, line in enumerate(lines):
                pause = PAUSE["para"] if li == 0 else PAUSE["poem_line"]
                for j, ch in enumerate(chunk_text(line, max_chars)):
                    add("narr", NARRATOR, "high", pause if j == 0 else PAUSE["chunk"], ch, pi)
            continue

        # диалог: «— речь, — ремарка. — речь.»
        narr_run = 0
        body = re.sub(r"^[—–-]\s*", "", " ".join(lines))
        parts = SPLIT_RE.split(body)
        # NOX_PT_V12: «— Маргит, — согласился он. Потом кивнул — я похолодел, — и сказал: — Мэри говорит…»:
        # тире после «похолодел,» закрывает вставку внутри ремарки, а не начинает речь (дальше — строчная буква)
        _k = 1
        while _k < len(parts) - 1:
            if len(re.findall(r"(?<![,.!?…;:»”\"])\s[—–]\s", " " + parts[_k] + " ")) % 2 == 1 and parts[_k + 1][:1].islower():
                parts = parts[:_k] + [parts[_k] + " — " + parts[_k + 1]] + parts[_k + 2:]
                continue
            _k += 2
        if force_kind not in (None, "!") and parts and parts[0].rstrip().endswith(":"):
            parts = [""] + parts             # абзац начинается словами автора: «Он сказал: — …»
        speech_parts = parts[0::2]
        narr_parts = parts[1::2]

        speaker, conf = None, None
        for np_ in narr_parts:                  # сначала ищем, кто говорит, по всем ремаркам строки
            s, c = tracker.from_attribution(np_)
            if s:
                speaker, conf = s, c
                break
        if speaker is None:
            speaker, conf = tracker.by_alternation()
        if force_kind is not None and force_role:        # NOX_PT_V8: говорящий задан сценарием
            speaker, conf = force_role, "script"
        tracker.commit(speaker)
        for np_ in narr_parts:
            tracker.note_narration(np_)

        for k, part in enumerate(parts):
            part = part.strip()
            if not part:
                continue
            if k % 2 == 0:
                kind, who, cf = "speech", speaker, conf
            else:
                kind, who, cf = "narr", NARRATOR, "high"
            for j, ch in enumerate(chunk_text(part, max_chars)):
                pause = PAUSE["dialog"] if (k == 0 and j == 0) else PAUSE["chunk"]
                add(kind, who, cf, pause, ch, pi)

    rows = _nox_merge_prepared_rows(HERE, rows)

    # roles.json — шаблон с описаниями голосов; существующий файл не перетираем, только дописываем новые роли
    roles = json.loads(ROLES_PATH.read_text(encoding="utf-8")) if ROLES_PATH.exists() else {
        "language": "Russian",
        "aliases": {},
        "sample_text": "Я расскажу вам эту историю так, как помню её сам: без прикрас, но и без умолчаний. "
                       "Слушайте внимательно — второй раз я повторять не стану.",
        "roles": {},
    }
    counts = Counter(r["speaker"] for r in rows if r["kind"] == "speech")
    counts[NARRATOR] = sum(1 for r in rows if r["kind"] != "speech")
    default_instruct = {
        "m": "A man in his forties with a natural, grounded conversational voice. Clear Russian diction, medium pace.",
        "f": "A woman in her thirties with a natural, warm conversational voice. Clear Russian diction, medium pace.",
    }
    for who, n in counts.most_common():
        if who.startswith(UNKNOWN):
            continue
        if who not in roles["roles"]:
            if who == NARRATOR:
                instr = ("Male narrator around 30, calm and warm storyteller voice with light irony, very clear Russian "
                         "diction, measured audiobook pace, intimate microphone tone.")
            elif who == ME:
                instr = "Young man, 25, slightly ironic, quick and lively, modern speech, clear Russian diction."
            else:
                instr = default_instruct.get(tracker.gender.get(who, "m"), default_instruct["m"])
            roles["roles"][who] = {"instruct": instr, "same_as": None, "count": n}
        else:
            roles["roles"][who]["count"] = n
    rows = _nox_commit_prepared_rows(HERE, rows, FIELDS, roles=roles)

    # статистика
    conf_cnt = Counter(r["conf"] for r in rows if r["kind"] == "speech")
    total_chars = sum(len(r["text"]) for r in rows)
    print(f"Сегментов: {len(rows)}  (глав/заголовков: {chapter}),  знаков: {total_chars}, "
          f"~{total_chars / 16 / 3600:.1f} ч аудио при ~16 зн/с")
    print(f"Реплик (сегментов речи): {sum(counts.values()) - counts[NARRATOR]};  уверенность: {dict(conf_cnt)}")
    print("Говорящие:", ", ".join(f"{w}={n}" for w, n in counts.most_common(25)))
    print(f"Имена, найденные автоматически: {sorted(names)}")
    print(f"-> {CSV_PATH}  (проверьте колонку speaker, особенно conf=low/none и UNKNOWN)\n-> {ROLES_PATH}  "
          f"(опишите голоса; aliases: {{\"старик\": \"Хорват\"}} склеивает роли)")


# ----------------------------------------------------------------------------------------------------------------------
# общие утилиты для TTS-этапов
# ----------------------------------------------------------------------------------------------------------------------

def load_rows():
    with CSV_PATH.open(encoding="utf-8") as f:
        return list(csv.DictReader(f, delimiter=";"))


_TRANSLIT = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                     ["a", "b", "v", "g", "d", "e", "yo", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s", "t",
                      "u", "f", "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"]))


def safe_name(role: str) -> str:
    """Имя файла для роли без кириллицы (надёжнее для аудиобиблиотек на Windows): «Хорват» -> horvat."""
    out = "".join(_TRANSLIT.get(ch, ch) for ch in role.lower())
    out = re.sub(r"[^a-z0-9_\-]+", "_", out).strip("_")
    return out or "role"


def voice_stems(roles: dict) -> dict:
    """Уникальные латинские имена файлов голосов: «голос» -> golos, «Голос» -> golos_2 (без перезаписи друг друга)."""
    stems, used = {}, set()
    for role in roles["roles"]:
        base = safe_name(role)
        stem, k = base, 2
        while stem in used:
            stem, k = f"{base}_{k}", k + 1
        used.add(stem)
        stems[role] = stem
    return stems


def voice_files(role: str, stems: dict | None = None):
    """Пара (wav, txt) образца голоса: сначала свой файл с кириллическим именем роли (если положили вручную),
    иначе латинское имя из voice_stems."""
    own = VOICES / f"{role}.wav"
    if own.exists():
        return own, VOICES / f"{role}.txt"
    stem = (stems or {}).get(role) or safe_name(role)
    return VOICES / f"{stem}.wav", VOICES / f"{stem}.txt"


def load_roles():
    return json.loads(ROLES_PATH.read_text(encoding="utf-8"))


def resolve_role(speaker: str, roles: dict) -> str:
    """speaker из CSV -> имя роли, чей голос использовать (aliases + same_as)."""
    who = roles.get("aliases", {}).get(speaker, speaker)
    if who.startswith(UNKNOWN) or who not in roles["roles"]:
        return roles.get("unknown_role", NARRATOR)
    seen = set()
    while roles["roles"][who].get("same_as") and who not in seen:
        seen.add(who)
        who = roles["roles"][who]["same_as"]
    return who


def seg_key(voice: str, text: str) -> str:
    """Имя wav-файла сегмента = хеш (голос + текст). Поэтому при обновлении рукописи или перенумерации
    сегментов всё уже озвученное переиспользуется, а переозвучивается только изменённый текст."""
    return hashlib.sha1(f"{voice}\n{text}".encode("utf-8")).hexdigest()[:16]


_VSIG = {}
_VST = [None, None]


def voice_sig(role: str, roles: dict) -> str:
    """NOX_PT_V18: отпечаток образца голоса роли (wav + текст образца), 8 знаков; образца ещё нет -> "nov"."""
    now = time.time()
    c = _VSIG.get(role)
    if c and now - c[0] < 20:
        return c[2]
    if _VST[0] is not roles:
        _VST[0], _VST[1] = roles, voice_stems(roles)
    wav_p, txt_p = voice_files(role, _VST[1])
    try:
        a = wav_p.stat()
        key = (a.st_size, a.st_mtime_ns, txt_p.stat().st_mtime_ns if txt_p.exists() else 0)
    except OSError:
        key = None
    if c and c[1] == key:
        sig = c[2]
    elif key is None:
        sig = "nov"
    else:
        try:
            h = hashlib.sha1(wav_p.read_bytes())
            if txt_p.exists():
                h.update(txt_p.read_bytes())
            sig = h.hexdigest()[:8]
        except OSError:
            sig = "nov"
    _VSIG[role] = (now, key, sig)
    return sig


def migrate_voice_keys(rows, roles) -> int:
    """18d: legacy recordings are not relabelled as the current voice sample."""
    if (WORK / "keys_v18.done").exists() or not WAVS.is_dir():
        return 0
    for row in rows:
        old = WAVS / (seg_key(resolve_role(row["speaker"], roles), row["text"]) + ".wav")
        if old.is_file() and old != wav_for(row, roles) and not wav_for(row, roles).exists():
            raise RuntimeError("есть озвучка по прежней схеме имён без подтверждения образца голоса; "
                               "автоматическая миграция остановлена, исходные WAV оставлены на месте")
    return 0


def wav_for(r: dict, roles: dict) -> Path:
    _role = resolve_role(r["speaker"], roles)                       # NOX_PT_V18: + отпечаток образца голоса
    return WAVS / f"{seg_key(_role + '|' + voice_sig(_role, roles), r['text'])}.wav"


def _wav_for_v17(r: dict, roles: dict) -> Path:
    return WAVS / f"{seg_key(resolve_role(r['speaker'], roles), r['text'])}.wav"


def migrate_legacy_wavs(rows, roles) -> int:
    """Старые файлы 000123.wav (по номеру сегмента) -> имена по хешу. Безопасно при нескольких воркерах."""
    return migrate_voice_keys(rows, roles)          # NOX_PT_V18: по номеру больше не переносим — номер мог смениться
    if not WAVS.is_dir():
        return 0
    n = 0
    for r in rows:
        legacy = WAVS / f"{int(r['id']):06d}.wav"
        if not legacy.exists():
            continue
        target = wav_for(r, roles)
        try:
            if target.exists():
                legacy.unlink()
            else:
                os.replace(legacy, target)
            n += 1
        except OSError:
            pass
    if n:
        print(f"перенёс {n} готовых wav на новые имена (по хешу текста)")
    return n


def _atomic_text(path: Path, text: str):
    """NOX_PT_V21: запись через временный файл; в Windows подмена повторяется, если файл в этот миг читают."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    for _k in range(8):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if _k == 7:
                raise
            time.sleep(0.05 * (_k + 1))


def write_wav_atomic(path: Path, data, sr):
    """Пишем во временный файл и переименовываем: сборщик частей никогда не увидит недописанный wav."""
    import soundfile as sf
    tmp = path.with_suffix(".tmp")
    sf.write(str(tmp), data, sr, format="WAV")
    os.replace(tmp, path)


def claim(path: Path) -> bool:
    """Атомарно «занять» сегмент (файл-замок рядом с будущим wav). Несколько воркеров могут работать
    над одной очередью одновременно, не мешая друг другу."""
    lock = path.with_suffix(".lock")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        return True
    except FileExistsError:
        try:
            if time.time() - lock.stat().st_mtime > LOCK_STALE_SEC and not path.exists():
                lock.touch()                 # брошенный замок упавшего воркера — забираем
                return True
        except OSError:
            pass
        return False


def release(path: Path):
    try:
        path.with_suffix(".lock").unlink()
    except OSError:
        pass


def load_qwen(model_id: str, attn: str):
    try:
        import torch
        from qwen_tts import Qwen3TTSModel
    except ImportError:
        sys.exit("pip install -U qwen-tts  (и torch с CUDA)")
    try:                                   # убираем «Setting pad_token_id…» на каждый вызов
        from transformers.utils import logging as hf_logging
        hf_logging.set_verbosity_error()
    except Exception:
        pass
    kw = dict(device_map="cuda:0", dtype=torch.bfloat16)
    if attn != "auto":
        kw["attn_implementation"] = attn
    t0 = time.time()
    model = Qwen3TTSModel.from_pretrained(model_id, **kw)
    print(f"Модель {model_id} загружена за {time.time() - t0:.0f} с")
    return model


def trim_silence(wav, sr, thresh_db=-48.0, pad_ms=60, tail_pad_ms=220):
    """Срезает тишину по краям. Порог — относительно пика фразы (тихие затухающие
    окончания не режутся), в конце оставляем увеличенный запас tail_pad_ms."""
    import numpy as np
    x = np.asarray(wav, dtype="float32")
    if x.ndim > 1:
        x = x.mean(axis=1)
    if len(x) == 0:
        return x
    peak = float(np.max(np.abs(x)))
    if not math.isfinite(peak) or peak <= 0.0:
        return x
    amp = peak * (10 ** (thresh_db / 20))
    idx = np.where(np.abs(x) > amp)[0]
    if len(idx) == 0:
        return x
    start_pad = int(sr * pad_ms / 1000)
    end_pad = int(sr * tail_pad_ms / 1000)
    a, b = max(0, idx[0] - start_pad), min(len(x), idx[-1] + end_pad + 1)
    return x[a:b]


def part_source_seconds(minutes: float, speed: float = 1.0) -> float:
    """Исходная длительность аудио, соответствующая заданной длительности после atempo."""
    try:
        minutes, speed = float(minutes), float(speed)
    except (TypeError, ValueError):
        raise ValueError("длительность части и скорость должны быть числами")
    if not math.isfinite(minutes) or minutes <= 0:
        raise ValueError("длительность части должна быть положительным конечным числом минут")
    if not math.isfinite(speed) or speed <= 0:
        raise ValueError("скорость сборки должна быть положительным конечным числом")
    return minutes * 60.0 * speed


WORD_DICT_PATH = WORK / "accent_dict.csv"
_WORD_DICT = None

# слова-омографы: ударение зависит от смысла; ruaccent решает по контексту, а мы помечаем
# УДВОЕНИЕМ гласной только их (остальным словам движок ставит ударение сам)
HOMOGRAPHS = set("""
замок замка замку замком замке замки замков замкам замками замках
дома стоит стоят духи мука орган органа органе полки белки кружки стрелки гвоздики
ирис атлас атласе хлопок жаркое пора потом села сели село жила пила вести пропасть
берега города леса луга снега окна стены земли воды горы реки доски ноги руки головы дороги
целую дорого крою строю звонок? печи свечи
ночи болото болота болоту болотом болоте болот болотам болотами болотах
болотный болотная болотное болотные болотной болотного болотную болотных болотным болотными
""".split())
HOMOGRAPHS.discard("звонок?")


def load_word_dict() -> dict:
    """work/accent_dict.csv: 'слово<TAB>замена' построчно; замена может содержать '+' перед ударной
    гласной. Пустые строки и строки с # пропускаются. Подстановка — по целому слову."""
    global _WORD_DICT
    if _WORD_DICT is None:
        _WORD_DICT = {}
        if WORD_DICT_PATH.exists():
            for line in WORD_DICT_PATH.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "\t" not in line:
                    continue
                word, fixed = line.split("\t", 1)
                word, fixed = word.strip(), fixed.strip()
                if word and fixed:
                    _WORD_DICT[word] = fixed
        try:                                            # NOX_PT_V21: правила студии (studio/rules_dict.tsv)
            for line in (HERE / "studio" / "rules_dict.tsv").read_text(encoding="utf-8").splitlines():
                if line.strip() and not line.startswith("#") and "\t" in line:
                    word, fixed = line.split("\t", 1)
                    if word.strip() and fixed.strip():
                        _WORD_DICT[word.strip()] = fixed.strip()
        except OSError:
            pass
    return _WORD_DICT


def apply_word_dict(text: str):
    """Заменяет слова словаря на плейсхолдеры (чтобы ruaccent их не трогал) -> (текст, {плейсхолдер: замена})."""
    d = load_word_dict()
    if not d:
        return text, {}
    pattern = re.compile(r"\b(" + "|".join(re.escape(w) for w in sorted(d, key=len, reverse=True)) + r")\b")
    ph = {}

    def repl(m):
        key = f"\ue000{len(ph)}\ue001"
        ph[key] = d[m.group(1)]
        return key

    return pattern.sub(repl, text), ph


def _fix_ruaccent_onnx(acc):
    # ruaccent 1.5.8 + transformers 5: токенизатор не отдаёт token_type_ids, onnx-модель их требует.
    # Оборачиваем session.run каждой модели: недостающие входы дополняем нулями, лишние убираем.
    import numpy as np
    for name in ("accent_model", "omograph_model", "stress_usage_model", "yo_homograph_model"):
        sess = getattr(getattr(acc, name, None), "session", None)
        if sess is None or getattr(sess, "_nox_fixed", False):
            continue
        need = [i.name for i in sess.get_inputs()]
        orig = sess.run

        def run(output_names, feed, *a, _orig=orig, _need=need, **k):
            feed = dict(feed)
            ref = feed.get("input_ids")
            for nm in _need:
                if nm not in feed and ref is not None:
                    feed[nm] = np.zeros_like(ref)
            for nm in list(feed):
                if nm not in _need:
                    del feed[nm]
            return _orig(output_names, feed, *a, **k)

        sess.run = run
        sess._nox_fixed = True


TEXT_LAYER_VERSION = "k1a-v9"   # NOX_PT_V16: ruaccent turbo3   # v5: ударения знаком U+0301 для дообученной модели; v4 = чистый текст
VOWELS_RU = "аеёиоуыэюяАЕЁИОУЫЭЮЯ"


def _ruaccent_marked(acc, t):
    # ruaccent точнее на тексте в нижнем регистре (совет автора stress-модели) -> размечаем lower(),
    # а регистр букв возвращаем из оригинала пословно; если слова не сошлись — надёжный путь без хитростей
    low = t.lower()
    marked = acc.process_all(low)
    if low == t:
        return marked
    orig = re.findall(r"[А-Яа-яЁё]+", t)
    got = re.findall(r"[а-яё+]+", marked)
    if len(orig) != len(got) or any(a.lower() != b.replace("+", "") for a, b in zip(orig, got)):
        return acc.process_all(t)
    it = iter(orig)

    def recase(m):
        o = next(it)
        out, j = [], 0
        for ch in m.group(0):
            if ch == "+":
                out.append(ch)
            else:
                out.append(o[j])
                j += 1
        return "".join(out)
    return re.sub(r"[а-яё+]+", recase, marked)


RUACC_MODEL = None
_RUACC_CHECK = "замок на двери был сломан, а все стояли у дома."


def _load_ruaccent():
    """ruaccent с самой точной моделью омографов. -> готовый объект; RUACC_MODEL = имя загруженной модели.
    Пакет ruaccent не установлен (ImportError) — как раньше; любая неудача на всех моделях — SystemExit с причиной."""
    global RUACC_MODEL
    from ruaccent import RUAccent
    avail = getattr(RUAccent(), "omograph_models_paths", {}) or {}
    order = [m for m in ("turbo3.1", "turbo3") if m in avail] + ["turbo"]
    errs = []
    for m in order:
        try:
            acc = RUAccent()
            acc.load(omograph_model_size=m, use_dictionary=True)
            _fix_ruaccent_onnx(acc)
            _extend_homographs(acc)
            _tune_ruaccent(acc)
            chk = _ruaccent_marked(acc, _RUACC_CHECK)
            if chk.count("+") < 4 or not re.search(r"з\+амок|зам\+ок", chk):
                raise RuntimeError(f"проверочная фраза размечена не полностью: {chk!r}")
            RUACC_MODEL = m
            if errs:
                print("ВНИМАНИЕ: ruaccent: " + "; ".join(errs) + f" -> работаю на запасной модели {m}. "
                      "Пришлите это сообщение в чат.", flush=True)
            return acc
        except Exception as e:                    # в т.ч. ImportError внутри load (onnxruntime и т.п.)
            errs.append(f"{m}: {type(e).__name__}: {str(e)[:160]}")
    raise SystemExit("ruaccent не загрузился ни с одной моделью — без ударений озвучивать нельзя, остановлено:\n  "
                     + "\n  ".join(errs) + "\nПришлите это сообщение в чат.")


def _plus_to_acute(t, sparse=False):
    # '+' ruaccent -> U+0301 после ударной гласной (формат stress-модели); ё и односложные не метим.
    # sparse: метим только слова от 3 слогов и контекстные (омографы ruaccent); словарь имён помечен всегда
    omo = set()
    if sparse:
        try:
            omo = set(getattr(_ACCENTIZER, "omographs", {}).keys())
        except Exception:
            omo = set()

    def f(m):
        w = m.group(0)
        bare = w.replace("+", "")
        nv = sum(ch in VOWELS_RU for ch in bare)
        if "ё" in bare.lower() or nv < 2 or (sparse and nv < 3 and bare.lower() not in omo):
            return bare
        return re.sub(r"\+([" + VOWELS_RU + r"])", "\\1\u0301", w).replace("+", "")
    return re.sub(r"[А-Яа-яЁё]*\+[А-Яа-яЁё]+", f, t)


def _tune_ruaccent(acc):
    # 1) ё: автор пишет ё последовательно (ещё/её/всё/идёт — 7.6 тыс. слов с ё, без пропусков), а модель
    #    ruaccent сама меняла е->ё по догадке (небо->нёбо, все->всё) -> её ё-преобразование выключаем совсем
    acc._process_yo = lambda words, sentence: words
    # 2) считаем слова, которых нет в словаре ruaccent (ударение гадала нейросеть) — для аудита имён
    global _NEURAL_WORDS
    _NEURAL_WORDS = {}
    m = getattr(acc, "accent_model", None)
    if m is not None and hasattr(m, "put_accent"):
        orig = m.put_accent

        def put_accent(word, _orig=orig):
            _NEURAL_WORDS[word.lower()] = _NEURAL_WORDS.get(word.lower(), 0) + 1
            return _orig(word)

        m.put_accent = put_accent


def _dict_pattern():
    # регэксп словаря, терпимый к '+' ruaccent внутри слова: Хорв+ата -> ключ Хорвата
    global _DICT_RE
    if "_DICT_RE" not in globals():
        d = load_word_dict()
        if not d:
            _DICT_RE = None
        else:
            alts = ["\\+?".join(re.escape(c) for c in w) for w in sorted(d, key=len, reverse=True)]
            _DICT_RE = re.compile(r"(?<![А-Яа-яЁёA-Za-z0-9])\+?(" + "|".join(alts) + r")(?![А-Яа-яЁёA-Za-z0-9+])")
    return _DICT_RE


def apply_word_dict_marked(t: str) -> str:
    # словарь автора главнее ruaccent: заменяем слово целиком вместе с его '+'
    rx = _dict_pattern()
    if rx is None:
        return t
    d = load_word_dict()
    return rx.sub(lambda m: d[m.group(1).replace("+", "")], t)


def _extend_homographs(acc):
    # контекстные слова самого ruaccent (ночи/дома/стоит/руки...) + work/stress_words.txt автора
    stop = set("уже потом большая большие большое большую большой большим большими большом мою твою свою".split())
    try:
        HOMOGRAPHS.update(w for w in getattr(acc, "omographs", {}) if w.isalpha() and w not in stop)
    except Exception:
        pass
    p = WORK / "stress_words.txt"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            w = line.split("#")[0].strip().lower()
            if w:
                HOMOGRAPHS.add(w)



# NOX_PT_V11: числа, время, римские номера глав, латиница -> слова (до ruaccent, чтобы слова получили ударения)
_N_ONES = ["ноль", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять", "десять", "одиннадцать",
           "двенадцать", "тринадцать", "четырнадцать", "пятнадцать", "шестнадцать", "семнадцать", "восемнадцать",
           "девятнадцать"]
_N_TENS = ["", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят", "семьдесят", "восемьдесят", "девяносто"]
_N_HUND = ["", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот", "семьсот", "восемьсот", "девятьсот"]
_ORD_ONES = ["", "перв", "втор", "трет", "четвёрт", "пят", "шест", "седьм", "восьм", "девят", "десят", "одиннадцат",
             "двенадцат", "тринадцат", "четырнадцат", "пятнадцат", "шестнадцат", "семнадцат", "восемнадцат",
             "девятнадцат"]
_ORD_TENS = ["", "", "двадцат", "тридцат", "сороков", "пятидесят", "шестидесят", "семидесят", "восьмидесят", "девяност"]


def _num_words(n: int, fem: bool = False) -> str:
    if n == 0:
        return "ноль"
    if n >= 1_000_000:
        return " ".join(_num_words(int(c)) for c in str(n))
    parts = []
    th, n = divmod(n, 1000)
    if th:
        if th == 1:
            parts.append("тысяча")
        else:
            l2, l1 = th % 100, th % 10
            suf = "тысяч" if 11 <= l2 <= 14 else "тысяча" if l1 == 1 else "тысячи" if 2 <= l1 <= 4 else "тысяч"
            parts.append(_num_words(th, fem=True) + " " + suf)
    h, r = divmod(n, 100)
    if h:
        parts.append(_N_HUND[h])
    if r:
        if r < 20:
            w = _N_ONES[r]
        else:
            t_, o = divmod(r, 10)
            w = _N_TENS[t_] + (" " + _N_ONES[o] if o else "")
        if fem:
            w = re.sub(r"\bодин$", "одна", w)
            w = re.sub(r"\bдва$", "две", w)
        parts.append(w)
    return " ".join(parts)


def _ord_fem(n: int, case: str = "nom") -> str:
    """Порядковое женского рода 1..99: «двадцать девятая» / (case='prep') «двадцать девятой»."""
    if not 1 <= n <= 99:
        return _num_words(n)
    t_, o = divmod(n, 10)
    if n < 20:
        stem, last = _ORD_ONES[n], n
    elif o == 0:
        stem, last = _ORD_TENS[t_], n
    else:
        stem, last = _ORD_ONES[o], o
    soft = last == 3                                   # третья / третьей
    end = ("ья" if soft else "ая") if case == "nom" else ("ьей" if soft else "ой")
    word = stem + end
    if n >= 20 and o:
        return _N_TENS[t_] + " " + word
    return word


_ROMAN_RX = r"(?=[IVXLC])(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})"


def _roman(s: str):
    vals = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}
    if not s or not re.fullmatch(_ROMAN_RX, s):
        return None
    total, prev = 0, 0
    for ch in reversed(s):
        v = vals[ch]
        total = total - v if v < prev else total + v
        prev = max(prev, v)
    return total if 1 <= total <= 99 else None


_LAT_SYL = {"KA": "Ка", "UN": "Ун", "IN": "Ин", "ET": "Эт", "CHA": "Ча", "RO": "Ро", "ZO": "Зо", "DO": "До"}
_LAT_WORDS = {"Nox": "Нокс", "Westwood": "Вествуд", "Studios": "Студиос"}
_LAT_DIGRAPHS = [("sch", "ш"), ("ch", "ч"), ("sh", "ш"), ("th", "т"), ("ph", "ф"), ("oo", "у"), ("ee", "и"), ("ck", "к"),
                 ("qu", "кв"), ("ou", "ау")]
_LAT_LETTERS = dict(zip("abcdefghijklmnopqrstuvwxyz",
                        ["а", "б", "к", "д", "е", "ф", "г", "х", "и", "дж", "к", "л", "м", "н", "о", "п", "к", "р", "с",
                         "т", "у", "в", "в", "кс", "и", "з"]))


def _translit(w: str) -> str:
    if w in _LAT_WORDS:
        return _LAT_WORDS[w]
    if w.upper() in _LAT_SYL and w.isupper():
        return _LAT_SYL[w.upper()]
    s = w.lower()
    for a, b in _LAT_DIGRAPHS:
        s = s.replace(a, b)
    s = "".join(_LAT_LETTERS.get(c, c) for c in s)
    return s.capitalize() if w[:1].isupper() else s


def _normalize_numbers(t: str) -> str:
    if not re.search(r"[0-9A-Za-z№·]", t):
        return t
    d = load_word_dict()                                   # словарь автора: фразы с цифрами/латиницей — раньше общих правил
    for k in sorted((k for k in d if re.search(r"[0-9A-Za-z]", k)), key=len, reverse=True):
        if k in t:
            t = t.replace(k, d[k].replace("́", "").replace("+", ""))
    t = re.sub(r"\s*·\s*", ", ", t)
    # римские номера глав: «(XXIX)», «(IX–X)», «глава XVIII», «в XLII», «III. Пещеры…» в начале строки
    def _rng(m):
        a, b = _roman(m.group(1)), _roman(m.group(2))
        if a and b:
            return f"(главы {_ord_fem(a)} — {_ord_fem(b)})"
        return m.group(0)
    t = re.sub(r"\(([IVXLC]+)[–—-]([IVXLC]+)\)", _rng, t)
    t = re.sub(r"\(([IVXLC]+)\)", lambda m: f"(глава {_ord_fem(_roman(m.group(1)))})" if _roman(m.group(1)) else m.group(0), t)
    t = re.sub(r"\b([Гг]лав[аыеуой]*)\s+([IVXLC]+)\b",
               lambda m: f"{m.group(1)} {_ord_fem(_roman(m.group(2)), 'prep' if m.group(1).lower() in ('главе', 'главой') else 'nom')}"
               if _roman(m.group(2)) else m.group(0), t)
    t = re.sub(r"\b([вВ])\s+([IVXLC]{2,})\b",
               lambda m: f"{m.group(1)} главе {_ord_fem(_roman(m.group(2)), 'prep')}" if _roman(m.group(2)) else m.group(0), t)
    t = re.sub(r"^([IVXLC]+)\.\s",
               lambda m: f"Глава {_ord_fem(_roman(m.group(1)))}. " if _roman(m.group(1)) else m.group(0), t)
    # время 23:47
    t = re.sub(r"\b([01]?\d|2[0-3]):([0-5]\d)\b",
               lambda m: _num_words(int(m.group(1))) + " " + ("ноль " + _num_words(int(m.group(2)))
                                                              if m.group(2).startswith("0") and m.group(2) != "00"
                                                              else ("ноль ноль" if m.group(2) == "00" else _num_words(int(m.group(2))))), t)
    t = re.sub(r"№\s*(\d+)", lambda m: "номер " + _num_words(int(m.group(1))), t)
    t = re.sub(r"\d+", lambda m: _num_words(int(m.group(0))), t)
    t = re.sub(r"[A-Za-z]+(?:'[a-z]+)?", lambda m: _translit(m.group(0)), t)
    return t


NOX_STAGE = 21
STRESS_FIX_PATH = WORK / "stress_fixes.tsv"
_SFIX = None
_CAPS_WORDS = set("да не ни но он мы вы ты я и а у о в с к на за из от до по без для про нет все всё чей вон кто что как "
                  "где тут там вот мой мне нам вам они она оно его её их ещё уже или это эти тот так ну ох ах эх ой эй".split())
_VOW17 = "аеёиоуыэюя"


def _decaps(t):
    def one(w, in_phrase):
        low = w.lower()
        nv = sum(c in _VOW17 for c in low)
        if nv == 0:                                   # МВД, КПП — сокращение
            return w
        cvc = len(low) == 3 and low[0] not in _VOW17 and low[1] in _VOW17 and low[2] not in _VOW17   # ТИС, НЕТ, ЧЕЙ
        if in_phrase or len(low) >= 4 or cvc or low in _CAPS_WORDS:
            return w[0] + low[1:]
        return w

    def run(m):                                       # «ЖИВЫМ ИЛИ МЁРТВЫМ»: во фразе капсом все слова — обычные
        seg = m.group(0)
        words = re.findall(r"[А-ЯЁ]+", seg)
        phrase = len(words) > 1 and any(len(x) >= 4 for x in words)
        return re.sub(r"[А-ЯЁ]{2,}", lambda mm: one(mm.group(0), phrase), seg)
    return re.sub(r"(?<![А-Яа-яЁё])[А-ЯЁ]{2,}(?:[ ,\u2014\u2013-]+[А-ЯЁ]{2,})*(?![А-Яа-яЁё])", run, t)


def _load_stress_fixes():
    global _SFIX
    if _SFIX is None:
        _SFIX = {}
        if STRESS_FIX_PATH.exists():
            for line in STRESS_FIX_PATH.read_text(encoding="utf-8").splitlines():
                if not line.strip() or line.startswith("#"):
                    continue
                p = line.split("\t")
                if len(p) < 4:
                    continue
                try:
                    _SFIX.setdefault(p[0].strip(), []).append((int(p[1]), p[2].strip(), p[3].strip()))
                except ValueError:
                    continue
    return _SFIX


def apply_stress_fixes(raw, t):
    """Ударение по месту: k-е слово готового текста реплики -> форма из stress_fixes.tsv (если слово то же)."""
    fx = _load_stress_fixes().get(hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12])
    if not fx:
        return t
    ms = list(re.finditer(r"[А-Яа-яЁё́]+", t))
    rep = {}
    for k, bare, new in fx:
        if k < len(ms) and ms[k].group(0).replace("́", "").lower() == bare.lower():
            w = ms[k].group(0)
            rep[k] = new[:1].upper() + new[1:] if w[:1].isupper() else new
        else:
            globals()["SFIX_MISSED"] = globals().get("SFIX_MISSED", 0) + 1
    if not rep:
        return t
    out, pos = [], 0
    for k, m in enumerate(ms):
        if k in rep:
            out.append(t[pos:m.start()])
            out.append(rep[k])
            pos = m.end()
    out.append(t[pos:])
    return "".join(out)


def _parens(t):
    """NOX_PT_V18: «смотришь (тогда он притворялся мёртвым), а» -> «смотришь — тогда он притворялся мёртвым, а».
    Скобок движок не произносит и на них сбивается; тире даёт ту же паузу, что делает чтец перед вставкой."""
    t = t.replace("„", "").replace("‚", "")
    if "(" not in t and ")" not in t:
        return t
    t = re.sub(r"\s*\(\s*", " \x02 ", t)
    t = re.sub(r"\s*\)\s*", " \x03 ", t)
    t = re.sub(r"^\s*\x02\s*", "", t)                          # скобка в начале реплики — просто убрать
    t = re.sub(r"([.!?…])\s*\x02\s*", r"\1 ", t)          # после конца предложения — убрать
    t = re.sub(r"([.!?…,;:])\s*\x03", r"\1", t)           # «(Это было давно.)» — знак уже есть
    t = re.sub(r"\s*\x03\s*([.,!?…;:])", r"\1", t)        # «мёртвым), а» -> «мёртвым, а»
    t = re.sub(r"\s*\x03\s*$", "", t)
    t = re.sub(r"\s*[\x02\x03]\s*", " — ", t)             # остальное — пауза-тире
    t = re.sub(r"(—\s*){2,}", "— ", t)
    t = re.sub(r"[ \t]{2,}", " ", t).strip()
    return t


def prepare_text(text: str, use_num2words: bool, accent: str) -> str:
    # NOX_PT_V3/v5: ruaccent (без его ё, в нижнем регистре) -> словарь автора -> знак ударения (acute) или удвоение (dbl)
    t = text.replace("\u00ab", "").replace("\u00bb", "").replace("\u201c", "").replace("\u201d", "")
    t = _parens(t)                                   # NOX_PT_V18: скобки -> паузы-тире (до ruaccent: он их склеивал)
    t = _decaps(t)                                   # NOX_PT_V17: «НЕ ВХОДИТЬ» -> «Не Входить» (не по буквам)
    t = _normalize_numbers(t)                        # NOX_PT_V11: 23:47, (XXIX), №14, KA 7 -> слова
    t = re.sub(r"(?<=[А-яЁё])-(?=[А-яЁё])", " ", t)      # дефис внутри слова -> пробел (кто-то -> кто то)
    t = t.replace("\u2026", "...").replace("\u2013", "\u2014")   # «…» и «–» ruaccent вырезает — заменяем
    if use_num2words:
        try:
            from num2words import num2words
            t = re.sub(r"\d+", lambda m: num2words(int(m.group(0)), lang="ru"), t)
        except ImportError:
            pass
    if accent != "none":
        try:
            global _ACCENTIZER
            if "_ACCENTIZER" not in globals():
                _ACCENTIZER = _load_ruaccent()            # NOX_PT_V16: turbo3.1/turbo3, проверка, громкий отказ
            t = _ruaccent_marked(_ACCENTIZER, t).replace(" ~ ", " \u2014 ")   # '+' перед ударной гласной
        except ImportError:
            global _NO_RUACCENT
            if "_NO_RUACCENT" not in globals():
                _NO_RUACCENT = True
                print("ruaccent не установлен: контекстные ударения (замок/дома/ночи...) не ставятся", flush=True)
        except Exception as e:                   # ruaccent споткнулся: сегмент без контекстных ударений, воркер живёт
            global _RUACCENT_ERRS
            _RUACCENT_ERRS = globals().get("_RUACCENT_ERRS", 0) + 1
            if _RUACCENT_ERRS <= 3:
                print(f"ruaccent: {type(e).__name__}: {str(e)[:100]} -> сегмент без контекстных ударений", flush=True)
    t = apply_word_dict_marked(t)                      # словарь автора — поверх ruaccent
    if accent in ("acute", "sparse"):
        t = _plus_to_acute(t, accent == "sparse")
        t = apply_stress_fixes(text, t)              # NOX_PT_V17: ударения по месту (work/stress_fixes.tsv)
    elif accent == "dbl":
        def _dbl(m):
            w = m.group(0)
            i = w.index("+")
            bare = w.replace("+", "")
            if i == 0 or bare[i] in "ёЁ" or bare.lower() not in HOMOGRAPHS:   # контекстные слова; не первая буква; ё и так ударная
                return bare
            return bare[:i] + bare[i] + bare[i:]
        t = re.sub(r"[А-Яа-яЁё]*\+[А-Яа-яЁё]+", _dbl, t)
        t = t.replace("+", "")
    elif accent == "none":                            # чистый текст: ни '+', ни знаков ударения (и из словаря тоже)
        t = t.replace("+", "").replace("\u0301", "")
    return t


# ----------------------------------------------------------------------------------------------------------------------
# 2. CAST — создаём голоса ролей по текстовому описанию (VoiceDesign) -> work/voices/<role>.wav + .txt
# ----------------------------------------------------------------------------------------------------------------------

def cast(args):
    import soundfile as sf
    roles = load_roles()
    stems = voice_stems(roles)
    aliased = set(roles.get("aliases", {}))          # «старик» -> «Хорват»: свой голос ему не нужен
    VOICES.mkdir(parents=True, exist_ok=True)
    todo = [r for r, cfg in roles["roles"].items()
            if not cfg.get("same_as") and (r not in aliased or (args.only and r in args.only))
            and (not args.only or r in args.only)
            and (args.force or not voice_files(r, stems)[0].exists())]
    if not todo:
        print("Все голоса уже есть в", VOICES, "(--force чтобы пересоздать, --only РОЛЬ чтобы выбрать)")
        return
    model = load_qwen(args.model or "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign", args.attn)
    sample = roles.get("sample_text")
    lang = roles.get("language", "Russian")
    for role in todo:
        cfg = roles["roles"][role]
        text = cfg.get("sample_text") or sample
        if args.seed is not None:
            import torch
            torch.manual_seed(args.seed)
        wavs, sr = model.generate_voice_design(text=text, language=lang, instruct=cfg["instruct"])
        wav_p, txt_p = voice_files(role, stems)
        sf.write(str(wav_p), wavs[0], sr)
        txt_p.write_text(text, encoding="utf-8")
        print(f"[cast] {role}: {wav_p}  ({len(wavs[0]) / sr:.1f} с)")
    print("Послушайте образцы; не понравился — поправьте instruct в roles.json и: cast --only РОЛЬ --force --seed N\n"
          "Можно вместо этого положить свой wav (10–20 с чистой речи) + .txt с его расшифровкой в", VOICES,
          "(имя файла — имя роли или его транслит, напр. Хорват.wav или horvat.wav)")


# ----------------------------------------------------------------------------------------------------------------------
# 3. SYNTH — озвучка сегментов клонированием (Base), батчами, по ролям; возобновляемая
# ----------------------------------------------------------------------------------------------------------------------

def build_queue(rows, roles, order: str, window: int):
    """Очередь на озвучку. order=book: порядок книги, но внутри окон по `window` сегментов — сортировка по длине
    (батчи ровные, а главы готовятся по порядку, и части книги можно слушать, не дожидаясь конца).
    order=length: глобальная сортировка по длине (чуть быстрее, но книга готова только вся сразу)."""
    items = [(resolve_role(r["speaker"], roles), r) for r in rows]
    if order == "length":
        items.sort(key=lambda x: len(x[1]["text"]))
        return items
    out = []
    for i in range(0, len(items), window):
        chunk = items[i:i + window]
        chunk.sort(key=lambda x: len(x[1]["text"]))
        out.extend(chunk)
    return out


def synth(args):
    rows = load_rows()
    roles = load_roles()
    WAVS.mkdir(parents=True, exist_ok=True)
    lang = roles.get("language", "Russian")
    migrate_legacy_wavs(rows, roles)

    if args.chapter is not None:
        rows = [r for r in rows if int(r["chapter"]) in args.chapter]
    if args.shard:
        i, n = (int(x) for x in args.shard.split("/"))
        rows = [r for k, r in enumerate(rows) if k % n == i]
    total_all = len(rows)
    rows = [r for r in rows if not wav_for(r, roles).exists()]
    if not rows:
        print("Нечего озвучивать: все сегменты уже готовы")
        return

    by_role = Counter(resolve_role(r["speaker"], roles) for r in rows)
    print("План:", ", ".join(f"{role}={n}" for role, n in by_role.most_common()))
    queue = build_queue(rows, roles, args.order, args.window)

    model = load_qwen(args.model or "Qwen/Qwen3-TTS-12Hz-1.7B-Base", args.attn)
    stems = voice_stems(roles)
    prompts = {}
    for role in by_role:
        wav_p, txt_p = voice_files(role, stems)
        if not wav_p.exists():
            sys.exit(f"Нет образца голоса {wav_p} — сначала `cast` (или положите свой wav+txt)")
        prompts[role] = model.create_voice_clone_prompt(
            ref_audio=str(wav_p), ref_text=txt_p.read_text(encoding="utf-8").strip() if txt_p.exists() else "",
            x_vector_only_mode=not txt_p.exists())

    total_chars = sum(len(r["text"]) for _, r in queue)
    done, done_chars, t0, audio_sec = 0, 0, time.time(), 0.0
    max_items, budget = max(1, args.batch), max(1, args.char_budget)
    batch_fail_streak = 0
    failed_log = WORK / "failed.txt"
    suspicious_log = WORK / "suspicious.txt"
    tag = f"[w{args.worker}]" if args.worker else "[synth]"

    def cuda_empty():
        try:
            import torch
            torch.cuda.empty_cache()
        except Exception:
            pass

    def vram_free_gb():
        try:
            import torch
            free, _ = torch.cuda.mem_get_info()
            return free / 2 ** 30
        except Exception:
            return None

    def cap_for(texts):
        """Лимит кадров кодека на батч: ~2,5x от нормальной длины самой длинной реплики (12,5 кадра/с, ~13 зн/с).
        Не даёт одной «забормотавшей» реплике держать весь батч до 80 секунд."""
        return int(min(args.max_new_tokens, max(len(t) for t in texts) * 2.4 + 40))

    def flag(r, note):
        with suspicious_log.open("a", encoding="utf-8") as f:
            f.write(f"{r['id']}\t{note}\t{r['text'][:80]}\n")

    def save(r, wav, sr, cap):
        nonlocal audio_sec
        raw = len(wav) / sr
        x = trim_silence(wav, sr) if args.trim else wav
        write_wav_atomic(wav_for(r, roles), x, sr)
        dur = len(x) / sr
        audio_sec += dur
        if raw >= 0.97 * cap / FRAME_RATE:
            flag(r, f"{dur:.1f}s упёрлось в лимит (обрезано или бормотание)")
        elif dur > 0.2 * len(r["text"]) + 3:
            flag(r, f"{dur:.1f}s слишком длинно для текста")

    def gen(batch, texts):
        cap = cap_for(texts)
        prompt_items = [prompts[role][0] for role, _ in batch]
        if len(texts) == 1:
            wavs, sr = model.generate_voice_clone(text=texts[0], language=lang, voice_clone_prompt=prompt_items,
                                                  max_new_tokens=cap)
        else:
            wavs, sr = model.generate_voice_clone(text=texts, language=[lang] * len(texts),
                                                  voice_clone_prompt=prompt_items, max_new_tokens=cap)
        return wavs, sr, cap

    def gen_one(role, r, text):
        try:
            wavs, sr, cap = gen([(role, r)], [text])
            save(r, wavs[0], sr, cap)
        except Exception as e:                          # один сегмент не вышел — пишем в failed.txt и идём дальше
            cuda_empty()
            print(f"  !! сегмент {r['id']} не озвучен: {type(e).__name__}: {str(e)[:100]}")
            with failed_log.open("a", encoding="utf-8") as f:
                f.write(f"{r['id']}\t{r['text'][:80]}\n")

    def take_batch(pos):
        """Следующий батч из очереди: пропускаем занятое другими воркерами и уже готовое; батч ограничен
        числом реплик и «бюджетом» (реплик x длина самой длинной <= budget)."""
        batch, skipped = [], 0
        while pos < len(queue) and len(batch) < max_items:
            role, r = queue[pos]
            p = wav_for(r, roles)
            if p.exists():
                pos += 1
                continue
            if batch and (len(batch) + 1) * len(r["text"]) > budget:
                break
            if not claim(p):
                pos += 1
                skipped += 1
                continue
            batch.append((role, r))
            pos += 1
        return batch, pos, skipped

    pos, skipped_total = 0, 0
    current = []
    try:
        while pos < len(queue):
            free = vram_free_gb()
            if free is not None and free < args.vram_reserve and max_items > 1:
                max_items, budget = max(1, max_items - 2), max(200, int(budget * 0.7))
                print(f"{tag} свободной VRAM {free:.1f} ГБ — батч до {max_items} реплик, бюджет {budget}")
            batch, pos, skipped = take_batch(pos)
            skipped_total += skipped
            if not batch:
                continue
            current = batch
            n = len(batch)
            texts = [prepare_text(r["text"], args.num2words, args.accent) for _, r in batch]
            n_roles = len({role for role, _ in batch})
            label = batch[0][0] if n_roles == 1 else f"{n_roles} ролей"
            if n == 1:
                gen_one(batch[0][0], batch[0][1], texts[0])
            else:
                try:
                    wavs, sr, cap = gen(batch, texts)
                    for (_, r), w in zip(batch, wavs):
                        save(r, w, sr, cap)
                    batch_fail_streak = 0
                except Exception as e:
                    cuda_empty()
                    if "out of memory" in str(e).lower():     # не влезло в VRAM — уменьшаем батч и повторяем
                        for _, r in batch:
                            release(wav_for(r, roles))
                        max_items, budget = max(1, n // 2), max(200, budget // 2)
                        print(f"{tag} VRAM не хватило: батч до {max_items} реплик, бюджет {budget} зн.")
                        pos = queue.index(batch[0])          # откат к первому сегменту батча — возьмём их заново
                        current = []
                        continue
                    batch_fail_streak += 1
                    print(f"{tag} батч не прошёл ({type(e).__name__}: {str(e)[:100]}) — эти {n} по одной")
                    for (role, r), t in zip(batch, texts):
                        gen_one(role, r, t)
                    if batch_fail_streak >= 2:
                        max_items = 1
                        print(f"{tag} батчи дважды подряд не прошли — дальше по одной реплике")
            for _, r in batch:
                release(wav_for(r, roles))
            current = []
            done += n
            done_chars += sum(len(r["text"]) for _, r in batch)
            el = time.time() - t0
            eta = (total_chars - done_chars) * el / max(done_chars, 1) / 60
            ready = sum(1 for _ in WAVS.glob("*.wav"))
            print(f"{tag} {done}/{len(queue)} (x{n:<2}) {label:<12} {el / 60:5.1f} мин, аудио {audio_sec / 60:6.1f} мин, "
                  f"RTF={el / max(audio_sec, 1e-6):.3f}, осталось ~{eta:.0f} мин | всего готово {ready}/{total_all}")
    finally:
        for _, r in current:                            # Ctrl+C посреди батча — отпускаем замки
            release(wav_for(r, roles))
    print("Готово:", WAVS, f"(пропущено занятых другими воркерами: {skipped_total})")
    if failed_log.exists():
        print(f"Не озвучены сегменты из {failed_log} — запустите synth ещё раз (готовые пропустятся)")
    if suspicious_log.exists():
        print(f"Подозрительные сегменты записаны в {suspicious_log}: послушайте; чтобы переозвучить — удалите их wav "
              f"и запустите synth снова")

# ----------------------------------------------------------------------------------------------------------------------
# 4. BUILD — главы из wav с паузами -> m4b с оглавлением (ffmpeg)
# ----------------------------------------------------------------------------------------------------------------------

def find_ffmpeg():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    tools = HERE / "tools"                     # куда его кладёт run_all.ps1
    if tools.is_dir():
        for p in tools.rglob("ffmpeg.exe"):
            return str(p)
    return None


def load_durations():
    try:
        return json.loads(DUR_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def duration_of(path: Path, cache: dict) -> float:
    import soundfile as sf
    try:
        k = f"{path.stem}:{path.stat().st_size}"   # NOX_PT_V13
    except OSError:
        k = path.stem
    if k not in cache:
        info = sf.info(str(path))
        cache[k] = info.frames / info.samplerate
    return cache[k]


TEMPO_G = 1.0        # общий множитель темпа при сборке (--tempo)
TEMPO_EQ = True      # выравнивание: медленные реплики подтягиваются к медиане темпа своей роли (до x1.25)
PAUSE_SCALE = 0.85   # множитель пауз между сегментами при сборке (--pauses)
TRIM_PAUSES = True   # сжимать длинные паузы внутри фраз (--no-trim-pauses выключает)
SQZ_KEEP = float(SETTINGS.get("sqz_keep", 0.35))   # паузы внутри фразы короче этого не трогаются, с
SQZ_CAP = float(SETTINGS.get("sqz_cap", 0.8))      # и после сжатия не длиннее этого, с


def _squeeze_pauses(x, sr, keep=0.35, frac=0.4, cap=0.8):
    # паузы внутри фразы длиннее keep сжимаются: new = keep + (dur-keep)*frac, не больше cap;
    # вырезается середина паузы (края затухания остаются) — без щелчков, речь не трогается
    import numpy as np
    w = int(sr * 0.01)
    k = len(x) // w
    if k < 20:
        return x
    a = np.abs(x[:k * w]).reshape(k, w).max(axis=1)
    thr = max(1.5e-3, 0.012 * float(np.percentile(a, 95)))   # NOX_PT_V13: тихие окончания слов — не пауза
    sil = a < thr
    out, i = [], 0
    while i < k:
        j = i
        while j < k and sil[j] == sil[i]:
            j += 1
        if sil[i] and (j - i) * 0.01 > keep:
            dur = (j - i) * 0.01
            n_keep = int(min(cap, keep + (dur - keep) * frac) / 0.01)
            head = n_keep // 2
            tail = n_keep - head
            out.append(_fade(x[i * w:(i + head) * w], 0, int(sr * 0.005)))   # NOX_PT_V15: стык без щелчка
            out.append(_fade(x[(j - tail) * w:j * w], int(sr * 0.005), 0))
        else:
            out.append(x[i * w:j * w])
        i = j
    out.append(x[k * w:])
    return np.concatenate(out)
RE_RU_LETTERS = re.compile(r"[^А-Яа-яЁё]")


def _stretch(x, sr, factor, key):
    # ускорение без изменения тона (ffmpeg atempo); результат кэшируется в work/tempo
    import subprocess
    import soundfile as sf
    if abs(factor - 1.0) < 0.015:
        return x
    cdir = WORK / "tempo"
    cdir.mkdir(exist_ok=True)
    cp = cdir / f"{key}_{factor:.3f}.wav"
    if cp.exists():
        y, _ = sf.read(str(cp), dtype="float32")
        return y
    ff = find_ffmpeg()
    if not ff:
        return x
    src = cdir / f"{key}_src.wav"
    tmp = cdir / f"{key}_{factor:.3f}.tmp.wav"
    import numpy as np
    sf.write(str(src), np.concatenate([x, np.zeros(int(sr * 0.3), dtype="float32")]), sr, format="WAV",
             subtype="PCM_16")   # NOX_PT_V13: запас тишины — конец слова не съедается
    r = subprocess.run([ff, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
                        "-af", atempo_chain(factor), str(tmp)])
    try:
        src.unlink()
    except OSError:
        pass
    if r.returncode != 0 or not tmp.exists():
        return x
    y, _ = sf.read(str(tmp), dtype="float32")
    y = y[:int(len(x) / factor) + int(sr * 0.03)]          # запас обратно (вся речь внутри)
    sf.write(str(tmp), y, sr, format="WAV", subtype="PCM_16")
    os.replace(tmp, cp)
    return y


EQ_MAX = float(SETTINGS.get("eq_max", 1.15))      # NOX_PT_V13
NORM_ON = bool(SETTINGS.get("norm", True))
NORM_DB = float(SETTINGS.get("norm_db", -20.0))


def _active_sec(x, sr):
    # длительность звучащей речи (паузы длиннее 0,15 с не считаются) — честное сравнение темпа реплик
    import numpy as np
    w = int(sr * 0.01)
    k = len(x) // w
    if k < 5:
        return len(x) / sr
    a = np.abs(x[:k * w]).reshape(k, w).max(axis=1)
    sil = a < max(1.5e-3, 0.012 * float(np.percentile(a, 95)))
    edges = np.flatnonzero(np.diff(np.concatenate([[0], sil.astype(np.int8), [0]])))
    runs = edges[1::2] - edges[0::2]
    n_sil = int(runs[runs > 15].sum())
    return max((k - n_sil) * 0.01, 0.05)


def _level(x, sr, target_db=-20.0):
    # громкость звучащей речи -> target_db (RMS); поправка не больше ±6 дБ, пик не выше -0,3 дБ
    import numpy as np
    w = int(sr * 0.02)
    k = len(x) // w
    if k < 3:
        return x
    rms = np.sqrt(np.mean(np.square(x[:k * w].reshape(k, w)), axis=1))
    act = rms[rms > max(1e-3, 0.1 * float(np.percentile(rms, 95)))]
    if len(act) == 0:
        return x
    cur = 20 * np.log10(float(np.sqrt(np.mean(np.square(act)))) + 1e-9)
    g = min(max(10 ** ((target_db - cur) / 20), 0.5), 2.0)
    g = min(g, 0.966 / (float(np.max(np.abs(x))) + 1e-9))
    return (x * g).astype("float32")


_OKV_C = [0.0, ""]


def _okv_req():
    # NOX_PT_V20: метка ТЕКУЩЕЙ проверки распознаванием (verify пишет её в work/asr_okv.txt при запуске, напр. «v5»);
    # пустая строка — проверка для этой книги не запускалась
    if time.time() - _OKV_C[0] > 20:
        try:
            _OKV_C[1] = (WORK / "asr_okv.txt").read_text(encoding="utf-8").strip()
        except OSError:
            _OKV_C[1] = ""
        _OKV_C[0] = time.time()
    return _OKV_C[1]


def _asr_gate():
    # проверка распознаванием включена и проверяльщик жив (его сердцебиение не старше 10 минут)
    if not SETTINGS.get("asr_check"):
        return False
    # Последовательный режим — автор слушает часть и принимает её вручную.
    # ASR блокирует готовность только в автоматическом режиме «Вся книга».
    _mode = globals().get("by_parts")
    if callable(_mode) and _mode():
        return False
    if _okv_req():          # NOX_PT_V20: проверка этой книги запущена — сборка ждёт её с первой минуты, даже пока она грузится
        return True
    try:
        return time.time() - (WORK / "asr_active.txt").stat().st_mtime < 600
    except OSError:
        return False


def _seg_ok(r, roles, gate):
    p = wav_for(r, roles)
    if not gate:
        return p.exists()
    try:
        st_ = p.stat()
        _okt = p.with_suffix(".ok").read_text(encoding="utf-8")   # NOX_PT_V15
        _v = _okv_req()                                       # NOX_PT_V20: метка текущей проверки, не прежней
        return _okt.split("\t")[0] == f"{st_.st_size}:{st_.st_mtime_ns}" and "\tv2" in _okt \
            and (not _v or _v in _okt.strip().split("\t"))
    except OSError:
        return False


def _part_sig(rows_slice, roles):
    # отпечаток содержимого части: какие записи (имя, размер, время записи) и паузы в неё входят
    h = hashlib.blake2b(digest_size=8)
    for r in rows_slice:
        p = wav_for(r, roles)
        try:
            st_ = p.stat()
        except OSError:
            return None
        h.update(f"{p.name}:{st_.st_size}:{st_.st_mtime_ns}:{r['pause_before_ms']}|".encode("utf-8"))
        try:                                                # NOX_PT_V20: границы предложений — по содержимому
            h.update(b"g" + p.with_suffix(".gaps").read_bytes() + b"|")
        except OSError:
            pass
    return h.hexdigest()


def _concat_parts(files, out, title, author):
    # книга целиком = склейка готовых частей без перекодирования (быстро, память не нужна, громкость как в частях)
    ff = find_ffmpeg()
    if not ff or not files:
        return False
    lst = WORK / "concat.txt"
    lst.write_text("".join("file '" + str(f).replace("\\", "/") + "'\n" for f in files), encoding="utf-8")
    tmp = out.with_name(out.stem + ".tmp" + out.suffix)
    cmd = [ff, "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
           "-map", "0:a", "-c", "copy", "-map_metadata", "-1", "-metadata", f"title={title}", "-metadata", f"artist={author}"]
    if out.suffix.lower() == ".mp3":
        cmd += ["-id3v2_version", "3", "-f", "mp3"]
    else:
        cmd += ["-movflags", "+faststart", "-f", "ipod"]
    r = subprocess.run(cmd + [str(tmp)])
    if r.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        return False
    os.replace(tmp, out)
    lst.unlink(missing_ok=True)
    return True


SENT_GAP_ON = False   # NOX_PT_V15 -> V16: вставка тишины в готовую запись отключена (резала слова)
_TL = []                                                 # карта времени последней собранной части: (начало, конец, строка)
_SENT_END = re.compile(r"[.!?…][\"»”)]*\s+(?=[—–-]?\s*[«\"(]?[А-ЯЁA-Z0-9])")


def _sent_gaps(x, sr, text, target):
    # Внутри одного куска движок иногда почти не делает паузу после точки — следующее предложение «налезает» на
    # предыдущее. Знаки препинания текста по порядку сопоставляются с паузами в записи (по доле прочитанных букв);
    # пауза, которая пришлась на конец предложения, дотягивается до target секунд. Остальные паузы не трогаются.
    import numpy as np
    letters = re.compile(r"[А-Яа-яЁёA-Za-z0-9]")
    total = len(letters.findall(text))
    marks = []
    for m in re.finditer(r"[,;:.!?…—–]+[\"»”)]*\s+", text):
        frac = len(letters.findall(text[:m.start()])) / max(total, 1)
        is_sent = bool(re.match(r"[.!?…]", m.group(0))) and \
            re.match(r"[—–-]?\s*[«\"(]?[А-ЯЁA-Z0-9]", text[m.end():]) is not None
        if 0.02 < frac < 0.98:
            marks.append((frac, is_sent))
    if not any(sn for _, sn in marks) or target <= 0:
        return x
    w = int(sr * 0.01)
    k = len(x) // w
    if k < 30:
        return x
    a = np.abs(x[:k * w]).reshape(k, w).max(axis=1)
    thr = max(1.5e-3, 0.012 * float(np.percentile(a, 95)))
    loud = np.flatnonzero(a >= thr)
    if len(loud) < 10:
        return x
    s0, s1 = int(loud[0]), int(loud[-1])
    sil = (a[s0:s1 + 1] < thr).astype(np.int8)
    edges = np.flatnonzero(np.diff(np.concatenate([[0], sil, [0]])))
    runs = [(s0 + int(edges[j]), s0 + int(edges[j + 1])) for j in range(0, len(edges), 2)]
    runs = [(b, e) for b, e in runs if e - b >= 8]                 # только настоящие паузы, от 80 мс
    if not runs:
        return x
    span = s1 - s0
    win = max(30.0, 0.08 * span)   # осторожно: без распознавания место паузы известно лишь примерно
    M, N = len(marks), len(runs)
    exp = [s0 + span * f for f, _ in marks]
    cen = [(b + e) / 2 for b, e in runs]
    # монотонное сопоставление «знак <-> пауза» (как выравнивание): максимум суммы (1 - расстояние/окно)
    best = [[0.0] * (N + 1) for _ in range(M + 1)]
    back = [[0] * (N + 1) for _ in range(M + 1)]
    for i in range(1, M + 1):
        for j in range(1, N + 1):
            opts = [(best[i - 1][j], 1), (best[i][j - 1], 2)]
            d = abs(exp[i - 1] - cen[j - 1])
            if d < win:
                opts.append((best[i - 1][j - 1] + 1.0 - d / win + 0.02 * (runs[j - 1][1] - runs[j - 1][0])
                             - (0.01 if marks[i - 1][1] else 0.0), 3))   # при равенстве — не трогать
            best[i][j], back[i][j] = max(opts)
    ins, i, j = [], M, N
    tgt = int(round(target * 100))
    while i > 0 and j > 0:
        bk = back[i][j]
        if bk == 3:
            if marks[i - 1][1]:
                b, e = runs[j - 1]
                if e - b < tgt:
                    ins.append(((b + e) // 2, tgt - (e - b)))
            i, j = i - 1, j - 1
        elif bk == 1:
            i -= 1
        else:
            j -= 1
    if not ins:
        return x
    out, prev, nf = [], 0, int(sr * 0.005)
    for pos, add in sorted(ins):
        out.append(_fade(x[prev:pos * w], nf if prev else 0, nf))
        out.append(np.zeros(add * w, dtype="float32"))
        prev = pos * w
    out.append(_fade(x[prev:], nf, 0))
    return np.concatenate(out)


def cut_tail_db(x, sr) -> float:
    # громкость последних 40 мс записи относительно громкой речи (95-й процентиль), дБ. Естественный конец фразы
    # затухает до тишины (−40 дБ и ниже); если запись кончается на звуке (−16 дБ и выше) — конец слова обрублен.
    import numpy as np
    x = np.asarray(x, dtype="float32")
    if x.ndim > 1:
        x = x.mean(axis=1)
    w = int(sr * 0.01)
    k = len(x) // w
    if k < 20:
        return -99.0
    rms = np.sqrt(np.mean(np.square(x[:k * w].reshape(k, w)), axis=1)) + 1e-9
    ref = float(np.percentile(rms, 95))
    tail = float(np.sqrt(np.mean(np.square(x[-int(sr * 0.04):])))) + 1e-9
    return round(20 * np.log10(tail / ref), 1)


TAIL_DB_MAX = float(SETTINGS.get("tail_db", -16))
_VOWELS = set("аеёиоуыэюяАЕЁИОУЫЭЮЯ")
_PLOSIVES = set("тдкгпбцчТДКГПБЦЧ")


def _db2(x, sr, ms=2):
    # громкость кадрами по 2 мс относительно громкой речи (95-й процентиль кадров по 10 мс), дБ
    import numpy as np
    w = max(1, int(sr * ms / 1000))
    k = len(x) // w
    rms = np.sqrt(np.mean(np.square(x[:k * w].reshape(k, w)), axis=1)) + 1e-9
    m = len(x) // (5 * w)
    r10 = np.sqrt(np.mean(np.square(x[:m * 5 * w].reshape(m, 5 * w)), axis=1)) + 1e-9 if m else rms
    return 20 * np.log10(rms / float(np.percentile(r10, 95))), w


def _last_letter(text):
    for ch in reversed(text or ""):
        if ch.isalpha() and ch not in "ьъЬЪ":
            return ch
    return ""


def _speech_runs(db, thr=-40.0, min_len=20):
    import numpy as np
    on = (db > thr).astype(np.int8)
    e = np.flatnonzero(np.diff(np.concatenate([[0], on, [0]])))
    return [(int(e[i]), int(e[i + 1])) for i in range(0, len(e), 2) if e[i + 1] - e[i] >= min_len]


_CONS16 = set("бвгджзйклмнпрстфхцчшщ")


def _edge_word_ok(text, head=True):
    """Можно ли снимать щелчок у первого (head) / последнего слова: слово с гласной и без стечения согласных на краю,
    где внутри слова бывает законная тишина (смычка перед т/к/п: «Спасибо», «Кто», «Макс»; предлог «С тех пор»; «Хм»)."""
    ws = re.findall(r"[А-Яа-яЁёA-Za-z]+", (text or "").replace("́", "").replace("+", ""))
    if not ws:
        return False
    w = (ws[0] if head else ws[-1]).lower()
    if not any(c in _VOWELS for c in w):
        return False
    if head:
        return not (len(w) >= 2 and w[0] in _CONS16 and w[1] in "тдкгпбцч")
    w = w.rstrip("ьъ")
    return not (len(w) >= 2 and w[-2] in "тдкгпбцч" and w[-1] in "сзшщжфхв")


def declick(x, sr, text=""):
    """Щелчки по краям записи (найдены на записях книги 1): в первых ~80 мс — всплеск шума, обрывающийся в тишину
    (стык образца голоса и новой фразы в кодеке), и короткий щелчок сразу после последнего слова. Оба гасятся.
    Отзвук взрывной согласной на конце (т, к, п…) не трогается. -> (запись, что сделано)"""
    import numpy as np
    x = np.array(x, dtype="float32", copy=True)
    if len(x) < sr // 5:
        return x, []
    db, w = _db2(x, sr)
    runs = [(a, b) for a, b in _speech_runs(db) if float(db[a:b].max()) > -20]   # настоящая речь громкая
    if not runs:
        return x, []
    fixes = []
    s0 = runs[0][0]
    quiet = (db[:s0] < -60).astype(np.int8)
    e = np.flatnonzero(np.diff(np.concatenate([[0], quiet, [0]])))
    for i in range(0, len(e), 2):
        g0, g1 = int(e[i]), int(e[i + 1])
        if g0 == 0 or g1 - g0 < 15:
            continue
        if (g0 <= 75 and g1 - g0 >= 75 and int((db[:g0] > -60).sum()) <= 40   # NOX_PT_V16
                and -60 < float(db[:g0].max()) < -20 and (g1 - g0 >= 125 or _edge_word_ok(text, head=True))):
            x[:g0 * w] = 0.0
            fixes.append(f"щелчок в начале ({g0 * 2} мс)")
        break
    e0 = runs[-1][1]
    if _last_letter(text) not in _PLOSIVES and _edge_word_ok(text, head=False):   # NOX_PT_V16
        tail = db[e0:]
        loud = np.flatnonzero(tail > -35)
        if len(loud):
            b0, b1 = int(loud[0]), int(loud[-1]) + 1
            gap = 0
            for v in tail[:b0][::-1]:
                if v < -45:
                    gap += 1
                else:
                    break
            if gap >= 4 and b1 - b0 <= 15:
                cut = e0 + b0 - gap
                x[cut * w:] = 0.0
                fixes.append("щелчок в конце")
    return x, fixes


def vowel_cut_ms(x, sr, text=""):
    """Реплика кончается на гласную: за сколько мс звук в конце падает с −10 до −45 дБ (естественно 30–100 мс;
    10–15 мс — гласная оборвана, «Возьмит…»). None — если последняя буква не гласная."""
    import numpy as np
    if _last_letter(text) not in _VOWELS:
        return None
    y, _ = declick(x, sr, text)
    db, w = _db2(y, sr)
    runs = _speech_runs(db)
    if not runs:
        return None
    e0 = runs[-1][1]
    hi = np.flatnonzero(db[:e0] > -10)
    if not len(hi):
        return None
    lo = np.flatnonzero(db[int(hi[-1]):] < -45)
    return int(lo[0]) * 2 if len(lo) else None


VOWEL_CUT_MS = float(SETTINGS.get("vowel_cut_ms", 20))


def _load_gaps(p):
    # границы предложений по распознаванию (verify_k1a.py): [[конец слова, начало следующего], ...] в секундах
    try:
        return json.loads(p.with_suffix(".gaps").read_text(encoding="utf-8"))
    except Exception:
        return None


def _gap_fix(x, sr, gaps, target):
    # пауза после точки там, где распознавание услышало конец предложения: рядом с границей ищем паузу (или самое
    # тихое место) и дотягиваем тишину до target секунд; стыки — с плавными краями
    import numpy as np
    if not gaps or target <= 0:
        return x
    w = int(sr * 0.01)
    k = len(x) // w
    if k < 10:
        return x
    rms = np.sqrt(np.mean(np.square(x[:k * w].reshape(k, w)), axis=1)) + 1e-9
    amax = np.abs(x[:k * w]).reshape(k, w).max(axis=1)
    thr = max(1.5e-3, 0.012 * float(np.percentile(amax, 95)))
    ins = []
    for a, b in gaps:
        f0 = max(0, int((min(a, b) - 0.08) * 100))
        f1 = min(k, int((max(a, b) + 0.08) * 100) + 1)
        if f1 - f0 < 2:
            continue
        sil = amax[f0:f1] < thr
        best_len, best_at, run, run_start = 0, None, 0, 0
        for j, v in enumerate(sil):
            if v:
                if run == 0:
                    run_start = j
                run += 1
                if run > best_len:
                    best_len, best_at = run, run_start
            else:
                run = 0
        have = best_len / 100
        if have >= target:
            continue
        pos = f0 + (best_at + best_len // 2 if best_at is not None else int(np.argmin(rms[f0:f1])))
        ins.append((pos, int(round((target - have) * 100))))
    if not ins:
        return x
    out, prev, nf = [], 0, int(sr * 0.005)
    for pos, add in sorted(ins):
        out.append(_fade(x[prev:pos * w], nf if prev else 0, nf))
        out.append(np.zeros(add * w, dtype="float32"))
        prev = pos * w
    out.append(_fade(x[prev:], nf, 0))
    return np.concatenate(out)


def _fade(seg, n_in, n_out):
    # плавные края куска (косинус): стык с тишиной или с другим куском не щёлкает
    import numpy as np
    seg = np.array(seg, dtype="float32", copy=True)
    if n_in and len(seg) > n_in:
        seg[:n_in] *= (0.5 - 0.5 * np.cos(np.linspace(0, np.pi, n_in))).astype("float32")
    if n_out and len(seg) > n_out:
        seg[-n_out:] *= (0.5 + 0.5 * np.cos(np.linspace(0, np.pi, n_out))).astype("float32")
    return seg


def _edges(x, sr):
    # конец каждой реплики: без постоянной составляющей (она даёт «тык» на стыке с тишиной) и с затуханием 30 мс,
    # начало — нарастание 8 мс. Если запись оборвана на звуке, обрыв превращается в короткое затухание, а не щелчок.
    import numpy as np
    if len(x) < sr // 10:
        return x
    x = np.asarray(x, dtype="float32") - np.float32(np.mean(x))
    return _fade(x, int(sr * 0.008), int(sr * 0.03))


def assemble(rows, roles, head_pause=True):
    # NOX_ASM_V2: склейка сегментов с паузами + темп (TEMPO_G x выравнивание по роли) -> (audio, sr, главы, missing)
    import numpy as np
    import soundfile as sf
    loaded, sr, missing = [], None, 0
    for i, r in enumerate(rows):
        p = wav_for(r, roles)
        if not p.exists():
            missing += 1
            continue
        x, s_ = sf.read(str(p), dtype="float32")
        if x.ndim > 1:
            x = x.mean(axis=1)
        sr = sr or s_
        if s_ != sr:
            sys.exit(f"разная частота дискретизации: {p} {s_} != {sr}")
        loaded.append((i, r, x))
    if not loaded:
        return None, None, [], missing
    factors = {}
    if TEMPO_EQ and TEMPO_G > 0:
        by_role = {}
        for i, r, x in loaded:
            letters = len(RE_RU_LETTERS.sub("", r["text"]))
            if letters >= 40 and r["kind"] != "head":
                by_role.setdefault(resolve_role(r["speaker"], roles), []).append((i, letters / max(_active_sec(x, sr), 0.05)))
        for role, items in by_role.items():
            if len(items) >= 5:
                med = float(np.median([v for _, v in items]))
                for i, rate in items:
                    factors[i] = min(max(med / rate, 1.0), EQ_MAX)   # только подтягиваем медленных (NOX_PT_V13)
    pdiv = TEMPO_G ** 0.5 if TEMPO_G > 1 else 1.0            # паузы укорачиваем мягче, чем речь
    pieces, chapters, t = [], [], 0.0
    _TL.clear()                                            # NOX_PT_V15
    for i, r, x in loaded:
        _raw = r["id"] in _RAW_IDS                          # NOX_PT_V20: бережно — без снятия щелчков и правки пауз
        if not _raw:
            x, _dk = declick(x, sr, r['text'])             # NOX_PT_V15: щелчки на краях записи
        if SENT_GAP_ON and r['kind'] != 'head' and not _raw:
            _rc = roles['roles'].get(resolve_role(r['speaker'], roles), {})
            _f0 = TEMPO_G * factors.get(i, 1.0) * float(_rc.get('tempo', 1.0) or 1.0)
            _tg = PAUSE['sentence'] / 1000 * PAUSE_SCALE / pdiv * 0.75 * max(_f0, 1.0)
            _gp = _load_gaps(wav_for(r, roles))
            x = _gap_fix(x, sr, _gp, _tg) if _gp is not None else _sent_gaps(x, sr, r['text'], _tg)
        _h = hashlib.blake2b(np.ascontiguousarray(x).tobytes(), digest_size=6).hexdigest()   # NOX_PT_V13
        pause = int(r["pause_before_ms"]) if (i > 0 or head_pause) else 0
        pause = int(pause * PAUSE_SCALE / pdiv)
        pieces.append(np.zeros(int(sr * pause / 1000), dtype="float32"))
        t += pause / 1000
        if r["kind"] == "head":
            if chapters and chapters[-1][1] == r["ch_title"] and t - chapters[-1][0] < 15:
                chapters.pop()
            chapters.append((t, r["ch_title"]))
        rcfg = roles["roles"].get(resolve_role(r["speaker"], roles), {})   # NOX_PT_V10: темп/громкость роли
        if TRIM_PAUSES and not _raw:
            x = _squeeze_pauses(x, sr, keep=SQZ_KEEP, cap=SQZ_CAP)
        f = TEMPO_G * factors.get(i, 1.0) * float(rcfg.get("tempo", 1.0) or 1.0)
        sfx = ""
        if TRIM_PAUSES:
            sfx = "t" if (SQZ_KEEP, SQZ_CAP) == (0.35, 0.8) else f"t{int(SQZ_KEEP * 100)}_{int(SQZ_CAP * 100)}"
        if _raw:
            sfx = "r"                                      # NOX_PT_V20: бережная — свой кэш темпа (иначе вернётся сжатая)
        x = _stretch(x, sr, f, wav_for(r, roles).stem + "_" + _h + sfx)
        if NORM_ON:
            x = _level(x, sr, NORM_DB)        # NOX_PT_V13: ровная громкость реплик и голосов
        gain = float(rcfg.get("gain", 1.0) or 1.0)
        if abs(gain - 1.0) > 0.01:
            x = np.clip(x * gain, -1.0, 1.0)
        x = _edges(x, sr)                                  # NOX_PT_V15
        _TL.append((t, t + len(x) / sr, r))
        pieces.append(x)
        t += len(x) / sr
    pieces.append(np.zeros(int(sr * PAUSE["chapter_end"] / 1000 * PAUSE_SCALE / pdiv), dtype="float32"))
    return np.concatenate(pieces), sr, chapters, missing


def atempo_chain(speed: float) -> str:
    """ffmpeg atempo принимает только 0.5..2.0 за один фильтр — для более сильных изменений собираем цепочку."""
    stages, s = [], speed
    while s > 2.0:
        stages.append(2.0); s /= 2.0
    while s < 0.5:
        stages.append(0.5); s /= 0.5
    stages.append(round(s, 6))
    return ",".join(f"atempo={x}" for x in stages)


LOUDNORM_AF = "loudnorm=I=-18:TP=-2:LRA=11"        # NOX_PT_V21


def encode_m4b(audio, sr, chapters, out: Path, title: str, author: str, bitrate: str, cover=None,
               speed: float = 1.0):
    """float32 -> временный wav -> ffmpeg (loudnorm, AAC, главы) -> out (временное имя). speed=1.15 —
    ускорение темпа (atempo, без изменения тона), 1.0 — без изменений."""
    import soundfile as sf
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        sys.exit("нужен ffmpeg в PATH (или в папке tools рядом со скриптом)")
    out.parent.mkdir(parents=True, exist_ok=True)
    wav_tmp = out.with_suffix(".wav.tmp")
    sf.write(str(wav_tmp), audio, sr, format="WAV", subtype="PCM_16")
    total = len(audio) / sr
    meta = [";FFMETADATA1", f"title={title}", f"artist={author}", f"album={title}", "genre=Audiobook"]
    marks = chapters or [(0.0, title)]
    if marks[0][0] > 0.5:
        marks = [(0.0, marks[0][1] + " (продолжение)")] + marks
    if speed != 1.0:                                    # atempo меняет длительность — сдвигаем метки глав
        marks = [(t / speed, name) for t, name in marks]
        total = total / speed
    for i, (start, name) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else total
        meta += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(start * 1000)}", f"END={int(end * 1000) - 1}",
                 f"title={name}"]
    meta_path = out.with_suffix(".meta.txt")
    meta_path.write_text("\n".join(meta) + "\n", encoding="utf-8")
    is_mp3 = out.suffix.lower() == ".mp3"
    if is_mp3 and bitrate == "96k":
        bitrate = "128k"                        # у mp3 эффективность ниже, чем у AAC — чуть больше битрейт
    tmp_out = out.with_name(out.stem + ".tmp" + out.suffix)
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(wav_tmp), "-i", str(meta_path)]
    if cover:
        cmd += ["-i", cover]
    af = LOUDNORM_AF
    if speed != 1.0:
        af = atempo_chain(speed) + "," + af
    cmd += ["-map_metadata", "1", "-af", af, "-ac", "1", "-b:a", bitrate]
    if is_mp3:
        cmd += ["-c:a", "libmp3lame", "-id3v2_version", "3", "-f", "mp3"]   # главы — ID3v2 CHAP, читают не все плееры
    else:
        cmd += ["-c:a", "aac", "-ar", "48000", "-movflags", "+faststart", "-f", "ipod"]   # NOX_PT_V21
    if cover:
        cmd += ["-map", "0:a", "-map", "2:v", "-c:v", "copy", "-disposition:v", "attached_pic"]
    cmd.append(str(tmp_out))
    subprocess.run(cmd, check=True)
    os.replace(tmp_out, out)
    wav_tmp.unlink(missing_ok=True)
    meta_path.unlink(missing_ok=True)
    return total


def build(args):
    rows = load_rows()
    roles = load_roles()
    migrate_legacy_wavs(rows, roles)
    if args.parts:
        return build_parts(rows, roles, args.parts, args.title, args.author, args.bitrate, args.format,
                           quiet=False, speed=args.speed)
    audio, sr, chapters, missing = assemble(rows, roles)
    if audio is None:
        sys.exit("нет ни одного озвученного сегмента")
    if missing:
        print(f"ВНИМАНИЕ: пропущено {missing} сегментов без wav (сначала доозвучьте: synth)")
        if args.strict:
            sys.exit(1)
    out = Path(args.out)
    if out.suffix.lower() not in (".m4b", ".mp3", ".m4a"):
        out = out.with_suffix("." + args.format)
    if not out.is_absolute():
        out = HERE / out
    total = encode_m4b(audio, sr, chapters, out, args.title, args.author, args.bitrate, args.cover,
                       speed=args.speed)
    print(f"Готово: {out}  ({total / 3600:.1f} ч, {len(chapters)} глав)")


# ----------------------------------------------------------------------------------------------------------------------
# 5. PARTS / WATCH — части по ~N минут из уже готового начала книги; watch собирает их по мере озвучки и копирует
# ----------------------------------------------------------------------------------------------------------------------

# NOX_PT_V21: ПО ЧАСТЯМ. Закреплённые автором части — studio/fixed_parts.json (пишет «Нокс Студия»).
FIXED_PATH = HERE / "studio" / "fixed_parts.json"
MODE_PATH = WORK / "run_mode.json"


def row_uids(rows):
    """Метки реплик, как в студии: sha1(текст)[:12] + «.» + порядковый номер такого текста в книге."""
    seen, out = {}, []
    for r in rows:
        k = hashlib.sha1(r["text"].encode("utf-8")).hexdigest()[:12]
        o = seen.get(k, 0)
        seen[k] = o + 1
        out.append(f"{k}.{o}")
    return out


def fixed_parts(rows):
    # Both real files are mandatory when parts are fixed. A corrupt file stops
    # the pipeline before it can reinterpret protected audio as unprotected.
    return [(a, b) for a, b, _number in _nox_read_protection(HERE, rows).ranges]


def fixed_prefix(rows, fx=None):
    """Закреплённые части подряд с начала книги (части 1..k): их номера и границы уже не меняются."""
    out, nxt = [], 0
    for a, b in (fixed_parts(rows) if fx is None else fx):
        if a != nxt:
            break
        out.append((a, b))
        nxt = b + 1
    return out


def fixed_set(rows, fx=None):
    # Individual locks are significant even before their part is locked.
    state = _nox_read_protection(HERE, rows)
    out = set(state.protected_indices)
    for a, b in ([(a, b) for a, b, _n in state.ranges] if fx is None else fx):
        out.update(range(a, b + 1))
    return out


def run_mode():
    try:
        d = json.loads(MODE_PATH.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def by_parts():
    """Режим «по частям» (go включает его по умолчанию): work/run_mode.json. Нет файла — прежний режим, вся книга."""
    return bool(run_mode().get("by_parts"))


def _part_pos(p, rows, roles, idpos, key_pos=None):
    """Границы собранной части из work/parts.json -> (a, b) | None. Сначала по номерам реплик (a_id/b_id), потом по
    карте времени части (в ней номера реплик) и только потом по именам записей, как раньше."""
    def good(a, b):
        return (a is not None and b is not None and a <= b and wav_for(rows[a], roles).stem == p.get("start")
                and wav_for(rows[b], roles).stem == p.get("end"))
    a, b = idpos.get(str(p.get("a_id"))), idpos.get(str(p.get("b_id")))
    if good(a, b):
        return a, b
    try:
        ids = [ln.split("\t")[2] for ln in
               (PARTS / f"part_{int(p['n']):02d}.times.tsv").read_text(encoding="utf-8").splitlines() if ln.count("\t") >= 4]
        a, b = idpos.get(ids[0]), idpos.get(ids[-1])
        if good(a, b):
            return a, b
    except Exception:
        pass
    if key_pos is None:
        key_pos = {wav_for(r, roles).stem: i for i, r in enumerate(rows)}
    a, b = key_pos.get(p.get("start")), key_pos.get(p.get("end"))
    return (a, b) if a is not None and b is not None else None


def work_window(rows, roles):
    """Окно работы «по частям» -> (i0, i1, номер части, собрана ли).
    i0 — первая реплика первой незакреплённой части, i1 — последняя реплика, которую сейчас нужно озвучить и проверить.
    Часть уже собиралась (её границы есть в work/parts.json) — это её конец. Ещё нет — точка разреза по готовым записям,
    а где записей пока нет — оценка по числу букв (без запаса: дальше части ничего не озвучивается; окно само уточняется, когда записи появляются).
    Собрана = файл части есть и собран из нынешних записей: тогда воркерам и проверке делать нечего."""
    fx = fixed_prefix(rows)
    i0 = fx[-1][1] + 1 if fx else 0
    num = len(fx) + 1
    if i0 >= len(rows):
        return len(rows), len(rows) - 1, num, True
    _protection = _nox_read_protection(HERE, rows)
    _opened = next(((a, b) for a, b, k in _protection.opened_ranges if k == num), None)
    _next_fixed = min((a for a, b, k in _protection.ranges if a > i0), default=len(rows))
    _limit = _opened[1] if _opened else _next_fixed - 1
    idpos = {str(r["id"]): i for i, r in enumerate(rows)}
    try:
        for p in json.loads(PARTS_INDEX.read_text(encoding="utf-8")):
            if int(p.get("n", 0)) != num or not p.get("sig") or p.get("fixed"):
                continue
            pos = _part_pos(p, rows, roles, idpos)
            if pos and pos[0] == i0 and pos[1] <= _limit and (not _opened or pos[1] == _opened[1]):
                has = any((PARTS / f"part_{num:02d}.{x}").exists() for x in ("mp3", "m4b"))
                return i0, pos[1], num, bool(has and _part_sig(rows[i0:pos[1] + 1], roles) == p["sig"])
    except Exception:
        pass
    if _opened:
        return i0, _opened[1], num, False  # an opened Studio part retains its proved boundary
    md = run_mode()
    target = float(md.get("part_sec") or part_source_seconds(float(SETTINGS.get("part_min", 19) or 19), 1.0))
    cache = load_durations()
    # Длина ещё не озвученной реплики оценивается по уже озвученным: d = a + буквы / темп (у каждой записи есть «хвосты»,
    # поэтому короткие реплики идут медленнее длинных). Оценка берётся с запасом в большую сторону (+25 %): окно может
    # оказаться чуть короче части и дорастёт, когда записи появятся, — зато дальше части не озвучивается ни одной реплики.
    sn = sl = sd = sll = sld = 0.0

    def _add(letters, d):
        nonlocal sn, sl, sd, sll, sld
        sn += 1
        sl += letters
        sd += d
        sll += letters * letters
        sld += letters * d

    def _est(letters):
        if sn >= 8:
            den = sn * sll - sl * sl
            if den > 1e-6:
                b = (sn * sld - sl * sd) / den
                a = (sd - b * sl) / sn
                if b > 0.02 and 0.0 <= a <= 3.0:
                    return 1.25 * (a + b * letters)
        if sd >= 15:
            return 1.25 * letters / (sl / sd)
        return 1.25 * (0.45 + letters / 12.5)          # записей ещё нет: обычный темп движка

    for r in rows[max(0, i0 - 300):i0]:                 # сначала — по предыдущей части
        p = wav_for(r, roles)
        if p.exists():
            try:
                _add(len(RE_RU_LETTERS.sub("", r["text"])), duration_of(p, cache))
            except Exception:
                pass
    acc = 0.0
    for i in range(i0, _limit + 1):                      # озвученное — по настоящей длине, остальное — по оценке
        r = rows[i]
        letters = len(RE_RU_LETTERS.sub("", r["text"]))
        p = wav_for(r, roles)
        d = None
        if p.exists():
            try:
                d = duration_of(p, cache)
                _add(letters, d)
            except Exception:
                d = None
        if d is None:
            d = _est(letters)
        acc += int(r["pause_before_ms"]) / 1000 + d
        nxt = rows[i + 1] if i + 1 < len(rows) else None
        boundary_ok = nxt is None or nxt["kind"] == "head" or int(nxt["pause_before_ms"]) >= PAUSE["para"]
        if acc >= target and boundary_ok:
            return i0, i, num, False
    return i0, _limit, num, False


def plan_parts(rows, roles, minutes: float, upto=None, speed: float=1.0):
    """-> (parts, complete). parts: список (start_idx, end_idx_incl, closed). Границы уже собранных частей
    заморожены в work/parts.json, чтобы номера частей не «ехали» между запусками.
    NOX_PT_V21: закреплённые автором части — в начале цепочки, их записи не проверяются; upto — дальше этой реплики
    не смотреть (режим «по частям»)."""
    cache = load_durations()
    idpos = {str(r["id"]): i for i, r in enumerate(rows)}
    fx = fixed_prefix(rows)
    fxs = fixed_set(rows)
    frozen = list(fx)
    try:
        key_pos = None
        for p in json.loads(PARTS_INDEX.read_text(encoding="utf-8")):
            if key_pos is None and not (p.get("a_id") is not None and p.get("b_id") is not None):
                key_pos = {wav_for(r, roles).stem: i for i, r in enumerate(rows)}
            pos = _part_pos(p, rows, roles, idpos, key_pos)
            if pos is None:
                break
            a, b = pos
            if fx and b <= fx[-1][1]:
                continue                                # место этой части занято закреплёнными
            if b < a or a != (frozen[-1][1] + 1 if frozen else 0):
                break
            frozen.append((a, b))
    except Exception:
        frozen = list(fx)
    # сколько сегментов готово подряд с начала книги (закреплённые готовы по определению)
    ready = fx[-1][1] + 1 if fx else 0
    gate = _asr_gate()                                  # NOX_PT_V13
    lim = len(rows) if upto is None else min(len(rows), int(upto) + 1)
    while ready < lim and (ready in fxs or _seg_ok(rows[ready], roles, gate)):
        ready += 1
    complete = ready == len(rows)
    parts = [(a, b, True) for a, b in frozen]
    start = frozen[-1][1] + 1 if frozen else 0
    acc = 0.0
    for i in range(start, ready):
        r = rows[i]
        try:
            acc += int(r["pause_before_ms"]) / 1000 + duration_of(wav_for(r, roles), cache)
        except Exception:                               # закреплённая реплика без черновой записи — по числу букв
            acc += int(r["pause_before_ms"]) / 1000 + len(r["text"]) / 13.0
        nxt = rows[i + 1] if i + 1 < len(rows) else None
        boundary_ok = nxt is None or nxt["kind"] == "head" or int(nxt["pause_before_ms"]) >= PAUSE["para"]
        if acc >= part_source_seconds(minutes, speed) and boundary_ok:
            parts.append((start, i, True))
            start, acc = i + 1, 0.0
    if complete and start < len(rows):
        parts.append((start, len(rows) - 1, True))     # хвост книги — последняя часть, пусть и короче
    elif start < ready:
        parts.append((start, ready - 1, False))        # недособранная часть (ещё растёт)
    # Refuse a partition that would renumber or incorporate another fixed part.
    for _a, _b, _n in _nox_read_protection(HERE, rows).ranges:
        if len(parts) >= _n and tuple(parts[_n - 1][:2]) != (_a, _b):
            raise RuntimeError(f"защита редакции: новая сборка меняет границы закреплённой части {_n}")
        for _k, (_x, _y, _closed) in enumerate(parts, 1):
            if max(_a, _x) <= min(_b, _y) and (_k, _x, _y) != (_n, _a, _b):
                raise RuntimeError(f"защита редакции: часть {_k} пересекает закреплённую часть {_n}")
    _atomic_text(DUR_CACHE, json.dumps(cache))
    return parts, ready, complete


_RAW_IDS = set()                 # NOX_PT_V20: реплики, у которых сборка «съела» речь, — собираются бережно
PART_CHECK = WORK / "part_check.txt"


def _speech_s(x, sr):
    # сколько секунд звучит речь: кадры 20 мс громче -42 дБ и громче 8 % от пиковых (как в проверке распознаванием)
    import numpy as np
    w = max(1, int(sr * 0.02))
    k = len(x) // w
    if k < 1:
        return 0.0
    rms = np.sqrt(np.mean(np.square(np.asarray(x[:k * w], dtype="float32").reshape(k, w)), axis=1))
    thr = max(10 ** (-42 / 20), 0.08 * float(np.percentile(rms, 95)))
    return float((rms > thr).sum()) * 0.02


@_nox_guard_metadata
def _part_check(num, audio, sr, roles):
    """ВТОРАЯ ПРОВЕРКА (NOX_PT_V20): в собранной части на месте каждой реплики звучит речь.
    -> "ok" | "raw" (собрать часть заново бережно) | "redo" (пустая запись ушла на переозвучку — часть ждёт)."""
    import soundfile as sf
    tries = max(1, int(SETTINGS.get("asr_tries", 3) or 3))
    res, lines = "ok", []
    try:
        redo_done = set(json.loads((WORK / "part_check_redo.json").read_text(encoding="utf-8")))
    except Exception:
        redo_done = set()
    try:                        # переозвучку отдавать, только пока работает проверка (с ней живут и воркеры)
        live = time.time() - (WORK / "asr_active.txt").stat().st_mtime < 600
    except OSError:
        live = False
    _current_rows = load_rows()
    _protected_audio = {wav_for(_current_rows[i], roles) for i in
                        _nox_read_protection(HERE, _current_rows).protected_indices}
    for t0, t1, r in list(_TL):
        if wav_for(r, roles) in _protected_audio:
            continue
        lt = len(RE_RU_LETTERS.sub("", r["text"]))
        if lt == 0:
            continue
        got = _speech_s(audio[int(t0 * sr):int(t1 * sr)], sr)
        need = max(0.15, 0.025 * lt)            # темп сборки ускоряет речь — порог мягче, чем у самой записи
        if got >= need:
            continue
        p = wav_for(r, roles)
        try:
            x, s_ = sf.read(str(p), dtype="float32")
            if x.ndim > 1:
                x = x.mean(axis=1)
            raw = _speech_s(x, s_)
        except Exception:
            raw = 0.0
        if raw >= max(0.2, 0.03 * lt):
            if r["id"] not in _RAW_IDS:
                _RAW_IDS.add(r["id"])
                verdict = "сборка потеряла речь — часть собирается заново бережно"
                if res == "ok":
                    res = "raw"
            else:
                verdict = "сборка потеряла речь и бережно — оставлено как есть"
        else:
            _what = "нет речи" if raw < 0.2 else "мало речи для текста"
            try:
                okt = p.with_suffix(".ok").read_text(encoding="utf-8")
            except OSError:
                okt = ""
            rej = sorted(p.parent.glob(p.stem + ".rej*.wav"))
            if "лучшая из" in okt or p.stem in redo_done or len(rej) + 1 >= tries:
                verdict = f"в записи {_what}, попытки кончились — оставлено как есть"
            elif not live:
                verdict = f"в записи {_what}; проверка не работает, переозвучить некому — оставлено как есть"
            else:
                k = len(rej) + 1
                dst = p.with_name(f"{p.stem}.rej{k}.wav")
                try:
                    _nox_preserve_bundle(HERE, p)
                    os.replace(p, dst)
                    dst.with_suffix(".json").write_text(json.dumps(
                        {"score": 0.0, "why": f"сборка: {_what} {raw:.2f} с", "heard": "", "gaps": []},
                        ensure_ascii=False), encoding="utf-8")
                    for ext in (".ok", ".gaps"):
                        p.with_suffix(ext).unlink(missing_ok=True)
                    redo_done.add(p.stem)
                    verdict = f"в записи {_what} — на переозвучку (попытка {k + 1} из {tries})"
                    res = "redo"
                except OSError as e:
                    verdict = f"в записи {_what}; на переозвучку не ушла ({type(e).__name__})"
        lines.append(f"{time.strftime('%Y-%m-%d %H:%M')}\tчасть {num:02d}\tid {r['id']}\t{resolve_role(r['speaker'], roles)}"
                     f"\tв части {got:.2f} с, в записи {raw:.2f} с, нужно {need:.2f} с\t{verdict}\t{r['text'][:90]}\n")
    if lines:
        try:
            with PART_CHECK.open("a", encoding="utf-8") as f:
                f.writelines(lines)
            (WORK / "part_check_redo.json").write_text(json.dumps(sorted(redo_done)), encoding="utf-8")
        except OSError:
            pass
        print(f"[watch] {time.strftime('%H:%M')}  часть {num:02d}: вторая проверка — без речи {len(lines)} реплик"
              f" (work\\part_check.txt)", flush=True)
    return res


def build_parts(rows, roles, minutes, title, author, bitrate, fmt="m4b", quiet=False, speed=1.0, only=None, upto=None):
    """Собирает закрытые части, которых нет в parts/ ИЛИ в которых переозвучена реплика (NOX_PT_V13).
    Часть, где запись ещё на переозвучке/проверке, ждёт (прежняя версия остаётся) — дыр в частях не бывает.
    NOX_PT_V21: закреплённые автором части не собираются; only=N — собирать только часть N (режим «по частям»)."""
    parts, ready, complete = plan_parts(rows, roles, minutes, upto, speed=speed)
    _fixed_numbers = {k for _a, _b, k in _nox_read_protection(HERE, rows).ranges}
    _source_csv = CSV_PATH.read_bytes()
    PARTS.mkdir(exist_ok=True)
    old_sig = {}
    try:
        for p in json.loads(PARTS_INDEX.read_text(encoding="utf-8")):
            old_sig[int(p["n"])] = p.get("sig")
    except Exception:
        pass
    new_files = []
    frozen = []
    for n, (a, b, closed) in enumerate(parts, 1):
        if not closed:
            break
        out = PARTS / f"part_{n:02d}.{fmt}"
        entry = {"n": n, "start": wav_for(rows[a], roles).stem, "end": wav_for(rows[b], roles).stem,
                 "sig": old_sig.get(n), "a_id": rows[a]["id"], "b_id": rows[b]["id"]}
        frozen.append(entry)
        if n in _fixed_numbers:                                  # NOX_PT_V21: закреплена автором — конвейер её не трогает
            entry["fixed"] = True
            entry["sig"] = entry["sig"] or "fixed"
            continue
        if only is not None and n != only:
            continue                                   # по частям: остальные части ждут своей очереди
        sig = _part_sig(rows[a:b + 1], roles) if b < ready else None
        if sig is None:
            continue                                   # в части реплика на переозвучке — подождём
        if out.exists() and old_sig.get(n) == sig:
            continue
        audio, sr, chapters, missing = assemble(rows[a:b + 1], roles, head_pause=False)
        if missing or audio is None:
            continue                                   # запись ушла на переозвучку прямо сейчас — соберём позже
        _pc = _part_check(n, audio, sr, roles)         # NOX_PT_V20: вторая проверка — речь на месте каждой реплики
        if _pc == "raw":
            audio, sr, chapters, missing = assemble(rows[a:b + 1], roles, head_pause=False)
            if missing or audio is None:
                continue
            _pc = _part_check(n, audio, sr, roles)
        if _pc == "redo":
            continue                                   # пустая запись ушла на переозвучку — часть подождёт её
        ch_name = rows[a]["ch_title"]
        # Encode under a unique draft name; only publish after checking the
        # current edition again under the same OS lock used by Studio.
        _draft = PARTS / f".nox_part_{n:02d}_{os.getpid()}_{time.time_ns()}.{fmt}"
        try:
            total = encode_m4b(audio, sr, chapters, _draft, f"{title} — часть {n:02d} ({ch_name})", author, bitrate,
                              speed=speed)
            with _nox_write_lock(HERE):
                _latest = _nox_read_protection(HERE, rows)
                if CSV_PATH.read_bytes() != _source_csv:
                    raise RuntimeError("разбивка изменилась во время сборки; прежний файл части сохранён")
                if n in {k for _a, _b, k in _latest.ranges}:
                    entry["fixed"], entry["sig"] = True, "fixed"
                    continue
                if _part_sig(rows[a:b + 1], roles) != sig:
                    continue
                os.replace(_draft, out)
                try:                                               # NOX_PT_V15
                    def _mmss(v):
                        v = v / (speed or 1.0)
                        return f"{int(v // 60):02d}:{v % 60:04.1f}"
                    _atomic_text(out.with_name(f"part_{n:02d}.times.tsv"),
                        "".join(f"{_mmss(a_)}\t{_mmss(b_)}\t{r_['id']}\t{resolve_role(r_['speaker'], roles)}\t{wav_for(r_, roles).stem}"
                                f"\t{r_['text'][:120]}\t{a_ / (speed or 1.0):.3f}\t{b_ / (speed or 1.0):.3f}\n"   # NOX_PT_V18: секунды
                                for a_, b_, r_ in _TL))
                except Exception:
                    pass

        finally:
            _draft.unlink(missing_ok=True)
        entry["sig"] = sig
        new_files.append(out)
        if not quiet:
            print(f"[parts] {out.name}  {total / 60:5.1f} мин  с «{ch_name}»")
        else:
            print(f"[watch] {time.strftime('%H:%M')}  собрана {out.name} ({total / 60:.1f} мин)"
                  + (" — пересобрана: в ней переозвучены реплики" if old_sig.get(n) else ""), flush=True)
    with _nox_write_lock(HERE):
        _latest = _nox_read_protection(HERE, rows)
        if CSV_PATH.read_bytes() != _source_csv:
            raise RuntimeError("разбивка изменилась; индекс частей не заменён")
        _entries = {int(p["n"]): p for p in frozen}
        for _a, _b, _n in _latest.ranges:
            _entry = _entries.setdefault(_n, dict(n=_n))
            _entry.update(start=wav_for(rows[_a], roles).stem, end=wav_for(rows[_b], roles).stem,
                          a_id=rows[_a]["id"], b_id=rows[_b]["id"], fixed=True)
            _entry["sig"] = _entry.get("sig") or "fixed"
        frozen = [_entries[k] for k in sorted(_entries)]
        _atomic_text(PARTS_INDEX, json.dumps(frozen, ensure_ascii=False, indent=1))
    if not quiet:
        print(f"готово сегментов подряд с начала: {ready}/{len(rows)}; частей собрано: {len(frozen)}"
              + (" — книга целиком озвучена" if complete else ""))
    complete = complete and all(e.get("sig") for e in frozen)
    return new_files, complete


def copy_to(dest: Path, src: Path):
    """Копия через временное имя: клиент облака не начнёт заливать недописанный файл."""
    dest.mkdir(parents=True, exist_ok=True)
    tmp = dest / (src.name + ".copying")
    shutil.copy2(src, tmp)
    os.replace(tmp, dest / src.name)


@_nox_guard_metadata
def _asm_setup(args, dest, sent):
    """NOX_PT_V21: сверка настроек сборки (темп, паузы, роли, длина части, формат). Изменились — черновики частей
    убираются и собираются заново; файлы частей, закреплённых автором, остаются. -> sent (что уже на Диске)."""
    tv = WORK / "tempo.ver"
    cur_t = f"{TEMPO_G:.3f}|{int(TEMPO_EQ)}|{PAUSE_SCALE:.2f}|{int(TRIM_PAUSES)}"
    prev_t = tv.read_text(encoding="utf-8").strip() if tv.exists() else ""
    # NOX_PT_V10: полный отпечаток сборки — темп, паузы, сжатие, темп/громкость ролей, части, формат, подписи
    roles0 = load_roles()
    _tdir = WORK / "tempo"                     # NOX_PT_V13: старый кэш ускорения (по имени реплики) — разово в мусор
    if _tdir.is_dir() and not (_tdir / ".v2").exists():
        shutil.rmtree(_tdir, ignore_errors=True)
    _tdir.mkdir(exist_ok=True)
    (_tdir / ".v2").touch()
    asm = dict(t=cur_t, pause=PAUSE, sqz=[SQZ_KEEP, SQZ_CAP], parts=args.parts, fmt=args.format, br=args.bitrate,
               cache=2, eq=EQ_MAX, norm=[NORM_ON, NORM_DB], sgap=[SENT_GAP_ON, 0.75, 2], edges=2, declick=2, gate=20,
               title=args.title, author=args.author, speed=args.speed,
               roles={k: [float(v.get("tempo", 1.0) or 1.0), float(v.get("gain", 1.0) or 1.0)]
                      for k, v in sorted(roles0["roles"].items())
                      if float(v.get("tempo", 1.0) or 1.0) != 1.0 or float(v.get("gain", 1.0) or 1.0) != 1.0})
    asm_s = json.dumps(asm, ensure_ascii=False, sort_keys=True)
    av = WORK / "assembly.json"
    prev_a = av.read_text(encoding="utf-8").strip() if av.exists() else ""
    asm_changed = (prev_a != asm_s) if prev_a else (prev_t != cur_t)
    if asm_changed:
        _state = _nox_read_protection(HERE, load_rows())
        _numbers = {k for _a, _b, k in _state.ranges}
        _previous = json.loads(PARTS_INDEX.read_text(encoding="utf-8")) if PARTS_INDEX.exists() else []
        if not isinstance(_previous, list) or any(not isinstance(p, dict) for p in _previous):
            raise RuntimeError("повреждён work/parts.json; прежние аудиофайлы оставлены")
        for p in _previous:
            if int(p.get("n", 0)) in _numbers:
                p["fixed"] = True
                p["sig"] = p.get("sig") or "fixed"
            else:
                p["sig"] = None
                p.pop("fixed", None)
        _atomic_text(PARTS_INDEX, json.dumps(_previous, ensure_ascii=False, indent=1))
        _atomic_text(WORK / "book.sig", "")
        print("watch: настройки сборки изменились; незакреплённые части будут собраны заново. "
              "Прежние аудиофайлы, копии на Диске и версии со звуками сохранены до успешной замены.")
    _atomic_text(tv, cur_t)
    _atomic_text(av, asm_s)
    return sent


def watch(args):
    dest = Path(args.dest) if args.dest else None
    sent_f = WORK / "sent.json"            # NOX_PT_V13: какие версии частей уже на Диске
    try:
        sent = json.loads(sent_f.read_text(encoding="utf-8"))
    except Exception:
        sent = {}
    sent = _asm_setup(args, dest, sent)             # NOX_PT_V21
    print(f"watch: части по ~{args.parts} мин -> {PARTS}" + (f", копирую в {dest}" if dest else "")
          + f"; проверка каждые {args.every} мин. Ctrl+C — выйти.")
    while True:
        rows = load_rows()
        roles = load_roles()
        migrate_legacy_wavs(rows, roles)
        try:
            new_files, complete = build_parts(rows, roles, args.parts, args.title, args.author, args.bitrate,
                                              args.format, quiet=True, speed=args.speed)
        except SystemExit as e:
            print("watch:", e)
            new_files, complete = [], False
        except Exception as e:
            print(f"watch: сборка не удалась ({type(e).__name__}: {str(e)[:120]}), повторю позже")
            new_files, complete = [], False
        for f in sorted(PARTS.glob(f"part_*.{args.format}")) if PARTS.is_dir() else []:
            if not dest:
                continue
            st_ = f.stat()
            sg = f"{st_.st_size}:{st_.st_mtime_ns}"
            if sent.get(f.name) == sg and (dest / f.name).exists():
                continue
            again = f.name in sent
            copy_to(dest, f)
            sent[f.name] = sg
            sent_f.write_text(json.dumps(sent), encoding="utf-8")
            print(f"[watch] {time.strftime('%H:%M')}  {'обновлена на Диске' if again else 'отправлена'} {f.name}")
        n_parts = len(list(PARTS.glob(f"part_*.{args.format}"))) if PARTS.is_dir() else 0
        ready = sum(1 for r in rows if wav_for(r, roles).exists())
        _gate = _asr_gate()
        _chk = sum(1 for r in rows if _seg_ok(r, roles, True)) if _gate else None
        print(f"[watch] {time.strftime('%H:%M')}  озвучено {ready}/{len(rows)} сегментов"
              + (f", проверено распознаванием {_chk}" if _chk is not None else "") + f", частей готово {n_parts}")
        if _chk is not None and _chk < ready and _okv_req():      # NOX_PT_V20: проверка умерла — части ждут
            try:
                _hb = time.time() - (WORK / "asr_active.txt").stat().st_mtime
            except OSError:
                _hb = None
            if _hb is None or _hb > 900:
                print(f"[watch] !! проверка распознаванием не отвечает (см. verify.log, verify.err.log) — части ждут "
                      f"проверенных записей; запустите go ещё раз", flush=True)
        if complete:
            full = HERE / f"book.{args.format}"
            plist = sorted(PARTS.glob(f"part_*.{args.format}"))
            bsig = hashlib.blake2b("|".join(f"{f.name}:{f.stat().st_size}:{f.stat().st_mtime_ns}" for f in plist)
                                   .encode("utf-8"), digest_size=8).hexdigest()
            bsf = WORK / "book.sig"
            if not full.exists() or (bsf.read_text(encoding="utf-8").strip() if bsf.exists() else "") != bsig:
                if not _concat_parts(plist, full, args.title, args.author):
                    audio, sr, chapters, _ = assemble(rows, roles)
                    encode_m4b(audio, sr, chapters, full, args.title, args.author, args.bitrate, speed=args.speed)
                bsf.write_text(bsig, encoding="utf-8")
                print(f"[watch] собрана книга целиком: {full}")
            if dest:
                copy_to(dest, full)
                print(f"[watch] отправлена {full.name}")
            print("watch: всё готово")
            return
        time.sleep(args.every * 60)

# ----------------------------------------------------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("segment", help="один или несколько .docx по порядку -> work/segments.csv + work/roles.json")
    s.add_argument("docx", type=Path, nargs="+", help="главы склеиваются в указанном порядке")
    s.add_argument("--max-chars", type=int, default=MAX_CHARS)

    c = sub.add_parser("cast", help="голоса ролей по описанию (VoiceDesign)")
    c.add_argument("--only", nargs="*", help="только эти роли")
    c.add_argument("--force", action="store_true")
    c.add_argument("--seed", type=int)
    c.add_argument("--model", help="по умолчанию Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign")
    c.add_argument("--attn", default="auto", help="flash_attention_2 | sdpa | auto")

    y = sub.add_parser("synth", help="озвучка сегментов клонированием (Base)")
    y.add_argument("--batch", type=int, default=16, help="максимум реплик в батче (короткие реплики идут пачками)")
    y.add_argument("--char-budget", type=int, default=3200,
                   help="реплик в батче x длина самой длинной <= этого числа (3200 = 16 реплик по 200 знаков)")
    y.add_argument("--chapter", type=int, nargs="*", help="только эти главы (номера из CSV), напр. --chapter 1")
    y.add_argument("--shard", help="i/n — доля сегментов для этого процесса (2 процесса: 0/2 и 1/2)")
    y.add_argument("--max-new-tokens", type=int, default=1500, help="жёсткий потолок кадров кодека (12,5 кадра = 1 с)")
    y.add_argument("--num2words", action="store_true", help="цифры -> слова (pip install num2words)")
    y.add_argument("--accent", default="acute", choices=["none", "plus", "acute", "sparse", "dbl"],
                   help="расставить ударения через ruaccent ('+' или знак ударения) — проверьте, что модель их понимает")
    y.add_argument("--no-trim", dest="trim", action="store_false", help="не обрезать тишину по краям")
    y.add_argument("--model", help="по умолчанию Qwen/Qwen3-TTS-12Hz-1.7B-Base")
    y.add_argument("--attn", default="auto")
    y.add_argument("--order", default="book", choices=["book", "length"],
                   help="book: главы готовятся по порядку (части можно слушать сразу); length: глобально по длине")
    y.add_argument("--window", type=int, default=200, help="окно сортировки по длине при --order book (сегментов)")
    y.add_argument("--worker", type=int, default=0, help="номер воркера (только для подписи строк прогресса)")
    y.add_argument("--vram-reserve", type=float, default=2.5,
                   help="если свободной VRAM меньше (ГБ) — воркер сам уменьшает батч (важно при нескольких воркерах)")

    b = sub.add_parser("build", help="сборка m4b (целиком или частями)")
    b.add_argument("--title", default="Аудиокнига")
    b.add_argument("--author", default="")
    b.add_argument("--out", default="book")
    b.add_argument("--format", default="m4b", choices=["m4b", "mp3"], help="mp3 — читается любым плеером")
    b.add_argument("--bitrate", default="96k")
    b.add_argument("--cover", help="jpg/png обложка")
    b.add_argument("--speed", type=float, default=1.0,
                   help="ускорение темпа при сборке (atempo, без изменения тона); 1.15 = +15%")
    b.add_argument("--strict", action="store_true", help="падать, если нет какого-то wav")
    b.add_argument("--tempo", type=float, default=1.0, help="темп речи при сборке (1.3 = +30%)")
    b.add_argument("--no-equalize", action="store_true")
    b.add_argument("--pauses", type=float, default=0.85)
    b.add_argument("--no-trim-pauses", action="store_true")
    b.add_argument("--parts", type=float, default=0,
                   help="вместо целой книги — собрать части по ~N минут из готового начала (parts/part_NN.m4b)")

    w = sub.add_parser("watch", help="пока идёт озвучка: собирать части по ~N минут и копировать их в папку облака")
    w.add_argument("--parts", type=float, default=19)
    w.add_argument("--dest", help="куда копировать готовые части (папка автозагрузки Яндекс Диска)")
    w.add_argument("--every", type=float, default=3, help="период проверки, мин")
    w.add_argument("--title", default="Аудиокнига")
    w.add_argument("--author", default="")
    w.add_argument("--bitrate", default="96k")
    w.add_argument("--format", default="mp3", choices=["m4b", "mp3"])
    w.add_argument("--speed", type=float, default=1.0, help="тот же смысл, что у build --speed")
    w.add_argument("--tempo", type=float, default=1.0, help="темп речи при сборке (1.3 = +30%), паузы укорачиваются мягче")
    w.add_argument("--no-equalize", action="store_true", help="не подтягивать медленные реплики к темпу роли")
    w.add_argument("--pauses", type=float, default=0.85, help="множитель пауз между репликами/абзацами при сборке")
    w.add_argument("--no-trim-pauses", action="store_true", help="не сжимать длинные паузы внутри фраз")

    args = ap.parse_args()
    global TEMPO_G, TEMPO_EQ, PAUSE_SCALE, TRIM_PAUSES
    TEMPO_G = float(getattr(args, "tempo", 1.0) or 1.0)
    TEMPO_EQ = not getattr(args, "no_equalize", False)
    PAUSE_SCALE = float(getattr(args, "pauses", 0.85) or 0.85)
    TRIM_PAUSES = not getattr(args, "no_trim_pauses", False)
    try:
        if args.cmd == "segment":
            segment(args.docx, args.max_chars)
        elif args.cmd == "cast":
            cast(args)
        elif args.cmd == "synth":
            synth(args)
        elif args.cmd == "build":
            build(args)
        elif args.cmd == "watch":
            watch(args)
    except KeyboardInterrupt:
        print("\nПрервано. Готовое сохранено — запустите ту же команду снова, продолжит с места остановки.")
        sys.exit(130)


if __name__ == "__main__":
    main()
