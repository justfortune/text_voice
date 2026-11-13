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
import threading
from contextlib import contextmanager
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
_CLAIMS = threading.local()  # lock token is owned by the thread that claimed the WAV
LOCK_STALE_SEC = 45 * 60   # «замок» старше этого без готового wav считается брошенным (упавший воркер)

NARRATOR = "narrator"
ME = "Джек"            # рассказчик говорит от первого лица — его реплики («сказал я») идут этой роли
UNKNOWN = "UNKNOWN"

MAX_CHARS = 300        # максимальная длина одного сегмента для TTS (дальше режем по предложениям)
FRAME_RATE = 12.5      # кадров кодека в секунду у Qwen3-TTS-12Hz (24000 Гц / 1920)

# паузы, мс
PAUSE = dict(chunk=120, para=550, dialog=320, poem_line=250, heading=1200, chapter_start=1500, chapter_end=2000)

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
        is_head = style.startswith("Heading") or style.startswith("Заголовок") or bool(HEAD_RE.match(txt))
        is_block = "Block" in style or "Quote" in style or "Цитата" in style
        out.append((txt, is_head, is_block))
    return out


def split_sentences(text: str):
    parts = [s.strip() for s in SENT_RE.split(text) if s.strip()]
    return parts or [text]


def chunk_text(text: str, max_chars: int = MAX_CHARS):
    """Делит текст на непустые куски не длиннее max_chars, не теряя пунктуацию."""
    text = str(text or "").strip()
    if not text:
        return []
    max_chars = max(1, int(max_chars))
    chunks, cur = [], ""

    def append_piece(piece):
        nonlocal cur
        piece = piece.strip()
        if not piece:
            return
        if len(piece) > max_chars:
            # Резервное деление по словам; сверхдлинное «слово» режется только в крайнем случае.
            for word in re.findall(r"\S+", piece):
                while len(word) > max_chars:
                    if cur:
                        chunks.append(cur)
                        cur = ""
                    chunks.append(word[:max_chars])
                    word = word[max_chars:]
                if word:
                    if cur and len(cur) + 1 + len(word) > max_chars:
                        chunks.append(cur)
                        cur = word
                    else:
                        cur = (cur + " " + word).strip()
            return
        if cur and len(cur) + 1 + len(piece) > max_chars:
            chunks.append(cur)
            cur = piece
        else:
            cur = (cur + " " + piece).strip()

    for sentence in split_sentences(text):
        sentence = sentence.strip()
        if len(sentence) <= max_chars:
            append_piece(sentence)
            continue
        # Сначала сохраняем естественные границы по запятым, точкам с запятой и двоеточиям.
        for fragment in re.split(r"(?<=[,;:])\s+", sentence):
            append_piece(fragment)
    if cur:
        chunks.append(cur)
    if any(not c or len(c) > max_chars for c in chunks):
        raise ValueError("chunk_text: нарушена граница длины сегмента")
    return chunks


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


def _atomic_write_text(path: Path, text: str):
    """Publish UTF-8 state through a unique temp file; never truncate the previous edition."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _snapshot_current_edition():
    """Archive the current book definition and time maps before segment() replaces them.

    Audio outputs are not copied: they remain in their existing parts/ directory and are
    listed in the manifest. This keeps the snapshot small while preserving the old mapping.
    """
    if not CSV_PATH.is_file() or not ROLES_PATH.is_file():
        return None
    editions = WORK / "editions"
    sources = [("segments.csv", CSV_PATH), ("roles.json", ROLES_PATH)]
    if PARTS_INDEX.is_file():
        sources.append(("parts.json", PARTS_INDEX))
    for time_map in sorted(PARTS.glob("part_*.times.tsv")):
        sources.append((f"parts/{time_map.name}", time_map))

    records = []
    for rel, source in sources:
        records.append({"path": rel, "size": source.stat().st_size, "sha256": _file_sha256(source)})
    outputs = []
    for output in sorted(PARTS.glob("part_*")):
        if output.is_file() and output.suffix.lower() in (".m4b", ".m4a", ".mp3"):
            outputs.append({"path": f"parts/{output.name}", "size": output.stat().st_size,
                            "sha256": _file_sha256(output)})
    identity_data = {"files": records, "outputs_kept_in_place": outputs}
    identity = hashlib.sha256(json.dumps(identity_data, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    target = editions / f"edition_{identity[:20]}"

    def verify(snapshot: Path):
        try:
            manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
            if (manifest.get("edition_id") != identity or manifest.get("files") != records
                    or manifest.get("outputs_kept_in_place") != outputs):
                return False
            for record in records:
                file_path = snapshot / Path(record["path"])
                if not file_path.is_file() or file_path.stat().st_size != record["size"] \
                        or _file_sha256(file_path) != record["sha256"]:
                    return False
            return True
        except (OSError, ValueError, TypeError, KeyError):
            return False

    if target.exists():
        if verify(target):
            return target
        raise RuntimeError(f"архив прежней редакции повреждён: {target}; исходные файлы не заменены")

    editions.mkdir(parents=True, exist_ok=True)
    tmp_dir = editions / f".edition_{identity[:20]}.{os.getpid()}.{time.time_ns()}.tmp"
    try:
        tmp_dir.mkdir()
        for record, (_, source) in zip(records, sources):
            destination = tmp_dir / Path(record["path"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        manifest = {
            "schema": 1,
            "edition_id": identity,
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "files": records,
            "outputs_kept_in_place": outputs,
            "audio_copied": False,
        }
        _atomic_write_text(tmp_dir / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        tmp_dir.rename(target)
        return target
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise


def _atomic_write_segments(rows):
    """Write a complete segments.csv before atomically replacing the previous one."""
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CSV_PATH.with_name(f"{CSV_PATH.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS, delimiter=";")
            writer.writeheader()
            writer.writerows(rows)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, CSV_PATH)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def segment(docx_paths, max_chars: int):
    # Uses the same process-shared guard as parts.json/build_parts: a book cannot be
    # resegmented while another process is freezing or publishing its part timeline.
    with _claim_guard(CSV_PATH):
        return _segment_locked(docx_paths, max_chars)


def _segment_locked(docx_paths, max_chars: int):
    WORK.mkdir(exist_ok=True)
    if CSV_PATH.exists() and ROLES_PATH.exists():       # старые wav по номерам -> имена по хешу, пока жив старый CSV
        migrate_legacy_wavs(load_rows(), load_roles())
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

    def add(kind, speaker, conf, pause, text, para_idx):
        nonlocal seg_id
        seg_id += 1
        rows.append(dict(id=seg_id, chapter=chapter, ch_title=ch_title, para=para_idx, kind=kind, speaker=speaker,
                         conf=conf, pause_before_ms=pause, text=text))

    for pi, (txt, is_head, is_block) in enumerate(paras):
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
        is_dialog = txt[0] in DASHES

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
    # Archive old inputs before replacing them; publish each new file atomically.
    previous_edition = _snapshot_current_edition()
    if previous_edition:
        print(f"Предыдущая редакция сохранена: {previous_edition}")
    _atomic_write_segments(rows)
    _atomic_write_text(ROLES_PATH, json.dumps(roles, ensure_ascii=False, indent=2) + "\n")

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


def wav_for(r: dict, roles: dict) -> Path:
    return WAVS / f"{seg_key(resolve_role(r['speaker'], roles), r['text'])}.wav"


def migrate_legacy_wavs(rows, roles) -> int:
    """Старые файлы 000123.wav (по номеру сегмента) -> имена по хешу. Безопасно при нескольких воркерах."""
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


def write_wav_atomic(path: Path, data, sr):
    """Пишем во временный файл и переименовываем: сборщик частей никогда не увидит недописанный wav."""
    import soundfile as sf
    tmp = path.with_suffix(".tmp")
    sf.write(str(tmp), data, sr, format="WAV")
    os.replace(tmp, path)


def _pid_alive(pid: int):
    """Проверяет PID без отправки сигналов процессу; None означает, что статус неизвестен."""
    if pid <= 0:
        return None
    if os.name == "nt":
        # На Windows os.kill(pid, 0) НЕ является POSIX-проверкой существования:
        # ненулевой/неподдерживаемый сигнал может завершить целевой процесс.
        import ctypes
        from ctypes import wintypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            # ERROR_INVALID_PARAMETER: такого PID уже нет. Остальные ошибки
            # (например, ACCESS_DENIED) трактуем консервативно как unknown.
            return False if error == 87 else None
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return None
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return None
    except OSError:
        return None


@contextmanager
def _claim_guard(path: Path):
    """Короткая межпроцессная секция для проверки/перехвата lock-файла.

    WAV lock остаётся совместимым со старым PID-only форматом. Guard-файл один
    на каталог WAV и намеренно не удаляется: удаление inode при ожидающих процессах
    разрушило бы взаимное исключение.
    """
    guard = path.parent / ".nox-claims.guard"
    guard.parent.mkdir(parents=True, exist_ok=True)
    with open(guard, "a+b") as f:
        if os.name == "nt":
            import msvcrt
            f.seek(0, os.SEEK_END)
            if f.tell() == 0:
                f.write(b"\\0")
                f.flush()
            f.seek(0)
            while True:
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    time.sleep(0.02)
            try:
                yield
            finally:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _claim_tokens() -> dict:
    tokens = getattr(_CLAIMS, "tokens", None)
    if tokens is None:
        tokens = {}
        _CLAIMS.tokens = tokens
    return tokens


def _write_claim(lock: Path, path: Path) -> bool:
    """Создать lock с уникальным токеном. Вызывается только под _claim_guard."""
    token = f"{os.getpid()}:{os.urandom(16).hex()}"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, token.encode("ascii"))
        finally:
            os.close(fd)
    except FileExistsError:
        return False
    _claim_tokens()[str(lock.absolute())] = token
    return True


def claim(path: Path) -> bool:
    """Атомарно занять WAV; конкурентный stale-reclaim сериализован между процессами."""
    lock = path.with_suffix(".lock")
    with _claim_guard(path):
        if path.exists():
            return False
        try:
            st = lock.stat()
        except FileNotFoundError:
            return _write_claim(lock, path)
        except OSError:
            return False

        if time.time() - st.st_mtime <= LOCK_STALE_SEC:
            return False
        try:
            raw = lock.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            return False
        if raw:
            # Старый формат PID-only и новый PID:token читаются одинаково.
            try:
                owner_pid = int(raw.partition(":")[0])
            except (ValueError, OverflowError):
                return False                 # неизвестный владелец — не перехватываем
            if _pid_alive(owner_pid) is not False:
                return False                 # живой или неизвестный PID — lock не трогаем
        try:
            # _claim_guard закрывает TOCTOU между проверкой старого владельца,
            # удалением stale-lock и созданием нового lock другим воркером.
            lock.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            return False
        return _write_claim(lock, path)


def release(path: Path):
    """Удаляет только lock, токен которого принадлежит текущему потоку."""
    lock = path.with_suffix(".lock")
    key = str(lock.absolute())
    tokens = _claim_tokens()
    token = tokens.get(key)
    if not token:
        return
    with _claim_guard(path):
        try:
            owner = lock.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            tokens.pop(key, None)
            return
        if owner == token:
            try:
                lock.unlink()
            except OSError:
                pass
        # Даже при несовпадении забываем старое владение: чужой новый lock не наш.
        tokens.pop(key, None)



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


def trim_silence(wav, sr, thresh_db=-42.0, pad_ms=60):
    import numpy as np
    x = np.asarray(wav, dtype="float32")
    if x.ndim > 1:
        x = x.mean(axis=1)
    amp = 10 ** (thresh_db / 20)
    idx = np.where(np.abs(x) > amp)[0]
    if len(idx) == 0:
        return x
    pad = int(sr * pad_ms / 1000)
    a, b = max(0, idx[0] - pad), min(len(x), idx[-1] + pad)
    return x[a:b]


WORD_DICT_PATH = WORK / "accent_dict.csv"
_WORD_DICT = None


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


def prepare_text(text: str, use_num2words: bool, accent: str) -> str:
    t = text.replace("«", "").replace("»", "").replace("“", "").replace("”", "")
    t, ph = apply_word_dict(t)
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
                from ruaccent import RUAccent
                _ACCENTIZER = RUAccent()
                _ACCENTIZER.load(omograph_model_size="turbo", use_dictionary=True)
            t = _ACCENTIZER.process_all(t)          # ставит '+' перед ударной гласной
        except ImportError:
            pass
    for key, val in ph.items():                      # свой словарь — поверх ruaccent, он главнее
        t = t.replace(key, val)
    if accent == "acute":
        t = re.sub(r"\+([аеёиоуыэюяАЕЁИОУЫЭЮЯ])", "\\1\u0301", t)
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
    """Длительность WAV с инвалидацией кэша при перезаписи файла."""
    import soundfile as sf
    st = path.stat()
    k = f"{path.name}|{st.st_size}|{st.st_mtime_ns}"
    if k not in cache:
        info = sf.info(str(path))
        if not info.samplerate:
            raise ValueError(f"некорректная частота дискретизации WAV: {path}")
        cache[k] = info.frames / info.samplerate
    return float(cache[k])


def assemble(rows, roles, head_pause=True):
    """Склейка сегментов с паузами -> (audio float32, sr, [(offset_sec, title)], missing)."""
    import numpy as np
    import soundfile as sf
    pieces, chapters, missing, sr, t = [], [], 0, None, 0.0
    for i, r in enumerate(rows):
        p = wav_for(r, roles)
        if not p.exists():
            missing += 1
            continue
        x, s = sf.read(str(p), dtype="float32")
        if x.ndim > 1:
            x = x.mean(axis=1)
        sr = sr or s
        if s != sr:
            sys.exit(f"разная частота дискретизации: {p} {s} != {sr}")
        pause = int(r["pause_before_ms"]) if (i > 0 or head_pause) else 0
        pieces.append(np.zeros(int(sr * pause / 1000), dtype="float32"))
        t += pause / 1000
        if r["kind"] == "head":
            if chapters and chapters[-1][1] == r["ch_title"] and t - chapters[-1][0] < 15:
                chapters.pop()                       # «Часть …» + «Глава …» подряд — одна метка, на самой главе
            chapters.append((t, r["ch_title"]))
        pieces.append(x)
        t += len(x) / sr
    if not pieces:
        return None, None, [], missing
    pieces.append(np.zeros(int(sr * PAUSE["chapter_end"] / 1000), dtype="float32"))
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
    af = "loudnorm=I=-18:TP=-2:LRA=11"
    if speed != 1.0:
        af = atempo_chain(speed) + "," + af
    cmd += ["-map_metadata", "1", "-af", af, "-ac", "1", "-b:a", bitrate]
    if is_mp3:
        cmd += ["-c:a", "libmp3lame", "-id3v2_version", "3", "-f", "mp3"]   # главы — ID3v2 CHAP, читают не все плееры
    else:
        cmd += ["-c:a", "aac", "-movflags", "+faststart", "-f", "ipod"]
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


def _read_parts_index() -> list:
    """Read the saved part index without silently treating damage as an empty/new edition."""
    try:
        raw = PARTS_INDEX.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except (OSError, UnicodeError) as e:
        raise ValueError(f"не удаётся прочитать work/parts.json: {e}; старые части не изменены") from e
    try:
        saved = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError("work/parts.json повреждён: индекс частей не будет сброшен автоматически; "
                         "сохраните копию файла и готовых частей перед восстановлением") from e
    if not isinstance(saved, list):
        raise ValueError("work/parts.json имеет неизвестную структуру: ожидался список частей; "
                         "старые файлы сохранены")
    for i, part in enumerate(saved, 1):
        if not isinstance(part, dict) or not isinstance(part.get("start"), str) or not part["start"] \
                or not isinstance(part.get("end"), str) or not part["end"]:
            raise ValueError(f"work/parts.json: запись части {i} неполная; автоматическая перестройка остановлена")
        if ("start_id" in part) != ("end_id" in part):
            raise ValueError(f"work/parts.json: у части {i} задана только одна граница ID; индекс сохранён")
        if "sig" in part and part["sig"] and not isinstance(part["sig"], str):
            raise ValueError(f"work/parts.json: неверная подпись содержимого части {i}; индекс сохранён")
    return saved


def _write_parts_index_atomic(parts: list):
    """Publish the part index only after a complete JSON file has reached disk."""
    PARTS_INDEX.parent.mkdir(parents=True, exist_ok=True)
    tmp = PARTS_INDEX.with_name(f"{PARTS_INDEX.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(parts, f, ensure_ascii=False, indent=1)
            f.write("\n")
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, PARTS_INDEX)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def plan_parts(rows, roles, minutes: float, speed: float = 1.0):
    """-> (parts, ready, complete). Существующие границы частей остаются замороженными.
    Новая граница рассчитывается по длительности исходного звука с учётом скорости финального atempo."""
    target_source_seconds = part_source_seconds(minutes, speed)
    cache = load_durations()
    keys = [wav_for(r, roles).stem for r in rows]
    # WAV cache keys may repeat by design; book positions must instead use unique row IDs.
    frozen = []
    id_pos = {str(r["id"]): i for i, r in enumerate(rows)}
    if len(id_pos) != len(rows):
        raise ValueError("в segments.csv повторяются ID сегментов: невозможно безопасно привязать части")
    key_positions = {}
    for i, key in enumerate(keys):
        key_positions.setdefault(key, []).append(i)
    saved = _read_parts_index()
    for part_no, p in enumerate(saved, 1):
        if "n" in p and str(p["n"]) != str(part_no):
            raise ValueError(f"work/parts.json: нарушена последовательность номеров частей у записи {part_no}")
        if "start_id" in p and "end_id" in p:
            a, b = id_pos.get(str(p["start_id"])), id_pos.get(str(p["end_id"]))
        else:
            # Старый индекс по WAV-хешу неоднозначен при повторяющихся репликах.
            starts = key_positions.get(p.get("start"), [])
            ends = key_positions.get(p.get("end"), [])
            if len(starts) != 1 or len(ends) != 1:
                raise ValueError("work/parts.json содержит старые неоднозначные границы (повторяющиеся реплики). "
                                 "Автоматическая миграция остановлена, чтобы не перепутать части; сохраните старый индекс "
                                 "и готовые файлы частей перед ручной перестройкой.")
            a, b = starts[0], ends[0]
        if a is None or b is None or b < a or (frozen and a != frozen[-1][1] + 1) or (not frozen and a != 0):
            raise ValueError("work/parts.json относится к другой редакции книги: сохранённые границы "
                             "больше не находятся в текущей разбивке. Старые части и индекс сохранены.")
        if keys[a] != p.get("start") or keys[b] != p.get("end"):
            raise ValueError("work/parts.json относится к другой редакции книги: границы частей больше не "
                             "совпадают с аудио текущих реплик. Старые части и индекс сохранены; не запускайте "
                             "пересборку поверх них, пока не сохраните отдельную копию старой редакции.")
        saved_sig = p.get("sig")
        if saved_sig:
            current_sig = hashlib.sha256("\n".join(keys[a:b + 1]).encode("utf-8")).hexdigest()
            if saved_sig != current_sig:
                raise ValueError("содержимое части изменилось после сборки (work/parts.json): "
                                 "сохраните старую редакцию и её аудио отдельно перед пересборкой.")
        frozen.append((a, b))
    # сколько сегментов готово подряд с начала книги
    ready = 0
    while ready < len(rows) and wav_for(rows[ready], roles).exists():
        ready += 1
    complete = ready == len(rows)
    parts = [(a, b, True) for a, b in frozen]
    start = frozen[-1][1] + 1 if frozen else 0
    acc = 0.0
    for i in range(start, ready):
        r = rows[i]
        acc += int(r["pause_before_ms"]) / 1000 + duration_of(wav_for(r, roles), cache)
        nxt = rows[i + 1] if i + 1 < len(rows) else None
        boundary_ok = nxt is None or nxt["kind"] == "head" or int(nxt["pause_before_ms"]) >= PAUSE["para"]
        if acc >= target_source_seconds and boundary_ok:
            parts.append((start, i, True))
            start, acc = i + 1, 0.0
    if complete and start < len(rows):
        parts.append((start, len(rows) - 1, True))     # хвост книги — последняя часть, пусть и короче
    elif start < ready:
        parts.append((start, ready - 1, False))        # недособранная часть (ещё растёт)
    DUR_CACHE.write_text(json.dumps(cache), encoding="utf-8")
    return parts, ready, complete


def _part_build_signature(rows, roles, a, b, title, author, bitrate, fmt, speed) -> str:
    """Identity of the encoded part, not just its spoken text.

    Existing M4B reuse also depends on chapter labels/pauses, voice assignments,
    actual WAV revisions and the encoder configuration.
    """
    wavs = []
    selected_rows = []
    for row in rows[a:b + 1]:
        wav = wav_for(row, roles)
        try:
            st = wav.stat()
        except OSError as e:
            raise ValueError(f"нет готового WAV для части: {wav.name}") from e
        wavs.append([wav.name, st.st_size, st.st_mtime_ns])
        selected_rows.append({field: row.get(field) for field in FIELDS})
    payload = {
        "schema": 1,
        "rows": selected_rows,
        "wav": wavs,
        "roles": roles,
        "title": title,
        "author": author,
        "bitrate": bitrate,
        "format": fmt,
        "speed": float(speed),
        "pauses": PAUSE,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def build_parts(rows, roles, minutes, title, author, bitrate, fmt="m4b", quiet=False, speed=1.0):
    """Сериализует сборку частей между процессами: общие временные имена и индекс нельзя писать параллельно."""
    with _claim_guard(PARTS_INDEX):
        return _build_parts_locked(rows, roles, minutes, title, author, bitrate, fmt, quiet, speed)


def _build_parts_locked(rows, roles, minutes, title, author, bitrate, fmt="m4b", quiet=False, speed=1.0):
    """Собирает все закрытые части; существующий файл пропускается только при совпадении индекса и подписи."""
    saved_index = _read_parts_index()
    parts, ready, complete = plan_parts(rows, roles, minutes, speed=speed)
    PARTS.mkdir(exist_ok=True)
    new_files = []
    # Keep the entire validated suffix: a crash during rebuilding an early part must not
    # truncate index records for later, already-existing output files.
    frozen = [dict(part) for part in saved_index]
    planned_count = len(parts)
    for existing in PARTS.glob(f"part_*.{fmt}"):
        match = re.fullmatch(rf"part_(\d+)\.{re.escape(fmt)}", existing.name)
        if match and int(match.group(1)) > planned_count:
            raise ValueError(f"найден старый файл {existing.name}, которого нет в текущем плане частей; "
                             "он сохранён, автоматическая синхронизация остановлена")
    for n, (a, b, closed) in enumerate(parts, 1):
        out = PARTS / f"part_{n:02d}.{fmt}"
        if not closed:
            if out.exists():
                raise ValueError(f"{out.name} существует для ещё не завершённой части; файл сохранён")
            break
        part_keys = [wav_for(row, roles).stem for row in rows[a:b + 1]]
        part_sig = hashlib.sha256("\n".join(part_keys).encode("utf-8")).hexdigest()
        build_sig = _part_build_signature(rows, roles, a, b, title, author, bitrate, fmt, speed)
        record = {"n": n, "start_id": str(rows[a]["id"]), "end_id": str(rows[b]["id"]),
                  "start": part_keys[0], "end": part_keys[-1], "sig": part_sig, "build_sig": build_sig}
        if out.exists():
            if n > len(saved_index) or not saved_index[n - 1].get("sig"):
                raise ValueError(f"{out.name} уже существует, но work/parts.json не подтверждает содержимое "
                                 "этой части. Старый файл не перезаписан и не принят за новую редакцию; "
                                 "сохраните копию старых частей и индекса перед восстановлением.")
            if saved_index[n - 1].get("sig") != part_sig:
                raise ValueError(f"{out.name} не соответствует подписи текущей части; старый файл сохранён")
            old_build_sig = saved_index[n - 1].get("build_sig")
            if not old_build_sig:
                raise ValueError(f"{out.name} имеет старый индекс без подписи параметров сборки; нельзя подтвердить "
                                 "скорость, кодек, главы и параметры голоса. Старый файл сохранён; сделайте копию "
                                 "старой редакции и её частей перед явной пересборкой.")
            if old_build_sig != build_sig:
                raise ValueError(f"{out.name} собран с другими параметрами или данными WAV; старый файл сохранён")
            old_sha = saved_index[n - 1].get("output_sha256")
            if not old_sha:
                raise ValueError(f"{out.name} не имеет контрольной суммы в индексе; автоматический пропуск запрещён, "
                                 "старый файл сохранён")
            if _file_sha256(out) != old_sha:
                raise ValueError(f"{out.name} изменён после сборки (контрольная сумма не совпадает); "
                                 "старый файл сохранён, автоматический пропуск запрещён")
            record["output_sha256"] = old_sha
            record["output_size"] = out.stat().st_size
            if n <= len(frozen):
                frozen[n - 1] = record
            else:
                frozen.append(record)
            continue

        audio, sr, chapters, _ = assemble(rows[a:b + 1], roles, head_pause=False)
        ch_name = rows[a]["ch_title"]
        total = encode_m4b(audio, sr, chapters, out, f"{title} — часть {n:02d} ({ch_name})", author, bitrate,
                           speed=speed)
        if not out.is_file() or out.stat().st_size <= 0:
            raise RuntimeError(f"кодировщик не создал готовый файл части: {out.name}")
        record["output_sha256"] = _file_sha256(out)
        record["output_size"] = out.stat().st_size
        if n <= len(frozen):
            frozen[n - 1] = record
        else:
            frozen.append(record)
        _write_parts_index_atomic(frozen)
        new_files.append(out)
        if not quiet:
            print(f"[parts] {out.name}  {total / 60:5.1f} мин  с «{ch_name}»")
    _write_parts_index_atomic(frozen)
    if not quiet:
        print(f"готово сегментов подряд с начала: {ready}/{len(rows)}; частей собрано: {len(frozen)}"
              + (" — книга целиком озвучена" if complete else ""))
    return new_files, complete


def copy_to(dest: Path, src: Path):
    """Копия через временное имя: клиент облака не начнёт заливать недописанный файл."""
    dest.mkdir(parents=True, exist_ok=True)
    tmp = dest / (src.name + ".copying")
    shutil.copy2(src, tmp)
    os.replace(tmp, dest / src.name)


def watch(args):
    dest = Path(args.dest) if args.dest else None
    sent = set()
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
            if dest and f.name not in sent:
                copy_to(dest, f)
                sent.add(f.name)
                print(f"[watch] {time.strftime('%H:%M')}  отправлена {f.name}")
        n_parts = len(list(PARTS.glob(f"part_*.{args.format}"))) if PARTS.is_dir() else 0
        ready = sum(1 for r in rows if wav_for(r, roles).exists())
        print(f"[watch] {time.strftime('%H:%M')}  озвучено {ready}/{len(rows)} сегментов, частей готово {n_parts}")
        if complete:
            full = HERE / f"book.{args.format}"
            if not full.exists():
                audio, sr, chapters, _ = assemble(rows, roles)
                encode_m4b(audio, sr, chapters, full, args.title, args.author, args.bitrate, speed=args.speed)
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
    y.add_argument("--accent", default="none", choices=["none", "plus", "acute"],
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

    args = ap.parse_args()
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
