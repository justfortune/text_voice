#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# NOX_KIT 2026-11-11g
"""
nastroyki_ozvuchki.py — конфигуратор озвучки: таблица «Настройки озвучки.xlsx» в папке книги (D:\\kniga1a и т.п.).

  python nastroyki_ozvuchki.py read  ПАПКА_КНИГИ   общие параметры из таблицы -> work/settings.json
                                                   (нет таблицы — значения по умолчанию)
  python nastroyki_ozvuchki.py roles ПАПКА_КНИГИ   роли: правки из таблицы -> work/roles.json (пол, возраст, описание,
                                                   темп, громкость, «голос как у», пересоздать) + обновление таблицы
                                                   (новые роли, число реплик, образцы, проверка пола)
Вызывается из go автоматически (окружение envfast, нужен openpyxl). Таблица — единственное место, где что-то правят руками.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

CMD = sys.argv[1] if len(sys.argv) > 1 else "read"
ROOT = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else Path(__file__).resolve().parent
WORK = ROOT / "work"
XLSX = ROOT / "Настройки озвучки.xlsx"
SETTINGS_JSON = WORK / "settings.json"
FIXED_ROLES = ("narrator", "Джек", "Хорват")        # голоса, утверждённые автором: меняются только явной правкой в таблице

# значения по умолчанию, зависящие от книги (по имени папки)
BOOK_DEFAULTS = {
    "kniga1a": dict(title="Посох Забвения", author="Д.", dest="Posoh_izdanie3", me="Джек"),
    "kniga2a": dict(title="Имя города", author="Д.", dest="Imya_goroda", share_from="D:\\kniga1a", me="Джек"),
    # книга 3 — приквел, «Тетрадь капитана»: «я» = капитан в молодости (Ян); общие голоса — из книги 2, иначе из книги 1
    "kniga3a": dict(title="Тот, кто держал", author="Д.", dest="Tot_kto_derzhal",
                    share_from="D:\\kniga2a;D:\\kniga1a", me="Ян"),
}

YES, NO = "да", "нет"
ACCENT_CHOICES = {"все слова (рекомендуется)": "acute", "только имена и трудные слова": "sparse",
                  "без ударений (базовая модель)": "base"}
FORMAT_CHOICES = {"mp3": "mp3", "m4b": "m4b"}
ASR_CHOICES = {"small (быстрее)": "small", "medium (точнее; в 3 раза медленнее)": "medium"}

E_ASM = "без переозвучки — только пересборка частей"
E_SEG = "без переозвучки — только пересборка частей"
E_RUN = "со следующего запуска go"
E_ALL = "ОСТОРОЖНО: переозвучка почти всей книги"

# ключ, раздел, название, по умолчанию, тип, (мин, макс) или варианты, что делает, когда действует
PARAMS = [
    ("tempo_pct", "Темп и паузы", "Скорость речи, %", 112, "int", (60, 180),
     "100 — как прочитал движок; 112 — на 12% быстрее. Высота голоса не меняется.", E_ASM),
    ("pauses_pct", "Темп и паузы", "Паузы между репликами и абзацами, %", 85, "int", (30, 200),
     "Все паузы между кусками текста целиком: 100 — как задано ниже, 85 — на 15% короче.", E_ASM),
    ("trim", "Темп и паузы", "Сжимать затянутые паузы внутри фраз", YES, "bool", None,
     "Движок иногда «задумывается» посреди фразы — такие паузы укорачиваются (речь не трогается).", E_ASM),
    ("keep_ms", "Темп и паузы", "Пауза внутри фразы: короче этого не трогать, мс", 350, "int", (100, 2000),
     "Обычные паузы на запятых и точках сохраняются как есть.", E_ASM),
    ("cap_ms", "Темп и паузы", "Пауза внутри фразы: не длиннее, мс", 800, "int", (200, 3000),
     "Самая длинная пауза, которая может остаться внутри одной фразы после сжатия.", E_ASM),
    ("equalize", "Темп и паузы", "Подтягивать слишком медленные реплики к темпу роли", YES, "bool", None,
     "Реплика, прочитанная заметно медленнее обычного для этого персонажа, ускоряется (не больше, чем указано ниже).",
     E_ASM),
    ("eq_max_pct", "Темп и паузы", "Подтягивать медленные реплики не больше чем на, %", 15, "int", (0, 40),
     "Сверх общей скорости. Темп сравнивается по звучащей речи, паузы внутри реплики не в счёт.", E_ASM),
    ("pause_para", "Темп и паузы", "Пауза между абзацами, мс", 550, "int", (0, 5000), "До применения процента выше.", E_SEG),
    ("pause_dialog", "Темп и паузы", "Пауза перед репликой, мс", 320, "int", (0, 5000), "", E_SEG),
    ("pause_sentence", "Темп и паузы", "Пауза после точки внутри длинного абзаца, мс", 420, "int", (0, 3000),
     "Длинный абзац озвучивается кусками; если кусок кончился точкой, следующее предложение начинается после "
     "этой паузы (раньше было 120 мс — следующая фраза шла сразу после точки).", E_SEG),
    ("pause_turn", "Темп и паузы", "Пауза при смене голоса внутри абзаца, мс", 260, "int", (0, 3000),
     "«— Стой! — крикнул он»: между репликой героя и словами автора.", E_SEG),
    ("pause_chunk", "Темп и паузы", "Пауза между кусками одной фразы (на запятой), мс", 120, "int", (0, 3000), "", E_SEG),
    ("pause_poem_line", "Темп и паузы", "Пауза между строками стихов, мс", 250, "int", (0, 3000), "", E_SEG),
    ("pause_heading", "Темп и паузы", "Пауза после заголовка и разрыва сцены, мс", 1200, "int", (0, 8000), "", E_SEG),
    ("pause_chapter_start", "Темп и паузы", "Пауза перед главой, мс", 1500, "int", (0, 8000), "", E_SEG),
    ("pause_chapter_end", "Темп и паузы", "Пауза в конце части, мс", 2000, "int", (0, 8000), "", E_SEG),

    ("norm", "Темп и паузы", "Выравнивать громкость реплик", YES, "bool", None,
     "Все реплики и голоса приводятся к одной громкости (поправка не больше ±6 дБ); громкость роли — на листе «Роли».",
     E_ASM),

    ("asr_check", "Проверка", "Проверять каждую реплику распознаванием речи", YES, "bool", None,
     "Распознаватель Whisper (на процессоре, видеокарту не занимает) слушает каждую озвученную реплику и сверяет с "
     "текстом. Недочитанный конец, выброшенные слова, повторы — реплика переозвучивается; в части на Диск идёт "
     "только проверенное. Отбраковка: work\\asr_log.txt; под вопросом после всех попыток: work\\asr_suspicious.txt.",
     E_RUN),
    ("asr_model", "Проверка", "Распознаватель", "small (быстрее)", "choice", ASR_CHOICES,
     "small достаточно, чтобы поймать обрыв; medium точнее, но медленнее.", E_RUN),
    ("asr_threads", "Проверка", "Потоков процессора для проверки", 4, "int", (1, 16),
     "Больше — быстрее проверка, но меньше процессора остаётся воркерам озвучки.", E_RUN),
    ("asr_strict_pct", "Проверка", "Сходство услышанного с текстом не ниже, %", 60, "int", (30, 95),
     "Выше — строже (больше переозвучек из-за ошибок самого распознавателя).", E_RUN),
    ("tail_db", "Проверка", "Порог «обрублен конец слова», дБ", -16, "int", (-40, -6),
     "Запись, которая кончается на звуке громче этого (относительно речи), переозвучивается: естественный конец фразы "
     "затухает до −40 дБ и тише. Ближе к нулю — мягче проверка, дальше (−25) — строже.", E_RUN),
    ("vowel_cut_ms", "Проверка", "Порог «оборвана гласная на конце», мс", 20, "int", (0, 60),
     "Реплика кончается на гласную, а звук обрывается быстрее этого — переозвучивается («Возьмит…» вместо «Возьмите»). "
     "Естественное затухание 30–100 мс. 0 — не проверять.", E_RUN),
    ("asr_tries", "Проверка", "Попыток на реплику при отбраковке", 3, "int", (1, 6),
     "Сколько раз всего озвучить реплику, если проверка её не пропускает; потом берётся лучшая из попыток.", E_RUN),

    ("title", "Выпуск", "Название книги", "", "text", None, "Пишется в свойства файлов-частей.", E_SEG),
    ("author", "Выпуск", "Автор", "", "text", None, "", E_SEG),
    ("dest", "Выпуск", "Папка на Яндекс Диске", "", "text", None,
     "Имя папки внутри Яндекс Диска (или полный путь, например E:\\Книги\\Посох). Каждый запуск go кладёт части "
     "в свою папку внутри неё — с датой и временем начала запуска (2026-11-05_23-05); прежние не трогаются.", E_RUN),
    ("part_min", "Выпуск", "Длина одной части, мин", 19, "int", (3, 180),
     "Части режутся по границам абзацев, поэтому длина примерная.", E_SEG),
    ("format", "Выпуск", "Формат частей", "mp3", "choice", FORMAT_CHOICES,
     "mp3 — играет везде; m4b — формат аудиокниг с главами.", E_SEG),
    ("bitrate", "Выпуск", "Качество, кбит/с", 96, "int", (48, 320), "96 — хорошо для речи; 128 — с запасом.", E_SEG),

    ("workers", "Озвучка", "Воркеров (параллельных озвучек на видеокарте)", 3, "int", (1, 4),
     "3 — на 24 ГБ видеопамяти; при нехватке памяти — 2.", E_RUN),
    ("attempts", "Озвучка", "Попыток при обрыве или проглатывании слов", 3, "int", (1, 6),
     "Фраза перегенерируется, если оборвана, упёрлась в предел длины или прочитана подозрительно быстро.", E_RUN),
    ("rate_max", "Озвучка", "Порог «слишком быстро», букв в секунду", 14.8, "float", (10.0, 25.0),
     "Нормальная речь — 11–13,5 буквы в секунду; быстрее — вероятно, слова выброшены.", E_RUN),
    ("accent", "Озвучка", "Ударения", "все слова (рекомендуется)", "choice", ACCENT_CHOICES,
     "Знак ударения над словами (модель ru-stress) или чистый текст для базовой модели.", E_ALL),
    ("max_chars", "Озвучка", "Длина куска текста для озвучки, знаков", 200, "int", (80, 400),
     "Короче — меньше риск «проглотить» слова, но больше склеек.", E_ALL),
    ("docx", "Озвучка", "Рукопись (docx) — для новой книги", "", "text", None,
     "Пусто — рукопись, уже скопированная в папку книги (book.docx), или Загрузки\\kniga1.docx / kniga2.docx / kniga3.docx.",
     E_ALL),
    ("share_from", "Озвучка", "Голоса общих персонажей брать из книги", "", "text", None,
     "Папка другой книги (например D:\\kniga1a): персонажи с тем же именем звучат так же, как там. Можно несколько через «;» — "
     "берётся первая, где персонаж есть. Пусто — свои голоса.",
     "со следующего запуска go: переозвучка ролей, чей голос поменялся"),
    ("me", "Озвучка", "Кто говорит «я» (рассказчик-герой)", "Джек", "text", None,
     "Реплики «сказал я», которых нет в сценарии ролей, идут этой роли. Книги 1–2 — Джек, книга 3 — Ян.",
     "со следующего запуска go: переозвучка только таких реплик"),
]
P = {p[0]: p for p in PARAMS}

GENDERS = ["муж", "жен", "юноша", "мальчик", "девочка"]
GENDER_PREFIX = {"муж": "Male voice.", "жен": "Female voice.", "юноша": "Young male voice.",
                 "мальчик": "Boy's voice (child, not an adult).", "девочка": "Little girl's voice (child)."}
PREFIX_TO_GENDER = [("Young male voice", "юноша"), ("Female voice", "жен"), ("Boy's voice", "мальчик"),
                    ("Little girl's voice", "девочка"), ("Male voice", "муж")]
GENDER_RU = {"male": "мужской", "female": "женский", "child": "детский", "teen": "юношеский"}
AGE_RX = re.compile(r"\b(around|about|aged|age|of)\s+(\d{1,3})\b", re.I)
AGE_RX2 = re.compile(r"\b(\d{1,3})-year-old\b", re.I)
GENERIC_SPLIT = re.compile(r"^(.*) \(гл\.(\d+)\)$")

ROLE_COLS = ["Роль", "Кто это", "Пол", "Возраст", "Описание голоса (лучше по-английски)", "Темп роли, %",
             "Громкость, %", "Голос как у роли", "Пересоздать голос", "Реплик", "Главы",
             "Образец (клик — послушать)", "Проверка пола по тону", "Состояние"]
C = {name: i for i, name in enumerate(ROLE_COLS)}
EDITABLE = {"Кто это", "Пол", "Возраст", "Описание голоса (лучше по-английски)", "Темп роли, %", "Громкость, %",
            "Голос как у роли", "Пересоздать голос"}


# ---------------------------------------------------------------------------------------------------------------- общие

def book_defaults() -> dict:
    d = dict(BOOK_DEFAULTS.get(ROOT.name.lower(), {}))
    d.setdefault("title", ROOT.name)
    d.setdefault("author", "")
    d.setdefault("dest", ROOT.name)
    d.setdefault("share_from", "")
    d.setdefault("me", "Джек")
    return d


def default_of(key):
    bd = book_defaults()
    return bd[key] if key in bd else P[key][3]


def parse_value(key, raw):
    """-> (значение в виде для таблицы, предупреждение или None)"""
    _, _, name, _, typ, rng, _, _ = P[key]
    dflt = default_of(key)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return dflt, None
    if isinstance(raw, (datetime.date, datetime.datetime, datetime.time)):
        return dflt, f"«{name}»: Excel превратил значение в дату — взято по умолчанию {dflt}. Дробные числа пишите через запятую."
    if typ in ("int", "float"):
        try:
            v = float(str(raw).replace(",", ".").replace("%", "").strip())
        except ValueError:
            return dflt, f"«{name}»: «{raw}» — не число, взято по умолчанию {dflt}"
        lo, hi = rng
        if name.endswith(", мс") and 0 < v < 10 and lo >= 10:      # 0,7 — это секунды
            v2 = int(round(v * 1000))
            if lo <= v2 <= hi:
                return v2, f"«{name}»: {raw} понято как секунды — {v2} мс"
        if not lo <= v <= hi:
            v2 = min(max(v, lo), hi)
            return (int(round(v2)) if typ == "int" else v2), f"«{name}»: {raw} вне пределов {lo}–{hi}, взято {v2:g}"
        return (int(round(v)) if typ == "int" else round(v, 2)), None
    if typ == "bool":
        s = str(raw).strip().lower()
        if s in ("да", "yes", "1", "true", "истина", "+", "вкл"):
            return YES, None
        if s in ("нет", "no", "0", "false", "ложь", "-", "выкл"):
            return NO, None
        return dflt, f"«{name}»: «{raw}» — напишите да или нет; взято по умолчанию"
    if typ == "choice":
        s = str(raw).strip()
        for k in rng:
            if s.lower() == k.lower() or s.lower() == rng[k]:
                return k, None
        return dflt, f"«{name}»: «{raw}» — выберите из списка; взято по умолчанию"
    return str(raw).strip(), None


def read_general_from_xlsx() -> tuple[dict, list]:
    vals, warns = {}, []
    if not XLSX.exists():
        return vals, warns
    from openpyxl import load_workbook
    try:
        wb = load_workbook(XLSX, data_only=True)
    except Exception as e:
        warns.append(f"таблицу не прочитать ({type(e).__name__}: {e}) — взяты прежние настройки")
        return None, warns
    if "Общие" not in wb.sheetnames:
        return vals, warns
    ws = wb["Общие"]
    for row in ws.iter_rows(min_row=2, values_only=True):
        if len(row) < 6:
            continue
        key = row[5]
        if key in P:
            v, w = parse_value(key, row[1])
            if key == "dest" and ROOT.name.lower() == "kniga1a" and str(v).strip() == "Posoh_izdanie2":
                v = "Posoh_izdanie3"          # третье издание книги 1 (05.11): своя папка, второе остаётся как было
            vals[key] = v
            if w:
                warns.append(w)
    return vals, warns


def to_settings(vals: dict) -> dict:
    g = {k: vals.get(k, default_of(k)) for k in P}
    return {
        "tempo": g["tempo_pct"] / 100.0,
        "pauses": g["pauses_pct"] / 100.0,
        "trim": g["trim"] == YES,
        "equalize": g["equalize"] == YES,
        "sqz_keep": g["keep_ms"] / 1000.0,
        "sqz_cap": max(g["cap_ms"], g["keep_ms"]) / 1000.0,
        "pause": {k[6:]: int(g[k]) for k in P if k.startswith("pause_") and k != "pauses_pct"},
        "title": g["title"], "author": g["author"], "dest": g["dest"],
        "part_min": int(g["part_min"]), "format": FORMAT_CHOICES.get(g["format"], "mp3"),
        "bitrate": f"{int(g['bitrate'])}k",
        "workers": int(g["workers"]), "attempts": int(g["attempts"]), "rate_max": float(g["rate_max"]),
        "accent": ACCENT_CHOICES.get(g["accent"], "acute"), "max_chars": int(g["max_chars"]),
        "docx": g["docx"], "share_from": g["share_from"], "me": (g["me"] or "Джек").strip(),
        "eq_max": 1.0 + int(g["eq_max_pct"]) / 100.0, "norm": g["norm"] == YES, "norm_db": -20.0,
        "asr_check": g["asr_check"] == YES, "asr_model": ASR_CHOICES.get(g["asr_model"], "small"),
        "asr_threads": int(g["asr_threads"]), "asr_strict": int(g["asr_strict_pct"]) / 100.0,
        "asr_tries": int(g["asr_tries"]), "sent_gap": False, "tail_db": int(g["tail_db"]),   # sent_gap: вставка тишины в запись убрана (резала слова)
        "vowel_cut_ms": int(g["vowel_cut_ms"]),
        "_ui": {k: g[k] for k in P},
    }


def cmd_read():
    WORK.mkdir(parents=True, exist_ok=True)
    old = {}
    if SETTINGS_JSON.exists():
        try:
            old = json.loads(SETTINGS_JSON.read_text(encoding="utf-8"))
        except Exception:
            old = {}
    vals, warns = read_general_from_xlsx()
    if vals is None:                      # таблица битая — оставляем прежние настройки
        for w in warns:
            print("  ВНИМАНИЕ:", w)
        if not old:
            SETTINGS_JSON.write_text(json.dumps(to_settings({}), ensure_ascii=False, indent=1), encoding="utf-8")
        return
    st = to_settings(vals)
    write_atomic(SETTINGS_JSON, json.dumps(st, ensure_ascii=False, indent=1))
    if not XLSX.exists():
        write_xlsx(general_ui=st["_ui"], role_rows=None)
        print(f"  создана таблица настроек: {XLSX}")
    lock = XLSX.with_name("~$" + XLSX.name)
    if lock.exists():
        print("  таблица сейчас открыта в Excel — учтено только СОХРАНЁННОЕ (Ctrl+S)")
    for w in warns:
        print("  ВНИМАНИЕ:", w)
    ou = old.get("_ui", {})
    changed = [k for k in P if ou and k in ou and str(ou.get(k)) != str(st["_ui"][k])]
    for k in changed:
        print(f"  изменено: {P[k][2]}: {ou.get(k)} -> {st['_ui'][k]}   ({P[k][7]})")
    u = st["_ui"]
    print(f"  скорость речи {u['tempo_pct']}%, паузы {u['pauses_pct']}%, сжатие пауз внутри фраз: {u['trim']}, "
          f"воркеров {u['workers']}, части по ~{u['part_min']} мин ({u['format']}), ударения: {u['accent']}")


# ---------------------------------------------------------------------------------------------------------------- роли

def load_book_voices():
    """Заготовки голосов этой книги (voices_<папка>.py: V, FEMALE, ...), если есть."""
    sys.path.insert(0, str(ROOT))
    name = f"voices_{ROOT.name.lower()}"
    if not (ROOT / f"{name}.py").exists():
        return None
    try:
        return __import__(name)
    except Exception as e:
        print(f"  (заготовки {name}.py не загрузились: {type(e).__name__}: {e})")
        return None


def gender_from_instruct(instr: str):
    for pfx, g in PREFIX_TO_GENDER:
        if instr.startswith(pfx):
            return g
    head = instr[:80].lower()
    if re.search(r"\b(woman|female|girl|lady|grandmother|landlady|sorceress|wife|queen|nun)\b", head):
        return "девочка" if re.search(r"\b(little girl|girl of \d|child)\b", head) else "жен"
    if re.search(r"\b(boy|child|kid)\b", head):
        return "мальчик"
    if re.search(r"\b(young man|teen|teenage|youth|apprentice)\b", head):
        return "юноша"
    return None


def gender_from_sets(role, vk):
    if vk is None:
        return None
    m = GENERIC_SPLIT.match(role)
    r = m.group(1) if m else role
    if r in getattr(vk, "GIRL", set()):
        return "девочка"
    if r in getattr(vk, "BOY", set()):
        return "мальчик"
    if r in getattr(vk, "FEMALE", set()):
        return "жен"
    if r in getattr(vk, "TEEN", set()):
        return "юноша"
    return None


def strip_prefix(instr: str) -> str:
    for pfx, _ in PREFIX_TO_GENDER:
        if instr.startswith(pfx):
            rest = instr[len(pfx):]
            rest = re.sub(r"^\s*\([^)]*\)", "", rest)   # «(child, not an adult)»
            return rest.lstrip(" .").strip()
    return instr.strip()


AGE_RX3 = re.compile(r"^[^.:]{0,40}?,\s*(\d{1,3})\b")      # «Young man, 25, slightly ironic»


def age_match(text: str):
    """-> (начало, конец) числа-возраста в описании или None"""
    for rx in (AGE_RX, AGE_RX2, AGE_RX3):
        m = rx.search(text)
        if m:
            g = 2 if rx is AGE_RX else 1
            if 1 <= int(m.group(g)) <= 120:
                return m.start(g), m.end(g)
    return None


DECADES = {"twenties": 25, "thirties": 35, "forties": 45, "fifties": 55, "sixties": 65, "seventies": 75, "eighties": 85}
AGE_RX4 = re.compile(r"\bin (his|her|their) (" + "|".join(DECADES) + r")\b", re.I)


def age_from(text: str):
    sp = age_match(text)
    if sp:
        return int(text[sp[0]:sp[1]])
    m = AGE_RX4.search(text)
    return DECADES[m.group(2).lower()] if m else None


SWAP_M2F = [("man", "woman"), ("men", "women"), ("boy", "girl"), ("male", "female"), ("gentleman", "lady"),
            ("grandfather", "grandmother"), ("grandpa", "grandma"), ("father", "mother"), ("husband", "wife"),
            ("king", "queen"), ("monk", "nun"), ("brother", "sister"), ("son", "daughter"), ("sorcerer", "sorceress"),
            ("he", "she"), ("his", "her"), ("him", "her"), ("мужчина", "женщина"), ("старик", "старуха"),
            ("мальчик", "девочка"), ("юноша", "девушка"), ("парень", "девушка")]


def swap_gender_words(text: str, to_female: bool) -> str:
    pairs = SWAP_M2F if to_female else [(b, a) for a, b in SWAP_M2F if (a, b) not in (("him", "her"),)]
    mp = {}
    for a, b in pairs:
        mp.setdefault(a, b)
    rx = re.compile(r"\b(" + "|".join(sorted(map(re.escape, mp), key=len, reverse=True)) + r")\b", re.I)

    def rep(m):
        w = m.group(0)
        r = mp.get(w.lower(), w)
        return r.capitalize() if w[:1].isupper() else r
    return rx.sub(rep, text)


def cat(g):
    return "f" if g in ("жен", "девочка") else "m"


def compose(gender, age, desc, old_gender=None) -> str:
    d = (desc or "").strip()
    if old_gender and cat(old_gender) != cat(gender):
        d = swap_gender_words(d, to_female=cat(gender) == "f")
    if age:
        sp = age_match(d)
        if sp:
            d = d[:sp[0]] + str(age) + d[sp[1]:]
        elif AGE_RX4.search(d):
            d = AGE_RX4.sub(f"about {age} years old", d, count=1)
        else:
            d = f"About {age} years old. " + d
    return f"{GENDER_PREFIX.get(gender, 'Male voice.')} {d}".strip()


def norm_gender(v):
    s = str(v or "").strip().lower()
    for g in GENDERS:
        if s == g or (len(s) >= 3 and g.startswith(s)):
            return g
    aliases = {"м": "муж", "мужской": "муж", "мужчина": "муж", "ж": "жен", "женский": "жен", "женщина": "жен",
               "девушка": "жен", "парень": "юноша", "подросток": "юноша", "ребёнок": "мальчик", "ребенок": "мальчик"}
    return aliases.get(s)


def to_int(v, lo, hi):
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if isinstance(v, (datetime.date, datetime.datetime, datetime.time)):
        return None
    try:
        x = int(round(float(str(v).replace(",", ".").replace("%", "").strip())))
    except ValueError:
        return None
    return min(max(x, lo), hi)


def read_roles_from_xlsx() -> dict | None:
    if not XLSX.exists():
        return {}
    from openpyxl import load_workbook
    try:
        wb = load_workbook(XLSX, data_only=True)
    except Exception as e:
        print(f"  ВНИМАНИЕ: таблицу не прочитать ({type(e).__name__}) — правки ролей не применены")
        return None
    if "Роли" not in wb.sheetnames:
        return {}
    ws = wb["Роли"]
    hdr = [c.value for c in ws[1]]
    idx = {h: i for i, h in enumerate(hdr) if h}
    out = {}
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row or idx.get("Роль") is None or not row[idx["Роль"]]:
            continue

        def g(col):
            i = idx.get(col)
            return row[i] if i is not None and i < len(row) else None
        role = str(g("Роль")).strip()
        out[role] = {
            "note": (str(g("Кто это")).strip() if g("Кто это") is not None else ""),
            "gender": norm_gender(g("Пол")),
            "gender_raw": g("Пол"),
            "age": to_int(g("Возраст"), 1, 120),
            "desc": (str(g("Описание голоса (лучше по-английски)")).strip()
                     if g("Описание голоса (лучше по-английски)") is not None else ""),
            "tempo": to_int(g("Темп роли, %"), 50, 200) or 100,
            "gain": to_int(g("Громкость, %"), 20, 300) or 100,
            "same_as": (str(g("Голос как у роли")).strip() if g("Голос как у роли") else ""),
            "recast": str(g("Пересоздать голос") or "").strip().lower() in ("да", "yes", "1", "+"),
        }
    return out


def seed_row(role, cfg, vk):
    instr = cfg.get("instruct") or ""
    user = bool(cfg.get("user_cfg"))           # правленные в таблице роли: пол/возраст — как задал автор
    gender = ((cfg.get("gender") if user and cfg.get("gender") in GENDERS else None) or gender_from_instruct(instr)
              or gender_from_sets(role, vk) or (cfg.get("gender") if cfg.get("gender") in GENDERS else None) or "муж")
    note = cfg.get("note") or ""
    if not note and vk is not None:
        V = getattr(vk, "V", {})
        m = GENERIC_SPLIT.match(role)
        if role in V:
            note = V[role][1]
        elif m and m.group(1) in V:
            note = f"{V[m.group(1)][1]} — сцена главы {m.group(2)}"
    if not note:
        note = {"narrator": "рассказчик — весь авторский текст (голос утверждён автором)",
                "Джек": "герой, говорит от первого лица (голос утверждён автором)",
                "Хорват": "архи-волшебник, старик, учитель Джека (голос утверждён автором)"}.get(role, "")
    return {"note": note, "gender": gender, "age": (cfg.get("age") if user else None) or age_from(instr),
            "desc": strip_prefix(instr),
            "tempo": int(round(float(cfg.get("tempo", 1.0) or 1.0) * 100)),
            "gain": int(round(float(cfg.get("gain", 1.0) or 1.0) * 100)),
            "same_as": cfg.get("same_as") or "", "recast": False}


def write_atomic(path: Path, text: str):
    """Запись через временный файл: идущая озвучка/сборка никогда не прочтёт недописанный roles.json."""
    import time
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    for _ in range(50):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:          # Windows: файл в эту секунду читает другой процесс
            time.sleep(0.1)
    path.write_text(text, encoding="utf-8")
    tmp.unlink(missing_ok=True)


def share_voices(roles, n, src_root: str, done: set | None = None) -> tuple[set, list]:
    """Общие персонажи звучат так же, как в другой книге: образец голоса и его описание копируются оттуда.
    -> (роли, у которых голос сменился (их реплики переозвучить), сообщения)"""
    import hashlib
    import shutil
    msgs, changed = [], set()
    src = Path(src_root)
    srj = src / "work" / "roles.json"
    if not srj.exists():
        return changed, [f"  ВНИМАНИЕ: «Голоса брать из книги» = {src_root} — там нет work/roles.json, общие голоса не взяты"]
    sroles = json.loads(srj.read_text(encoding="utf-8"))
    sstems = n.voice_stems(sroles)
    tstems = n.voice_stems(roles)
    svoices = src / "work" / "voices"

    def h(p):
        return hashlib.sha1(p.read_bytes()).hexdigest() if p.exists() else None
    shared = 0
    for role, cfg in roles["roles"].items():
        own_edit = cfg.get("user_cfg") and cfg.get("user_cfg_book", ROOT.name.lower()) == ROOT.name.lower()
        if GENERIC_SPLIT.match(role) or own_edit or role not in sroles["roles"] or (done is not None and role in done):
            continue
        scfg = sroles["roles"][role]
        sr = role
        seen = set()
        while sroles["roles"].get(sr, {}).get("same_as") and sr not in seen:   # там роль звучит чужим голосом
            seen.add(sr)
            sr = sroles["roles"][sr]["same_as"]
        s_wav = svoices / f"{sr}.wav"
        if not s_wav.exists():
            s_wav = svoices / f"{sstems.get(sr, sr)}.wav"
        if not s_wav.exists():
            continue
        s_txt = s_wav.with_suffix(".txt")
        t_wav, t_txt = n.voice_files(role, tstems)
        hs, ht = h(s_wav), h(t_wav)
        if hs != ht:
            t_wav.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s_wav, t_wav)
            if s_txt.exists():
                shutil.copy2(s_txt, t_txt)
            if ht is not None:
                changed.add(role)
                msgs.append(f"  голос «{role}» обновлён из {src.name} — его реплики переозвучатся")
        for k in ("instruct", "gender", "age"):
            if scfg.get(k) is not None:
                cfg[k] = scfg[k]
        cfg["shared"] = src.name
        shared += 1
        if done is not None:
            done.add(role)
    msgs.insert(0, f"  общих персонажей с голосом из {src.name}: {shared}")
    return changed, msgs


def voice_key(r):
    return (r.get("gender"), r.get("age"), (r.get("desc") or "").strip())


def cmd_roles():
    sys.path.insert(0, str(ROOT))
    import nox_audiobook as n
    if not n.ROLES_PATH.exists():
        print("  roles.json ещё нет — роли появятся после разбивки")
        return
    roles = n.load_roles()
    rows = n.load_rows() if n.CSV_PATH.exists() else []
    vk = load_book_voices()
    aliases = roles.get("aliases", {})
    lines, chs = Counter(), defaultdict(set)
    for r in rows:
        if r["kind"] == "speech":
            who = aliases.get(r["speaker"], r["speaker"])
            lines[who] += 1
            chs[who].add(int(r["chapter"]))
    lines["narrator"] = sum(1 for r in rows if r["kind"] != "speech")
    xl = read_roles_from_xlsx()
    if xl is None:
        return
    vc = {}
    vcp = WORK / "voicecheck.json"
    if vcp.exists():
        try:
            vc = json.loads(vcp.read_text(encoding="utf-8"))
        except Exception:
            vc = {}

    # роли без единой реплики (старые догадки, базовые «старик», «женщина»...) — убрать, если это не меняет имена файлов
    if rows:
        keep = set(lines) | set(FIXED_ROLES) | set(aliases.values()) \
            | {c.get("same_as") for c in roles["roles"].values() if c.get("same_as")}
        for role in [r for r in roles["roles"] if r not in keep]:
            before = n.voice_stems(roles)
            trial = dict(roles, roles={k: v for k, v in roles["roles"].items() if k != role})
            after = n.voice_stems(trial)
            if all(after[k] == before[k] for k in trial["roles"]):
                roles["roles"] = trial["roles"]

    st0 = {}
    if SETTINGS_JSON.exists():
        try:
            st0 = json.loads(SETTINGS_JSON.read_text(encoding="utf-8"))
        except Exception:
            st0 = {}
    share_src = (st0.get("share_from") or "").strip()
    share_changed, share_msgs = set(), []
    srcs = [x.strip() for x in share_src.split(";") if x.strip() and Path(x.strip()).resolve() != ROOT]
    if srcs:
        done = set()
        for c in roles["roles"].values():
            c.pop("shared", None)
        for src_root in srcs:                      # несколько книг через «;»: персонаж берётся из первой, где он есть
            ch_, ms_ = share_voices(roles, n, src_root, done)
            share_changed |= ch_
            share_msgs += ms_
    else:
        for c in roles["roles"].values():
            c.pop("shared", None)

    recast, notes, warn = set(), [], []
    notes += share_msgs
    retempo = []
    out_rows = {}
    for role, cfg in roles["roles"].items():
        seed = seed_row(role, cfg, vk)
        ui = cfg.get("ui")
        row = xl.get(role)
        if row is None:
            row = dict(seed)
        else:
            row = dict(row)
            if row.get("gender") is None:
                if row.get("gender_raw"):
                    warn.append(f"{role}: пол «{row['gender_raw']}» не понят — оставлен прежний ({seed['gender']}); "
                                f"варианты: {', '.join(GENDERS)}")
                row["gender"] = (ui or seed)["gender"]
            if not row.get("desc"):
                row["desc"] = (ui or seed)["desc"]
        base = ui or seed
        raw = {"gender": row["gender"], "age": row["age"], "desc": row["desc"]}
        voice_edited = voice_key(row) != voice_key(base)
        if not voice_edited and not cfg.get("user_cfg"):
            row.update(gender=seed["gender"], age=seed["age"], desc=seed["desc"])   # обновлено заготовками книги
        if voice_edited:
            new_instr = compose(row["gender"], row["age"], row["desc"], old_gender=base.get("gender"))
            what = []
            if row["gender"] != base.get("gender"):
                what.append(f"пол {base.get('gender')} -> {row['gender']}")
            if row["age"] != base.get("age"):
                what.append(f"возраст {base.get('age') or '?'} -> {row['age'] or '?'}")
            if row["desc"] != base.get("desc"):
                what.append("описание")
            row["desc"] = strip_prefix(new_instr)           # показываем ровно то, что уйдёт в генератор голоса
            if new_instr != cfg.get("instruct"):
                cfg["instruct"] = new_instr
                recast.add(role)
                notes.append(f"  голос «{role}» будет создан заново: {', '.join(what) or 'описание'}")
                if role in FIXED_ROLES:
                    notes.append(f"  ВНИМАНИЕ: меняется утверждённый голос «{role}» — переозвучка всех его реплик ({lines.get(role, 0)})")
            cfg["user_cfg"] = True
            cfg["user_cfg_book"] = ROOT.name.lower()
        cfg["gender"] = row["gender"]
        cfg["age"] = row["age"]
        if row.get("note") and row["note"] != seed["note"]:
            cfg["note"] = row["note"]
        # темп и громкость роли — только пересборка
        t, gn = (row.get("tempo") or 100) / 100.0, (row.get("gain") or 100) / 100.0
        if abs(float(cfg.get("tempo", 1.0) or 1.0) - t) > 1e-6 or abs(float(cfg.get("gain", 1.0) or 1.0) - gn) > 1e-6:
            retempo.append(f"{role}: темп {int(t * 100)}%, громкость {int(gn * 100)}%")
        cfg["tempo"], cfg["gain"] = t, gn
        # голос как у другой роли
        _pre = (getattr(vk, "SAME_AS", {}) or {}).get(role) if vk is not None else None
        if _pre and cfg.get("same_as_preset") != _pre and _pre in roles["roles"]:   # решение автора из заготовок — разово
            row["same_as"] = _pre
            cfg["same_as_preset"] = _pre
        sa = row.get("same_as") or ""
        if sa and (sa not in roles["roles"] or sa == role):
            warn.append(f"{role}: «Голос как у роли» = «{sa}» — такой роли нет, не применено")
            sa = ""
        if sa:
            seen, cur = {role}, sa
            while cur and cur not in seen:
                seen.add(cur)
                cur = roles["roles"].get(cur, {}).get("same_as")
            if cur in seen and cur:
                warn.append(f"{role}: «Голос как у роли» даёт круг ({sa}) — не применено")
                sa = ""
        if (cfg.get("same_as") or "") != sa:
            notes.append(f"  «{role}» теперь звучит голосом «{sa}»" if sa else f"  «{role}» снова своим голосом")
            cfg["same_as"] = sa or None
            cfg["user_cfg"] = True
            cfg["user_cfg_book"] = ROOT.name.lower()
        # пересоздать по просьбе
        if row.get("recast"):
            if not cfg.get("recast_ack"):
                recast.add(role)
                notes.append(f"  голос «{role}» пересоздаётся по вашей отметке")
                cfg["recast_ack"] = True
        else:
            cfg.pop("recast_ack", None)
        row["recast"] = False
        cfg["ui"] = raw if voice_edited else {"gender": row["gender"], "age": row["age"], "desc": row["desc"]}
        out_rows[role] = row

    write_atomic(n.ROLES_PATH, json.dumps(roles, ensure_ascii=False, indent=2))

    stems = n.voice_stems(roles)
    removed_wavs = 0
    if share_changed and rows and n.WAVS.is_dir():
        for r in rows:
            if n.resolve_role(r["speaker"], roles) in share_changed:
                p = n.wav_for(r, roles)
                if p.exists():
                    p.unlink()
                    removed_wavs += 1
                    t = p.with_suffix(".txt")
                    if t.exists():
                        t.unlink()
    if recast:
        for role in recast:
            for p in n.voice_files(role, stems):
                if p.exists():
                    p.unlink()
        if rows and n.WAVS.is_dir():
            for r in rows:
                if n.resolve_role(r["speaker"], roles) in recast:
                    p = n.wav_for(r, roles)
                    if p.exists():
                        p.unlink()
                        removed_wavs += 1
                        t = p.with_suffix(".txt")
                        if t.exists():
                            t.unlink()

    # строки таблицы: справочные колонки
    table = []
    for role, row in out_rows.items():
        cfg = roles["roles"][role]
        wav_p = n.voice_files(role, stems)[0]
        v = vc.get(role)
        if cfg.get("same_as"):
            chk = f"голос роли «{cfg['same_as']}»"
        elif v and v.get("f0"):
            chk = (f"{v['f0']:.0f} Гц — " + ("ок" if v.get("ok") else "НЕ СОВПАЛ")
                   + f" (ожидался {GENDER_RU.get(v.get('expected'), v.get('expected'))})")
        else:
            chk = ""
        if role in recast:
            state = "голос создаётся заново"
        elif cfg.get("same_as"):
            state = f"звучит как «{cfg['same_as']}»"
        elif cfg.get("shared"):
            state = f"голос из книги {cfg['shared']}"
        elif not wav_p.exists():
            state = "образец будет создан"
        elif v and not v.get("ok", True):
            state = "пол не совпал — пересоздаётся автоматически"
        else:
            state = "готов"
        if rows and lines.get(role, 0) == 0:
            state = "нет реплик (не используется)"
        ch = sorted(chs.get(role, []))
        ch_s = "все" if role == "narrator" else (", ".join(map(str, ch)) if len(ch) <= 10 else f"{ch[0]}–{ch[-1]} ({len(ch)} глав)")
        table.append(dict(row, role=role, lines=lines.get(role, 0), chapters=ch_s,
                          sample=(wav_p if wav_p.exists() else None), check=chk, state=state))
    table.sort(key=lambda d: (-(10 ** 9 if d["role"] == "narrator" else d["lines"]), d["role"]))

    st = {}
    if SETTINGS_JSON.exists():
        try:
            st = json.loads(SETTINGS_JSON.read_text(encoding="utf-8"))
        except Exception:
            st = {}
    general_ui = st.get("_ui")
    if XLSX.exists():
        gv, _ = read_general_from_xlsx()
        if gv:
            general_ui = {k: gv.get(k, default_of(k)) for k in P}
    write_xlsx(general_ui=general_ui, role_rows=table)

    for w in warn:
        print("  ВНИМАНИЕ:", w)
    for s in notes:
        print(s)
    if retempo:
        print("  темп/громкость ролей (только пересборка частей): " + "; ".join(retempo[:12])
              + (" …" if len(retempo) > 12 else ""))
    if recast or share_changed:
        print(f"  голосов на пересоздание: {len(recast)}; обновлено из другой книги: {len(share_changed)}; "
              f"реплик на переозвучку: {removed_wavs}")
    if not (warn or notes or retempo or recast):
        print(f"  роли: изменений в таблице нет ({len(table)} ролей)")


# ---------------------------------------------------------------------------------------------------------------- xlsx

HELP = [
    "КАК ПОЛЬЗОВАТЬСЯ",
    "",
    "1. Правьте только жёлтые колонки. Серые — справка (их перепишет программа).",
    "2. Сохраните таблицу (Ctrl+S). Excel можно не закрывать, но учитывается только сохранённое.",
    "3. Запустите go как обычно. Он сам поймёт, что поменялось, и сделает минимум работы:",
    "   • скорость, паузы, темп и громкость ролей, длина частей — только пересборка частей (минуты), без переозвучки;",
    "   • пол, возраст, описание голоса, «Пересоздать голос» — новый образец голоса и переозвучка только этой роли;",
    "   • «Голос как у роли» — роль начинает звучать голосом другой (её реплики переозвучиваются);",
    "   • ударения и длина куска текста — переозвучка почти всей книги (помечено красным).",
    "",
    "ЛИСТ «РОЛИ»",
    "• Пол: муж / жен / юноша / мальчик / девочка (выпадающий список).",
    "• Возраст: число. Если в описании есть «around 65», число в описании заменится на новый возраст.",
    "• Описание голоса: для генератора голосов; лучше по-английски (по-русски тоже работает, но менее точно).",
    "  Меняя пол, слова man/woman, boy/girl в описании поправятся сами.",
    "• Темп роли, %: 100 — как все; 90 — этот персонаж говорит медленнее, 110 — быстрее (поверх общей скорости).",
    "• Громкость, %: 100 — как есть; 120 — громче, 80 — тише.",
    "• Голос как у роли: впишите имя другой роли (точно как в колонке «Роль») — у этой роли будет тот же голос.",
    "  Пример: все безымянные «стражник (гл.N)» одним голосом — впишите им «стражник (гл.9)».",
    "• Пересоздать голос: «да» — при следующем запуске голос сгенерируется заново (если образец не понравился).",
    "• Образец: щелчок открывает запись голоса (Excel спросит разрешение — нажмите «Да»).",
    "• Проверка пола по тону: измеренная высота голоса; «НЕ СОВПАЛ» — образец пересоздаётся автоматически.",
    "• Роли «(гл.N)» — безымянные персонажи сцены (у каждой сцены свой голос).",
    "• narrator, Джек, Хорват — утверждённые голоса; меняйте, только если точно нужно (переозвучка всех их реплик).",
    "",
    "ЛИСТ «ОБЩИЕ»",
    "• Скорость речи, %: 100 — как прочитал движок; 112 — на 12% быстрее. Высота голоса не меняется.",
    "• Колонка «Когда действует» говорит, во что обойдётся правка.",
    "• Пустая ячейка значения = значение по умолчанию.",
    "",
    "ПРОВЕРКА РАСПОЗНАВАНИЕМ (раздел «Проверка»)",
    "• Пока идёт озвучка, распознаватель речи слушает каждую готовую реплику и сверяет с текстом.",
    "  Недочитанный конец, выброшенные слова, повторы — реплика уходит на переозвучку, а часть на Диск ждёт исправления.",
    "• Если реплику уже переозвучили после отправки части — часть пересобирается и обновляется на Диске сама.",
    "• Что отбраковано: work\\asr_log.txt. Что не удалось исправить за все попытки (послушать вручную): work\\asr_suspicious.txt.",
    "",
    "Если таблицу испортить или удалить — она создастся заново из текущих настроек при следующем запуске go.",
]


def write_xlsx(general_ui: dict | None, role_rows: list | None):
    from openpyxl import Workbook, load_workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.worksheet.datavalidation import DataValidation

    if role_rows is None and XLSX.exists():
        try:                                    # роли не пересчитывались — сохраняем лист ролей как есть
            old = load_workbook(XLSX)
            if "Роли" in old.sheetnames:
                role_rows = []
                ws0 = old["Роли"]
                hdr = [c.value for c in ws0[1]]
                for r in ws0.iter_rows(min_row=2, values_only=True):
                    d = dict(zip(hdr, r))
                    if d.get("Роль"):
                        role_rows.append(dict(role=d.get("Роль"), note=d.get("Кто это") or "", gender=d.get("Пол"),
                                              age=d.get("Возраст"), desc=d.get("Описание голоса (лучше по-английски)") or "",
                                              tempo=d.get("Темп роли, %") or 100, gain=d.get("Громкость, %") or 100,
                                              same_as=d.get("Голос как у роли") or "", recast=False,
                                              lines=d.get("Реплик"), chapters=d.get("Главы"), sample=None,
                                              sample_text=d.get("Образец (клик — послушать)"),
                                              check=d.get("Проверка пола по тону"), state=d.get("Состояние")))
        except Exception:
            role_rows = None

    yellow = PatternFill("solid", fgColor="FFF4CC")
    grey = PatternFill("solid", fgColor="EFEFEF")
    head = PatternFill("solid", fgColor="3B5B7A")
    sect = PatternFill("solid", fgColor="D9E2EC")
    red_font = Font(color="B00020")
    hfont = Font(bold=True, color="FFFFFF")
    wrap = Alignment(wrap_text=True, vertical="top")
    wrap_c = Alignment(wrap_text=True, vertical="center")

    wb = Workbook()
    # ---- Общие
    ws = wb.active
    ws.title = "Общие"
    ws.append(["Параметр", "Значение", "По умолчанию", "Что это", "Чего стоит правка (применяется при запуске go)", "ключ"])
    for c in ws[1]:
        c.fill, c.font = head, hfont
        c.alignment = Alignment(wrap_text=True, vertical="center")
    ws.row_dimensions[1].height = 32
    gu = general_ui or {}
    dv_bool = DataValidation(type="list", formula1='"да,нет"', allow_blank=True)
    dv_acc = DataValidation(type="list", formula1='"' + ",".join(ACCENT_CHOICES) + '"', allow_blank=True)
    dv_fmt = DataValidation(type="list", formula1='"mp3,m4b"', allow_blank=True)
    dv_asr = DataValidation(type="list", formula1='"' + ",".join(ASR_CHOICES) + '"', allow_blank=True)
    for dv in (dv_bool, dv_acc, dv_fmt, dv_asr):
        ws.add_data_validation(dv)
    last_sec = None
    for key, sec, name, _, typ, rng, what, when in PARAMS:
        if sec != last_sec:
            ws.append([sec])
            r = ws.max_row
            for col in range(1, 6):
                ws.cell(r, col).fill = sect
            ws.cell(r, 1).font = Font(bold=True)
            last_sec = sec
        val = gu.get(key, default_of(key))
        ws.append([name, val, default_of(key), what, when, key])
        r = ws.max_row
        ws.cell(r, 2).fill = yellow
        ws.cell(r, 2).font = Font(bold=True, size=12 if typ != "choice" and typ != "text" else 11)
        ws.cell(r, 2).alignment = Alignment(horizontal="center", vertical="center")
        ws.cell(r, 3).alignment = Alignment(horizontal="center", vertical="center")
        ws.cell(r, 3).fill = grey
        for col in (4, 5):
            ws.cell(r, col).fill = grey
            ws.cell(r, col).alignment = wrap_c
        ws.cell(r, 1).alignment = wrap_c
        if when == E_ALL:
            ws.cell(r, 5).font = red_font
        if typ == "bool":
            dv_bool.add(ws.cell(r, 2))
        elif typ == "choice":
            (dv_acc if rng is ACCENT_CHOICES else dv_asr if rng is ASR_CHOICES else dv_fmt).add(ws.cell(r, 2))
        elif typ in ("int", "float"):
            dv = DataValidation(type="whole" if typ == "int" else "decimal", operator="between",
                                formula1=str(rng[0]), formula2=str(rng[1]), allow_blank=True,
                                error=f"Число от {rng[0]} до {rng[1]}", errorTitle="Недопустимое значение",
                                showErrorMessage=True)
            ws.add_data_validation(dv)
            dv.add(ws.cell(r, 2))
    for col, w in zip("ABCDEF", (44, 34, 26, 62, 36, 6)):
        ws.column_dimensions[col].width = w
    ws.column_dimensions["F"].hidden = True
    ws.freeze_panes = "B2"
    for sh in (ws,):
        sh.page_setup.orientation = "landscape"
        sh.sheet_properties.pageSetUpPr.fitToPage = True
        sh.page_setup.fitToWidth = 1
        sh.page_setup.fitToHeight = 0

    # ---- Роли
    wr = wb.create_sheet("Роли")
    wr.append(ROLE_COLS)
    for c in wr[1]:
        c.fill, c.font = head, hfont
        c.alignment = Alignment(wrap_text=True, vertical="center")
    dv_g = DataValidation(type="list", formula1='"' + ",".join(GENDERS) + '"', allow_blank=False,
                          error="Выберите из списка: " + ", ".join(GENDERS), showErrorMessage=True)
    dv_age = DataValidation(type="whole", operator="between", formula1="1", formula2="120", allow_blank=True,
                            error="Возраст — целое число 1–120", showErrorMessage=True)
    dv_t = DataValidation(type="whole", operator="between", formula1="50", formula2="200", allow_blank=True,
                          error="Темп роли — 50–200 %", showErrorMessage=True)
    dv_v = DataValidation(type="whole", operator="between", formula1="20", formula2="300", allow_blank=True,
                          error="Громкость — 20–300 %", showErrorMessage=True)
    dv_r = DataValidation(type="list", formula1='"да"', allow_blank=True)
    for dv in (dv_g, dv_age, dv_t, dv_v, dv_r):
        wr.add_data_validation(dv)
    for d in role_rows or []:
        sample_name = d["sample"].name if d.get("sample") else (d.get("sample_text") or "")
        wr.append([d["role"], d.get("note") or "", d.get("gender"), d.get("age"), d.get("desc") or "",
                   d.get("tempo") or 100, d.get("gain") or 100, d.get("same_as") or "",
                   "да" if d.get("recast") else "", d.get("lines"), d.get("chapters"), sample_name,
                   d.get("check") or "", d.get("state") or ""])
        r = wr.max_row
        for name, i in C.items():
            cell = wr.cell(r, i + 1)
            cell.fill = yellow if name in EDITABLE else grey
            if name in ("Описание голоса (лучше по-английски)", "Кто это", "Проверка пола по тону", "Состояние"):
                cell.alignment = wrap_c
            elif name in ("Пол", "Возраст", "Темп роли, %", "Громкость, %", "Пересоздать голос", "Реплик"):
                cell.alignment = Alignment(horizontal="center", vertical="center")
            else:
                cell.alignment = Alignment(vertical="center")
        if d.get("sample"):
            cell = wr.cell(r, C["Образец (клик — послушать)"] + 1)
            cell.hyperlink = d["sample"].resolve().as_uri()
            cell.font = Font(color="1F5FAF", underline="single")
        st = d.get("state") or ""
        if "НЕ СОВПАЛ" in (d.get("check") or "") or "не совпал" in st or "заново" in st:
            wr.cell(r, C["Состояние"] + 1).font = red_font
        dv_g.add(wr.cell(r, C["Пол"] + 1))
        dv_age.add(wr.cell(r, C["Возраст"] + 1))
        dv_t.add(wr.cell(r, C["Темп роли, %"] + 1))
        dv_v.add(wr.cell(r, C["Громкость, %"] + 1))
        dv_r.add(wr.cell(r, C["Пересоздать голос"] + 1))
    widths = [24, 34, 9, 8, 70, 9, 9, 18, 11, 8, 16, 22, 30, 30]
    for i, w in enumerate(widths):
        wr.column_dimensions[chr(ord("A") + i)].width = w
    wr.freeze_panes = "B2"
    wr.row_dimensions[1].height = 32
    wr.page_setup.orientation = "landscape"
    wr.sheet_properties.pageSetUpPr.fitToPage = True
    wr.page_setup.fitToWidth = 1
    wr.page_setup.fitToHeight = 0
    if wr.max_row > 1:
        wr.auto_filter.ref = f"A1:{chr(ord('A') + len(ROLE_COLS) - 1)}{wr.max_row}"
    if role_rows is None:
        wr.cell(2, 1, "(роли появятся после первой разбивки книги — запустите go)")

    # ---- Как пользоваться
    wh = wb.create_sheet("Как пользоваться")
    for line in HELP:
        wh.append([line])
    wh.column_dimensions["A"].width = 130
    wh["A1"].font = Font(bold=True, size=13)

    tmp = XLSX.with_name("~tmp_" + XLSX.name)
    wb.save(tmp)
    try:
        os.replace(tmp, XLSX)
    except PermissionError:
        try:
            tmp.unlink()
        except OSError:
            pass
        print("  таблица открыта в Excel — обновить её не удалось (ваши сохранённые правки учтены); "
              "закройте Excel, и при следующем запуске она обновится")


if __name__ == "__main__":
    os.chdir(ROOT)
    if CMD == "read":
        cmd_read()
    elif CMD == "roles":
        cmd_roles()
    else:
        sys.exit(__doc__)
