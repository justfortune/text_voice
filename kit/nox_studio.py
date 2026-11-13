#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# NOX_STUDIO 2026-11-12a
"""
nox_studio.py — «Нокс Студия»: ручная правка аудиокниги поверх черновой озвучки.

Что это. Конвейер (go) озвучивает книгу начерно и собирает части. Студия — следующий слой: слушаем часть, правим
огрехи (ударение, слово, кто говорит, скорость, пауза), переозвучиваем фразу на лету, сравниваем дубли, закрепляем
фразу, потом часть — и готовая часть уходит в отдельную папку. Потом так же глава.

Главные правила (на них держится сохранность работы):
  * Конвейер студию не знает и её файлы не трогает. Всё своё студия держит в <книга>\\studio (правки, дубли, копии)
    и <книга>\\gotovo (готовые части и главы). Закреплённая фраза хранит СВОЮ копию записи — зачистка и
    переозвучка конвейера ей не страшны.
  * Правка привязана к самой фразе (отпечаток текста + соседи), а не к номеру: книгу можно переразбивать.
  * Правило ударения действует «только здесь», «дальше в этой части» или «дальше по всей книге» — и никогда не
    трогает закреплённое.
  * Каждое изменение сразу на диске (атомарно), плюс журнал studio\\edits.log и копии studio\\backup.
  * Звук предпросмотра = звук готовой части: обработка та же, что у сборки конвейера (те же функции).

Запуск:  envfast\\Scripts\\pythonw.exe nox_studio.py --root D:\\kniga1a      (ярлык делает nox_studio.ps1)
Интерфейс: nox_studio.html рядом с этим файлом; открывается отдельным окном на http://127.0.0.1:8765
"""
from __future__ import annotations

import argparse
import csv
import bisect
import collections
import concurrent.futures
import copy
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "2026-11-12a"
ACUTE = "\u0301"
GLUE_GAP = 0.12                   # пауза на стыке склеенных записей, с (как между кусками одного предложения)
WORD_RE = re.compile(r"[А-Яа-яЁё\u0301]+")
VOWELS = "аеёиоуыэюяАЕЁИОУЫЭЮЯ"
STAGE_OK = 21                     # этап конвейера, с чьей сборкой сверен звук студии (сборка этапов 20 и 21 одна)
n = None                          # модуль nox_audiobook книги (подключается в main)
np = None


class Oops(Exception):
    """Ошибка, которую надо показать человеку как есть (не сбой программы)."""


# ---------------------------------------------------------------------------------------------- мелочи
def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def sha(s: str, k: int = 12) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:k]


def jd(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def skel(t: str) -> str:
    """Только русские буквы без ударений и регистра: одинаково у текстов с одними и теми же словами."""
    return re.sub(r"[^а-яё]", "", (t or "").replace(ACUTE, "").lower())


def bare(w: str) -> str:
    return w.replace(ACUTE, "").lower()


def norm_t(t) -> str:
    return re.sub(r"\s+", " ", (t or "").strip())


SND_SR = 24000
SND_EXT = (".wav", ".webm", ".mp3", ".ogg", ".oga", ".flac", ".m4a", ".aac", ".opus", ".wma", ".aif", ".aiff")
CUE_KINDS = ("sfx", "amb", "mus")
CUE_NAME = {"sfx": "звук", "amb": "фон", "mus": "музыка"}
# Правки автора в звуке, пришедшем из партитуры книги: что именно он менял (остальное при обновлении партитуры берётся из неё)
KIT_ASPECT = {"pos": ("uid", "at", "off"), "end": ("e_uid", "e_at", "e_off", "len"), "vol": ("vol",), "on": ("on",),
              "loop": ("loop",), "label": ("label",), "kind": ("kind",), "file": ("file",), "fin": ("fin",),
              "fout": ("fout",), "foff": ("foff",), "env": ("env",)}
# Версии готовых файлов со звуком: (ключ, какие дорожки входят, хвост имени папки, как называется). Голос без звуков —
# всегда (gotovo\chasti, gotovo\glavy); версии лежат рядом: gotovo\chasti_<хвост>, gotovo\glavy_<хвост>.
MIX_VARS = (("sfx", ("sfx",), "effekty", "с эффектами"),
            ("sfx_amb", ("sfx", "amb"), "effekty_okruzhenie", "с эффектами и окружением"),
            ("sfx_mus", ("sfx", "mus"), "effekty_muzyka", "с эффектами и музыкой"),
            ("all", ("sfx", "amb", "mus"), "so_zvukom", "всё вместе: эффекты, окружение, музыка"))
ENV_RAMP = 0.25                   # с: за сколько громкость участка внутри звука плавно меняется на его краях
KIT_CAT = {"ambient": "окружение", "creatures": "существа", "misc": "разное", "objects": "предметы", "player": "герой",
           "spells": "заклинания", "staffs_potions": "посохи и зелья", "ui_story": "интерфейс и сюжет", "weapons": "оружие"}
_LAY_EN = "qwertyuiop[]asdfghjkl;'zxcvbnm,.`"
_LAY_RU = "йцукенгшщзхъфывапролджэячсмитьбюё"
_EN2RU, _RU2EN = str.maketrans(_LAY_EN, _LAY_RU), str.maketrans(_LAY_RU, _LAY_EN)


def s_norm(s) -> str:
    return re.sub(r"[^0-9a-zа-я]+", " ", str(s).lower().replace("ё", "е")).strip()


def s_stem(w: str) -> str:
    """Основа слова для поиска: «двери», «дверью» -> «двер»; короткие слова и латиница — как есть."""
    if len(w) < 4 or not re.search(r"[а-я]", w):
        return w
    m = re.sub(r"(?:ами|ями|ов|ев|ом|ем|ой|ей|ах|ях|ью)$|[аеиоуыэюяйь]{1,2}$", "", w)
    return m if len(m) >= 3 else w[:3]


def s_score(toks, words, hay: str) -> float:
    """Насколько запись подходит запросу: каждое слово запроса должно найтись (целым словом, началом слова, основой
    или куском); 0 — не подходит."""
    tot = 0.0
    for tk in toks:
        if tk in words:
            b = 3.0
        elif any(w.startswith(tk) for w in words):
            b = 2.5
        elif len(tk) >= 4 and any(w.startswith(s_stem(tk)) for w in words):
            b = 2.0
        elif tk in hay:
            b = 1.0
        else:
            return 0.0
        tot += b
    return tot


MIX_DEF = {"sfx": 0.16, "amb": 0.12, "mus": 0.10}        # громкость дорожек относительно голоса: звуки 16 %, фон 12 %, музыка 10 %
MIX_WAS = {"sfx": 0.33}                                  # прежнее значение по умолчанию (до 12a) — при обновлении заменяется новым
LANE_RU = {"sfx": "звуки", "amb": "фон", "mus": "музыка"}
DEAD_WHY = ("нет речи",)                                 # замечание движка: в записи нет звука вовсе
WEAK_WHY = ("мало речи", "нет голоса")                   # звук есть, но это обрывок или шум без голоса
TINY_LETTERS = 2                                         # фраза в одну-две буквы («Д.», «А?»): речь и должна быть короче 0,2 с
_LET_RE = re.compile(r"[^А-Яа-яЁё]")


def why_dead(why, tech="", speech_s=None, dur=None) -> bool:
    """Запись без единого звука? Так движок помечает её замечанием «нет речи» (речи меньше 0,2 с). У фразы в одну-две
    буквы столько речи и не бывает — быстрая речь идёт по 15 букв в секунду. Поэтому у такой фразы запись, где речь
    всё-таки слышна (не меньше 0,07 с на букву) и сама запись длиннее двух пустых кадров движка (0,16 с), тишиной не
    считается: это короткий дубль, он остаётся в списке."""
    if not any(str(w).startswith(DEAD_WHY) for w in (why or [])):
        return False
    letters = len(_LET_RE.sub("", str(tech or "")))
    try:
        sp, du = float(speech_s or 0), float(dur or 0)
    except (TypeError, ValueError):
        sp = du = 0.0
    return not (1 <= letters <= TINY_LETTERS and sp >= 0.067 * letters and du >= 0.25)
SILENT_MSG = ("движок вернул тишину вместо речи (попыток: {k}) — на очень коротких фразах так бывает. Дубль не записан, "
              "в части звучит прежняя запись. Нажмите «Озвучить заново» ещё раз; если тишина повторяется — дайте фразе свою "
              "запись («Своя запись с компьютера») или соедините её с соседней фразой той же роли")


def active_rms(x, sr=SND_SR) -> float:
    """Громкость звучащей части (кадры 50 мс громче 10 % от пиковых): паузы её не занижают. Как в сведении конвейера."""
    w = int(sr * 0.05)
    k = len(x) // w
    if k < 1:
        return float(np.sqrt(np.mean(np.square(x)))) if len(x) else 0.0
    rms = np.sqrt(np.mean(np.square(x[:k * w].reshape(k, w)), axis=1))
    act = rms[rms > 0.1 * float(np.percentile(rms, 95))]
    return float(np.sqrt(np.mean(np.square(act)))) if len(act) else 0.0


def soft_limit(x, knee=0.89, inplace=False):
    """Мягкий ограничитель (как в сведении конвейера): всё, что громче knee, плавно прижимается и не доходит до 1.0."""
    m = np.abs(x) > knee
    if m.any():
        y = x if inplace else x.copy()
        y[m] = np.sign(x[m]) * (knee + (1 - knee) * np.tanh((np.abs(x[m]) - knee) / (1 - knee)))
        return y
    return x


def trim_edges(x, sr=SND_SR):
    """Тишина по краям звукового файла. mp3 добавляет её сам (задержка кодировщика и набивка, десятки миллисекунд):
    без обрезки звук вступал бы с опозданием, а зацикленный фон — с дырой на каждом круге."""
    if not len(x):
        return x
    pk = float(np.max(np.abs(x)))
    if pk < 1e-5:
        return x
    nz = np.flatnonzero(np.abs(x) > max(pk * 0.003, 1e-4))     # −50 дБ от пика
    a = max(0, int(nz[0]) - int(0.004 * sr))
    b = min(len(x), int(nz[-1]) + 1 + int(0.03 * sr))
    return x[a:b]


def loop_body(a, sr=SND_SR):
    """Звук -> (сколько отсчётов сыграть один раз в начале, тело петли). Хвост файла плавно (равной мощностью)
    перетекает в его начало, поэтому на стыке кругов нет ни щелчка, ни провала. Петля = файл минус перекрытие."""
    xf = int(min(0.5 * sr, len(a) // 4))
    if xf < int(0.02 * sr):
        return 0, a
    body = a[xf:].copy()
    th = np.linspace(0.0, 1.0, xf, endpoint=False, dtype="float32") * (np.pi / 2)
    tail, head = body[-xf:], a[:xf]
    # Мощность суммы двух сигналов = cos² + sin² + 2ρ·cos·sin = 1 + ρ·sin 2θ, где ρ — их похожесть. У шума и фона
    # ρ ≈ 0 (обычное «равномощное» перетекание), у ровного тона ρ ≈ 1 — без поправки на шве был бы выброс +3 дБ.
    den = float(np.sqrt(np.sum(tail * tail) * np.sum(head * head)))
    rho = float(np.sum(tail * head)) / den if den > 1e-9 else 0.0
    norm = np.sqrt(np.maximum(1.0 + min(max(rho, -0.9), 1.0) * np.sin(2 * th), 0.1))
    body[-xf:] = ((tail * np.cos(th) + head * np.sin(th)) / norm).astype("float32")
    return xf, body


PLUS_WORD = re.compile(r"[А-Яа-яЁё\u0301+]*\+[А-Яа-яЁё\u0301+]*")


def plus_to_acute(t: str) -> str:
    """«п+осох» -> «по́сох»: плюс перед гласной — ударение (так удобно печатать). Понимается и плюс ПОСЛЕ гласной
    («посо+х»). Прежнее ударение в слове при этом снимается: плюсом его переносят, а не добавляют второе."""
    def fix(m):
        w = m.group(0)
        b = w.replace(ACUTE, "")
        b2 = re.sub(r"\+([" + VOWELS + r"])", lambda k: k.group(1) + ACUTE, b)
        b2 = re.sub(r"([" + VOWELS + r"])\+", lambda k: k.group(1) + ACUTE, b2)
        return b2 if b2 != b else w
    return PLUS_WORD.sub(fix, t)


def write_atomic(path: Path, data):
    """Запись через временный файл: при сбое на диске остаётся прежняя версия. Антивирус может держать файл — 5 попыток."""
    tmp = path.with_name(path.name + ".tmp")
    b = data.encode("utf-8") if isinstance(data, str) else data
    created_tmp = False
    for k in range(5):
        try:
            with open(tmp, "wb") as f:
                created_tmp = True
                f.write(b)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(tmp, path)
            return
        except OSError:
            if k == 4:
                # Не оставляем собственный незавершённый файл после окончательного сбоя.
                # Старый целевой файл не трогаем; ошибку очистки не подменяем исходной.
                if created_tmp:
                    try:
                        tmp.unlink(missing_ok=True)
                    except OSError:
                        pass
                raise
            time.sleep(0.2 * (k + 1))


def read_json(path: Path, dflt):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return dflt


def mmss(v: float) -> str:
    return f"{int(v // 60):02d}:{v % 60:04.1f}"


def safe_file(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", name).strip(" .")[:80] or "glava"


def pid_alive(pid: int) -> bool:
    """Проверяет PID без сигналов. Ошибка доступа/проверки означает «считаем живым» (fail-closed)."""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                # Только ERROR_INVALID_PARAMETER (87) доказывает, что PID отсутствует.
                # ACCESS_DENIED и прочие ошибки не дают права объявлять процесс мёртвым.
                return ctypes.get_last_error() != 87
            try:
                code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return True
                return code.value == 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except Exception:
        return True


LOG = None
LOG_IS_STDOUT = False
EV = collections.deque(maxlen=4000)      # последние события — их показывает «Журнал» в окне студии
EV_LOCK = threading.Lock()
EV_N = [0]


def ev(msg: str, lvl: str = "info", src: str = "студия", more: str = ""):
    """Строка журнала для окна. lvl: info — обычное, ok — сделано, run — началось, warn — внимание, err — сбой,
    edit — правка автора, eng — вывод движка озвучки. more — подробности (раскрываются щелчком)."""
    with EV_LOCK:
        EV_N[0] += 1
        EV.append(dict(n=EV_N[0], t=time.strftime("%H:%M:%S"), lvl=lvl, src=src, msg=str(msg)[:700],
                       more=str(more)[:8000]))


def log(msg: str, lvl: str | None = None, src: str = "студия"):
    """Запись в studio\\studio.log и в журнал окна."""
    line = f"{now()}  {msg}"
    try:
        if LOG:
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError:
        pass
    if not LOG_IS_STDOUT:
        try:
            print(line, flush=True)
        except Exception:
            pass
    head, _, rest = str(msg).partition("\n")
    ev(head, lvl or ("err" if head.startswith(("сбой", "студия не запустилась")) else "info"), src, rest)


class Op:
    """Долгая операция. Пока идёт — видна в окне (что делается, какой шаг, сколько сделано); закончилась — строка
    в журнале со временем. with Op(studio, "готовая часть 5") as op: op.stage("запись mp3"); op.step()"""

    def __init__(self, st, what: str, total: int = 0, src: str = "студия", quiet: bool = False):
        self.st, self.what, self.total, self.src, self.quiet = st, what, int(total), src, quiet
        self.done, self.t0, self.note, self.stage_s, self.id = 0, time.time(), "", "", 0

    def __enter__(self):
        with EV_LOCK:
            self.st._opn += 1
            self.id = self.st._opn
            self.st.ops[self.id] = self
        return self

    def stage(self, text: str, total: int = 0):
        self.stage_s, self.done, self.total = text, 0, int(total)

    def step(self, k: int = 1):
        self.done += k

    def __exit__(self, et, e, tb):
        with EV_LOCK:
            self.st.ops.pop(self.id, None)
        if et is None and not self.quiet:
            log(f"{self.what} — готово за {time.time() - self.t0:.1f} с" + (f": {self.note}" if self.note else ""),
                "ok", self.src)
        return False


def _ram():
    """(всего, свободно) оперативной памяти в МБ; None — узнать не удалось."""
    try:
        if os.name == "nt":
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            m = MS()
            m.dwLength = ctypes.sizeof(MS)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
                return None
            return int(m.ullTotalPhys >> 20), int(m.ullAvailPhys >> 20)
        d = {}
        with open("/proc/meminfo", encoding="ascii") as f:
            for ln in f:
                k, _, v = ln.partition(":")
                d[k] = int(v.split()[0]) >> 10
        return d["MemTotal"], d.get("MemAvailable", d.get("MemFree", 0))
    except Exception:
        return None


def _cpu_ticks():
    """(простой, всего) — счётчики времени процессора. Загрузка = 1 − Δпростой / Δвсего между двумя замерами."""
    try:
        if os.name == "nt":
            import ctypes

            class FT(ctypes.Structure):
                _fields_ = [("lo", ctypes.c_ulong), ("hi", ctypes.c_ulong)]
            i, k, u = FT(), FT(), FT()
            if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(i), ctypes.byref(k), ctypes.byref(u)):
                return None
            v = [(x.hi << 32) | x.lo for x in (i, k, u)]
            return v[0], v[1] + v[2]                              # «время ядра» уже включает простой
        with open("/proc/stat", encoding="ascii") as f:
            v = [int(x) for x in f.readline().split()[1:8]]
        return v[3] + v[4], sum(v)
    except Exception:
        return None


def gb(mb) -> str:
    return f"{mb / 1024:.1f} ГБ" if mb is not None and mb >= 1000 else f"{int(mb or 0)} МБ"


# ---------------------------------------------------------------------------------------------- студия
class Studio:
    def __init__(self, root: Path):
        self.root = root
        self.dir = root / "studio"
        self.takes_dir = self.dir / "takes"
        self.cache_dir = self.dir / "cache"
        self.render_dir = self.dir / "render"
        self.backup_dir = self.dir / "backup"
        self.final_parts = root / "gotovo" / "chasti"
        self.final_ch = root / "gotovo" / "glavy"
        self.final_parts_mix = root / "gotovo" / "chasti_so_zvukom"
        self.final_ch_mix = root / "gotovo" / "glavy_so_zvukom"
        self.sounds_dir = self.dir / "sounds"
        self._snd, self._lvl, self._cat, self._kst, self._zv = {}, {}, None, None, None
        self.kit_job = None                                    # идёт распаковка нового звукового комплекта: {name, pct, error}
        self._kit_lock = threading.Lock()                      # распаковка — одна за раз
        self._fsha, self._loops = {}, {}
        self.slock = threading.Lock()            # расшифровка звуков и клипы — по одному (окно просит их пачкой)
        self.qdir = n.WORK / "studio_queue"
        self.stress, self.stress_run_at, self.stress_busy = None, {}, None     # сверка ударений: {uid: {h, f}}
        self.tries, self.go_proc, self.go_at, self.go_log = {}, None, 0.0, []
        self.tool_busy, self._bank, self._auto_eng = None, None, 0.0
        self.plock, self._pcache = threading.RLock(), {}       # проект сохраняется / загружается — по одному
        for d in (self.dir, self.takes_dir, self.cache_dir, self.render_dir, self.backup_dir, self.qdir):
            d.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()            # данные (правки, план)
        self.rlock = threading.Lock()            # сборка предпросмотра и экспорт — по одному
        self.token = secrets.token_hex(16)
        self.jobs = {}
        self.undo = []
        self.nsave = 0
        self.last_ping = time.time()
        self.bye_at = None
        self.engine_proc = None
        self.engine_note = ""
        self.strikes = 0
        self._gpu = (0.0, None)
        self._smi = None                         # путь к nvidia-smi ("" — программы нет, больше не ищем)
        self.ops, self._opn = {}, 0              # идущие долгие операции (для окна)
        self._cpu = (None, 0.0, None)            # прошлый замер процессора: (счётчики, когда, загрузка %)
        self._cstat = (0.0, None)                # размер кэша студии (считается не чаще раза в две минуты)
        self._seen = {}                          # часть -> когда её открывали в окне (её предпросмотр не убирать)
        self._clock = (0.0, {})
        self._clock_sig = None
        self._wseen = set()
        self.check = None                        # итог самопроверки
        self.engine2, self.e2_busy, self.e2_tried, self.e1_at = None, 0.0, 0.0, 0.0
        self.threads = max(1, min(8, (os.cpu_count() or 4) - 1))     # сборка части: фразы обрабатываются в несколько потоков
        try:
            self.eng_pos = (root / "studio_engine.log").stat().st_size
        except OSError:
            self.eng_pos = 0
        if hasattr(n, "find_ffmpeg"):            # конвейер ищет ffmpeg заново при каждой растяжке фразы — запомнить
            ff0 = n.find_ffmpeg()
            if ff0:
                n.find_ffmpeg = lambda: ff0
        self._side = {}
        self._takes = {}
        self._audit = (None, {})
        self._pcheck = (None, {})
        self.rows_sig = None
        self._abs = {}                           # присоединённая фраза -> её головная (места в ряду)
        self._fsig = {}                          # отпечатки образцов голосов: путь -> (ключ файла, отпечаток)
        self._upgraded = None                    # прежняя версия студии, если правки записаны ею
        # память обработанного текста: текст фразы книги -> он же с ударениями и правками словаря, каким его прочёл конвейер.
        # Черновая запись (и её текст рядом) привязана к образцу голоса роли: сменили голос — запись «исчезла», а текст с
        # ударениями нужен по-прежнему (показать автору, отдать движку на переозвучку).
        self.tcache = read_json(self.dir / "tech_cache.json", None) or {}
        self._tc_dirty, self._tc_at, self._acc = False, time.time(), (0.0, "acute")
        self.load_asm()
        self.load_rows(force=True)
        self.load_store()
        self.auto_th, self._auto_t0, self._auto_rows0, self.ch_busy, self._auto_redone = None, None, 0, None, {}
        self.auto_load()

    # ------------------------------------------------------------------ настройки сборки — как у конвейера
    def load_asm(self):
        a = read_json(n.WORK / "assembly.json", {}) or {}
        st = getattr(n, "SETTINGS", None) or {}
        try:
            tg, teq, ps, trim = str(a["t"]).split("|")
            n.TEMPO_G, n.TEMPO_EQ, n.PAUSE_SCALE, n.TRIM_PAUSES = float(tg), bool(int(teq)), float(ps), bool(int(trim))
            self.asm_src = "как у последней сборки частей"
        except Exception:
            n.TEMPO_G = float(st.get("tempo", 1.12) or 1.12)
            n.PAUSE_SCALE = float(st.get("pauses", 0.85) or 0.85)
            n.TRIM_PAUSES = st.get("trim", True) is not False
            n.TEMPO_EQ = st.get("equalize", True) is not False
            self.asm_src = "из таблицы настроек"
        self.fmt = str(a.get("fmt") or st.get("format") or "mp3")
        self.bitrate = str(a.get("br") or st.get("bitrate") or "96k")
        self.title = str(a.get("title") or st.get("title") or self.root.name)
        self.author = str(a.get("author") or st.get("author") or "")
        self.speed = float(a.get("speed", 1.0) or 1.0)
        self.part_min = float(a.get("parts") or st.get("part_min") or 19)

    def asm_sig(self) -> str:
        return jd([n.TEMPO_G, n.TEMPO_EQ, n.PAUSE_SCALE, n.TRIM_PAUSES, n.SQZ_KEEP, n.SQZ_CAP, n.NORM_ON, n.NORM_DB,
                   n.EQ_MAX, n.SENT_GAP_ON, getattr(n, "NOX_STAGE", 0)])

    # ------------------------------------------------------------------ книга: реплики, роли, привязка
    def load_rows(self, force=False, strict=False) -> bool:
        try:
            a, b = n.CSV_PATH.stat(), n.ROLES_PATH.stat()
            sig = (a.st_size, a.st_mtime_ns, b.st_size, b.st_mtime_ns)
        except OSError:
            sig = None
        if not force and sig == self.rows_sig and time.time() - getattr(self, "rows_at", 0) < 60:
            return False
        try:
            rows, roles = n.load_rows(), n.load_roles()
            need = ("id", "chapter", "ch_title", "para", "kind", "speaker", "pause_before_ms", "text")
            if not rows or any(k not in rows[0] for k in need) or any(r.get("text") is None for r in rows):
                raise ValueError("в таблице реплик не хватает столбцов или строка оборвана")
        except Exception as e:                     # конвейер прямо сейчас переписывает файлы — живём на прежних
            if strict or getattr(self, "rows", None) is None:
                raise Oops(f"не читается разбивка книги (work\\segments.csv): {type(e).__name__}: {e}") from e
            return False
        keys, uids, seen = [], [], {}
        for r in rows:
            k = sha(r["text"])
            o = seen.get(k, 0)
            seen[k] = o + 1
            keys.append(k)
            uids.append(f"{k}.{o}")
        pidx = list(range(len(rows)))
        ins = (getattr(self, "store", None) or {}).get("ins") or []
        if ins:                                        # фразы, вставленные автором в студии, — на свои места
            rows, keys, uids, pidx = self.splice_ins(rows, keys, uids, ins, roles)
        with self.lock:
            row_attrs = ("rows", "roles", "keys", "uids", "pidx", "sidx", "idx_of", "id2i", "_spans",
                         "_clock", "_clock_sig", "stems", "base_roles", "role_cnt", "wavs", "chapters", "rows_sig", "rows_at", "_abs")
            missing = object()
            previous_rows = {key: getattr(self, key, missing) for key in row_attrs}
            previous_store = self.store
            previous_store_data = json.loads(jd(previous_store)) if strict and previous_store is not None else None
            try:
                self.rows, self.roles, self.keys, self.uids = rows, roles, keys, uids
                self.pidx = pidx                           # номер реплики конвейера (None — вставка студии)
                self.sidx = [i for i, q in enumerate(pidx) if q is not None]      # реплика конвейера -> место в ряду студии
                self.idx_of = {u: i for i, u in enumerate(uids)}
                self.id2i = {r["id"]: i for i, r in enumerate(rows)}
                self._spans = {}
                self._clock = (0.0, {})
                self._clock_sig = None
                self.stems = n.voice_stems(roles)
                self.base_roles = [self._role_of(r, roles) for r in rows]
                self.role_cnt = collections.Counter(self.base_roles)
                self.wavs = [n.wav_for(r, roles) if not r.get("ins") else self.takes_dir / "_ins" / (r["ins"] + ".wav") for r in rows]
                ch = {}
                for i, r in enumerate(rows):
                    c = int(r["chapter"])
                    if c not in ch:
                        ch[c] = [i, i, r["ch_title"]]
                    ch[c][1] = i
                self.chapters = ch
                self.rows_sig, self.rows_at = sig, time.time()
                if getattr(self, "store", None) is not None:
                    self.reanchor(persist=not strict)
            except Exception:
                if strict:
                    if previous_store_data is not None:
                        self.store = previous_store_data
                    for key, value in previous_rows.items():
                        if value is missing:
                            self.__dict__.pop(key, None)
                        else:
                            setattr(self, key, value)
                raise
        return True

    @staticmethod
    def _role_of(r: dict, roles: dict) -> str:
        try:
            return n.resolve_role(r["speaker"], roles)
        except Exception:
            return "narrator" if r.get("ins") else r["speaker"]

    def splice_ins(self, rows, keys, uids, ins, roles):
        """Вставленные автором фразы встают в ряд книги сразу после своей «опорной» реплики (after — её метка; '' —
        самое начало книги), по порядку ord. Опорная реплика исчезла из книги (рукопись переразбита) — вставка
        остаётся у реплики с тем же номером и помечается moved: место стоит проверить. Для конвейера вставок нет."""
        pos = {u: i for i, u in enumerate(uids)}
        byid = {r["id"]: i for i, r in enumerate(rows)}
        after = {}
        for x in ins:
            a = -1 if x.get("after") == "" else pos.get(x.get("after"))
            if a is None:                              # опорной реплики больше нет
                a = byid.get(str(x.get("after_id")))
                if a is None:
                    m = re.match(r"\d+", str(x.get("after_id") or ""))
                    num = int(m.group(0)) if m else None
                    cand = [i for i, r in enumerate(rows) if num is not None and str(r["id"]).isdigit() and int(r["id"]) <= num]
                    a = cand[-1] if cand else len(rows) - 1
                x["after"], x["after_id"], x["moved"] = uids[a], rows[a]["id"], True
            after.setdefault(a, []).append(x)
        out_r, out_k, out_u, pidx = [], [], [], []

        def put(a):
            base = rows[a] if a >= 0 else rows[0]
            for k, x in enumerate(sorted(after.get(a, []), key=lambda z: (float(z.get("ord") or 0), z["id"])), 1):
                role = x.get("role") or "narrator"
                out_r.append({"id": f"{base['id'] if a >= 0 else 0}+{k}", "chapter": base["chapter"], "ch_title": base["ch_title"],
                              "para": base["para"], "kind": "narr" if role == "narrator" else "speech", "speaker": role,
                              "conf": "high", "pause_before_ms": str(int(x.get("pause") or 420)), "text": x["text"],
                              "ins": x["id"]})
                out_k.append(x["id"])
                out_u.append(x["id"])
                pidx.append(None)
        put(-1)
        for i, r in enumerate(rows):
            out_r.append(r)
            out_k.append(keys[i])
            out_u.append(uids[i])
            pidx.append(i)
            put(i)
        return out_r, out_k, out_u, pidx

    def ins_rec(self, iid: str):
        return next((x for x in self.store.get("ins") or [] if x["id"] == iid), None)

    def ins_tail(self, b: int) -> int:
        """Вставки после реплики b принадлежат той же части, что и она."""
        while b + 1 < len(self.rows) and (self.rows[b + 1].get("ins") or (b + 1) in self._abs):
            b += 1
        return b

    def pipe_at(self, i: int, lo: int = 0) -> int:
        """Ближайшая реплика конвейера не правее i (вставки пропускаются); нет такой — i."""
        j = i
        while j >= lo and self.pidx[j] is None:
            j -= 1
        return j if j >= lo else i

    def rows_changed(self):
        """Состав ряда изменился (вставка добавлена, удалена, переписана) — перечитать и пересчитать части."""
        self.load_rows(force=True)
        with self.lock:
            self._parts_cache = None

    def insert_add(self, after_uid: str, text: str, role: str = "") -> str:
        """Новая фраза после указанной: её нет в рукописи, она живёт в студии. Озвучивается движком (ударения он ставит
        сам, как в книге) или своей записью с компьютера."""
        text = re.sub(r"\s+", " ", str(text or "")).strip()
        if not re.search("[А-Яа-яЁё]", text):
            raise Oops("в тексте вставки нет русских букв — озвучивать нечего")
        if len(text) > 600:
            raise Oops("вставка длиннее 600 знаков — разбейте её на две-три фразы: длинные куски движок читает хуже")
        with self.lock:
            i = self.idx_of.get(after_uid)
            if i is None:
                raise Oops("фразы, после которой вставляем, больше нет — обновите окно")
            k = self.part_of(i)
            if k and self.store["parts"][k - 1].get("locked"):
                raise Oops("часть закреплена — сначала снимите закрепление")
            role = role or self.eff(self._abs.get(i, i))["role"]
            if role not in self.roles["roles"]:
                raise Oops("такой роли нет")
            i = self.chain(self._abs.get(i, i))[-1]            # после соединённой фразы — после всей, а не внутрь неё
            j = i
            while j >= 0 and self.pidx[j] is None:
                j -= 1
            a_uid, a_id = (self.uids[j], self.rows[j]["id"]) if j >= 0 else ("", "0")
            sibs = sorted((x for x in self.store["ins"] if x.get("after") == a_uid), key=lambda z: (float(z.get("ord") or 0), z["id"]))
            if self.pidx[i] is None:                       # вставляем после другой вставки
                cur = self.ins_rec(self.rows[i]["ins"])
                k0 = sibs.index(cur)
                lo = float(cur.get("ord") or 0)
                hi = float(sibs[k0 + 1].get("ord") or 0) if k0 + 1 < len(sibs) else lo + 2.0
            else:
                lo, hi = 0.0, (float(sibs[0].get("ord") or 0) if sibs else 2.0)
            iid = self.next_id("ins")
            self.store["ins"].append(dict(id=iid, after=a_uid, after_id=a_id, ord=(lo + hi) / 2, text=text, role=role,
                                          pause=420, created=now()))
            self.undo.append(("#ins", jd({"op": "add", "id": iid}), "вставка фразы"))
            self.save("вставлена фраза", {"uid": iid, "after_id": a_id, "role": role, "text": text[:80]})
            self.rows_changed()
        return iid

    def insert_edit(self, uid: str, text: str):
        text = re.sub(r"\s+", " ", str(text or "")).strip()
        if not re.search("[А-Яа-яЁё]", text):
            raise Oops("в тексте вставки нет русских букв — озвучивать нечего")
        if len(text) > 600:
            raise Oops("вставка длиннее 600 знаков — разбейте её на две-три фразы")
        with self.lock:
            x = self.ins_rec(uid)
            if x is None:
                raise Oops("это не вставка (текст фраз книги меняется в рукописи) или её уже нет")
            e = self.store["segs"].get(uid) or {}
            if e.get("locked"):
                raise Oops("фраза закреплена — сначала снимите закрепление")
            if x["text"] != text:
                self.undo.append(("#ins", jd({"op": "edit", "id": uid, "text": x["text"], "tech": x.get("tech"), "etech": e.get("tech")}),
                                  "текст вставки"))
                x["text"] = text
                x.pop("tech", None)                        # ударения расставит движок при озвучке
                if e.get("tech") is not None:
                    e.pop("tech", None)
                self.save("текст вставки изменён", {"uid": uid, "text": text[:80]})
                self.rows_changed()

    def insert_delete(self, uid: str):
        """Убрать вставленную фразу. Её дубли и записи остаются на диске; действие отменяется (Ctrl+Z)."""
        with self.lock:
            x = self.ins_rec(uid)
            if x is None:
                raise Oops("это не вставка: фразы книги не удаляются (их можно только переозвучить)")
            e = self.store["segs"].get(uid)
            if (e or {}).get("locked"):
                raise Oops("фраза закреплена — сначала снимите закрепление")
            i = self.idx_of.get(uid)
            k = self.part_of(i) if i is not None else None
            if k and self.store["parts"][k - 1].get("locked"):
                raise Oops("часть закреплена — сначала снимите закрепление")
            self.store["ins"].remove(x)
            self.store["segs"].pop(uid, None)
            self.undo.append(("#ins", jd({"op": "del", "ins": x, "e": e}), "удаление вставки"))
            self.save("вставка удалена", {"uid": uid, "text": x["text"][:80]})
            self.rows_changed()

    def role_names(self):
        out = []
        for name in self.roles["roles"]:
            if self.roles["roles"][name].get("same_as"):
                continue
            wav_p, _ = n.voice_files(name, self.stems)
            cfg = self.roles["roles"][name]
            out.append({"name": name, "voice": wav_p.exists(), "count": self.role_cnt.get(name, 0),
                        "gender": cfg.get("gender"), "age": cfg.get("age")})
        return out

    # ------------------------------------------------------------------ хранилище правок
    def load_store(self):
        f = self.dir / "edits.json"
        st = read_json(f, None)
        if st is None and f.exists():              # файл есть, но не читается — не затираем, берём свежую копию
            bad = self.dir / f"edits_bad_{time.strftime('%Y%m%d_%H%M%S')}.json"
            shutil.copy2(f, bad)
            for b in sorted(self.backup_dir.glob("edits_*.json"), reverse=True):
                st = read_json(b, None)
                if st is not None:
                    log(f"edits.json не читается (сохранён как {bad.name}); взята копия {b.name}")
                    break
        self.store = self.store_defaults(st or {"v": 1, "segs": {}, "rules": [], "parts": [], "chapters": {},
                                                "orphans": [], "seq": 0})
        was = str(self.store.get("studio") or "")
        if f.exists() and was > VERSION:
            # правки записаны более НОВОЙ студией: прежняя версия не знает её полей и испортила бы их при первом сохранении
            raise Oops(f"правки этой книги записаны более новой версией студии ({was}), а запущена {VERSION}. "
                       "Скачайте свежий комплект и запустите студию из него — правки целы, ничего не изменено")
        if f.exists() and was != VERSION:
            self._upgraded = was or "до 2026-11-12a"
            d = self.backup_dir / f"pered_{VERSION}_{time.strftime('%Y%m%d_%H%M%S')}"
            try:                                           # всё состояние правок — как оставила прежняя версия
                d.mkdir(parents=True, exist_ok=True)
                for name in ("edits.json", "stress.json", "sid_map.json", "fixed_parts.json", "home.id"):
                    if (self.dir / name).exists():
                        shutil.copy2(self.dir / name, d / name)
                if (self.dir / "sounds" / "index.json").exists():
                    shutil.copy2(self.dir / "sounds" / "index.json", d / "sounds_index.json")
                log(f"студия обновлена: {self._upgraded} -> {VERSION}. Правки прежней версии целиком сохранены в {d}")
            except OSError as e:
                log(f"копия правок перед обновлением не сделана: {e}", "warn")
        self.store["studio"] = VERSION
        if f.exists():
            self.backup()
        if self.store["ins"]:
            self.load_rows(force=True)                     # вставки встают в ряд книги; правки привязываются там же
        else:
            self.reanchor()

    @staticmethod
    def store_defaults(st: dict) -> dict:
        for k, d in (("segs", {}), ("rules", []), ("parts", []), ("chapters", {}), ("orphans", []), ("seq", 0),
                     ("cues", []), ("cue_seen", []), ("cue_orphans", []), ("sound_map", {}), ("kit_off", []),
                     ("mix", dict(MIX_DEF, on={"sfx": True, "amb": True, "mus": True}))):
            st.setdefault(k, d)
        for k, d in MIX_DEF.items():
            st["mix"].setdefault(k, d)
        st.setdefault("ins", [])
        st["mix"].setdefault("on", {})
        st["mix"].setdefault("vars", {})                   # какие версии готовых файлов собирать (кроме «только голос»)
        for v in MIX_VARS:
            st["mix"]["vars"].setdefault(v[0], True)
        for k in CUE_KINDS:
            st["mix"]["on"].setdefault(k, True)
        return st

    def backup(self):
        f = self.dir / "edits.json"
        if not f.exists():
            return
        try:
            shutil.copy2(f, self.backup_dir / f"edits_{time.strftime('%Y%m%d_%H%M%S')}.json")
            old = sorted(self.backup_dir.glob("edits_*.json"))
            for b in old[:-60]:
                b.unlink(missing_ok=True)
        except OSError as e:
            log(f"копия правок не сделана: {e}")

    def journal(self, rec: dict):
        try:
            with open(self.dir / "edits.log", "a", encoding="utf-8") as f:
                f.write(jd(dict(rec, t=now())) + "\n")
        except OSError:
            pass

    EDIT_NAMES = {"tech": "текст", "role": "роль", "tempo": "скорость", "pause": "пауза перед", "mark": "пометка",
                  "heard": "прослушано", "take": "дубль", "after": "пауза после", "vkeep": "запись прежним голосом оставлена"}
    COUNT_NAMES = (("count", "фраз"), ("new", "новых"), ("replaced", "заменено"), ("back_to_draft", "вернулось к черновику"),
                   ("moved", "перенесено"), ("lost", "без места"))

    def _why_line(self, why: str, rec: dict) -> str:
        """Строка журнала о правке — человеческими словами."""
        bits = []
        if rec.get("id") is not None:
            bits.append(f"фраза № {rec['id']}")
        st = rec.get("set")
        if isinstance(st, (dict, list)) and st:
            bits.append(", ".join(self.EDIT_NAMES.get(k, str(k)) for k in st))
        if rec.get("label"):
            bits.append(f"«{rec['label']}»")
        for k, name in self.COUNT_NAMES:
            v = rec.get(k)
            v = len(v) if isinstance(v, (list, dict)) else v
            if isinstance(v, int) and not isinstance(v, bool) and v:
                bits.append(f"{name}: {v}")
        if rec.get("file"):
            bits.append(Path(str(rec["file"])).name)
        return why + (" — " + "; ".join(bits) if bits else "")

    def save(self, why: str, rec: dict | None = None):
        write_atomic(self.dir / "edits.json", json.dumps(self.store, ensure_ascii=False, indent=1))
        self.journal(dict(rec or {}, why=why))
        # в журнал окна — только правки автора: у заявок и дублей свои строки, служебные записи не показываются
        if why not in ("заявка на переозвучку", "получен дубль") and not why.startswith(("собрана готовая часть", "собрана глава")) \
                and not why.endswith(("взята в работу", "опорный темп ролей", "отметка «закреплена»")):
            ev(self._why_line(why, rec or {}), "edit", "правка")
        self.nsave += 1
        if self.nsave % 25 == 0:
            self.backup()

    def next_id(self, pfx: str) -> str:
        self.store["seq"] = int(self.store.get("seq", 0)) + 1
        return f"{pfx}{self.store['seq']:06d}"

    def ctx_of(self, i: int) -> dict:
        return {"prev": self.keys[i - 1] if i else "", "next": self.keys[i + 1] if i + 1 < len(self.keys) else "",
                "id": self.rows[i]["id"], "para": self.rows[i]["para"]}

    def E(self, uid: str, create=False):
        e = self.store["segs"].get(uid)
        if create and self._abs and self.idx_of.get(uid) in self._abs:
            raise Oops("эта фраза присоединена к предыдущей — правится соединённая фраза целиком; чтобы править её отдельно, разъедините фразы")
        if e is None and create:
            sid = self.next_id("s")
            while (self.takes_dir / sid).exists():             # номер занят записями из другой ветки работы — следующий
                sid = self.next_id("s")
            e = {"sid": sid, "ctx": self.ctx_of(self.idx_of[uid])}
            self.store["segs"][uid] = e
        return e

    def new_tid(self, e: dict) -> str:
        """Номер следующего дубля фразы — только свободный: под ним на диске нет ни записи, ни её описания. Так новая
        запись не может лечь поверх имеющейся, что бы ни случилось со счётчиком (ручной возврат правок из копии и т. п.)."""
        while True:
            e["tseq"] = int(e.get("tseq", 0)) + 1
            tid = f"t{e['tseq']:03d}"
            f = self.take_path(e["sid"], tid)
            if not f.exists() and not f.with_suffix(".json").exists():
                return tid

    @staticmethod
    def e_empty(e: dict) -> bool:
        return not any(e.get(k) not in (None, "", False, []) for k in ("role", "tech", "tempo", "pause", "take", "locked",
                                                                        "mark", "ack", "after", "join", "into"))

    def reanchor(self, persist=True):
        """Правки ищут свои фразы заново (после переразбивки книги): тот же текст и хотя бы один прежний сосед.
        Фраза исчезла из книги — правка уходит в «без места» (не удаляется)."""
        by_key = {}
        for i, k in enumerate(self.keys):
            by_key.setdefault(k, []).append(i)
        new, moved, lost, remap = {}, 0, 0, {}
        self._abs = {}
        for uid, e in list(self.store["segs"].items()):
            ctx = e.get("ctx") or {}

            def score(i):
                p = self.keys[i - 1] if i else ""
                x = self.keys[i + 1] if i + 1 < len(self.keys) else ""
                return int(p == ctx.get("prev")) + int(x == ctx.get("next"))
            cand = by_key.get(uid.split(".")[0], [])
            i0 = self.idx_of.get(uid)
            best = max(cand, key=score) if cand else None
            if best is not None and i0 is not None and score(i0) >= score(best):
                best = i0
            if best is None or self.uids[best] in new:
                if self.e_empty(e) and not e.get("tseq"):
                    continue
                self.store["orphans"].append({"uid": uid, "e": e, "at": now()})
                lost += 1
                continue
            nu = self.uids[best]
            if nu != uid:
                moved += 1
            e["ctx"] = self.ctx_of(best)
            new[nu] = e
            remap[uid] = nu
        for e in new.values():
            if e.get("join"):
                e["join"] = [remap.get(u, u) for u in e["join"]]
            if e.get("into"):
                e["into"] = remap.get(e["into"], e["into"])
        self.store["segs"] = new
        self._parts_cache = None
        dropped = self.joins_refresh()
        self.reanchor_cues()
        if dropped:
            log(f"соединённые фразы: снято соединений — {dropped} (книга переразбита или между фразами встала вставка); "
                "фразы вернулись к виду до соединения", "warn")
            if persist:
                self.save("соединения фраз сняты: цепочка разошлась", {"count": dropped})
        if moved or lost:
            log(f"привязка правок: переехало {moved}, без места {lost}")
            if persist:
                self.save("привязка правок после переразбивки", {"moved": moved, "lost": lost})

    # ------------------------------------------------------------------ соединённые фразы
    # Автор соединяет фразу со следующей: дальше это одна фраза — в списке, в звуке, при переозвучке. В книге конвейера обе
    # остаются как были (ряд фраз не меняется): у головной в правках join = [метки присоединённых по порядку] и jwas — что
    # у неё было до каждого соединения (текст, дубль, пауза после), у присоединённой — into = метка головной. Свои правки и
    # записи присоединённой не трогаются: «разъединить» возвращает обе фразы точно такими, какими они были.
    def chain(self, i: int) -> list:
        """Места фразы и присоединённых к ней (обычная фраза — [i])."""
        if not self._abs or self._abs.get(i + 1) != i:
            return [i]
        j = i + 1
        while self._abs.get(j) == i:
            j += 1
        return list(range(i, j))

    def row_text(self, i: int) -> str:
        ch = self.chain(i)
        return self.rows[i]["text"] if len(ch) == 1 else " ".join(self.rows[j]["text"].strip() for j in ch)

    def base_tech_all(self, i: int) -> str:
        ch = self.chain(i)
        return self.base_tech(i) if len(ch) == 1 else " ".join(self.base_tech(j).strip() for j in ch)

    def auto_tech(self, i: int, ridx=None):
        """Технический текст фразы без ручной правки (запись конвейера + правила чтения) -> (текст, [сработавшие правила]);
        у соединённой фразы — всех её частей подряд."""
        ch = self.chain(i)
        if len(ch) == 1:
            t0 = self.base_tech(i)
            return self.apply_rules(t0, i, ridx=ridx)
        parts, hits = [], []
        for j in ch:
            t0 = self.base_tech(j)
            tj, hj = self.apply_rules(t0, j, ridx=ridx)
            parts.append(tj.strip())
            hits += [h for h in hj if h not in hits]
        return " ".join(p for p in parts if p), hits

    @staticmethod
    def fits(e, t) -> bool:
        """Дубль записан для нынешнего состава фразы: у обычной — обычный, у соединённой — с теми же присоединёнными.
        Остальные дубли лежат на месте и возвращаются в список, когда фраза снова станет такой же."""
        return list((t or {}).get("cover") or []) == list((e or {}).get("join") or [])

    def joins_refresh(self) -> int:
        """Собрать указатель присоединённых фраз. Цепочка разошлась (книгу переразбили, между фразами встала вставка) —
        соединение снимается целиком, головная возвращается к виду до соединения. -> сколько соединений снято."""
        segs, absd, bad = self.store["segs"], {}, []
        for uid, e in segs.items():
            js = e.get("join")
            if not js:
                continue
            i = self.idx_of.get(uid)
            ok = i is not None and i + len(js) < len(self.uids) and self.pidx[i] is not None and all(
                self.uids[i + 1 + k] == u and (segs.get(u) or {}).get("into") == uid and self.pidx[i + 1 + k] is not None
                for k, u in enumerate(js))
            if ok:
                for k in range(len(js)):
                    absd[i + 1 + k] = i
            else:
                bad.append(uid)
        for uid in bad:
            e = segs[uid]
            was = (e.get("jwas") or [{}])[0]
            e.pop("join", None)
            e.pop("jwas", None)
            self._join_restore(e, was)
        for uid, e in segs.items():
            if e.get("into") and absd.get(self.idx_of.get(uid, -1)) is None:
                e.pop("into", None)
                if (e.get("locked") or {}).get("head"):
                    e.pop("locked", None)
        self._abs = absd
        return len(bad)

    def _join_restore(self, e: dict, was: dict):
        for k in ("tech", "take", "after"):
            if was.get(k) in (None, ""):
                e.pop(k, None)
            else:
                e[k] = was[k]
        if e.get("take") and e["take"] not in self.takes_of(e):       # прежнего дубля уже нет — звучит черновая запись
            e.pop("take", None)

    def _glue(self, i: int, e: dict, sca: dict, scb: dict, role: str, tech: str, vsig: str = "") -> str:
        """Склейка записей двух фраз в новый дубль головной: [первая][короткая пауза][вторая]. Тишина на стыке убирается,
        громкость частей выравнивается; исходные записи обеих фраз остаются как были."""
        import soundfile as sf

        def raw(sc):
            x, sr = self.read_audio(sc["path"])
            tr = sc.get("trim")
            if tr:
                i0 = max(0, int(float(tr[0]) * sr))
                x = x[i0:max(i0 + int(0.05 * sr), int(float(tr[1]) * sr))]
            x = np.asarray(x, dtype="float32")
            if n.NORM_ON and len(x) > sr // 10:
                x = np.asarray(n._level(x, sr, n.NORM_DB), dtype="float32")
            return x, sr

        def edge(x, sr, tail):
            """Сколько отсчётов оставить: до конца речи (+40 мс) у первой записи, от начала речи (−30 мс) у второй."""
            w = max(1, int(sr * 0.01))
            k = len(x) // w
            if k < 6:
                return len(x) if tail else 0
            env = np.sqrt(np.mean(np.square(x[:k * w].reshape(k, w)), axis=1))
            hot = np.nonzero(env > max(0.06 * float(np.percentile(env, 95)), 0.003))[0]
            if not len(hot):
                return len(x) if tail else 0
            return min(len(x), (hot[-1] + 1) * w + int(sr * 0.04)) if tail else max(0, hot[0] * w - int(sr * 0.03))

        xa, sra = raw(sca)
        xb, srb = raw(scb)
        if sra != srb:
            raise Oops("записи этих фраз в разном формате — их звук не склеить; соедините фразы и озвучьте заново")
        xa, xb = xa[:edge(xa, sra, True)], xb[edge(xb, srb, False):]
        x = np.concatenate([xa, np.zeros(int(sra * GLUE_GAP), dtype="float32"), xb])
        tid = self.new_tid(e)
        dst = self.take_path(e["sid"], tid)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.stem + ".tmp.wav")
        meta = dst.with_suffix(".json")
        try:
            sf.write(str(tmp), x, sra, subtype="PCM_16")
            os.replace(tmp, dst)
            write_atomic(meta, jd(dict(
                tid=tid, src="glue", created=now(), role=role, tech=tech, raw=self.row_text(i), basis="tech",
                dur=round(len(x) / sra, 3), why=[], cut=round(len(xa) / sra, 3), cover=list(e.get("join") or []),
                **({"vsig": vsig} if vsig else {}))))
        except Exception:
            # _glue owns this fresh ID; do not leave a WAV without its metadata if
            # the second half of the artifact transaction fails.
            for artifact in (tmp, dst, meta):
                try:
                    artifact.unlink(missing_ok=True)
                except OSError:
                    pass
            raise
        self._takes.pop(e["sid"], None)
        return tid

    def join_next(self, uid: str, force: bool = False) -> dict:
        """Соединить фразу со следующей: дальше это одна фраза. Записи обеих склеиваются в новый дубль «склейка» — часть
        звучит без дыр сразу; «Озвучить заново» прочтёт соединённый текст целиком, на одном дыхании."""
        with self.lock:
            i = self.idx_of.get(uid)
            if i is None:
                raise Oops("этой фразы больше нет в книге — обновите окно")
            if i in self._abs:
                raise Oops("эта фраза уже присоединена к предыдущей")
            k = self.part_of(i)
            if k is None:
                raise Oops("часть ещё не взята в работу")
            if self.store["parts"][k - 1].get("locked"):
                raise Oops("часть закреплена — сначала снимите закрепление")
            j = self.chain(i)[-1] + 1
            if j >= len(self.rows) or j > self.plan()[k - 1]["b"]:
                raise Oops("это последняя фраза части — соединять её не с чем")
            ra, rb = self.rows[i], self.rows[j]
            if ra.get("ins") or rb.get("ins"):
                raise Oops("вставленные фразы не соединяются: у вставки можно просто изменить текст (F2)")
            if ra["kind"] == "head" or rb["kind"] == "head":
                raise Oops("заголовок главы с текстом не соединяется")
            ub = self.uids[j]
            if (self.store["segs"].get(ub) or {}).get("join"):
                raise Oops(f"следующая фраза (№ {rb['id']}) сама соединена с соседней — сначала разъедините её")
            if any(q["uid"] in (uid, ub) and q["status"] in ("queued", "running") for q in self.jobs.values()):
                raise Oops("одна из этих фраз сейчас озвучивается — дождитесь дубля")
            # E(create=True), joins_refresh и _glue могут менять несколько связанных структур.
            # Снимок делается до первого такого изменения, а rollback восстанавливает их напрямую.
            segs_before = json.loads(jd(self.store["segs"]))
            abs_before = dict(self._abs)
            undo_before = list(self.undo)
            takes_before = dict(self._takes)
            tid = None
            glue_sid = None
            try:
                ea, eb = self.E(uid, create=True), self.E(ub, create=True)
                glue_sid = ea["sid"]
                if ea.get("locked") or eb.get("locked"):
                    raise Oops("фраза закреплена — сначала снимите закрепление")
                efa, efb, sca, scb = self.eff(i), self.eff(j), self.src(i), self.src(j)
                if sca.get("imp") or scb.get("imp"):
                    raise Oops("у одной из фраз звучит ваша собственная запись — такие фразы не соединяются: "
                               "поставьте ей дубль движка или дайте одну свою запись на обе фразы (U, затем «дальше по этой записи»)")
                if efa["role"] != efb["role"] and not force:
                    who = lambda r: "Рассказчик" if r == "narrator" else r
                    raise Oops(f"у фраз разные голоса ({who(efa['role'])} и {who(efb['role'])}) — соединённую фразу целиком прочтёт {who(efa['role'])}")
                was_a, was_b = json.loads(jd(ea)), json.loads(jd(eb))
                tech_new = norm_t(efa["tech"].strip() + " " + efb["tech"].strip())
                spoken = None
                if sca["path"] is not None and scb["path"] is not None:
                    spoken = norm_t((sca["tech"] or efa["tech"]).strip() + " " + (scb["tech"] or efb["tech"]).strip())
                ea.setdefault("jwas", []).append({q: ea.get(q) for q in ("tech", "take", "after")})
                ea.setdefault("join", []).append(ub)
                eb["into"] = uid
                if eb.get("after"):
                    ea["after"] = eb["after"]
                else:
                    ea.pop("after", None)
                self.joins_refresh()
                if norm_t(self.auto_tech(i)[0]) == tech_new:
                    ea.pop("tech", None)
                else:
                    ea["tech"] = tech_new
                if spoken is not None:
                    vs = ""
                    if efa["role"] == efb["role"] and not self.voice_stale(efa, sca, was_a) and not self.voice_stale(efb, scb, was_b):
                        vs = self.vsig_now(efa["role"])
                    tid = self._glue(i, ea, sca, scb, efa["role"], spoken, "" if vs == "nov" else vs)
                    ea["take"] = tid
                self.undo.append(("#join", jd({"a": uid, "ea": was_a, "b": ub, "eb": was_b}), "соединение фраз"))
                del self.undo[:-80]
                self.save("фразы соединены", {"uid": uid, "id": ra["id"], "with": rb["id"], "take": tid})
                return dict(uid=uid, take=tid, count=len(ea["join"]) + 1, long=len(tech_new) > 420)
            except Exception:
                self.store["segs"] = segs_before
                self._abs = abs_before
                self.undo = undo_before
                self._takes = takes_before
                # Удаляем исключительно новый артефакт, созданный этой операцией.
                if tid:
                    try:
                        glued = self.take_path(glue_sid, tid)
                        glued.unlink(missing_ok=True)
                        glued.with_suffix(".json").unlink(missing_ok=True)
                    except OSError:
                        pass
                raise

    def join_split(self, uid: str) -> dict:
        """Разъединить: последняя присоединённая фраза снова сама по себе, со своей записью и правками; головная
        возвращается к тексту и дублю, которые были до этого соединения. Дубли соединённой фразы не удаляются."""
        with self.lock:
            i = self.idx_of.get(uid)
            if i is None:
                raise Oops("этой фразы больше нет в книге — обновите окно")
            if i in self._abs:
                i = self._abs[i]
                uid = self.uids[i]
            ea = self.store["segs"].get(uid) or {}
            js = list(ea.get("join") or [])
            if not js:
                raise Oops("эта фраза ни с чем не соединена")
            if ea.get("locked"):
                raise Oops("фраза закреплена — сначала снимите закрепление")
            if any(q["uid"] == uid and q["status"] in ("queued", "running") for q in self.jobs.values()):
                raise Oops("фраза сейчас озвучивается — дождитесь дубля")
            ub = js[-1]
            # joins_refresh, tidy и save могут затронуть не только пару UID.
            segs_before = json.loads(jd(self.store["segs"]))
            abs_before = dict(self._abs)
            undo_before = list(self.undo)
            try:
                ea = self.store["segs"].get(uid) or {}
                eb = self.store["segs"].get(ub) or {}
                was = (ea.get("jwas") or [{}])[-1]
                ea["join"], ea["jwas"] = js[:-1], list(ea.get("jwas") or [])[:-1]
                if not ea["join"]:
                    ea.pop("join")
                    ea.pop("jwas", None)
                eb.pop("into", None)
                tail_after = ea.get("after")
                self._join_restore(ea, was)
                if tail_after:
                    eb["after"] = tail_after
                else:
                    eb.pop("after", None)
                self.joins_refresh()
                self.undo.append(("#join", jd({"a": uid, "ea": segs_before[uid],
                                                "b": ub, "eb": segs_before.get(ub, {})}), "разъединение фраз"))
                del self.undo[:-80]
                self._tidy(uid)
                self._tidy(ub)
                self.save("фразы разъединены", {"uid": uid, "id": self.rows[i]["id"],
                                                "freed": self.rows[self.idx_of[ub]]["id"] if ub in self.idx_of else "?"})
                return dict(uid=uid, freed=ub, count=len(ea.get("join") or []) + 1)
            except Exception:
                self.store["segs"] = segs_before
                self._abs = abs_before
                self.undo = undo_before
                raise

    # ------------------------------------------------------------------ технический текст и правила
    def sidecar(self, p: Path):
        """Что именно было прочитано в черновой записи (<запись>.txt) или None."""
        try:
            st = p.stat()
        except OSError:
            return None
        c = self._side.get(p.name)
        if c and c[0] == st.st_mtime_ns:
            return c[1]
        try:
            t = p.with_suffix(".txt").read_text(encoding="utf-8")
        except OSError:
            t = None
        self._side[p.name] = (st.st_mtime_ns, t)
        return t

    def audit_text(self, rid: str):
        f = n.WORK / "audit_text.txt"
        try:
            m = f.stat().st_mtime_ns
        except OSError:
            return None
        if self._audit[0] != m:
            d = {}
            for line in f.read_text(encoding="utf-8").splitlines():
                p = line.split("\t", 2)
                if len(p) == 3:
                    d[p[0]] = p[2]
            self._audit = (m, d)
        return self._audit[1].get(rid)

    def tkey(self, i: int) -> str:
        return self.keys[i]                                # отпечаток текста фразы (тот же, что в метках фраз)

    def accent_mode(self) -> str:
        """Режим ударений движка (как его запускает конвейер: work\fast_args.json); none — движок читает без знаков ударения."""
        if time.time() - self._acc[0] > 30:
            fa = read_json(n.WORK / "fast_args.json", None) or {}
            self._acc = (time.time(), str(fa.get("accent") or "acute"))
        return self._acc[1]

    def tcache_put(self, i: int, tech: str):
        k = self.tkey(i)
        if self.tcache.get(k) != tech:
            self.tcache[k] = tech
            self._tc_dirty = True

    def tcache_save(self, force: bool = False):
        if self._tc_dirty and (force or time.time() - self._tc_at > 8):
            self._tc_dirty, self._tc_at = False, time.time()
            try:
                write_atomic(self.dir / "tech_cache.json", json.dumps(self.tcache, ensure_ascii=False))
            except OSError as e:
                log(f"память обработанного текста не записана: {e}", "warn")

    def base_tech(self, i: int) -> str:
        if self.rows[i].get("ins"):                        # вставка: ударения — те, что расставил движок при озвучке
            return (self.ins_rec(self.rows[i]["ins"]) or {}).get("tech") or self.rows[i]["text"]
        t = self.sidecar(self.wavs[i])
        if t is not None:
            self.tcache_put(i, t)                          # запомнить: при смене голоса роли эта запись «исчезнет», текст — нет
            return t
        t = self.audit_text(self.rows[i]["id"])
        if t is None:
            t = self.tcache.get(self.tkey(i))
        return t if t is not None else self.rows[i]["text"]

    def base_is_raw(self, i: int) -> bool:
        """У фразы книги нет обработанного текста (ни записи конвейера, ни аудита, ни памяти): есть только текст рукописи."""
        if self.rows[i].get("ins"):
            return False
        return self.sidecar(self.wavs[i]) is None and self.audit_text(self.rows[i]["id"]) is None \
            and self.tcache.get(self.tkey(i)) is None

    def rule_index(self):
        d = {}
        for r in self.store["rules"]:
            d.setdefault(r["word"], []).append(r)
        return d

    def apply_rules(self, t: str, i: int, part=None, ridx=None):
        """Правила студии поверх готового текста -> (текст, [id сработавших правил])."""
        ridx = ridx if ridx is not None else self.rule_index()
        if not ridx:
            return t, []
        hits, out, pos = [], [], 0
        for m in WORD_RE.finditer(t):
            rs = ridx.get(bare(m.group(0)))
            if not rs:
                continue
            use = None
            for r in rs:                                    # позднее правило сильнее
                fi = self.idx_of.get(r.get("from_uid"), r.get("from_i", 0))
                if i < fi:
                    continue
                if r["scope"] == "part":
                    if part is None:
                        part = self.part_of(i)
                    if part != r.get("part"):
                        continue
                use = r
            if use is None:
                continue
            w, to = m.group(0), use["to"]
            if w[:1].isupper():
                to = to[:1].upper() + to[1:]
            if to != w:
                out.append(t[pos:m.start()])
                out.append(to)
                pos = m.end()
                hits.append(use["id"])
        out.append(t[pos:])
        return "".join(out), hits

    def eff(self, i: int, ridx=None) -> dict:
        """Действующие значения фразы: роль, технический текст, скорость, пауза (с учётом правок, правил, закрепления)."""
        e = self.store["segs"].get(self.uids[i]) or {}
        lk = e.get("locked")
        base_pause = int(self.rows[i]["pause_before_ms"])
        if lk and not lk.get("head"):                      # у присоединённой фразы отметка закрепления без своих значений
            return dict(role=lk["role"], tech=lk["tech"], tempo=float(lk.get("tempo") or 1.0),
                        pause=int(lk["pause"] if lk.get("pause") is not None else base_pause), locked=True,
                        tech_src="locked", hits=[], after=float(lk.get("after") or 0.0))
        role = e.get("role") if e.get("role") in self.roles["roles"] else self.base_roles[i]
        if e.get("tech") is not None:
            tech, src, hits = e["tech"], "manual", []
        else:
            tech, hits = self.auto_tech(i, ridx)
            src = "rule" if hits else "base"
        return dict(role=role, tech=tech, tempo=float(e.get("tempo") or 1.0),
                    pause=int(e["pause"] if e.get("pause") is not None else base_pause), locked=False, tech_src=src,
                    hits=hits, after=float(e.get("after") or 0.0))

    # ------------------------------------------------------------------ дубли
    def take_path(self, sid: str, tid: str) -> Path:
        return self.takes_dir / sid / f"{sid}_{tid}.wav"

    def takes_of(self, e: dict | None) -> dict:
        if not e or not e.get("sid"):
            return {}
        d = self.takes_dir / e["sid"]
        try:
            m = d.stat().st_mtime_ns
        except OSError:
            return {}
        c = self._takes.get(e["sid"])
        if c and c[0] == m:
            return c[1]
        out = {}
        for f in sorted(d.glob("*.json")):
            t = read_json(f, None)
            if t and t.get("tid") and self.take_path(e["sid"], t["tid"]).exists():
                out[t["tid"]] = t
        self._takes[e["sid"]] = (m, out)
        return out

    def src(self, i: int) -> dict:
        """Откуда берётся звук фразы: выбранный дубль студии или черновая запись конвейера."""
        e = self.store["segs"].get(self.uids[i]) or {}
        tid = (e.get("locked") or {}).get("take") or e.get("take")
        if tid:
            t = self.takes_of(e).get(tid)
            if t:
                return dict(path=self.take_path(e["sid"], tid), kind="take", role=t.get("role"), tech=t.get("tech"),
                            gaps=t.get("gaps"), tid=tid, basis=t.get("basis") or "raw", stem0=t.get("stem0"), cover=t.get("cover"),
                            vsig=(t.get("metrics") or {}).get("vsig") or t.get("vsig"),
                            trim=(e.get("trims") or {}).get(tid) or None, imp=t.get("src") == "import")
            return dict(path=None, kind="lost", role=None, tech=None, gaps=None, tid=tid)
        p = self.wavs[i]
        if p.exists():
            return dict(path=p, kind="draft", role=self.base_roles[i], tech=self.sidecar(p), gaps=n._load_gaps(p),
                        tid=None)
        return dict(path=None, kind="none", role=None, tech=None, gaps=None, tid=None)

    def vsig_now(self, role: str) -> str:
        """Отпечаток нынешнего образца голоса роли (как у конвейера); '' — узнать не вышло."""
        try:
            return str(n.voice_sig(role, self.roles) or "")
        except Exception:
            return ""

    def voice_stale(self, ef: dict, sc: dict, e: dict) -> bool:
        """Дубль записан ПРЕЖНИМ образцом голоса роли: голос с тех пор сменили, а автор не оставил эту запись нарочно.
        Свои записи автора и дубли без отметки образца (старые, склейки из разных записей) так не «стареют»."""
        vs = sc.get("vsig")
        if not vs or sc.get("kind") != "take" or sc.get("imp"):
            return False
        cur = self.vsig_now(ef["role"])
        if not cur or cur == "nov" or cur == vs:
            return False
        return list(e.get("vkeep") or []) != [sc.get("tid"), cur]

    def state(self, i: int, ef=None, sc=None) -> str:
        if i in self._abs:
            return "joined"
        ef = ef or self.eff(i)
        sc = sc or self.src(i)
        if sc["path"] is None:
            return "lost" if sc["kind"] == "lost" else "empty"
        if ef["locked"]:
            return "locked"
        if list(sc.get("cover") or []) != list((self.store["segs"].get(self.uids[i]) or {}).get("join") or []):
            return "stale"                                 # запись сделана для другого состава фразы (до соединения или после)
        if sc.get("imp"):                                  # своя запись автора: словарь и смена роли её не «старят» —
            return "edited"                                # её не заменит и «озвучить всё изменённое», пока автор сам не решит
        if sc["role"] != ef["role"] or (sc["tech"] is not None and norm_t(sc["tech"]) != norm_t(ef["tech"])):
            return "stale"
        e = self.store["segs"].get(self.uids[i]) or {}
        if self.voice_stale(ef, sc, e):
            return "stale"                                 # у роли сменили голос, а звучит запись прежним
        return "edited" if any(e.get(k) not in (None, "", []) for k in ("role", "tech", "tempo", "pause", "take", "after", "join")) else "draft"

    def flags(self, i: int) -> list:
        """Замечания конвейера к черновой записи (подсказки, куда послушать; закрепить не мешают)."""
        p, out = self.wavs[i], []
        try:
            ok = p.with_suffix(".ok").read_text(encoding="utf-8").split("\t")
            if len(ok) >= 3:
                why = ok[2]
                if "лучшая из" in why:
                    out.append("проверка: принята " + why + " — послушайте")
                elif not (why.startswith("ок") or why.startswith("коротко — ок") or why.startswith("коротко — проверено")):
                    out.append("проверка: " + why)
        except OSError:
            pass
        m = read_json(p.with_suffix(".meta"), None)
        if m:
            if m.get("why"):
                out.append("озвучка: " + ", ".join(map(str, m["why"])))
            if m.get("alien"):
                out.append("возможно, чужой голос")
        pc = self.part_check().get(self.rows[i]["id"])
        if pc:
            out.append("сборка: " + pc)
        return out

    def part_check(self) -> dict:
        f = n.WORK / "part_check.txt"
        try:
            m = f.stat().st_mtime_ns
        except OSError:
            return {}
        if self._pcheck[0] != m:
            d = {}
            for line in f.read_text(encoding="utf-8").splitlines():
                p = line.split("\t")
                if len(p) >= 6 and p[2].startswith("id "):
                    d[p[2][3:]] = p[5]
            self._pcheck = (m, d)
        return self._pcheck[1]

    # ------------------------------------------------------------------ части: план и взятие в работу
    def _part_span(self, num: int):
        """Первая и последняя фраза собранной части конвейера — по её карте времени (parts/part_NN.times.tsv)."""
        f = n.PARTS / f"part_{int(num):02d}.times.tsv"
        try:
            st = f.stat()
        except OSError:
            return None
        c = self._spans.get(num)
        if c and c[0] == (st.st_size, st.st_mtime_ns):
            return c[1]
        span = None
        try:
            ids = [ln.split("\t")[2] for ln in f.read_text(encoding="utf-8").splitlines() if ln.count("\t") >= 2]
            ia, ib = self.id2i.get(ids[0]), self.id2i.get(ids[-1])
            if ia is not None and ib is not None and ib >= ia:
                span = (ia, ib)
        except (OSError, IndexError, UnicodeDecodeError):
            pass
        self._spans[num] = ((st.st_size, st.st_mtime_ns), span)
        return span

    def pipeline_parts(self, start: int) -> list:
        """Части конвейера (work/parts.json), продолжающие наши с реплики start: [(a, b)]. Границы — из карт времени
        частей: так часть находится точно, даже когда её последняя фраза («— Да.») встречается в книге много раз
        (у одинаковых фраз одной роли запись общая, по записи их не различить). Карты нет — по записям начала и конца."""
        out, idx = [], read_json(n.PARTS_INDEX, []) or []

        def valid_entry(entry):
            # Current build_parts() writes stable row-ID and WAV-boundary fields, not the
            # historical "sig" marker. Accept both formats; reject incomplete records.
            return (isinstance(entry, dict) and bool(entry.get("start")) and bool(entry.get("end"))
                    and (bool(entry.get("sig")) or
                         (entry.get("start_id") is not None and entry.get("end_id") is not None)))

        for q, p in enumerate(idx):
            if not valid_entry(p):
                break
            a = start if not out else out[-1][1] + 1
            if a >= len(self.rows):
                break
            span = self._part_span(p.get("n", q + 1))
            if span is not None:
                ia, ib = span
                # Карта времени могла остаться от другой редакции/переразбивки книги.
                # UID обычно защищает от этого, но проверяем и реальные hash-WAV границы
                # из parts.json: не переносим старый диапазон на чужие строки с теми же позициями.
                if (not (0 <= ia <= ib < len(self.wavs))
                        or self.wavs[ia].stem != p.get("start")
                        or self.wavs[ib].stem != p.get("end")):
                    span = None
            if span is not None:
                ia, ib = span
                if ib < a:
                    continue                               # часть целиком до нашей границы
                if ia > a:
                    break                                  # дыра: фразы между нашей границей и этой частью не собраны
                out.append((a, self.ins_tail(ib)))         # ia <= a: часть (или её остаток после перенесённой границы)
                continue
            if self.wavs[a].stem != p.get("start"):
                if out:
                    break
                continue                                   # часть до нашей границы — пропускаем
            nxt = (idx[q + 1].get("start") if q + 1 < len(idx) and valid_entry(idx[q + 1]) else None)
            cand = [j for j in range(a, min(a + 4000, len(self.rows))) if self.wavs[j].stem == p.get("end")]
            b = next((j for j in cand if nxt is not None and j + 1 < len(self.rows) and self.wavs[j + 1].stem == nxt),
                     cand[0] if cand else None)
            if b is None:
                break
            out.append((a, self.ins_tail(b)))
        return out

    def own_plan(self, start: int) -> list:
        """Нет частей конвейера — режем сами тем же правилом: ~N минут, граница на абзаце."""
        cache, out, acc, a = n.load_durations(), [], 0.0, start
        for i in range(start, len(self.rows)):
            p = self.wavs[i]
            if not p.exists():
                break
            r = self.rows[i]
            acc += int(r["pause_before_ms"]) / 1000 + n.duration_of(p, cache)
            nxt = self.rows[i + 1] if i + 1 < len(self.rows) else None
            ok = nxt is None or nxt["kind"] == "head" or int(nxt["pause_before_ms"]) >= n.PAUSE["para"]
            # Считаем целевую длительность по той же конечной скорости, что и экспорт (_encode).
            # Общий TEMPO_G меняет темп отдельных голосов, но не является скоростью файла целиком.
            if (acc >= n.part_source_seconds(self.part_min, self.speed) and ok) or nxt is None:
                out.append((a, i))
                a, acc = i + 1, 0.0
        return out

    def plan(self) -> list:
        with self.lock:
            if self._parts_cache and time.time() - self._parts_cache[0] < 3:
                return self._parts_cache[1]
            out, start = [], 0
            for k, p in enumerate(self.store["parts"]):
                b = self.idx_of.get(p["last"])
                if b is None or b < start:
                    lp = p.get("last_p")
                    b = self.sidx[lp] if isinstance(lp, int) and 0 <= lp < len(self.sidx) else int(p.get("last_i", start))
                    b = min(max(b, start), len(self.rows) - 1)
                b = self.ins_tail(b)
                out.append({"n": k + 1, "a": start, "b": b, "opened": True})
                start = b + 1
            rest = self.pipeline_parts(start) if start < len(self.rows) else []
            if not rest and start < len(self.rows) and not (read_json(n.PARTS_INDEX, []) or []):
                rest = self.own_plan(start)
            a = start
            for _, b in rest:
                b = self.ins_tail(b)
                if b < a:
                    continue
                out.append({"n": len(out) + 1, "a": a, "b": b, "opened": False})
                a = b + 1
            self._parts_cache = (time.time(), out)
            return out

    def part_of(self, i: int):
        pl = [p for p in self.plan() if p["opened"]]
        k = bisect.bisect_left([p["b"] for p in pl], i)
        return pl[k]["n"] if k < len(pl) and pl[k]["a"] <= i else None

    def part_any(self, i: int):
        """Номер части по плану (и для ещё не открытых частей)."""
        return next((p["n"] for p in self.plan() if p["a"] <= i <= p["b"]), None)

    def part_eq(self, a: int, b: int) -> dict:
        """Опорный темп ролей в части (медиана), как у сборки конвейера; замораживается при взятии части в работу —
        потом новый дубль не меняет скорость соседних фраз."""
        by = {}
        for i in range(a, b + 1):
            if i in self._abs:
                continue
            r = self.rows[i]
            letters = len(n.RE_RU_LETTERS.sub("", self.row_text(i)))
            if letters < 40 or r["kind"] == "head":
                continue
            sc = self.src(i)
            if sc["path"] is None or sc.get("imp"):        # своя запись автора в расчёт темпа роли не идёт
                continue
            try:
                x, sr = self.read_audio(sc["path"])
            except Exception:
                continue
            by.setdefault(self.eff(i)["role"], []).append(letters / max(n._active_sec(x, sr), 0.05))
        return {role: float(np.median(v)) for role, v in by.items() if len(v) >= 5}

    def open_part(self, k: int):
        with self.lock:
            pl = self.plan()
            if k < 1 or k > len(pl):
                raise Oops(f"части {k} ещё нет: она появится, когда конвейер её озвучит и соберёт")
            for j in range(len(self.store["parts"]) + 1, k + 1):
                p = pl[j - 1]
                lb = self.pipe_at(p["b"], p["a"])          # граница части — по реплике конвейера (вставки идут за ней)
                self.store["parts"].append({"n": j, "last": self.uids[lb], "last_i": lb, "last_p": self.pidx[lb],
                                            "eq": None, "opened": now(), "locked": None, "export": None})
                self._parts_cache = None
                self.save(f"часть {j} взята в работу", {"part": j, "a": p["a"], "b": p["b"]})
            return self.plan()[k - 1]

    # ------------------------------------------------------------------ звук: обработка фразы, как у сборки конвейера
    @staticmethod
    def read_audio(path: Path):
        import soundfile as sf
        x, sr = sf.read(str(path), dtype="float32")
        if x.ndim > 1:
            x = x.mean(axis=1)
        return x, sr

    def file_sha(self, p: Path) -> str:
        """Отпечаток содержимого записи. По нему (а не по имени файла) ведётся кэш обработки: копия черновой записи,
        сделанная при закреплении, — тот же звук, и пересчитывать её незачем."""
        st = p.stat()
        key = (str(p), st.st_size, st.st_mtime_ns)
        h = self._fsha.get(key)
        if h is None:
            h = hashlib.sha1(p.read_bytes()).hexdigest()[:12]
            if len(self._fsha) > 80000:
                self._fsha.clear()
            self._fsha[key] = h
        return h

    def proc_key(self, i: int, eq: dict, ef: dict, sc: dict) -> str:
        r = self.rows[i]
        rc = self.roles["roles"].get(ef["role"], {})
        key = [self.file_sha(sc["path"]), r["text"], r["kind"], ef["role"], rc.get("tempo", 1.0),
               rc.get("gain", 1.0), round(float(ef["tempo"]), 3), eq.get(ef["role"]), sc.get("gaps"),
               "tech" if sc.get("basis") == "tech" else "raw", self.asm_sig()]
        if sc.get("trim") or sc.get("imp"):
            key.append(["обрезка", sc.get("trim"), bool(sc.get("imp"))])
        return sha(jd(key), 20)

    def proc_key_v1(self, i: int, eq: dict, ef: dict, sc: dict) -> str:
        """Ключ обработки прежних версий студии (до 11c). Нужен один раз — перевести отметки «готовый файл свеж»."""
        r = self.rows[i]
        rc = self.roles["roles"].get(ef["role"], {})
        st = sc["path"].stat()
        return sha(jd([sc["path"].name, st.st_size, st.st_mtime_ns, r["text"], r["kind"], ef["role"], rc.get("tempo", 1.0),
                       rc.get("gain", 1.0), round(float(ef["tempo"]), 3), eq.get(ef["role"]), sc.get("gaps"),
                       sc.get("basis"), self.asm_sig()]), 20)

    def _migrate_export(self, ex: dict, a: int, b: int, ver: str, k: int):
        """С версии 11c кэш обработки ведётся по содержимому записи, и «версия» части считается иначе. Готовый файл,
        собранный прежней студией, из-за этого выглядел бы устаревшим, хотя звук тот же. Поэтому при первом открытии
        такой части сверяем по прежнему счёту: был свеж тогда — свеж и теперь. Делается один раз на часть."""
        try:
            if self.timeline(a, b, False, keyfn=self.proc_key_v1)[0]["ver"] == ex.get("ver"):
                ex["ver"] = ver
                log(f"готовая часть {k} собрана прежней версией студии: звук тот же, пересобирать не нужно", "ok")
        except Exception:
            log("отметка готового файла не переведена (сам файл на месте; студия предложит собрать его заново):\n"
                + traceback.format_exc(), "warn")
        ex["kv"] = 2
        write_atomic(self.dir / "edits.json", json.dumps(self.store, ensure_ascii=False, indent=1))

    def proc(self, i: int, eq: dict, ef: dict, sc: dict, key: str, row_snapshot=None, roles_snapshot=None):
        """Запись фразы -> звук для части. Шаги и порядок — как в assemble() конвейера (сверено на этапе STAGE_OK)."""
        cp = self.cache_dir / f"{key}.npy"
        if cp.exists():
            try:
                x = np.load(cp)
                try:                                           # отметка «пригодилось»: уборка кэша убирает давно не нужное
                    if time.time() - cp.stat().st_mtime > 86400:
                        os.utime(cp, None)
                except OSError:
                    pass
                return x
            except Exception:
                cp.unlink(missing_ok=True)
        # A render can outlive a book reload. Use the row/voice configuration captured by timeline(),
        # never resolve the old row index against the newly loaded edition.
        r = row_snapshot if row_snapshot is not None else self.rows[i]
        role_data = roles_snapshot if roles_snapshot is not None else self.roles
        rc = role_data["roles"].get(ef["role"], {})
        x, sr = self.read_audio(sc["path"])
        tr = sc.get("trim")
        if tr:                                             # обрезка дубля: исходная запись цела, берётся её отрезок
            i0 = max(0, int(float(tr[0]) * sr))
            x = x[i0:max(i0 + int(0.05 * sr), int(float(tr[1]) * sr))]
        if sc.get("imp"):
            # Своя запись автора — живая речь: её не «чинят» как синтез (паузы, щелчки, общее ускорение книги не
            # применяются). Только темп самой фразы (если автор его менял), громкость — к уровню книги, края — плавно.
            f = float(ef["tempo"] or 1.0)
            if abs(f - 1.0) >= 0.005:
                x = n._stretch(x, sr, f, sc["path"].stem + "_" + hashlib.blake2b(np.ascontiguousarray(x).tobytes(), digest_size=6).hexdigest() + "i")
            if n.NORM_ON:
                x = n._level(x, sr, n.NORM_DB)
            gain = float(rc.get("gain", 1.0) or 1.0)
            if abs(gain - 1.0) > 0.01:
                x = np.clip(x * gain, -1.0, 1.0)
            x = np.asarray(n._edges(x, sr), dtype="float32")
            tmp = self.cache_dir / f"{key}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp.npy"
            try:
                np.save(tmp, x)
                os.replace(tmp, cp)
            finally:
                tmp.unlink(missing_ok=True)
            return x
        # Счёт букв и поиск пауз — по тому же тексту, что у конвейера (исходная фраза): копия черновой записи и дубль
        # с поправленным ударением обрабатываются точно как черновик. Только если в дубле другие СЛОВА — по прочитанному.
        text = sc["tech"].replace(ACUTE, "") if sc["kind"] == "take" and sc.get("basis") == "tech" and sc.get("tech") \
            else r["text"]
        letters = len(n.RE_RU_LETTERS.sub("", text))
        eqf = 1.0
        if n.TEMPO_EQ and n.TEMPO_G > 0 and letters >= 40 and r["kind"] != "head" and eq.get(ef["role"]):
            eqf = min(max(eq[ef["role"]] / (letters / max(n._active_sec(x, sr), 0.05)), 1.0), n.EQ_MAX)
        rtempo = float(rc.get("tempo", 1.0) or 1.0)
        pdiv = n.TEMPO_G ** 0.5 if n.TEMPO_G > 1 else 1.0
        x, _dk = n.declick(x, sr, text)
        if n.SENT_GAP_ON and r["kind"] != "head":
            tg = n.PAUSE["sentence"] / 1000 * n.PAUSE_SCALE / pdiv * 0.75 * max(n.TEMPO_G * eqf * rtempo, 1.0)
            x = n._gap_fix(x, sr, sc["gaps"], tg) if sc.get("gaps") is not None and not tr else n._sent_gaps(x, sr, text, tg)
        h = hashlib.blake2b(np.ascontiguousarray(x).tobytes(), digest_size=6).hexdigest()
        sfx = ""
        if n.TRIM_PAUSES:
            x = n._squeeze_pauses(x, sr, keep=n.SQZ_KEEP, cap=n.SQZ_CAP)
            sfx = "t" if (n.SQZ_KEEP, n.SQZ_CAP) == (0.35, 0.8) else f"t{int(n.SQZ_KEEP * 100)}_{int(n.SQZ_CAP * 100)}"
        # имя в кэше растяжки — как у конвейера (по имени черновой записи): у копии черновика берётся имя оригинала
        x = n._stretch(x, sr, n.TEMPO_G * eqf * rtempo * float(ef["tempo"] or 1.0),
                       (sc.get("stem0") or sc["path"].stem) + "_" + h + sfx)
        if n.NORM_ON:
            x = n._level(x, sr, n.NORM_DB)
        gain = float(rc.get("gain", 1.0) or 1.0)
        if abs(gain - 1.0) > 0.01:
            x = np.clip(x * gain, -1.0, 1.0)
        x = np.asarray(n._edges(x, sr), dtype="float32")
        tmp = self.cache_dir / f"{key}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp.npy"
        try:
            np.save(tmp, x)
            os.replace(tmp, cp)
        finally:
            tmp.unlink(missing_ok=True)
        return x

    def eq_for(self, i: int) -> dict:
        """Опорный темп ролей части. Считается один раз — при первой сборке части в студии — и замораживается."""
        k = self.part_of(i)
        if not k:
            return {}
        rec = self.store["parts"][k - 1]
        if rec.get("eq") is None:
            p = self.plan()[k - 1]
            rec["eq"] = self.part_eq(p["a"], p["b"])
            self.save(f"часть {k}: опорный темп ролей", {"part": k, "roles": len(rec["eq"])})
        return rec["eq"]

    def _proc_many(self, items: list, op=None, row_snapshots=None, roles_snapshot=None) -> dict:
        """Обработка фраз отрезка -> {ключ: звук}. Готовое берётся из кэша, остальное считается в несколько потоков:
        растяжка идёт через ffmpeg (отдельный процесс на фразу), а ядер у компьютера много. Фразы с общей записью
        (одинаковые реплики одной роли лежат одним файлом) идут друг за другом в одном потоке — у них общий файл
        растяжки, вдвоём они бы его испортили."""
        todo, groups = {}, {}
        for i, ef, sc, eq, key, pause in items:
            if key != "none" and key not in todo:
                row_snapshot = (row_snapshots or {}).get(i)
                todo[key] = (i, eq, ef, sc, key, row_snapshot, roles_snapshot)
                groups.setdefault(sc.get("stem0") or sc["path"].stem, []).append(key)
        hit = sum(1 for key in todo if (self.cache_dir / f"{key}.npy").exists())
        if op is not None:
            op.stage("обработка фраз", len(todo))
        out = {}

        def run(keys):
            for key in keys:
                out[key] = self.proc(*todo[key])
                if op is not None:
                    op.step()

        w = min(self.threads, len(groups)) if len(todo) - hit > 3 else 1
        if w > 1:
            with concurrent.futures.ThreadPoolExecutor(max_workers=w) as ex:
                list(ex.map(run, list(groups.values())))
        else:
            for keys in groups.values():
                run(keys)
        if op is not None:
            op.note = f"фраз {len(todo)}, заново обработано {len(todo) - hit}" + (f" (потоков: {w})" if w > 1 else "")
        return out

    def timeline(self, a: int, b: int, want_audio: bool, op=None, keyfn=None):
        """Склейка фраз a..b -> (описание, звук|None). Версия = отпечаток всех записей и пауз."""
        sr = 24000
        pdiv = n.TEMPO_G ** 0.5 if n.TEMPO_G > 1 else 1.0
        with self.lock:
            ridx = self.rule_index()
            items, aft = [], 0
            for i in range(a, b + 1):
                if i in self._abs:                             # присоединённая фраза звучит внутри головной
                    continue
                ef = copy.deepcopy(self.eff(i, ridx))
                sc = copy.deepcopy(self.src(i))
                eq = copy.deepcopy(self.eq_for(i))
                key = (keyfn or self.proc_key)(i, eq, ef, sc) if sc["path"] is not None else "none"
                pause = int((ef["pause"] if items else 0) * n.PAUSE_SCALE / pdiv) + aft
                items.append((i, ef, sc, eq, key, pause))
                # «пауза после фразы» — ровно столько секунд в готовом файле (общий множитель пауз книги её не меняет)
                aft = int(round(1000 * float(ef.get("after") or 0) * (self.speed or 1.0)))
            chains = {it[0]: self.chain(it[0]) for it in items} if self._abs else {}
            indices = {i for it in items for i in ([it[0]] + (chains.get(it[0]) or []))}
            # The processed-audio key ignores book position so identical speech can share a WAV cache.
            # Render metadata contains UID anchors and must be tied to this exact row snapshot.
            row_snapshots = {i: dict(self.rows[i]) for i in indices}
            uid_snapshots = {i: self.uids[i] for i in indices}
            roles_snapshot = copy.deepcopy(self.roles)
            identity = []
            for it in items:
                i = it[0]
                chain = chains.get(i) or [i]
                row = row_snapshots[i]
                identity.append([uid_snapshots[i], [uid_snapshots[j] for j in chain], row.get("chapter"),
                                 row.get("ch_title") if row.get("kind") == "head" else None])
            ver = sha(jd(identity) + "|" + "|".join(f"{it[4]}:{it[5]}" for it in items)
                      + (f"|+{aft}" if aft else "") + self.asm_sig(), 16)
        if not want_audio:
            return {"ver": ver, "items": items}, None
        done = self._proc_many(items, op, row_snapshots=row_snapshots, roles_snapshot=roles_snapshot)
        pieces, segs, chapters, t = [], [], [], 0.0
        for i, ef, sc, eq, key, pause in items:
            r = row_snapshots[i]
            pieces.append(np.zeros(int(sr * pause / 1000), dtype="float32"))
            t += pause / 1000
            if r["kind"] == "head":
                if chapters and chapters[-1][1] == r["ch_title"] and t - chapters[-1][0] < 15:
                    chapters.pop()
                chapters.append((t, r["ch_title"]))
            x = done[key] if key != "none" else np.zeros(int(sr * 0.8), dtype="float32")
            segs.append([uid_snapshots[i], round(t, 3), round(t + len(x) / sr, 3)])
            ch = chains.get(i) or [i]
            if len(ch) > 1:                                    # места присоединённых фраз внутри общей записи — по доле букв
                ws = [max(1, len(n.RE_RU_LETTERS.sub("", row_snapshots[j]["text"]))) for j in ch]
                tot, acc, ln = float(sum(ws)), ws[0], len(x) / sr
                for j, w in zip(ch[1:], ws[1:]):
                    segs.append([self.uids[j], round(t + ln * acc / tot, 3), round(t + ln * (acc + w) / tot, 3)])
                    acc += w
            pieces.append(x)
            t += len(x) / sr
        if aft:
            pieces.append(np.zeros(int(sr * aft / 1000), dtype="float32"))
        pieces.append(np.zeros(int(sr * n.PAUSE["chapter_end"] / 1000 * n.PAUSE_SCALE / pdiv), dtype="float32"))
        audio = np.concatenate(pieces)
        return {"ver": ver, "sr": sr, "total": round(len(audio) / sr, 3), "segs": segs,
                "chapters": [[round(c[0], 3), c[1]] for c in chapters]}, audio

    def ensure_render(self, k: int) -> dict:
        """Предпросмотр части на диске (studio/render/part_NN.wav + .json + .pk); пересобирается только при изменениях."""
        p = self.open_part(k)
        with self.rlock:
            head, _ = self.timeline(p["a"], p["b"], False)
            mf = self.render_dir / f"part_{k:02d}.json"
            meta = read_json(mf, None)
            if meta and meta.get("ver") == head["ver"] and self.render_wav(k, meta["ver"]).exists() and "ref" in meta:
                return meta
            with Op(self, f"часть {k}: сборка для прослушивания") as op:
                return self._render_build(k, p, mf, op)

    def _render_build(self, k: int, p: dict, mf: Path, op) -> dict:
        import soundfile as sf
        meta, audio = self.timeline(p["a"], p["b"], True, op)
        op.stage("запись предпросмотра")
        meta["ref"] = round(active_rms(audio), 5)
        # у каждой версии свой файл: в Windows нельзя заменить файл, который в эту минуту читает плеер
        wf = self.render_wav(k, meta["ver"])
        tmp = wf.with_name(wf.stem + ".tmp.wav")
        sf.write(str(tmp), audio, meta["sr"], format="WAV", subtype="PCM_16")
        os.replace(tmp, wf)
        w = meta["sr"] // 100                               # волна: максимум по 10 мс, один байт
        kf = len(audio) // w
        pk = np.abs(audio[:kf * w]).reshape(kf, w).max(axis=1) if kf else np.zeros(0)
        write_atomic(wf.with_suffix(".pk"), np.clip(pk * 255, 0, 255).astype("uint8").tobytes())
        write_atomic(mf, jd(meta))
        for f in self.render_dir.glob(f"part_{k:02d}_*.*"):      # прежние версии — убрать (занятые уйдут позже)
            if meta["ver"] not in f.name:
                try:
                    f.unlink()
                except OSError:
                    pass
        op.note = f"{mmss(meta['total'])}; {op.note}"
        return meta

    def render_wav(self, k: int, ver: str) -> Path:
        return self.render_dir / f"part_{k:02d}_{ver}.wav"

    # ------------------------------------------------------------------ что видит интерфейс
    def seg_view(self, i: int, t0=None, t1=None, ridx=None) -> dict:
        uid, r = self.uids[i], self.rows[i]
        e = self.store["segs"].get(uid) or {}
        ef, sc = self.eff(i, ridx), self.src(i)
        cur = (e.get("locked") or {}).get("take") or e.get("take")
        takes = [dict(tid=t["tid"], src=t.get("src", "synth"), dur=t.get("dur"), why=t.get("why") or [],
                      tech=t.get("tech"), role=t.get("role"), at=t.get("created"), chosen=t["tid"] == cur,
                      variant=t.get("variant"), trim=(e.get("trims") or {}).get(t["tid"]) or None,
                      name=t.get("orig_name"))
                 for t in self.takes_of(e).values() if self.fits(e, t)]
        return dict(uid=uid, id=r["id"], i=i, para=r["para"], kind=r["kind"], ch=int(r["chapter"]), text=self.row_text(i),
                    role=ef["role"], base_role=self.base_roles[i], tech=ef["tech"], base_tech=self.base_tech_all(i),
                    auto=ef["tech"] if ef["tech_src"] in ("base", "rule") else self.auto_tech(i, ridx)[0],
                    tech_src=ef["tech_src"], hits=ef["hits"], tempo=ef["tempo"], pause=ef["pause"],
                    base_pause=int(r["pause_before_ms"]), t0=t0, t1=t1, state=self.state(i, ef, sc),
                    locked=ef["locked"], mark=e.get("mark"), flags=[] if e.get("ack") else self.flags(i),
                    ack=bool(e.get("ack")), takes=takes, draft=self.wavs[i].exists() and not e.get("join"), src=sc["kind"],
                    after=ef["after"], joined=[dict(id=self.rows[j]["id"], text=self.rows[j]["text"][:160]) for j in self.chain(i)[1:]],
                    vstale=(not ef["locked"]) and self.voice_stale(ef, sc, e), vkeep=bool(e.get("vkeep")),
                    edits=[q for q in self.RESET_KEYS if e.get(q) not in (None, "", False, [])],
                    weak=bool(e.get("weak")), st=[] if ef["locked"] else self.stress_of(uid, ef["tech"]),
                    ins=bool(self.rows[i].get("ins")),
                    moved=bool((self.ins_rec(uid) or {}).get("moved")) if self.rows[i].get("ins") else False)

    def part_view(self, k: int) -> dict:
        self.load_rows()
        self._seen[int(k)] = time.time()
        meta = self.ensure_render(k)
        with self.lock:
            p = self.plan()[k - 1]
            rec = self.store["parts"][k - 1]
            ex0 = rec.get("export")
            if ex0 and ex0.get("ver") != meta["ver"] and not ex0.get("kv"):
                self._migrate_export(ex0, p["a"], p["b"], meta["ver"], k)
            ridx = self.rule_index()
            segs = [self.seg_view(self.idx_of[u], t0, t1, ridx) for u, t0, t1 in meta["segs"]
                    if u in self.idx_of and self.idx_of[u] not in self._abs]
            ex = self.export_view(rec.get("export"), meta["ver"])
            msig = self.mix_sig(p["a"], p["b"], meta)
            if ex is not None:
                mx = (rec.get("export") or {}).get("mix") or {}
                ex["mix"] = dict(file=mx.get("file"), count=mx.get("count"), fresh=mx.get("sig", "") == msig,
                                 exists=bool(mx.get("file")) and Path(mx["file"]).exists()) if (mx or msig) else None
                ex["vars"] = self.vars_view(rec.get("export"), p["a"], p["b"], meta)
            return dict(n=k, ver=meta["ver"], total=meta["total"], sr=meta["sr"], chapters=meta["chapters"], segs=segs,
                        locked=rec.get("locked"), export=ex, a=p["a"], b=p["b"],
                        cues=self.cues_view(k, meta), mix=self.store["mix"], msig=msig,
                        stress=self.stress_view(k, segs, auto=not rec.get("locked")))

    def vars_view(self, ex, a: int, b: int, meta: dict) -> list:
        """Версии готового файла со звуком — для окна: какие включены, собраны ли, не устарели ли."""
        have = (ex or {}).get("vars")
        if have is None:                                     # собрано до 12a: была одна версия — «всё вместе»
            have = {"all": ex["mix"]} if (ex or {}).get("mix") else {}
        on = {v[0] for v in self.vars_on()}
        out = []
        for vid, kinds, suf, name in MIX_VARS:
            v = have.get(vid)
            if vid not in on and not v:
                continue
            sig = self.mix_sig(a, b, meta, kinds)
            out.append(dict(id=vid, name=name, on=vid in on, file=(v or {}).get("file"), count=(v or {}).get("count"),
                            copy=bool((v or {}).get("copy")), exists=bool(v) and Path(str(v.get("file") or "-")).exists(),
                            fresh=bool(v) and (v.get("sig") or "") == sig))
        return out

    def files_missing(self, ex, sounds=None) -> list:
        """Каких готовых файлов нет на диске: голосового и включённых версий со звуком. sounds — есть ли в отрезке звуки
        (None — судить по записи о сборке). Пустой список — всё на месте."""
        ex = ex or {}
        miss = []
        if not ex.get("file") or not Path(str(ex["file"])).exists():
            miss.append("голос")
        vs = ex.get("vars")
        if vs is None:                                       # собрано до 12a: была одна версия — «всё вместе»
            vs = {"all": ex["mix"]} if ex.get("mix") else {}
        if sounds is None:
            sounds = bool(vs)
        if sounds:
            for vid, kinds, suf, name in self.vars_on():
                v = vs.get(vid)
                if not v or not Path(str(v.get("file") or "-")).exists():
                    miss.append(name)
        return miss

    def cues_sig(self, a: int, b: int) -> str:
        """Отпечаток звуков отрезка a..b по их описанию (без расчёта ленты и самого звука): что звучит, откуда, как
        громко. По нему видно, что звуки меняли после сборки готовых файлов. Конец звука за пределами отрезка записан
        как «до конца отрезка»: то, что он позже продлится в следующих частях, этот отрезок не меняет."""
        mx = self.store["mix"]
        items = []
        with self.lock:
            for c in self.store["cues"]:
                i = self.idx_of.get(c.get("uid"))
                if i is None:
                    continue
                ie = self.idx_of.get(c.get("e_uid")) if c.get("e_uid") else None
                if (ie if ie is not None else i) < a or i > b:
                    continue
                if not c.get("on", True) or not mx["on"].get(c["kind"], True) or self.cue_file(c) is None:
                    continue
                end = ["cut"] if ((ie is not None and ie > b) or c.get("open")) else [c.get("e_uid"), c.get("e_at"), c.get("e_off"), c.get("len"), c.get("est")]
                items.append([c["id"], c["kind"], c.get("uid"), c.get("at"), c.get("off"), end, str(self.cue_ref(c)),
                              c.get("vol", 1.0), bool(c.get("loop")), c.get("fin"), c.get("fout"), c.get("foff"), c.get("env")])
        items.sort(key=lambda x: x[0])
        return sha(jd([items, [mx.get(k) for k in CUE_KINDS], sorted(v[0] for v in self.vars_on())]), 16) if items else ""

    def export_stale(self, ex, a: int, b: int) -> bool:
        """Звуки отрезка меняли после сборки готовых файлов (для сборок до 12a неизвестно — считается, что нет)."""
        return bool(ex) and ex.get("csig") is not None and ex.get("csig") != self.cues_sig(a, b)

    def export_view(self, ex, ver):
        if not ex:
            return None
        return dict(file=ex.get("file"), dur=ex.get("dur"), at=ex.get("at"), fresh=ex.get("ver") == ver,
                    exists=Path(ex.get("file", "")).exists(), disk=ex.get("disk"))

    def state_view(self) -> dict:
        self.load_rows()
        with self.lock:
            pl = self.plan()
            parts = []
            for p in pl:
                d = dict(n=p["n"], a=p["a"], b=p["b"], opened=p["opened"], count=p["b"] - p["a"] + 1,
                         first=self.rows[p["a"]]["ch_title"], ch0=int(self.rows[p["a"]]["chapter"]),
                         ch1=int(self.rows[p["b"]]["chapter"]))
                if p["opened"]:
                    rec = self.store["parts"][p["n"] - 1]
                    meta = read_json(self.render_dir / f"part_{p['n']:02d}.json", None) or {}
                    cnt = {"locked": 0, "edited": 0, "stale": 0, "empty": 0, "mark": 0}
                    for i in range(p["a"], p["b"] + 1):
                        e = self.store["segs"].get(self.uids[i])
                        if not e:
                            continue
                        if e.get("locked"):
                            cnt["locked"] += 1
                        elif any(e.get(x) not in (None, "", []) for x in ("role", "tech", "tempo", "pause", "take", "after", "join")):
                            cnt["edited"] += 1
                        if e.get("mark"):
                            cnt["mark"] += 1
                    d.update(dur=meta.get("total"), locked=rec.get("locked"), cnt=cnt, auto=bool(rec.get("auto")),
                             export=self.export_view(rec.get("export"), meta.get("ver")))
                else:
                    d["dur"] = self.draft_dur(p["n"])
                parts.append(d)
            chapters = []
            for c, (a, b, title) in sorted(self.chapters.items()):
                lk = sum(1 for i in range(a, b + 1) if (self.store["segs"].get(self.uids[i]) or {}).get("locked"))
                ex = (self.store["chapters"].get(str(c)) or {}).get("export")
                chapters.append(dict(ch=c, title=title, a=a, b=b, count=b - a + 1, locked=lk,
                                     export=self.export_view(ex, (ex or {}).get("ver")) if ex else None,
                                     ready=all(self.wavs[i].exists() or i in self._abs or (self.store["segs"].get(self.uids[i]) or {}).get("take")
                                               for i in range(a, b + 1))))
            return dict(ver=VERSION, book=dict(title=self.title, author=self.author, root=str(self.root),
                                               rows=len(self.rows), stage=getattr(n, "NOX_STAGE", 0),
                                               stage_ok=STAGE_OK, asm=self.asm_src, tempo=n.TEMPO_G,
                                               part_min=self.part_min),
                        roles=self.role_names(), parts=parts, chapters=chapters, rules=self.store["rules"],
                        orphans=len(self.store["orphans"]), engine=self.engine_view(), mix=self.store["mix"],
                        go=self.go_view(pl), auto=self.auto_view(pl),
                        cue_labels=self.cue_labels(), sound_map=self.store["sound_map"],
                        score=self.score_view(),
                        gotovo=dict(parts=str(self.final_parts), chapters=str(self.final_ch), disk=self.disk_dir(False)))

    def draft_dur(self, k: int):
        f = n.PARTS / f"part_{k:02d}.times.tsv"
        try:
            last = f.read_text(encoding="utf-8").strip().splitlines()[-1].split("\t")
            return float(last[7]) if len(last) >= 8 else None
        except Exception:
            return None

    # ------------------------------------------------------------------ правка фразы
    def _push_undo(self, uid: str, label: str):
        e = self.store["segs"].get(uid)
        self.undo.append((uid, json.loads(jd(e)) if e is not None else None, label))
        del self.undo[:-80]

    def set_seg(self, uid: str, ch: dict) -> None:
        with self.lock:
            if uid not in self.idx_of:
                raise Oops("этой фразы больше нет в книге (книга переразбита) — обновите окно")
            i = self.idx_of[uid]
            e = self.E(uid, create=True)
            only_notes = set(ch) <= {"mark", "ack"}
            if e.get("locked") and not only_notes:
                raise Oops("фраза закреплена — сначала снимите закрепление")
            self._push_undo(uid, "правка")
            if "role" in ch:
                role = ch["role"]
                if role in (None, "", self.base_roles[i]):
                    e.pop("role", None)
                elif role not in self.roles["roles"]:
                    raise Oops(f"нет роли «{role}»")
                else:
                    e["role"] = role
            if "tech" in ch:
                t = ch["tech"]
                if t is None:
                    e.pop("tech", None)
                else:
                    t = norm_t(plus_to_acute(str(t)))
                    if not n.RE_RU_LETTERS.sub("", t):
                        raise Oops("в техническом тексте нет букв")
                    if norm_t(self.auto_tech(i)[0]) == t:
                        e.pop("tech", None)
                    else:
                        e["tech"] = t
            if "tempo" in ch:
                v = ch["tempo"]
                if v in (None, "") or abs(float(v) - 1.0) < 0.005:
                    e.pop("tempo", None)
                else:
                    e["tempo"] = round(min(max(float(v), 0.6), 1.6), 3)
            if "pause" in ch:
                v = ch["pause"]
                if v in (None, "") or int(v) == int(self.rows[i]["pause_before_ms"]):
                    e.pop("pause", None)
                else:
                    e["pause"] = int(min(max(int(v), 0), 6000))
            if "after" in ch:
                try:
                    v = 0.0 if ch["after"] in (None, "") else round(min(max(float(ch["after"]), 0.0), 20.0), 2)
                except (TypeError, ValueError):
                    raise Oops("пауза после фразы задаётся числом секунд")
                if v < 0.005:
                    e.pop("after", None)
                else:
                    e["after"] = v
            if "vkeep" in ch:
                if ch["vkeep"]:
                    sc0, cur = self.src(i), self.vsig_now(self.eff(i)["role"])
                    if sc0.get("kind") != "take" or not cur or cur == "nov":
                        raise Oops("оставлять нечего: у фразы звучит не дубль студии")
                    e["vkeep"] = [sc0["tid"], cur]
                else:
                    e.pop("vkeep", None)
            if "mark" in ch:
                if ch["mark"]:
                    e["mark"] = str(ch["mark"])[:300] if isinstance(ch["mark"], str) else True
                else:
                    e.pop("mark", None)
            if "ack" in ch:
                if ch["ack"]:
                    e["ack"] = True
                else:
                    e.pop("ack", None)
            self._tidy(uid)
            self.save("правка фразы", {"uid": uid, "id": self.rows[i]["id"], "set": ch})

    RESET_KEYS = ("role", "tech", "tempo", "pause", "after", "take", "vkeep")
    RESET_NAMES = {"role": "роль", "tech": "текст", "tempo": "скорость", "pause": "пауза перед фразой", "after": "пауза после фразы",
                   "take": "выбор дубля", "vkeep": "отметка «оставить запись»"}

    def reset_seg(self, uid: str) -> dict:
        """Точечный сброс: одна фраза снова такая, какой её сделал конвейер, — роль и текст как в книге, скорость и паузы
        обычные, звучит черновая запись. Дубли остаются в списке, пометка при прослушивании остаётся, другие фразы не
        затрагиваются. Одна отмена (Ctrl+Z) возвращает всё."""
        with self.lock:
            i = self.idx_of.get(uid)
            if i is None:
                raise Oops("этой фразы больше нет в книге — обновите окно")
            if self.rows[i].get("ins"):
                raise Oops("у вставки нет варианта из книги — её можно изменить (F2) или удалить (Delete)")
            e = self.store["segs"].get(uid) or {}
            if e.get("locked"):
                raise Oops("фраза закреплена — сначала снимите закрепление")
            if e.get("join") or e.get("into"):
                raise Oops("фраза соединена с соседней — сначала разъедините (Shift+S)")
            if any(q["uid"] == uid and q["status"] in ("queued", "running") for q in self.jobs.values()):
                raise Oops("фраза сейчас озвучивается — дождитесь дубля")
            was = [k for k in self.RESET_KEYS if e.get(k) not in (None, "", False, [])]
            if not was:
                raise Oops("у этой фразы нет правок — она и так как в книге")
            self._push_undo(uid, "сброс правок фразы")
            for k in was:
                e.pop(k, None)
            self._tidy(uid)
            what = ", ".join(self.RESET_NAMES[k] for k in was)
            self.save("правки фразы сброшены", {"uid": uid, "id": self.rows[i]["id"], "set": was})
            return dict(uid=uid, cleared=was, what=what)

    def _tidy(self, uid: str):
        e = self.store["segs"].get(uid)
        if e is not None and self.e_empty(e) and not e.get("tseq"):
            del self.store["segs"][uid]

    def undo_last(self) -> str:
        with self.lock:
            while self.undo:
                uid, old, label = self.undo.pop()
                if uid == "#cues":                             # звуки и громкости дорожек — одним снимком
                    snap = json.loads(old)
                    before = jd({key: {"present": key in self.store, "value": self.store.get(key)}
                                 for key in ("cues", "cue_seen", "sound_map", "mix", "kit_off")})
                    self.store["cues"], self.store["cue_seen"], self.store["sound_map"], self.store["mix"] = snap[:4]
                    if len(snap) > 4:
                        self.store["kit_off"] = snap[4]
                    try:
                        self.save("отмена: " + label)
                    except Exception:
                        for key, item in json.loads(before).items():
                            if item["present"]:
                                self.store[key] = item["value"]
                            else:
                                self.store.pop(key, None)
                        self.undo.append((uid, old, label))
                        raise
                    return uid
                if uid == "#ins":                              # вставка добавлена / удалена / переписана
                    d = json.loads(old)
                    x = self.ins_rec(d.get("id") or (d.get("ins") or {}).get("id") or "")
                    if d["op"] == "add":
                        if x is None:
                            continue
                        if (self.store["segs"].get(x["id"]) or {}).get("locked"):
                            raise Oops("вставка закреплена — снимите закрепление, чтобы отменить её")
                    elif d["op"] == "del" and x is None:
                        pass
                    elif d["op"] != "del" and x is None:
                        continue
                    before = jd({"ins": self.store["ins"], "segs": self.store["segs"]})
                    if d["op"] == "add":
                        self.store["ins"].remove(x)
                        self.store["segs"].pop(x["id"], None)
                    elif d["op"] == "del":
                        if x is None:
                            self.store["ins"].append(d["ins"])
                            if d.get("e"):
                                self.store["segs"][d["ins"]["id"]] = d["e"]
                    else:
                        x["text"] = d["text"]
                        if d.get("tech"):
                            x["tech"] = d["tech"]
                        else:
                            x.pop("tech", None)
                        if d.get("etech") is not None:
                            self.E(x["id"], create=True)["tech"] = d["etech"]
                    try:
                        self.save("отмена: " + label)
                    except Exception:
                        state = json.loads(before)
                        self.store["ins"], self.store["segs"] = state["ins"], state["segs"]
                        self.undo.append((uid, old, label))
                        raise
                    self.rows_changed()
                    return uid
                if uid == "#join":
                    d = json.loads(old)
                    snap_before = jd(self.store["segs"])
                    abs_before = jd(self._abs)
                    if any((self.store["segs"].get(d[q]) or {}).get("locked") for q in ("a", "b")):
                        self.undo.append((uid, old, label))
                        raise Oops("последняя правка — в закреплённой фразе; снимите закрепление, чтобы отменить")
                    try:
                        for q, w in (("a", "ea"), ("b", "eb")):
                            cur = self.store["segs"].get(d[q]) or {}
                            if d[w] is None:
                                self.store["segs"].pop(d[q], None)
                            else:
                                d[w]["tseq"] = max(int(d[w].get("tseq", 0)), int(cur.get("tseq", 0)))       # дубли не теряем
                                self.store["segs"][d[q]] = d[w]
                        self.joins_refresh()
                        self.save("отмена: " + label, {"uid": d["a"]})
                    except Exception:
                        self.store["segs"] = json.loads(snap_before)
                        self._abs = json.loads(abs_before)
                        self.undo.append((uid, old, label))
                        raise
                    return d["a"]
                if uid not in self.idx_of:
                    continue
                cur = self.store["segs"].get(uid) or {}
                if cur.get("locked") and not (old or {}).get("locked"):
                    raise Oops("последняя правка — в закреплённой фразе; снимите закрепление, чтобы отменить")
                before = jd(self.store["segs"])
                abs_before = jd(self._abs)
                try:
                    if old is None:
                        self.store["segs"].pop(uid, None)
                    else:
                        old["tseq"] = max(int(old.get("tseq", 0)), int(cur.get("tseq", 0)))   # дубли не теряем
                        self.store["segs"][uid] = old
                    self.joins_refresh()
                    self.save("отмена: " + label, {"uid": uid})
                except Exception:
                    self.store["segs"] = json.loads(before)
                    self._abs = json.loads(abs_before)
                    self.undo.append((uid, old, label))
                    raise
                return uid
            raise Oops("отменять нечего")

    # ------------------------------------------------------------------ переозвучка: заявки воркерам
    def synth(self, uid: str, variant: dict | None = None) -> str:
        with self.lock:
            if uid not in self.idx_of:
                raise Oops("этой фразы больше нет в книге — обновите окно")
            i = self.idx_of[uid]
            e = self.E(uid, create=True)
            if e.get("locked"):
                raise Oops("фраза закреплена — сначала снимите закрепление")
            ef = self.eff(i)
            wav_p, _ = n.voice_files(ef["role"], self.stems)
            if not wav_p.exists():
                raise Oops(f"у роли «{ef['role']}» нет образца голоса — озвучить ею нельзя")
            tid = self.new_tid(e)
            out = self.take_path(e["sid"], tid)
            out.parent.mkdir(parents=True, exist_ok=True)
            rid = f"q{int(time.time() * 1000):x}{secrets.token_hex(2)}"
            req = dict(rid=rid, role=ef["role"], text=ef["tech"], raw=ef["tech"].replace(ACUTE, ""), out=str(out),
                       variant=variant or {}, tries=2, rescue=True, atomic=bool(e.get("join")))
            ch = self.chain(i)
            prep = ACUTE not in ef["tech"] and (bool(self.rows[i].get("ins"))        # вставка без ударений: их расставит движок
                                                or (ef["tech_src"] != "manual" and self.accent_mode() != "none"
                                                    and all(self.base_is_raw(j) for j in ch)))   # фраза книги без обработанного текста
            if prep:
                req["prep"] = True
            write_atomic(self.qdir / f"{rid}.req.json", jd(req))
            basis = "raw" if not e.get("join") and skel(ef["tech"]) == skel(self.base_tech(i)) else "tech"     # слова те же / изменены
            self.jobs[rid] = dict(rid=rid, uid=uid, tid=tid, sid=e["sid"], role=ef["role"], tech=ef["tech"],
                                  raw=self.row_text(i), status="queued", t=time.time(), variant=variant or {},
                                  basis=basis, prep=prep, cover=list(e.get("join") or []))
            self.save("заявка на переозвучку", {"uid": uid, "rid": rid, "tid": tid})
            return rid

    def poll_jobs(self):
        """Ответы воркеров -> дубли. Вызывается из фонового потока раз в полсекунды."""
        for rid, j in list(self.jobs.items()):
            if j["status"] not in ("queued", "running"):
                if time.time() - j.get("t_done", time.time()) > 600:
                    self.jobs.pop(rid, None)
                continue
            done = self.qdir / f"{rid}.done.json"
            if done.exists():
                res = read_json(done, None)
                if res is None:
                    continue                                   # ещё дописывается
                with self.lock:
                    self._finish(j, res)
                done.unlink(missing_ok=True)
                continue
            if list(self.qdir.glob(f"{rid}.run*.json")):
                j["status"] = "running"
            if time.time() - j["t"] > 600:                   # воркер умер посреди заявки
                with self.lock:
                    j.update(status="error", error="движок не ответил за 10 минут", t_done=time.time())
                log("заявка на переозвучку осталась без ответа 10 минут — снята (движок не запущен или завис)", "err", "движок")
                for f in list(self.qdir.glob(f"{rid}.*")):
                    f.unlink(missing_ok=True)

    def _finish(self, j: dict, res: dict):
        j["t_done"] = time.time()
        wav = self.take_path(j["sid"], j["tid"])
        i0 = self.idx_of.get(j["uid"])
        no = self.rows[i0]["id"] if i0 is not None else "?"
        who = "Рассказчик" if j["role"] == "narrator" else j["role"]
        if not res.get("ok") or not wav.exists():
            j.update(status="error", error=res.get("error") or "запись не получена")
            wav.unlink(missing_ok=True)
            self.journal({"why": "переозвучка не удалась", "uid": j["uid"], "error": j["error"]})
            log(f"фраза № {no} ({who}) не озвучена: {j['error']}", "err", "движок")
            return
        why_all = [str(w) for w in (res.get("why") or [])]
        if why_dead(why_all, j["tech"], res.get("speech_s"), res.get("dur")):
            # В записи нет ни звука. До 12a такой «дубль» вставал в список и сам становился выбранным — фраза в части
            # умолкала. Теперь тишина дублем не становится: файл убирается, прежний выбор остаётся, автору — ясные слова.
            tried = int(res.get("tried") or res.get("attempts") or 1)
            j.update(status="error", error=SILENT_MSG.format(k=tried))
            wav.unlink(missing_ok=True)
            self.journal({"why": "переозвучка: движок вернул тишину", "uid": j["uid"], "tried": tried, "detail": why_all})
            log(f"фраза № {no} ({who}): движок вернул тишину вместо речи (попыток: {tried}) — дубль не записан, в части "
                f"звучит прежняя запись. Повторите «Озвучить заново»", "warn", "движок")
            return
        weak = any(w.startswith(WEAK_WHY + DEAD_WHY) for w in why_all)   # «нет речи» здесь — только у фразы в 1–2 буквы
        if j.get("prep") and res.get("text"):              # вставка: текст с ударениями, как его прочёл движок
            x = self.ins_rec(j["uid"])
            e0 = self.store["segs"].get(j["uid"]) or {}
            if x is not None and x["text"] == j["raw"] and e0.get("tech") is None:
                x["tech"] = str(res["text"])
                j["tech"] = str(res["text"])
            elif x is None and i0 is not None and e0.get("tech") is None and skel(str(res["text"])) == skel(j["tech"]):
                # фраза книги без обработанного текста: слова те же, движок расставил ударения — это теперь её текст
                if len(self.chain(i0)) == 1:
                    self.tcache_put(i0, str(res["text"]))
                    self.tcache_save(True)
                elif self.store["segs"].get(j["uid"]) is not None:
                    self.store["segs"][j["uid"]]["tech"] = norm_t(str(res["text"]))
                j["tech"] = norm_t(str(res["text"]))
        meta = dict(tid=j["tid"], src="synth", created=now(), role=j["role"], tech=j["tech"], raw=j["raw"],
                    basis=j.get("basis") or "raw", dur=res.get("dur"), why=res.get("why") or [], variant=res.get("variant"), sec=res.get("sec"),
                    metrics={k: res.get(k) for k in ("rate", "tail_db", "vowel_ms", "speech_s", "voiced_s", "attempts",
                                                     "worker", "vsig", "method")})
        if j.get("cover"):
            meta["cover"] = list(j["cover"])               # дубль соединённой фразы: для какого её состава записан
        write_atomic(wav.with_suffix(".json"), jd(meta))
        self._takes.pop(j["sid"], None)
        j.update(status="done", why=meta["why"], dur=meta["dur"], chosen=False)
        uid = j["uid"]
        e = self.store["segs"].get(uid)
        # обрывок или шум без голоса: если у фразы уже есть годная запись, такой дубль её не вытесняет — он остаётся в списке
        # (вдруг это шёпот). Если годной записи нет (фраза без записи или запись устарела), дубль выбирается, как любой другой
        keep_cur = bool(weak and e is not None and uid in self.idx_of and self.state(self.idx_of[uid]) in ("draft", "edited"))
        if keep_cur:
            log(f"фраза № {no}: дубль {j['tid']} записан, но не выбран — движок сомневается, что в нём речь "
                f"({', '.join(why_all)}). Он в списке дублей; в части звучит прежняя запись", "warn", "движок")
        elif e is not None and not e.get("locked") and uid in self.idx_of:
            ef = self.eff(self.idx_of[uid])
            cur = self.takes_of(e).get(e.get("take") or "") or {}
            own_later = False                              # автор уже после заявки дал фразе свою запись — она главнее
            if cur.get("src") == "import":
                try:
                    own_later = time.mktime(time.strptime(str(cur.get("created")), "%Y-%m-%d %H:%M:%S")) >= j["t"] - 1
                except Exception:
                    own_later = True
            if own_later:
                log(f"фраза № {no}: дубль {j['tid']} добавлен в список, в части по-прежнему звучит ваша запись", "ok", "движок")
            elif ef["role"] == j["role"] and norm_t(ef["tech"]) == norm_t(j["tech"]) and self.fits(e, meta):   # текст и состав с тех пор не меняли
                self._push_undo(uid, "новый дубль")
                e["take"] = j["tid"]
                j["chosen"] = True
        self.save("получен дубль", {"uid": uid, "tid": j["tid"], "why": meta["why"]})
        log(f"фраза № {no} ({who}) озвучена: дубль {j['tid']}, {float(meta['dur'] or 0):.1f} с звука за "
            f"{float(res.get('sec') or 0):.1f} с" + (f" (воркер {res['worker']})" if res.get("worker") is not None else "")
            + (f"; речь получилась с {int(res.get('tried') or 0)}-й попытки ({res['rescue']})" if res.get("rescue") and not weak else "")
            + ("; движок сам заметил: " + ", ".join(meta["why"]) if meta["why"] else ""),
            "warn" if meta["why"] else "ok", "движок")

    # ------------------------------------------------------------------ выбран дубль без речи: вернуть звучащий
    @staticmethod
    def take_dead(tk) -> bool:
        return bool(tk) and tk.get("src", "synth") == "synth" and why_dead(
            tk.get("why"), tk.get("tech"), (tk.get("metrics") or {}).get("speech_s"), tk.get("dur"))

    def heal_silent(self) -> dict:
        """Фразы, у которых ВЫБРАН дубль без речи. Студии до 12a сами выбирали свежий дубль, даже если движок вернул
        тишину, — фраза в части умолкала. Выбор возвращается к последнему звучащему дублю того же текста и той же роли;
        нет такого — к черновой записи (текст и роль правки остаются, фраза станет «переозвучить»). Закреплённые фразы
        не трогаются — о них сообщается. Сами пустые дубли остаются в списке: их видно и можно удалить."""
        fixed, locked = [], []
        with self.lock:
            for uid, e in list(self.store["segs"].items()):
                i = self.idx_of.get(uid)
                if i is None or not (e.get("take") or (e.get("locked") or {}).get("take")):
                    continue
                tk = self.takes_of(e)
                if self.take_dead(tk.get((e.get("locked") or {}).get("take") or "")):
                    locked.append(self.rows[i]["id"])
                    continue
                cur = tk.get(e.get("take") or "")
                if e.get("locked") or not self.take_dead(cur):
                    continue
                good = [x for x in tk.values()
                        if not self.take_dead(x) and self.fits(e, x) and x.get("role") == cur.get("role")
                        and norm_t(x.get("tech") or "") == norm_t(cur.get("tech") or "")
                        and not any(str(w).startswith(WEAK_WHY) for w in (x.get("why") or []))]
                good.sort(key=lambda x: (str(x.get("created") or ""), str(x.get("tid") or "")))
                if good:
                    e["take"] = good[-1]["tid"]
                else:
                    e.pop("take", None)
                fixed.append((self.rows[i]["id"], good[-1]["tid"] if good else ""))
            if fixed:
                self.undo.clear()                              # отмена не должна вернуть выбор пустого дубля
                self.save("выбор пустых дублей исправлен", {"count": len(fixed), "ids": [x[0] for x in fixed][:40]})
                self._parts_cache = None
        if fixed:
            log("у фраз № " + ", ".join(f"{a}" + (f" (теперь дубль {b[1:].lstrip('0') or '0'})" if b else " (черновая запись)")
                                        for a, b in fixed[:12]) + ("…" if len(fixed) > 12 else "")
                + " был выбран дубль без речи (его прислал движок, а прежняя версия студии выбрала сама) — выбор возвращён "
                  "к звучащей записи. Пустые дубли остались в списке, их можно удалить", "warn", "проверка")
        if locked:
            log("в ЗАКРЕПЛЁННЫХ фразах № " + ", ".join(map(str, locked[:12])) + ("…" if len(locked) > 12 else "")
                + " выбран дубль без речи — они молчат в готовом файле. Снимите закрепление, выберите звучащий дубль "
                  "(или переозвучьте) и закрепите снова", "warn", "проверка")
        return dict(fixed=len(fixed), locked=len(locked))

    def choose_take(self, uid: str, tid):
        with self.lock:
            i = self.idx_of.get(uid)
            if i is None:
                raise Oops("этой фразы больше нет в книге — обновите окно")
            e = self.E(uid, create=True)
            if e.get("locked"):
                raise Oops("фраза закреплена — сначала снимите закрепление")
            self._push_undo(uid, "выбор дубля")
            if not tid:                                       # вернуть черновую запись конвейера
                if e.get("join"):
                    raise Oops("черновая запись — это только первая из соединённых фраз; чтобы вернуться к ней, разъедините фразы")
                e.pop("take", None)
            else:
                t = self.takes_of(e).get(tid)
                if not t:
                    raise Oops("этого дубля уже нет на диске")
                if not self.fits(e, t):
                    raise Oops("этот дубль записан для другого состава фразы (до соединения или после него) — "
                               "он вернётся в список, когда фраза снова станет такой же")
                e["take"] = tid                                # дубль — это запись вместе со своим текстом и ролью
                if t.get("role") and t["role"] != self.base_roles[i]:
                    e["role"] = t["role"]
                else:
                    e.pop("role", None)
                if t.get("tech") and norm_t(self.auto_tech(i)[0]) != norm_t(t["tech"]):
                    e["tech"] = t["tech"]
                else:
                    e.pop("tech", None)
            self._tidy(uid)
            self.save("выбран дубль", {"uid": uid, "tid": tid})

    def delete_take(self, uid: str, tid: str):
        with self.lock:
            e = self.store["segs"].get(uid) or {}
            if (e.get("locked") or {}).get("take") == tid or e.get("take") == tid:
                raise Oops("этот дубль сейчас выбран — сначала выберите другой")
            p = self.take_path(e.get("sid", "-"), tid)
            trash = self.dir / "trash" / str(e.get("sid", "-"))    # не насовсем: файлы уходят в studio\trash
            trash.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            for f in (p, p.with_suffix(".json")):
                if f.exists():
                    try:
                        os.replace(f, trash / f"{f.stem}__{stamp}{f.suffix}")
                    except OSError:
                        f.unlink(missing_ok=True)
            self._takes.pop(e.get("sid"), None)
            if (e.get("trims") or {}).pop(tid, None) is not None:
                if not e["trims"]:
                    del e["trims"]
                self.save("дубль удалён: его обрезка снята", {"uid": uid, "tid": tid})
            self.journal({"why": "дубль удалён (в корзину studio\\trash)", "uid": uid, "tid": tid})

    def take_import(self, uid: str, name: str, data: bytes) -> dict:
        """Своя запись с компьютера автора -> НОВЫЙ дубль фразы. Ничего не перекрывается: прежние дубли остаются в списке,
        исходный файл кладётся как есть в studio\\imports, в работу идёт его копия в формате студии (моно, 24 кГц).
        Тишина по краям сразу обрезается — числами в правках, сам файл цел: запись всегда можно вернуть целиком."""
        name = Path(str(name).replace("\\", "/")).name or "zapis.wav"
        ext = Path(name).suffix.lower()
        if ext not in SND_EXT:
            raise Oops(f"«{name}» — не звуковой файл. Подходят: wav, mp3, ogg, flac, m4a")
        if not data:
            raise Oops(f"файл «{name}» пустой")
        with self.lock:
            i = self.idx_of.get(uid)
            if i is None:
                raise Oops("этой фразы больше нет — обновите окно")
            e = self.E(uid, create=True)
            if e.get("locked"):
                raise Oops("фраза закреплена — сначала снимите закрепление")
            ef = self.eff(i)
            tid = self.new_tid(e)
            sid, raw, no, cover = e["sid"], self.row_text(i), self.rows[i]["id"], list(e.get("join") or [])
            self.save("своя запись: место под дубль", {"uid": uid, "tid": tid})
        out = self.take_path(sid, tid)
        out.parent.mkdir(parents=True, exist_ok=True)
        imp = self.dir / "imports"
        imp.mkdir(exist_ok=True)
        orig = imp / f"{sid}_{tid}_{safe_file(Path(name).stem)[:50].strip() or 'zapis'}{ext}"
        write_atomic(orig, data)
        tmp = out.with_name(out.stem + ".tmp.wav")
        kw = {"creationflags": 0x08000000} if os.name == "nt" else {}
        r = subprocess.run([self.ffmpeg(), "-y", "-v", "error", "-i", str(orig), "-vn", "-ac", "1", "-ar", "24000",
                            "-c:a", "pcm_s16le", str(tmp)], capture_output=True, **kw)
        if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size < 2000:
            tmp.unlink(missing_ok=True)
            orig.unlink(missing_ok=True)
            raise Oops(f"«{name}» не читается как звук: " + (r.stderr.decode("utf-8", "replace").strip()[-160:] or "нет звуковой дорожки"))
        x, sr = self.read_audio(tmp)
        dur = len(x) / sr
        if dur < 0.2 or dur > 1800:
            tmp.unlink(missing_ok=True)
            orig.unlink(missing_ok=True)
            raise Oops("запись короче 0.2 секунды" if dur < 0.2 else "запись длиннее 30 минут — загружайте её по фразам")
        trim = self.quiet_edges(x, sr)
        os.replace(tmp, out)
        meta = dict(tid=tid, src="import", created=now(), role=ef["role"], tech=ef["tech"], raw=raw, basis="tech",
                    dur=round(dur, 3), why=[], orig=orig.name, orig_name=name)
        if cover:
            meta["cover"] = cover
        if trim:
            meta["trim0"] = trim                           # что предложено при загрузке (сама обрезка — в правках)
        write_atomic(out.with_suffix(".json"), jd(meta))
        with self.lock:
            self._takes.pop(sid, None)
            if uid in self.idx_of and not (self.store["segs"].get(uid) or {}).get("locked"):
                self._push_undo(uid, "своя запись")
                e = self.E(uid, create=True)
                e["take"] = tid
                if trim:
                    e.setdefault("trims", {})[tid] = trim
            self.save("загружена своя запись", {"uid": uid, "tid": tid, "file": name, "dur": round(dur, 2)})
        log(f"фраза № {no}: загружена своя запись «{name}» ({dur:.1f} с) — дубль {tid}; исходный файл сохранён в studio\\imports"
            + (f"; тишина по краям предложена к обрезке ({trim[0]:.2f}–{trim[1]:.2f} с)" if trim else ""), "edit", "правка")
        return dict(tid=tid, dur=round(dur, 3), trim=trim)

    @staticmethod
    def quiet_edges(x, sr):
        """Где в записи кончается тишина в начале и начинается в конце -> [t0, t1] (с запасом 0.08/0.15 с) или None,
        если резать почти нечего."""
        w = max(1, int(sr * 0.02))
        k = len(x) // w
        if k < 10:
            return None
        env = np.sqrt(np.mean(np.square(x[:k * w].reshape(k, w)), axis=1))
        thr = max(0.06 * float(np.percentile(env, 95)), 0.003)
        hot = np.nonzero(env > thr)[0]
        if not len(hot):
            return None
        t0 = max(0.0, hot[0] * w / sr - 0.08)
        t1 = min(len(x) / sr, (hot[-1] + 1) * w / sr + 0.15)
        if t0 < 0.12 and len(x) / sr - t1 < 0.2:
            return None
        return [round(t0, 3), round(t1, 3)]

    def take_trim(self, uid: str, tid: str, t0=None, t1=None):
        """Обрезка дубля: начало и конец в секундах от начала записи (None, None — вернуть запись целиком). Файл записи
        не меняется — в правках фразы хранятся два числа; отменяется Ctrl+Z, как любая правка."""
        with self.lock:
            e = self.store["segs"].get(uid) or {}
            if e.get("locked"):
                raise Oops("фраза закреплена — сначала снимите закрепление")
            pushed = False
            if not tid:
                # Черновая запись конвейера: обрезается её копия-дубль. Сам черновик конвейер может переозвучить, а числа
                # обрезки должны относиться к одной и той же записи.
                if t0 is None and t1 is None:
                    return
                i = self.idx_of.get(uid)
                sc = self.src(i) if i is not None else None
                if not sc or sc["kind"] != "draft":
                    raise Oops("у фразы сейчас звучит не черновая запись — обновите окно")
                if e.get("join"):
                    raise Oops("у соединённой фразы черновая запись — только первая её часть: сначала озвучьте фразу заново")
                try:                                       # числа проверяются до того, как делать копию
                    x0, sr0 = self.read_audio(sc["path"])
                    d0 = len(x0) / sr0
                    a0, b0 = min(max(0.0, float(t0 or 0)), d0), min(max(0.0, float(t1 if t1 is not None else d0)), d0)
                except (TypeError, ValueError):
                    raise Oops("обрезка задаётся двумя числами — секундами от начала записи")
                if b0 - a0 < 0.15:
                    raise Oops("после обрезки остаётся меньше 0.15 секунды — так фразу не услышать")
                if a0 < 0.005 and d0 - b0 < 0.005:
                    return                                 # обрезать нечего — копия не нужна
                e = self.E(uid, create=True)
                self._push_undo(uid, "обрезка записи")
                pushed = True
                tid = self._adopt(i, e, sc)
                e["take"] = tid
            tk = self.takes_of(e).get(tid)
            if not tk:
                raise Oops("такого дубля уже нет")
            dur = float(tk.get("dur") or 0)
            if not dur:
                x, sr = self.read_audio(self.take_path(e["sid"], tid))
                dur = len(x) / sr
            old = (e.get("trims") or {}).get(tid) or None
            if t0 is None and t1 is None:
                new = None
            else:
                try:
                    a = min(max(0.0, float(t0 or 0)), dur)
                    b = min(max(0.0, float(t1 if t1 is not None else dur)), dur)
                except (TypeError, ValueError):
                    raise Oops("обрезка задаётся двумя числами — секундами от начала записи")
                if b - a < 0.15:
                    raise Oops("после обрезки остаётся меньше 0.15 секунды — так фразу не услышать")
                new = None if a < 0.005 and dur - b < 0.005 else [round(a, 3), round(b, 3)]
            if new == old and not pushed:
                return
            if not pushed:
                self._push_undo(uid, "обрезка дубля")
            if new is None:
                (e.get("trims") or {}).pop(tid, None)
                if not e.get("trims"):
                    e.pop("trims", None)
            else:
                e.setdefault("trims", {})[tid] = new
            i = self.idx_of.get(uid)
            self.save("обрезка дубля", {"uid": uid, "id": self.rows[i]["id"] if i is not None else None, "tid": tid, "trim": new})

    def take_peaks(self, uid: str, tid: str) -> dict:
        """Очертание записи для окна обрезки: дубля (tid) или черновой записи конвейера (tid пуст)."""
        e = self.store["segs"].get(uid) or {}
        if not tid:
            i = self.idx_of.get(uid)
            if i is None or not self.wavs[i].exists():
                raise Oops("у этой фразы нет черновой записи")
            x, sr = self.read_audio(self.wavs[i])
            tk = {}
        else:
            tk = self.takes_of(e).get(tid)
            if not tk:
                raise Oops("такого дубля уже нет")
            x, sr = self.read_audio(self.take_path(e["sid"], tid))
        n_pts = int(min(max(600, len(x) / sr * 100), 12000))      # 10 мс на точку; у очень длинных записей — грубее
        k = max(1, len(x) // n_pts)
        m = len(x) // k
        pk = np.abs(x[:m * k].reshape(m, k)).max(axis=1) if m else np.zeros(1)
        top = float(pk.max()) or 1.0
        i = self.idx_of.get(uid)
        nxt = None                                         # следующая фраза той же части — для «дальше по этой записи»
        j1 = self.chain(i)[-1] + 1 if i is not None else 0
        if i is not None and tk.get("src") == "import" and j1 < len(self.rows) and self.part_of(j1) == self.part_of(i):
            nxt = dict(uid=self.uids[j1], id=self.rows[j1]["id"], text=self.row_text(j1)[:140],
                       locked=bool((self.store["segs"].get(self.uids[j1]) or {}).get("locked")))
        return dict(dur=round(len(x) / sr, 3), trim=(e.get("trims") or {}).get(tid) or None, trim0=tk.get("trim0") or None,
                    peaks=[round(float(v) / top, 3) for v in pk], next=nxt)

    def take_next(self, uid: str, tid: str, t0, t1) -> dict:
        """Одна своя запись на несколько фраз подряд (автор начитал абзац или сцену одним файлом). Обрезка этой фразы
        применяется, а следующая фраза получает ту же запись новым дублем — с началом там, где кончилась эта. Копии на
        диске не делается (жёсткая ссылка; не вышло — обычная копия); у каждой фразы свои числа обрезки."""
        self.take_trim(uid, tid, t0, t1)
        with self.lock:
            i = self.idx_of.get(uid)
            e = self.store["segs"].get(uid) or {}
            tk = self.takes_of(e).get(tid)
            if i is None or not tk or tk.get("src") != "import":
                raise Oops("по фразам раскладывается только своя запись")
            j = self.chain(i)[-1] + 1
            if j >= len(self.rows) or self.part_of(j) != self.part_of(i):
                raise Oops("это последняя фраза части — дальше раскладывать некуда")
            uid2 = self.uids[j]
            e2 = self.E(uid2, create=True)
            if e2.get("locked"):
                raise Oops(f"следующая фраза (№ {self.rows[j]['id']}) закреплена — снимите закрепление")
            ef2 = self.eff(j)
            dur = float(tk.get("dur") or 0)
            cut = (e.get("trims") or {}).get(tid) or [0.0, dur]
            end = float((tk.get("trim0") or [0.0, dur])[1])
            start = min(float(cut[1]), max(0.0, dur - 0.15))
            span = [round(start, 3), round(max(start + 0.15, min(dur, end)), 3)]
            tid2 = next((x["tid"] for x in self.takes_of(e2).values()            # эта запись у следующей фразы уже есть
                         if x.get("src") == "import" and x.get("orig") and x.get("orig") == tk.get("orig") and self.fits(e2, x)), None)
            self._push_undo(uid2, "своя запись: следующая фраза")
            if tid2 is None:
                tid2 = self.new_tid(e2)
                src, dst = self.take_path(e["sid"], tid), self.take_path(e2["sid"], tid2)
                dst.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.link(src, dst)
                except OSError:
                    shutil.copy2(src, dst)
                meta = dict(tk, tid=tid2, created=now(), role=ef2["role"], tech=ef2["tech"], raw=self.row_text(j))
                meta.pop("cover", None)
                if e2.get("join"):
                    meta["cover"] = list(e2["join"])
                write_atomic(dst.with_suffix(".json"), jd(meta))
                self._takes.pop(e2["sid"], None)
                e2.setdefault("trims", {})[tid2] = span
            elif not (e2.get("trims") or {}).get(tid2):
                e2.setdefault("trims", {})[tid2] = span
            e2["take"] = tid2
            self.save("своя запись: следующая фраза", {"uid": uid2, "id": self.rows[j]["id"], "tid": tid2})
            return dict(uid=uid2, tid=tid2)

    # ------------------------------------------------------------------ закрепление
    def _adopt(self, i: int, e: dict, sc: dict) -> str:
        """Черновая запись конвейера -> собственная копия студии (дубль «черновая»): закреплённое не зависит от конвейера."""
        h = hashlib.sha1(sc["path"].read_bytes()).hexdigest()[:12]
        for t in self.takes_of(e).values():
            if t.get("sha") == h:
                return t["tid"]
        tid = self.new_tid(e)
        out = self.take_path(e["sid"], tid)
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sc["path"], out)
        m = read_json(sc["path"].with_suffix(".meta"), {}) or {}
        import soundfile as sf
        info = sf.info(str(out))
        write_atomic(out.with_suffix(".json"), jd(dict(
            tid=tid, src="draft", created=now(), role=sc["role"], tech=sc["tech"] or self.base_tech(i),
            raw=self.rows[i]["text"], dur=round(info.frames / info.samplerate, 3), why=m.get("why") or [],
            gaps=sc.get("gaps"), sha=h, variant=m.get("variant"), basis="raw", stem0=sc["path"].stem)))
        self._takes.pop(e["sid"], None)
        return tid

    def lock_segs(self, idxs, label: str) -> int:
        with self.lock:
            idxs = [i for i in idxs if i not in self._abs]     # присоединённые фразы закрепляются вместе с головной
            bad = [(i, self.state(i)) for i in idxs]
            bad = [(i, s) for i, s in bad if s in ("stale", "empty", "lost")]
            if bad:
                names = {"stale": "текст или роль изменены, а запись прежняя — переозвучьте", "empty": "нет записи",
                         "lost": "потерян файл дубля"}
                raise Oops("закрепить нельзя, сначала разберитесь с фразами: "
                           + "; ".join(f"№{self.rows[i]['id']} ({names[s]})" for i, s in bad[:6])
                           + (f" и ещё {len(bad) - 6}" if len(bad) > 6 else ""))
            k = 0
            with Op(self, label, quiet=True) as op:
                op.stage("свои копии записей", len(idxs))
                for i in idxs:
                    op.step()
                    uid = self.uids[i]
                    e = self.E(uid, create=True)
                    if e.get("locked"):
                        continue
                    ef, sc = self.eff(i), self.src(i)
                    tid = sc["tid"] or self._adopt(i, e, sc)
                    e["locked"] = dict(at=now(), take=tid, role=ef["role"], tech=ef["tech"], tempo=ef["tempo"],
                                       pause=ef["pause"], after=ef["after"])
                    e["take"] = tid
                    for j in self.chain(i)[1:]:
                        self.store["segs"][self.uids[j]]["locked"] = dict(at=e["locked"]["at"], head=uid)
                    k += 1
            if k:
                self.undo.clear()
                self.save(label, {"count": k})
            return k

    def unlock_segs(self, idxs, label: str) -> int:
        with self.lock:
            k = 0
            for i in idxs:
                e = self.store["segs"].get(self.uids[i])
                if not e or not e.get("locked"):
                    continue
                lk = e.pop("locked")
                if lk.get("head"):                             # присоединённая: закрепление снимается вместе с головной
                    continue
                for j in self.chain(i)[1:]:
                    (self.store["segs"].get(self.uids[j]) or {}).pop("locked", None)
                # закреплённые значения остаются как обычные правки: звук тот же, но теперь его можно менять
                if lk["role"] != self.base_roles[i]:
                    e["role"] = lk["role"]
                if norm_t(self.auto_tech(i)[0]) != norm_t(lk["tech"]):
                    e["tech"] = lk["tech"]
                if abs(float(lk.get("tempo") or 1.0) - 1.0) >= 0.005:
                    e["tempo"] = lk["tempo"]
                if lk.get("pause") is not None and int(lk["pause"]) != int(self.rows[i]["pause_before_ms"]):
                    e["pause"] = lk["pause"]
                t = self.takes_of(e).get(lk.get("take")) or {}
                if t.get("src") == "draft" and t.get("sha") and self.wavs[i].exists():
                    try:                                       # копия черновой записи и сама запись — одно и то же
                        if hashlib.sha1(self.wavs[i].read_bytes()).hexdigest()[:12] == t["sha"]:
                            e.pop("take", None)
                    except OSError:
                        pass
                k += 1
            if k:
                self.undo.clear()
                self.save(label, {"count": k})
            return k

    def part_range(self, k: int):
        p = self.open_part(k)
        return list(range(p["a"], p["b"] + 1))

    def lock_part(self, k: int) -> dict:
        idxs = self.part_range(k)
        busy = [j for j in self.jobs.values() if j["status"] in ("queued", "running") and self.idx_of.get(j["uid"]) in set(idxs)]
        if busy:
            raise Oops("в этой части ещё озвучиваются дубли — дождитесь их")
        self.lock_segs(idxs, f"закреплена часть {k}")
        with self.lock:
            self.store["parts"][k - 1]["locked"] = now()
            self.save(f"часть {k}: отметка «закреплена»")
        self.write_fixed()
        try:                                                 # закреплённая часть — веха: точка возврата на всякий случай
            self.project_save(note=f"часть {k} закреплена", kind="auto")
        except Exception as e:
            log(f"точка возврата после закрепления части {k} не записана: {type(e).__name__}: {e}", "warn")
        try:
            return self.export_part(k)
        except Exception as e:
            if not isinstance(e, Oops):                      # диск, права, ffmpeg — подробности в studio.log
                log(f"готовый файл части {k} не собран:\n" + traceback.format_exc(), "err")
            why = str(e) if isinstance(e, Oops) else f"{type(e).__name__}: {e}"
            raise Oops(f"часть {k} закреплена, правки сохранены, но готовый файл не собран: {why[:300]}. Устраните причину и "
                       f"соберите файл ещё раз: «Части и главы» → «Собрать заново».")

    def unlock_part(self, k: int):
        self.auto_guard("снять закрепление")
        idxs = self.part_range(k)
        self.unlock_segs(idxs, f"снято закрепление части {k}")
        with self.lock:
            self.store["parts"][k - 1]["locked"] = None
            self.store["parts"][k - 1].pop("auto", None)       # часть снова в руках автора
            self.save(f"часть {k}: закрепление снято")
        self.write_fixed()

    # ------------------------------------------------------------------ текст книги в txt
    WORD_X = re.compile(r"[A-Za-zА-Яа-яЁё0-9]+")

    def _patch_words(self, orig: str, base: str, new: str, auto: str) -> str:
        """Слова, которые автор изменил в техническом тексте, перенести в текст книги, не трогая её знаки, кавычки и дефисы.
        Замены правил чтения (auto) текстом книги не считаются. Если слова книги и записи не сходятся один к одному
        (числа, сокращения) — берётся текст автора целиком, без знаков ударения."""
        import difflib
        plain = lambda s: (s or "").replace(ACUTE, "")
        key = lambda w: w.lower().replace("ё", "е")
        ow = list(self.WORD_X.finditer(orig))
        bw, ew, aw = (self.WORD_X.findall(plain(x)) for x in (base, new, auto))
        bk = [key(w) for w in bw]
        if [key(m.group(0)) for m in ow] != bk:
            return re.sub(r"\s+", " ", plain(new)).strip()
        def ops(words):
            sm = difflib.SequenceMatcher(None, bk, [key(w) for w in words], autojunk=False)
            return [(i1, i2, tuple(words[j1:j2])) for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal"]
        rule_ops = {(i1, i2, tuple(key(w) for w in ws)) for i1, i2, ws in ops(aw)}
        res, pos = [], 0
        for i1, i2, ws in ops(ew):
            if (i1, i2, tuple(key(w) for w in ws)) in rule_ops:
                continue                                   # так читает правило чтения — в тексте книги слово прежнее
            put = " ".join(ws)
            if i1 < i2:                                    # замена или удаление слов книги
                a, b = ow[i1].start(), ow[i2 - 1].end()
                if put and orig[a].isupper() and put[0].islower():
                    put = put[0].upper() + put[1:]
                res.append(orig[pos:a])
                res.append(put)
                pos = b
            elif i1 < len(ow):                             # вставка перед словом книги
                a = ow[i1].start()
                res.append(orig[pos:a])
                res.append(put + " ")
                pos = a
            else:                                          # вставка после последнего слова (перед конечным знаком)
                a = ow[-1].end() if ow else len(orig)
                res.append(orig[pos:a])
                res.append(" " + put)
                pos = a
        res.append(orig[pos:])
        s = re.sub(r"[ \t]{2,}", " ", "".join(res))
        return re.sub(r"\s+([,.;:!?…])", r"\1", s).strip()

    def export_text(self, upto: str = "", changes: bool = True) -> dict:
        """Текст книги в txt — таким, каким он стал в студии: слова, изменённые автором, вставленные фразы; ударения и
        правила чтения в текст не идут. upto — метка фразы: текст от начала книги до неё включительно; пусто — вся книга.
        Рядом кладётся список изменённых мест («в книге» / «в студии»), чтобы перенести правки в рукопись."""
        self.load_rows()
        with self.lock:
            last, upto_id = len(self.rows) - 1, None
            if upto:
                i = self.idx_of.get(upto)
                if i is None:
                    raise Oops("этой фразы больше нет в книге — обновите окно")
                last = self.chain(self._abs.get(i, i))[-1]
                upto_id = self.rows[self._abs.get(i, i)]["id"]
            ridx = self.rule_index()
            lines, buf, cur, log_, ins_log = [], [], None, [], []
            n_ph = n_changed = n_ins = n_join = 0

            def flush():
                if buf:
                    lines.append(" ".join(x for x in buf if x))
                    buf.clear()

            for i in range(0, last + 1):
                if i in self._abs:
                    continue
                r = self.rows[i]
                e = self.store["segs"].get(self.uids[i]) or {}
                orig = self.row_text(i).strip()
                text = orig
                n_ph += 1
                n_join += bool(e.get("join"))
                if r.get("ins"):
                    n_ins += 1
                    ins_log.append((r, orig))
                lk = e.get("locked") or {}
                if e.get("tech") is not None or lk.get("tech"):
                    ef = self.eff(i, ridx)
                    base = self.base_tech_all(i)
                    if skel(ef["tech"]) != skel(base):         # изменены слова, а не только ударения
                        new = self._patch_words(orig, base, ef["tech"], self.auto_tech(i, ridx)[0])
                        if skel(new) != skel(orig):
                            text = new
                            n_changed += 1
                            log_.append((r, orig, new))
                if r["kind"] == "head":
                    flush()
                    if lines and lines[-1] != "":
                        lines.append("")
                    lines.append(text)
                    lines.append("")
                    cur = None
                    continue
                k = (r["chapter"], r["para"])
                if k != cur:
                    flush()
                    cur = k
                buf.append(text)
            flush()
            while lines and lines[-1] == "":
                lines.pop()
            title = self.title or self.root.name
            stamp = time.strftime("%Y-%m-%d_%H-%M")
            what = f"до фразы № {upto_id}" if upto_id is not None else "вся книга"
            d = self.root / "gotovo" / "tekst"
            d.mkdir(parents=True, exist_ok=True)
            f = d / (safe_file(f"{title} — текст, {what}") + f" {stamp}.txt")
            write_atomic(f, ("\r\n".join(lines) + "\r\n").encode("utf-8-sig"))
            cf = None
            if changes and (log_ or ins_log):
                c = [f"Изменения текста, сделанные в «Нокс Студии»: «{title}», {what}. {now()}",
                     f"Фраз с изменёнными словами: {len(log_)}; вставленных фраз: {len(ins_log)}. "
                     "Ударения и правила чтения сюда не входят: текста книги они не меняют.", ""]
                for r, a, b in log_:
                    c += [f"№ {r['id']} (глава {r['chapter']}" + (f" «{r['ch_title']}»" if r.get("ch_title") else "") + ")",
                          "  в книге:  " + a, "  в студии: " + b, ""]
                for r, a in ins_log:
                    c += [f"вставка № {r['id']} (глава {r['chapter']}): " + a, ""]
                cf = f.with_name(f.stem + " — изменения.txt")
                write_atomic(cf, ("\r\n".join(c) + "\r\n").encode("utf-8-sig"))
        disk = None
        try:
            disk = self._to_disk(f, "tekst")
            if cf is not None:
                self._to_disk(cf, "tekst")
        except Exception:
            pass
        log(f"текст книги выгружен ({what}): {f.name} — абзацев {sum(1 for x in lines if x)}, фраз {n_ph}; изменённых слов "
            f"во фразах: {n_changed}, вставок: {n_ins}" + (f"; список изменений: {cf.name}" if cf else ""), "ok", "правка")
        return dict(file=str(f), name=f.name, what=what, paras=sum(1 for x in lines if x), phrases=n_ph, changed=n_changed,
                    inserts=n_ins, joins=n_join, changes_file=str(cf) if cf else None, disk=disk)

    # ------------------------------------------------------------------ готовые части и главы
    def disk_dir(self, make=True):
        """Папка книги на Яндекс Диске (как у go), подпапка gotovo; None — Диска нет. Когда книгу ведёт «Вся книга», подпапка
        своя — с отметкой времени первого запуска (kniga_ГГГГ-ММ-ДД_ЧЧ-ММ): весь набор готовых файлов лежит в одном месте
        и не смешивается с прежними пробами."""
        st = getattr(n, "SETTINGS", None) or {}
        name = str(st.get("dest") or "")
        if self.root.name.lower() == "kniga1a" and name in ("", "Posoh_izdanie2"):
            name = "Posoh_izdanie3"
        if re.match(r"^[A-Za-z]:\\", name):
            base = Path(name)
        else:
            yd = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "YandexDisk"
            if not yd.is_dir():
                return None
            base = yd / (name or self.root.name)
        d = base / (str((getattr(self, "auto", None) or {}).get("stamp") or "") or "gotovo")
        if make:
            try:
                d.mkdir(parents=True, exist_ok=True)
            except OSError:
                return None
        return str(d)

    def _to_disk(self, src: Path, sub: str):
        d = self.disk_dir()
        if not d:
            return None
        try:
            n.copy_to(Path(d) / sub, src)
            write_atomic(Path(d) / "edits_backup.json", (self.dir / "edits.json").read_bytes())   # правки — тоже в облако
            return str(Path(d) / sub / src.name)
        except Exception as e:
            log(f"на Диск не скопировано ({src.name}): {e}")
            return None

    def _encode(self, audio, sr, chapters, out: Path, title: str, bitrate=None) -> float:
        try:
            return n.encode_m4b(audio, sr, [(c[0], c[1]) for c in chapters], out, title, self.author,
                                bitrate or self.bitrate, speed=self.speed)
        except SystemExit as e:
            raise Oops(f"файл не собран: {e}")
        except subprocess.CalledProcessError as e:
            raise Oops(f"ffmpeg не собрал файл (код {e.returncode})")

    def _times_tsv(self, meta: dict) -> str:
        sp = self.speed or 1.0
        out = []
        for uid, t0, t1 in meta["segs"]:
            i = self.idx_of[uid]
            if i in self._abs:
                continue
            r, ef, sc = self.rows[i], self.eff(i), self.src(i)
            stem = sc["path"].stem if sc["path"] is not None else "-"
            out.append(f"{mmss(t0 / sp)}\t{mmss(t1 / sp)}\t{r['id']}\t{ef['role']}\t{stem}\t{self.row_text(i)[:120]}"
                       f"\t{t0 / sp:.3f}\t{t1 / sp:.3f}\n")
        return "".join(out)

    def _tech_txt(self, meta: dict, name: str) -> str:
        """Технический текст собранного файла: время, кто говорит, что именно прочитано (с ударениями), пометки."""
        sp = self.speed or 1.0
        out = [f"{self.title} — {name}. Технический текст: как это прочитано в готовом файле.",
               "Пометки: [закреплено], [правка] — текст или запись изменены вручную, [правило] — сработало правило чтения.", ""]
        para = None
        for uid, t0, t1 in meta["segs"]:
            i = self.idx_of[uid]
            if i in self._abs:
                continue
            r, ef, e = self.rows[i], self.eff(i), self.store["segs"].get(uid) or {}
            if para is not None and r["para"] != para:
                out.append("")
            para = r["para"]
            tags = (["закреплено"] if ef["locked"] else []) + (["правило"] if ef["hits"] else [])
            if ef["tech_src"] == "manual" or (ef["locked"] and norm_t(ef["tech"]) != norm_t(self.base_tech_all(i))) \
                    or ef["role"] != self.base_roles[i] or abs(ef["tempo"] - 1.0) >= 0.005:
                tags.append("правка")
            if e.get("join"):
                tags.append(f"соединено фраз: {len(e['join']) + 1}")
            who = "Рассказчик" if ef["role"] == "narrator" else ef["role"]
            out.append(f"{mmss(t0 / sp)}  {who}: {ef['tech']}" + (f"   [{', '.join(tags)}]" if tags else ""))
        return "\n".join(out) + "\n"

    def export_part(self, k: int) -> dict:
        p = self.open_part(k)
        name = f"{self.title} — часть {k:02d} ({self.rows[p['a']]['ch_title']})"
        with self.rlock, Op(self, f"готовая часть {k}") as op:
            meta, audio = self.timeline(p["a"], p["b"], True, op)
            out = self.final_parts / f"part_{k:02d}.{self.fmt}"
            op.stage(f"запись {self.fmt} с выравниванием громкости")
            total = self._encode(audio, meta["sr"], meta["chapters"], out, name)
            write_atomic(out.with_name(f"part_{k:02d}.times.tsv"), self._times_tsv(meta))
            write_atomic(out.with_name(f"part_{k:02d}.tech.txt"), self._tech_txt(meta, f"часть {k:02d}"))
            meta["ref"] = round(active_rms(audio), 5)          # та же опорная громкость голоса, что в предпросмотре
            mf = self.render_dir / f"part_{k:02d}.json"
            if (read_json(mf, None) or {}).get("ver") != meta["ver"]:
                # часть могли ни разу не открывать в окне (её принял автомат «вся книга»): длина и версия — для списка частей;
                # сам предпросмотр соберётся, когда часть откроют
                write_atomic(mf, jd(meta))
            op.stage("сведение со звуками и музыкой")
            vs = self._export_mixes(p["a"], p["b"], meta, out, "chasti", name, audio=audio, sr=meta["sr"], op=op)
            mix = vs.get("all")
            op.stage("копия на Яндекс Диск")
            disk = self._to_disk(out, "chasti")
            op.note = self._export_note(out, total, mix, disk, vs)
        with self.lock:
            ex = dict(file=str(out), dur=round(total, 1), at=now(), ver=meta["ver"], disk=disk, mix=mix, vars=vs,
                      csig=self.cues_sig(p["a"], p["b"]), kv=2)
            self.store["parts"][k - 1]["export"] = ex
            self.save(f"собрана готовая часть {k}", {"file": str(out), "dur": ex["dur"]})
        return ex

    def ffmpeg(self) -> str:
        return (n.find_ffmpeg() if hasattr(n, "find_ffmpeg") else None) or shutil.which("ffmpeg") or "ffmpeg"

    def _export_note(self, out: Path, total: float, mix, disk, vs=None) -> str:
        try:
            size = f", {out.stat().st_size / 1048576:.1f} МБ"
        except OSError:
            size = ""
        return (f"{out.name} ({mmss(total)}{size})" + (f"; со звуком — подмешано {mix['count']}" if mix else "")
                + (f"; версий со звуком: {len(vs)}" if vs else "")
                + ("; копия на Яндекс Диске" if disk else "; Яндекс Диск не найден — копии в облаке нет"))

    def _voice_pcm(self, audio, sr):
        """Голос после ТОГО ЖЕ выравнивания громкости (и ускорения), что в готовом файле, но до сжатия: в файле «со
        звуком» голос кодируется один раз, а не второй поверх mp3. None — не вышло (тогда берётся готовый файл)."""
        import soundfile as sf
        tmp = self.cache_dir / f"voice_{os.getpid()}_{threading.get_ident()}.wav"
        try:
            sf.write(str(tmp), audio, sr, format="WAV", subtype="PCM_16")
            af = getattr(n, "LOUDNORM_AF", "loudnorm=I=-18:TP=-2:LRA=11")
            sp = self.speed or 1.0
            if sp != 1.0:
                af = n.atempo_chain(sp) + "," + af
            r = subprocess.run([self.ffmpeg(), "-v", "error", "-i", str(tmp), "-af", af, "-f", "f32le", "-ac", "1",
                                "-ar", str(SND_SR), "-"], capture_output=True)
        except Exception as e:
            log(f"голос для сведения взят из готового файла ({type(e).__name__}: {e})")
            return None
        finally:
            tmp.unlink(missing_ok=True)
        if r.returncode != 0 or len(r.stdout) < 8:
            return None
        return np.frombuffer(r.stdout[:len(r.stdout) // 4 * 4], dtype="<f4").astype("float32")

    def var_dir(self, where: str, vid: str) -> Path:
        """Папка версии готовых файлов: where — chasti | glavy."""
        return self.root / "gotovo" / (where + "_" + next(x[2] for x in MIX_VARS if x[0] == vid))

    def vars_on(self) -> list:
        vs = self.store["mix"].get("vars") or {}
        return [x for x in MIX_VARS if vs.get(x[0], True)]

    def _export_mixes(self, a: int, b: int, meta: dict, voice_file: Path, where: str, title: str, audio=None, sr=None, op=None) -> dict:
        """Версии отрезка со звуками — отдельными файлами рядом с голосовым, каждая в своей папке (MIX_VARS):
        с эффектами; с эффектами и окружением; с эффектами и музыкой; всё вместе. -> {ключ: {file, count, sig, disk, copy}}.
        Звуков в отрезке нет вовсе (или все дорожки выключены) — версий нет, прежние файлы убираются. Звуки есть, но в
        какой-то версии здесь ничего не звучит (в части нет музыки) — в её папку кладётся голосовой файл: версия книги
        должна быть полной, без дыр. Голос для сведения готовится один раз на все версии."""
        out = {}

        def drop(vid):
            f = self.var_dir(where, vid) / voice_file.name
            if f.exists():
                try:
                    f.unlink()
                except OSError:
                    pass
        if not self.mix_sig(a, b, meta):
            for v in MIX_VARS:
                drop(v[0])
            return out
        voice = None
        for vid, kinds, suf, name in self.vars_on():
            if op is not None:
                op.stage("версия «" + name + "»")
            dst = self.var_dir(where, vid) / voice_file.name
            sig = self.mix_sig(a, b, meta, kinds)
            if not sig:                                      # в этой версии здесь ничего не звучит — голосовой файл как есть
                dst.parent.mkdir(parents=True, exist_ok=True)
                tmp = dst.with_name(dst.stem + ".tmp" + dst.suffix)
                shutil.copyfile(voice_file, tmp)
                os.replace(tmp, dst)
                out[vid] = dict(file=str(dst), count=0, sig="", copy=True, disk=self._to_disk(dst, f"{where}_{suf}"))
                continue
            if voice is None:
                voice = self._mix_base(voice_file, audio, sr)
            res = self._export_mix(a, b, meta, voice_file, dst, f"{title} ({'со звуком' if vid == 'all' else name})",
                                   f"{where}_{suf}", voice, kinds)
            if res:
                out[vid] = dict(res, sig=sig)
        for v in MIX_VARS:                                   # выключенные версии: прежний файл этого отрезка устарел бы молча
            if v[0] not in out:
                drop(v[0])
        return out

    def _mix_base(self, voice_file: Path, audio=None, sr=None):
        """Голос для сведения (после выравнивания громкости, до сжатия); не вышло — из готового голосового файла."""
        voice = self._voice_pcm(audio, sr) if audio is not None and sr else None
        if voice is None:                                    # запасной путь: готовый голосовой файл (второе сжатие)
            r = subprocess.run([self.ffmpeg(), "-v", "error", "-i", str(voice_file), "-f", "f32le", "-ac", "1", "-ar",
                                str(SND_SR), "-"], capture_output=True)
            if r.returncode != 0 or len(r.stdout) < 8:
                raise Oops("готовый голосовой файл не читается: " + r.stderr.decode("utf-8", "replace").strip()[:160])
            voice = np.frombuffer(r.stdout[:len(r.stdout) // 4 * 4], dtype="<f4").astype("float32")
        return voice

    def _export_mix(self, a: int, b: int, meta: dict, voice_file: Path, out: Path, title: str, sub: str, voice, kinds=None):
        """Отрезок со звуками выбранных дорожек — отдельным файлом рядом с голосовым.
        Порядок — как у сведения конвейера: берётся ГОТОВЫЙ голос (уже после выравнивания громкости), к нему
        подмешиваются звуки относительно его громкости, и результат кодируется без повторного выравнивания. Поэтому
        голос во всех версиях один и тот же, а музыка в паузах не «всплывает» (выравнивание громкости на готовой смеси
        подтягивало бы тихие места)."""
        ff = self.ffmpeg()
        mixed, cnt = self.mix_voice(a, b, meta, voice, ref=active_rms(voice), sp=self.speed or 1.0, kinds=kinds)
        if not cnt:
            return None
        import soundfile as sf
        out.parent.mkdir(parents=True, exist_ok=True)
        wav = out.with_suffix(".mix.wav")
        tmp = out.with_name(out.stem + ".tmp" + out.suffix)
        sf.write(str(wav), mixed, SND_SR, subtype="PCM_16")
        kb = int(re.sub(r"\D", "", str(self.bitrate)) or 96)     # музыке и шумам 96k мало — как и у сведения конвейера
        cmd = [ff, "-y", "-hide_banner", "-loglevel", "error", "-i", str(wav), "-i", str(voice_file), "-map", "0:a",
               "-map_metadata", "1", "-map_chapters", "1", "-metadata", f"title={title}", "-ac", "1",
               "-b:a", f"{max(kb, 128)}k"]
        cmd += ["-c:a", "libmp3lame", "-id3v2_version", "3", "-f", "mp3"] if out.suffix.lower() == ".mp3" else \
            ["-c:a", "aac", "-movflags", "+faststart", "-f", "ipod"]
        try:
            rr = subprocess.run(cmd + [str(tmp)], capture_output=True)
        finally:
            wav.unlink(missing_ok=True)
        if rr.returncode != 0 or not tmp.exists():
            tmp.unlink(missing_ok=True)
            raise Oops("ffmpeg не собрал файл со звуком: " + rr.stderr.decode("utf-8", "replace").strip()[:160])
        os.replace(tmp, out)
        return dict(file=str(out), count=cnt, sig=self.mix_sig(a, b, meta, kinds), disk=self._to_disk(out, sub))

    def chapter_range(self, c: int):
        if c not in self.chapters:
            raise Oops(f"главы {c} нет")
        a, b, title = self.chapters[c]
        need = [p["n"] for p in self.plan() if p["b"] >= a and p["a"] <= b]
        if not need or self.plan()[need[-1] - 1]["b"] < b:
            raise Oops("глава ещё не озвучена целиком — её части пока не собраны конвейером")
        self.open_part(max(need))
        return a, b, title

    def lock_chapter(self, c: int) -> dict:
        a, b, _ = self.chapter_range(c)
        self.lock_segs(list(range(a, b + 1)), f"закреплена глава {c}")
        return self.export_chapter(c)

    def export_chapter(self, c: int) -> dict:
        a, b, title = self.chapter_range(c)
        with self.rlock, Op(self, f"глава {c} отдельным файлом") as op:
            meta, audio = self.timeline(a, b, True, op)
            out = self.final_ch / f"{c:02d} {safe_file(title)}.{self.fmt}"
            op.stage(f"запись {self.fmt} с выравниванием громкости")
            total = self._encode(audio, meta["sr"], meta["chapters"], out, f"{self.title} — {title}")
            write_atomic(out.with_suffix(".tech.txt"), self._tech_txt(meta, title))
            meta["ref"] = round(active_rms(audio), 5)
            op.stage("сведение со звуками и музыкой")
            vs = self._export_mixes(a, b, meta, out, "glavy", f"{self.title} — {title}", audio=audio, sr=meta["sr"], op=op)
            mix = vs.get("all")
            op.stage("копия на Яндекс Диск")
            disk = self._to_disk(out, "glavy")
            op.note = self._export_note(out, total, mix, disk, vs)
        with self.lock:
            lk = sum(1 for i in range(a, b + 1) if (self.store["segs"].get(self.uids[i]) or {}).get("locked"))
            ex = dict(file=str(out), dur=round(total, 1), at=now(), ver=meta["ver"], disk=disk, locked=lk, count=b - a + 1,
                      mix=mix, vars=vs, csig=self.cues_sig(a, b))
            self.store["chapters"].setdefault(str(c), {})["export"] = ex
            self.save(f"собрана глава {c}", {"file": str(out)})
        return ex

    # ================================================================== ЗВУКИ, ФОН И МУЗЫКА (дорожки на ленте)
    # Заглушка (cue) — место на ленте, привязанное к фразе: {id, kind sfx|amb|mus, label, uid, at "s"|"e", off c,
    # len c | e_uid/e_at/e_off (конец по фразе), est c (оценка длины действия), file (звук из библиотеки) | нет,
    # vol, on, loop, fin, fout, foff, src auto|manual, why, conf}. Без файла заглушка молчит — это пометка «сюда звук».
    # Сведение — как у конвейера (zvuk): всё в моно 24 кГц, звук приводится к громкости голоса и умножается на
    # громкость дорожки (звуки 0.16, фон 0.12, музыка 0.10 от голоса) и самой заглушки. Что слышно в окне — то и в файле.
    def cue_labels(self) -> dict:
        try:
            import nox_cues
            return nox_cues.labels()
        except Exception as e:
            log(f"распознаватель звуков не загрузился: {e}")
            return {"sfx": [], "amb": [], "mus": []}

    def kit_dir(self):
        env = os.environ.get("NOX_ZVUK")
        for d in ([Path(env)] if env else []) + [self.root / "zvuk", Path(__file__).resolve().parent.parent / "zvuk"]:
            if d.is_dir():
                return d
        return None

    def kit_home(self) -> Path:
        """Общая папка звуковых комплектов: существующая, а если её ещё нет — где ей быть (рядом со студией)."""
        env = os.environ.get("NOX_ZVUK")
        return self.kit_dir() or (Path(env) if env else Path(__file__).resolve().parent.parent / "zvuk")

    def zvuk(self):
        """Сведение конвейера (zvuk_k1a.py из папки книги): у него общие со студией выбор комплекта и расчёт партитуры."""
        if self._zv is None:
            self._zv = False
            if (self.root / "zvuk_k1a.py").exists():
                try:
                    import importlib
                    self._zv = importlib.import_module("zvuk_k1a")
                except Exception as e:
                    log(f"zvuk_k1a.py не загрузился ({type(e).__name__}: {e}) — партитура книги недоступна", "warn", "звуки")
        return self._zv or None

    def kit_state(self, fresh=False) -> dict:
        """Действующий комплект (лучший из распакованных: новее поколением, затем архивом), остальные распакованные и
        архив из Загрузок, который лучше их всех и ждёт распаковки. Считается не чаще раза в минуту."""
        tm = time.time()
        if not fresh and self._kst is not None and tm - self._kst[0] < 60:
            return self._kst[1]
        st = dict(kit=None, zip=None, others=[], gen=0)
        z, kd = self.zvuk(), self.kit_dir()
        try:
            if z is not None and hasattr(z, "kit_pick"):
                pk = z.kit_pick(self.kit_home())
                st.update(kit=pk["kit"], zip=pk["zip"], others=pk["others"],
                          gen=z.kit_info(pk["kit"])["gen"] if pk["kit"] is not None else 0)
            elif kd is not None:                               # прежний zvuk_k1a (или его нет): первый комплект с партитурой
                cj = next((x for x in sorted(kd.rglob("cues_book1.json")) if (x.parent / "music").is_dir()), None)
                st["kit"] = cj.parent if cj else None
        except Exception as e:
            log(f"звуковой комплект не определён: {type(e).__name__}: {e}", "warn", "звуки")
        self._kst = (tm, st)
        return st

    def kit_active(self):
        return self.kit_state()["kit"]

    def kit_prepare(self):
        """В Загрузках лежит архив звукового комплекта лучше всех распакованных — распаковать его (фоном, разово).
        Прежний комплект остаётся на месте: на его файлы ссылаются звуки, уже стоящие на дорожках."""
        z = self.zvuk()
        if z is None or not hasattr(z, "kit_unpack") or not self._kit_lock.acquire(blocking=False):
            return
        try:
            self._kit_unpack(z)
        finally:
            self._kit_lock.release()

    def _kit_unpack(self, z):
        zp = self.kit_state(True)["zip"]
        if zp is None:
            self.kit_job = None
            return
        job = dict(name=zp["name"], pct=0, error=None)
        self.kit_job = job
        log(f"в Загрузках новый звуковой комплект {zp['name']} — распаковываю в {self.kit_home()} "
            f"(разово, ~{zp['size'] / 1e6:.0f} МБ)", "info", "звуки")
        try:
            d = z.kit_unpack(zp, self.kit_home(), progress=lambda a, b: job.__setitem__("pct", int(100 * a / max(1, b))))
            self.kit_job = None
            log(f"звуковой комплект распакован: {d.name}. Библиотека звуков уже берёт его; чтобы и звуки партитуры на "
                "дорожках взялись из него — кнопка «Обновить партитуру книги» (ваши правки в них сохранятся)", "ok", "звуки")
        except Exception as e:
            job["error"] = f"{type(e).__name__}: {e}"
            log(f"звуковой комплект {zp['name']} не распаковался: {e} — работаю с прежним", "warn", "звуки")
        self._kst, self._cat = None, None

    def kit_tables(self, kit: Path) -> dict:
        """Таблицы комплекта: русские названия музыки, фонов и звуков игры, длительности, реплики игры."""
        def table(name):
            try:
                with open(kit / name, encoding="utf-8-sig", newline="") as f:
                    return list(csv.DictReader(f, delimiter=";"))
            except (OSError, csv.Error, UnicodeError):
                return []

        def num(v):
            try:
                return round(float(str(v).replace(",", ".")), 2)
            except (TypeError, ValueError):
                return None
        sfx = {r.get("логическое_имя") or "": r for r in table("sfx_dictionary.csv")}
        raw = {}
        for nm, r in sfx.items():
            for f in (r.get("файлы") or "").split():
                raw.setdefault(f.lower(), nm)
        return dict(mus={(r.get("трек") or "").lower(): r for r in table("music_tracks.csv")},
                    amb={(r.get("петля") or "").lower(): r for r in table("ambient_loops.csv")}, sfx=sfx, raw=raw,
                    dur={(r.get("путь") or "").lower(): num(r.get("длит_с")) for r in table("sfx/sfx_index.csv")},
                    dlg={Path(r.get("файл_в_архиве") or "").stem.lower(): r for r in table("озвучка_диалогов_игры.csv")},
                    num=num)

    def kit_catalog(self) -> list:
        """Каталог действующего комплекта для библиотеки звуков: у каждого файла — понятное название по-русски (из
        таблиц комплекта), вид (музыка, фон, звук, вставка игры), длительность и строка для поиска. Разобранные звуки
        (sfx) идут первыми; сырой файл игры (sfx_all) показывается, только если его нет среди разобранных."""
        kit, base = self.kit_active(), self.kit_dir()
        if kit is None or base is None:
            return []
        if self._cat is not None and self._cat[0] == str(kit):
            return self._cat[1]
        T = self.kit_tables(kit)
        out, have, seen = [], set(), set()

        def add(f, kind, title, sub, extra="", dur=None, rank=0):
            try:
                rel = f.resolve().relative_to(base.resolve()).as_posix()
            except (ValueError, OSError):
                return
            if rel in have:
                return
            have.add(rel)
            hay = s_norm(" ".join((title or "", f.stem, sub or "", extra or "")))
            out.append(dict(file="kit:" + rel, name=f.name, title=(title or f.stem)[:120], sub=sub, kind=kind, dur=dur,
                            lib="kit", rank=rank, hay=hay, words=frozenset(hay.split())))

        def files(d, deep=False):
            if not d.is_dir():
                return []
            it = d.rglob("*") if deep else d.iterdir()
            return sorted((f for f in it if f.suffix.lower() in SND_EXT and f.is_file()), key=lambda x: str(x).lower())
        for f in files(kit / "music"):
            r = T["mus"].get(f.stem.lower()) or {}
            add(f, "mus", f"{f.stem} — {r['что_это']}" if r.get("что_это") else f.stem, "музыка игры",
                r.get("где_звучит_у_мага") or "", T["num"](r.get("длит_с")))
        for f in files(kit / "beds"):
            r = T["amb"].get(f.stem.lower()) or {}
            add(f, "amb", r.get("что_это") or f.stem, "фон местности (петля)", r.get("объекты_на_картах") or "")
        for f in files(kit / "sfx", deep=True):
            nm, _, rest = f.stem.partition("__")
            r = T["sfx"].get(nm) or {}
            seen.add((rest or nm).lower())
            rp = f.relative_to(kit / "sfx")
            add(f, "amb" if rp.parts[0] == "ambient" else "sfx", r.get("что_это") or nm,
                "звук игры" + (": " + " / ".join(KIT_CAT.get(x, x) for x in rp.parts[:-1]) if len(rp.parts) > 1 else ""), nm,
                T["dur"].get(rp.as_posix().lower()))
        for f in files(kit / "sfx_all"):
            if f.stem.lower() in seen:
                continue
            nm = T["raw"].get(f.stem.lower()) or ""
            r = T["sfx"].get(nm) or {}
            add(f, "sfx", r.get("что_это") or f.stem, "звук игры (файл без разбора)", nm, rank=1)
        for f in files(kit / "dialog"):
            r = T["dlg"].get(f.stem.lower()) or {}
            who, txt = (r.get("кто_говорит") or "").strip(), (r.get("русский_текст_реплики") or "").strip()
            add(f, "dlg", (who + (": " + txt if txt else "")) or f.stem, "реплика игры (английская речь)",
                (r.get("место_в_игре") or ""), T["num"](r.get("длит_с")), rank=2)
        for f in files(kit / "movies_audio"):
            add(f, "dlg", f.stem, "звук ролика игры", rank=2)
        for f in files(kit, deep=True):                        # всё прочее звуковое в комплекте — по имени файла
            add(f, "sfx", f.stem, f.parent.relative_to(kit).as_posix(), rank=3)
        self._cat = (str(kit), out, {x["file"]: x["title"] for x in out})
        return out

    def sound_index(self) -> dict:
        return read_json(self.sounds_dir / "index.json", None) or {}

    def sound_list(self, q: str = "", kind: str = "") -> dict:
        """Библиотека звуков: свои файлы книги и действующий комплект. Поиск — по русскому названию («дверь», «волк»,
        «шаги»), имени звука в игре и имени файла; слова — в любом порядке, ё = е, «двери» находит «дверь»; запрос,
        набранный не в той раскладке, тоже находится. kind (sfx/amb/mus) — что сейчас подбирают: такое идёт первым."""
        idx, my = self.sound_index(), []
        if self.sounds_dir.is_dir():
            for f in sorted(self.sounds_dir.iterdir(), key=lambda x: x.name.lower()):
                if f.suffix.lower() in SND_EXT:
                    m = idx.get(f.name) or {}
                    nm = m.get("name") or f.name
                    hay = s_norm(nm + " " + f.name)
                    my.append(dict(file=f.name, name=nm, title=nm, sub="мой файл", kind="", dur=m.get("dur"), lib="my",
                                   rank=-1, hay=hay, words=frozenset(hay.split())))
        kit = self.kit_active()
        allx = my + self.kit_catalog()
        pref = lambda x: 0 if (x["lib"] == "my" or not kind or x["kind"] == kind) else 1

        def find(text):
            toks = s_norm(text).split()
            if not toks:
                return None
            got = [(s_score(toks, x["words"], x["hay"]), x) for x in allx]
            return [(sc, x) for sc, x in got if sc > 0]
        hits, fixed = find(q), ""
        if hits is not None and not hits:                      # ничего: может, запрос набран не в той раскладке
            for alt in (str(q).lower().translate(_EN2RU), str(q).lower().translate(_RU2EN)):
                h2 = find(alt) if alt != str(q).lower() else None
                if h2:
                    hits, fixed = h2, alt
                    break
        partial = False
        if hits is not None and not hits and len(s_norm(q).split()) > 1:   # все слова разом не нашлись — показать похожее
            got = []
            for x in allx:
                sc = sum(s_score([tk], x["words"], x["hay"]) for tk in s_norm(q).split())
                if sc > 0:
                    got.append((sc, x))
            hits, partial = got, bool(got)
        if hits is None:
            res = sorted(allx, key=lambda x: (x["rank"], pref(x)))        # сортировка устойчивая: внутри — порядок каталога
        else:
            res = [x for _, x in sorted(hits, key=lambda p: (-p[0], pref(p[1]), p[1]["rank"]))]
        view = [{k: v for k, v in x.items() if k not in ("hay", "words", "rank")} for x in res[:300]]
        return dict(total=len(res), sounds=view, kit=str(kit) if kit else None, fixed=fixed, partial=partial, all=len(allx))

    def sound_path(self, ref):
        """Ссылка на звук -> файл: имя в библиотеке книги или «kit:путь» в общем комплекте. Наружу из этих папок нельзя."""
        if not ref or not isinstance(ref, str):
            return None
        if ref.startswith("kit:"):
            kit = self.kit_dir()
            if kit is None:
                return None
            p = (kit / ref[4:]).resolve()
            if kit.resolve() not in p.parents:
                return None
        else:
            if "/" in ref or "\\" in ref or ref.startswith(".") or ":" in ref:
                return None
            p = self.sounds_dir / ref
        return p if p.is_file() and p.suffix.lower() in SND_EXT else None

    def sound_name(self, ref) -> str:
        if not ref:
            return ""
        if ref.startswith("kit:"):
            return ref[4:].rsplit("/", 1)[-1]
        return (self.sound_index().get(ref) or {}).get("name") or ref

    def sound_title(self, ref) -> str:
        """Понятное название звука комплекта (по-русски, из его таблиц); у своих файлов — их имя."""
        if ref and ref.startswith("kit:"):
            self.kit_catalog()
            return (self._cat[2] if self._cat is not None else {}).get(ref) or self.sound_name(ref)
        return self.sound_name(ref)

    def sound_add(self, name: str, data: bytes) -> dict:
        """Файл с компьютера автора -> библиотека звуков книги (копия: исходный файл можно потом двигать и удалять)."""
        name = Path(str(name).replace("\\", "/")).name or "zvuk.wav"
        ext = Path(name).suffix.lower()
        if ext not in SND_EXT:
            raise Oops(f"«{name}» — не звуковой файл. Подходят: wav, mp3, ogg, flac, m4a")
        if not data:
            raise Oops(f"файл «{name}» пустой")
        fname = f"{safe_file(Path(name).stem)[:60].strip() or 'zvuk'}_{hashlib.sha1(data).hexdigest()[:8]}{ext}"
        self.sounds_dir.mkdir(parents=True, exist_ok=True)
        f = self.sounds_dir / fname
        fresh = not f.exists()
        if fresh:
            write_atomic(f, data)
        try:
            x = self.snd(f)
            if len(x) < 240:
                raise Oops("в файле нет звука")
        except Exception as e:
            if fresh:
                f.unlink(missing_ok=True)
            raise Oops(f"«{name}» не читается как звук ({e})")
        with self.lock:
            idx = self.sound_index()
            idx[fname] = dict(name=name, dur=round(len(x) / SND_SR, 2), at=now())
            write_atomic(self.sounds_dir / "index.json", jd(idx))
            self.journal({"why": "звук добавлен в библиотеку", "file": fname, "name": name})
        log(f"звук добавлен в библиотеку книги: {name} ({idx[fname]['dur']} с)", "ok", "звуки")
        return dict(file=fname, name=name, dur=idx[fname]["dur"], lib="my")

    def snd(self, path: Path):
        """Любой звук -> моно float32 24 кГц (как сведение конвейера). Расшифровка кэшируется на диске и в памяти."""
        st = path.stat()
        key = sha(f"{path}|{st.st_size}|{st.st_mtime_ns}", 20)
        x = self._snd.get(key)
        if x is not None:
            return x
        with self.slock:
            return self._snd_load(path, key)

    def _snd_load(self, path: Path, key: str):
        x = self._snd.get(key)
        if x is not None:
            return x
        cp = self.cache_dir / f"snd2_{key}.npy"
        if cp.exists():
            try:
                x = np.load(cp)
            except Exception:
                cp.unlink(missing_ok=True)
        if x is None:
            ff = (n.find_ffmpeg() if hasattr(n, "find_ffmpeg") else None) or shutil.which("ffmpeg") or "ffmpeg"
            r = subprocess.run([ff, "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "1", "-ar", str(SND_SR), "-"],
                               capture_output=True)
            if r.returncode != 0 or len(r.stdout) < 8:
                raise Oops("ffmpeg не прочитал файл: " + r.stderr.decode("utf-8", "replace").strip()[:160])
            x = trim_edges(np.frombuffer(r.stdout[:len(r.stdout) // 4 * 4], dtype="<f4").astype("float32"))
            tmp = self.cache_dir / f"snd2_{key}.tmp.npy"
            np.save(tmp, x)
            os.replace(tmp, cp)
        while self._snd and sum(len(v) for v in self._snd.values()) + len(x) > 90_000_000:    # ~360 МБ в памяти
            self._snd.pop(next(iter(self._snd)))
        self._snd[key] = x
        self._lvl[key] = active_rms(x)
        return x

    def loop_of(self, path: Path, a):
        st = path.stat()
        key = sha(f"{path}|{st.st_size}|{st.st_mtime_ns}", 20)
        c = self._loops.get(key)
        if c is None:
            c = loop_body(a)
            if len(self._loops) > 24:
                self._loops.pop(next(iter(self._loops)))
            self._loops[key] = c
        return c

    def snd_level(self, path: Path) -> float:
        st = path.stat()
        key = sha(f"{path}|{st.st_size}|{st.st_mtime_ns}", 20)
        if key not in self._lvl:
            self.snd(path)
        return self._lvl.get(key, 0.0)

    def cue_ref(self, c: dict):
        """Какой звук играет заглушка: свой файл, иначе общий для её подписи («все скрипы двери — этот файл»)."""
        return c.get("file") or self.store["sound_map"].get(c.get("label") or "")

    def cue_file(self, c: dict):
        return self.sound_path(self.cue_ref(c))

    def book_clock(self) -> dict:
        """Сквозное время книги по картам частей. Устаревшая карта не должна привязаться к повторно
        использованным числовым ID после переразбивки или сдвинуть время следующих частей."""
        with self.lock:
            # Cache by file identity as well as age: a new export must invalidate the clock immediately,
            # not after the old 30-second TTL expires.
            file_sig = []
            for num in range(1, 2000):
                f = n.PARTS / f"part_{num:02d}.times.tsv"
                try:
                    st = f.stat()
                except OSError:
                    break
                file_sig.append((num, st.st_size, st.st_mtime_ns))
            file_sig = tuple(file_sig)
            if (time.time() - self._clock[0] < 30
                    and getattr(self, "_clock_sig", None) == file_sig):
                return self._clock[1]

            ck, off, num = {}, 0.0, 1
            while num < 2000:
                try:
                    lines = (n.PARTS / f"part_{num:02d}.times.tsv").read_text(encoding="utf-8").splitlines()
                except (OSError, UnicodeDecodeError):
                    break
                part_map, last = {}, 0.0
                prev_i, prev_t0 = -1, -1.0
                invalid = False
                for ln in lines:
                    if not ln.strip():
                        continue
                    c = ln.split("\t")
                    if len(c) < 8:
                        invalid = True
                        break
                    try:
                        t0, t1 = float(c[6]), float(c[7])
                    except (TypeError, ValueError):
                        invalid = True
                        break
                    i = self.id2i.get(c[2])
                    if (i is None or not (0 <= i < len(self.rows)) or i <= prev_i
                            or t0 < 0 or t1 < t0 or t0 < prev_t0
                            or t0 != t0 or t1 != t1 or abs(t0) == float("inf") or abs(t1) == float("inf")):
                        invalid = True
                        break
                    # times.tsv carries the source stem, role and text as independent identity checks.
                    # Numeric segment IDs are sequential and may be reused by a newly segmented edition.
                    try:
                        src = self.src(i)
                        expected_stem = src["path"].stem if src.get("path") is not None else "-"
                        expected_text = self.row_text(i)[:120]
                        expected_role = self.eff(i).get("role")
                    except Exception:
                        invalid = True
                        break
                    if c[4] != expected_stem or c[5] != expected_text or c[3] != expected_role:
                        invalid = True
                        break
                    part_map[i] = (t0, t1)
                    prev_i, prev_t0 = i, t0
                    last = max(last, t1)
                # Fail closed for a mixed/old map. Do not use even its matching rows: their local offsets
                # may have shifted, and later parts cannot be placed on a trustworthy global clock.
                if invalid or not part_map:
                    break
                for i, (t0, t1) in part_map.items():
                    ck[i] = (off + t0, off + t1)
                off += last + 1.0
                num += 1
            self._clock = (time.time(), ck)
            self._clock_sig = file_sig
            return ck

    def cue_lead(self, c: dict, i: int, a: int) -> float:
        """Звук начался раньше отрезка (в прошлой части): сколько секунд он уже отыграл к началу отрезка. Неизвестно
        (частей конвейера нет) — 0, тогда он начнётся с начала файла."""
        ck = self.book_clock()
        s0, e0 = ck.get(i), ck.get(a)
        if s0 is None or e0 is None:
            return 0.0
        t0 = (s0[0] if c.get("at", "s") == "s" else s0[1]) + float(c.get("off") or 0)
        return round(max(0.0, e0[0] - t0) * (self.speed or 1.0), 2)

    def cue_times(self, c: dict, idx: dict, a: int, b: int, total: float):
        """Место заглушки на ленте отрезка a..b: (начало, конец, продолжение, обрезана ли концом отрезка) или None —
        она не здесь. «Продолжение» — звук начался в прошлом отрезке: число секунд, которые он уже отыграл (не ноль);
        иначе False. idx: uid -> (t0, t1) фраз отрезка."""
        i = self.idx_of.get(c.get("uid"))
        if i is None:
            return None
        ie = self.idx_of.get(c.get("e_uid")) if c.get("e_uid") else None
        # A finite cue without an end anchor (typically a short SFX) can still cross a part boundary.
        # Only an explicit end anchor strictly before this part makes it irrelevant here.
        if (ie is not None and ie < a) or i > b:
            return None
        cont = i < a
        if cont:
            t0 = 0.0
            cont = max(self.cue_lead(c, i, a), 0.001)
        else:
            s0, s1 = idx[c["uid"]]
            t0 = (s0 if c.get("at", "s") == "s" else s1) + float(c.get("off") or 0)
        t0 = min(max(t0, 0.0), max(total - 0.05, 0.0))
        cut = False
        if ie is not None and ie >= i:
            if ie > b:
                t1, cut = total, True
            else:
                e0, e1 = idx[c["e_uid"]]
                t1 = (e0 if c.get("e_at", "e") == "s" else e1) + float(c.get("e_off") or 0)
        else:
            ln = c.get("len")
            if ln is None:
                ln = c.get("est") or 1.5
                p = self.cue_file(c)
                if p is not None and not c.get("loop"):
                    try:
                        ln = max(0.05, len(self.snd(p)) / SND_SR - float(c.get("foff") or 0))
                    except Exception:
                        pass
            # cont is elapsed source time for a cue continued from an earlier part. Its remaining
            # duration must shrink by that amount; otherwise the next part contains a silent tail.
            elapsed = float(cont) if cont and not isinstance(cont, bool) and cont > 0.001 else 0.0
            remaining = float(ln) - elapsed
            if remaining <= 0.0:
                return None
            t1 = t0 + remaining
        if t1 > total:
            t1, cut = total, True
        return t0, max(t1, t0 + 0.05), cont, cut

    def cue_audio(self, c: dict, dur: float, cont: bool, cut: bool, ref: float, sp: float = 1.0):
        """Звук заглушки длиной dur с, приведённый к громкости голоса (без громкости дорожки и самой заглушки).
        Участки громкости (c["env"]: [[от, до, множитель], …], секунды от начала звука на ленте) уже учтены: внутри
        участка звук тише или громче, на краях — плавный переход за ENV_RAMP с. sp — ускорение готового файла."""
        p = self.cue_file(c)
        if p is None:
            return None
        a = self.snd(p)
        if not len(a):
            return None
        lvl = self.snd_level(p)
        g = min(ref / lvl, 10.0) if lvl > 1e-6 else 0.0          # тихий файл не раздувать сверх +20 дБ
        k = int(dur * SND_SR)
        lead = float(cont) if cont and not isinstance(cont, bool) and cont > 0.001 else 0.0     # уже отыграно в прошлой части
        pos = np.arange(k, dtype=np.int64) + int((float(c.get("foff") or 0) + lead) * SND_SR)
        if c.get("loop"):
            head, body = self.loop_of(p, a)                    # начало файла звучит один раз, дальше — петля без шва
            y = np.where(pos < head, a[np.minimum(pos, len(a) - 1)], body[(pos - head) % len(body)]).astype("float32")
        else:
            y = np.zeros(k, dtype="float32")
            ok = pos < len(a)
            y[ok] = a[pos[ok]]
        short = c["kind"] == "sfx"
        nat = len(a) / SND_SR - float(c.get("foff") or 0)
        fi = float(c["fin"]) if c.get("fin") is not None else (0.005 if short else 1.0)
        fo = float(c["fout"]) if c.get("fout") is not None else ((0.02 if dur >= nat - 0.03 else min(0.25, dur / 3)) if short else 2.0)
        if cont:
            fi = max(fi, 1.0)
        if cut:
            fo = max(fo, 1.5)
        fi, fo = min(fi, dur / 2), min(fo, dur / 2)
        t = np.arange(k, dtype="float32") / SND_SR
        env = np.ones(k, dtype="float32")
        if fi > 0:
            env *= np.clip(t / fi, 0, 1)
        if fo > 0:
            env *= np.clip((dur - t) / fo, 0, 1)
        if c.get("env"):
            loc = (t + lead) * float(sp or 1.0)                 # время от начала звука, как на ленте предпросмотра
            for ra, rb, rg in c["env"]:
                r = max(0.02, min(ENV_RAMP, (float(rb) - float(ra)) / 4))
                w = np.clip((loc - float(ra)) / r, 0, 1) * np.clip((float(rb) - loc) / r, 0, 1)
                env *= 1.0 + (float(rg) - 1.0) * w
        return (y * env * g).astype("float32")

    def cues_in(self, a: int, b: int, meta: dict) -> list:
        idx = {u: (t0, t1) for u, t0, t1 in meta["segs"]}
        out = []
        with self.lock:                                        # снимок: сведение идёт долго, а звуки в это время правят
            cues = list(self.store["cues"])
            for c in cues:
                try:
                    tt = self.cue_times(c, idx, a, b, meta["total"])
                except KeyError:
                    tt = None
                if tt:
                    out.append((c, tt))
        out.sort(key=lambda x: (x[1][0], x[0]["id"]))
        return out

    def cue_gain(self, c: dict, kinds=None) -> float:
        mx = self.store["mix"]
        if not c.get("on", True) or not mx["on"].get(c["kind"], True) or (kinds is not None and c["kind"] not in kinds):
            return 0.0
        return float(mx.get(c["kind"], MIX_DEF[c["kind"]])) * float(c.get("vol", 1.0))

    def cue_sig(self, c: dict, tt, ref: float):
        """Отпечаток звучания заглушки (без громкости) — версия её клипа для окна; None — файла нет."""
        p = self.cue_file(c)
        if p is None:
            return None
        st = p.stat()
        return sha(jd([c["id"], str(p), st.st_size, st.st_mtime_ns, round(tt[1] - tt[0], 3), tt[2], tt[3], bool(c.get("loop")),
                       c.get("fin"), c.get("fout"), c.get("foff"), round(ref, 5)] + ([c["env"]] if c.get("env") else [])), 16)

    def mix_sig(self, a: int, b: int, meta: dict, kinds=None) -> str:
        """Отпечаток всего, что подмешивается в отрезок: по нему видно, что готовый файл со звуком устарел.
        kinds — только эти дорожки (версия готового файла); None — все включённые."""
        ref = float(meta.get("ref") or 0.05)
        items = []
        for c, tt in self.cues_in(a, b, meta):
            g = self.cue_gain(c, kinds)
            sg = self.cue_sig(c, tt, ref) if g > 0 else None
            if sg:
                items.append([sg, round(tt[0], 3), round(g, 4)])
        return sha(jd(items) + meta["ver"], 16) if items else ""

    def mix_voice(self, a: int, b: int, meta: dict, voice, ref=None, sp: float = 1.0, kinds=None):
        """Голос + включённые звуки отрезка -> (звук, сколько звуков подмешано). ref — громкость голоса, к которой
        приводятся звуки (по умолчанию — как в предпросмотре); sp — общее ускорение готового файла (места звуков
        сдвигаются вместе с голосом, сами звуки не ускоряются); kinds — только эти дорожки (версия готового файла)."""
        ref = float(ref or meta.get("ref") or active_rms(voice) or 0.05)
        bus, k = None, 0
        for c, (t0, t1, cont, cut) in self.cues_in(a, b, meta):
            t0, t1 = t0 / sp, t1 / sp
            cont = cont / sp if cont else cont                 # сам звук не ускоряется: отыграно столько, сколько шёл файл
            g = self.cue_gain(c, kinds)
            if g <= 0:
                continue
            try:
                y = self.cue_audio(c, t1 - t0, cont, cut, ref, sp)
            except Exception as e:
                log(f"звук «{c.get('label')}» не подмешан: {e}")
                y = None
            if y is None:
                continue
            if bus is None:
                bus = np.array(voice, dtype="float32", copy=True)   # голос + звуки копятся в одной копии
            i0 = int(t0 * SND_SR)
            i1 = min(len(bus), i0 + len(y))
            if i1 > i0:
                bus[i0:i1] += y[:i1 - i0] * g
                k += 1
        return (soft_limit(bus, inplace=True), k) if k else (voice, 0)

    def cue_clip(self, k: int, cid: str) -> Path:
        """Клип заглушки для окна (WAV): тот же звук, что пойдёт в сведение, без громкости (её ставит окно)."""
        p = self.open_part(k)
        meta = self.ensure_render(k)
        with self.lock:
            c = next((x for x in self.store["cues"] if x["id"] == cid), None)
            if c is None:
                raise Oops("этого звука уже нет")
            idx = {u: (t0, t1) for u, t0, t1 in meta["segs"]}
            tt = self.cue_times(c, idx, p["a"], p["b"], meta["total"])
            if not tt:
                raise Oops("этот звук не в этой части")
            ref = float(meta.get("ref") or 0.05)
            sg = self.cue_sig(c, tt, ref)
            if sg is None:
                raise Oops("у заглушки нет звукового файла")
            c = dict(c)
        f = self.render_dir / f"cue_{cid}_{sg}.wav"
        if not f.exists():
            import soundfile as sf
            y = self.cue_audio(c, tt[1] - tt[0], tt[2], tt[3], ref)
            with self.slock:
                if not f.exists():
                    tmp = f.with_name(f.stem + ".tmp.wav")
                    sf.write(str(tmp), np.clip(soft_limit(y), -1.0, 1.0), SND_SR, format="WAV", subtype="PCM_16")
                    os.replace(tmp, f)
                for old in self.render_dir.glob(f"cue_{cid}_*.wav"):
                    if old != f:
                        try:
                            old.unlink()
                        except OSError:
                            pass
        return f

    def sound_preview(self, ref: str) -> Path:
        """Звук из библиотеки — в виде, который точно сыграет браузер (WAV 24 кГц моно)."""
        p = self.sound_path(ref)
        if p is None:
            raise Oops("такого звука нет в библиотеке")
        st = p.stat()
        f = self.render_dir / f"snd_{sha(f'{p}|{st.st_size}|{st.st_mtime_ns}', 16)}.wav"
        if not f.exists():
            import soundfile as sf
            x = self.snd(p)
            pk = float(np.max(np.abs(x))) if len(x) else 0.0
            with self.slock:
                if not f.exists():
                    tmp = f.with_name(f.stem + ".tmp.wav")
                    sf.write(str(tmp), (x / pk * 0.7) if pk > 1e-6 else x, SND_SR, format="WAV", subtype="PCM_16")
                    os.replace(tmp, f)
        return f

    def cue_view(self, c: dict, tt, ref: float) -> dict:
        r = self.cue_ref(c)
        p = self.sound_path(r)
        nat = None
        if p is not None:
            d = (self.sound_index().get(r) or {}).get("dur") if not str(r).startswith("kit:") else None
            nat = d
        i = self.idx_of.get(c["uid"])
        return dict(id=c["id"], kind=c["kind"], label=c.get("label") or "", uid=c["uid"], no=self.rows[i]["id"] if i is not None else "",
                    t0=round(tt[0], 3), t1=round(tt[1], 3), cont=bool(tt[2]), cut=tt[3], len=c.get("len"), est=c.get("est"),
                    lead=round(float(tt[2]), 1) if tt[2] else 0, fin=c.get("fin"), fout=c.get("fout"), foff=c.get("foff"),
                    bed=bool(c.get("e_uid")), file=c.get("file"), eff=r if p is not None else None,
                    fname=self.sound_name(r) if p is not None else "", shared=bool(p is not None and not c.get("file")),
                    ftitle=self.sound_title(r) if p is not None else "", mine=bool(c.get("mine")),
                    nat=nat, vol=float(c.get("vol", 1.0)), on=bool(c.get("on", True)), loop=bool(c.get("loop")),
                    env=[list(x) for x in (c.get("env") or [])],
                    src=c.get("src", "manual"), why=c.get("why") or "", conf=c.get("conf"),
                    cv=self.cue_sig(c, tt, ref) if p is not None else None)

    def cues_view(self, k: int, meta: dict) -> list:
        p = self.plan()[k - 1]
        ref = float(meta.get("ref") or 0.05)
        return [self.cue_view(c, tt, ref) for c, tt in self.cues_in(p["a"], p["b"], meta)]

    def _cue_undo(self, label: str):
        self.undo.append(("#cues", jd([self.store["cues"], self.store["cue_seen"], self.store["sound_map"], self.store["mix"],
                                       self.store["kit_off"]]), label))
        del self.undo[:-80]

    def _cue_place(self, c: dict, t: float, meta: dict, end=False):
        """Время на ленте части -> привязка к фразе: внутри фразы — от её начала, в паузе после — от её конца."""
        segs = meta["segs"]
        t = min(max(float(t), 0.0), meta["total"])
        j = 0
        for q, (u, s0, s1) in enumerate(segs):
            if s0 <= t:
                j = q
            else:
                break
        u, s0, s1 = segs[j]
        at, off = ("s", t - s0) if t <= s1 else ("e", t - s1)
        if end:
            c.update(e_uid=u, e_at=at, e_off=round(off, 3))
            c.pop("len", None)
        else:
            c.update(uid=u, at=at, off=round(off, 3))

    def cue_edit(self, k: int, d: dict) -> dict:
        """Создать, изменить, передвинуть или убрать заглушку. Время — в секундах ленты открытой части."""
        p = self.open_part(k)
        meta = self.ensure_render(k)
        idx = {u: (t0, t1) for u, t0, t1 in meta["segs"]}
        with self.lock:
            self._cue_undo("звук")
            if d.get("new"):
                nw = d["new"]
                kind = nw.get("kind")
                if kind not in CUE_KINDS:
                    raise Oops("вид заглушки: звук, фон или музыка")
                c = dict(id=self.next_id("c"), kind=kind, label=str(nw.get("label") or CUE_NAME[kind]).strip()[:80],
                         vol=1.0, on=True, src="manual", loop=kind != "sfx")
                t = float(nw.get("t") or 0)
                self._cue_place(c, t, meta)
                if nw.get("file"):
                    if self.sound_path(nw["file"]) is None:
                        raise Oops("такого звука нет в библиотеке")
                    c["file"] = nw["file"]
                if kind == "sfx":
                    c["est"] = float(nw.get("len") or 1.5)
                else:
                    self._cue_place(c, t + float(nw.get("len") or 30.0), meta, end=True)
                self.store["cues"].append(c)
                what = "новый звук"
            elif d.get("copy"):
                c0 = next((x for x in self.store["cues"] if x["id"] == d["copy"]), None)
                tt = self.cue_times(c0, idx, p["a"], p["b"], meta["total"]) if c0 else None
                if not tt:
                    raise Oops("этого звука уже нет в этой части")
                c = dict(c0, id=self.next_id("c"), src="manual")
                for key in ("why", "conf", "kid", "mine"):
                    c.pop(key, None)
                ln = tt[1] - tt[0]
                t = float(d["t"]) if d.get("t") is not None else tt[1] + 0.3     # по умолчанию — сразу следом
                t = min(max(t, 0.0), max(meta["total"] - 0.2, 0.0))
                if c.get("e_uid"):
                    self._cue_place(c, min(t + ln, meta["total"]), meta, end=True)
                self._cue_place(c, t, meta)
                self.store["cues"].append(c)
                what = "копия звука"
            else:
                c = next((x for x in self.store["cues"] if x["id"] == d.get("id")), None)
                if c is None:
                    raise Oops("этого звука уже нет")
                if d.get("delete"):
                    self.store["cues"] = [x for x in self.store["cues"] if x["id"] != c["id"]]
                    if c.get("src") == "kit" and c.get("kid"):     # убран автором: обновление партитуры его не вернёт
                        self.store["kit_off"] = sorted(set(self.store["kit_off"]) | {c["kid"]})
                    what = "звук убран"
                else:
                    st = d.get("set") or {}
                    touched = {key for key in ("label", "kind", "vol", "on", "loop", "fin", "fout", "foff", "file", "env") if key in st}
                    if "len" in st or "te" in d:
                        touched.add("end")
                    if "t" in d:
                        touched |= {"pos", "end"} if c.get("e_uid") else {"pos"}
                    if touched and c.get("src") == "kit" and c.get("kid"):
                        c["mine"] = sorted(set(c.get("mine") or []) | touched)
                    if "end" in touched:
                        c.pop("open", None)
                    if "label" in st:
                        c["label"] = str(st["label"]).strip()[:80] or CUE_NAME[c["kind"]]
                    if "kind" in st and st["kind"] in CUE_KINDS:
                        c["kind"] = st["kind"]
                    if "vol" in st:
                        c["vol"] = round(min(max(float(st["vol"]), 0.0), 4.0), 3)
                    if "env" in st:                              # участки своей громкости внутри звука
                        rg = []
                        for x in (st["env"] or [])[:40]:
                            try:
                                ra, rb, g_ = float(x[0]), float(x[1]), float(x[2])
                            except (TypeError, ValueError, IndexError):
                                raise Oops("участок громкости: нужны начало, конец и громкость")
                            ra, rb = max(0.0, min(ra, rb)), min(36000.0, max(ra, rb))
                            if rb - ra < 0.1:
                                raise Oops("участок громкости короче 0,1 с — задайте его длиннее")
                            rg.append([round(ra, 2), round(rb, 2), round(min(max(g_, 0.0), 4.0), 3)])
                        rg.sort()
                        if rg:
                            c["env"] = rg
                        else:
                            c.pop("env", None)
                    for key in ("on", "loop"):
                        if key in st:
                            c[key] = bool(st[key])
                    for key in ("fin", "fout", "foff"):
                        if key in st:
                            if st[key] in (None, ""):
                                c.pop(key, None)
                            else:
                                c[key] = round(min(max(float(st[key]), 0.0), 600.0), 3)
                    if "file" in st:
                        if st["file"] in (None, ""):
                            c.pop("file", None)
                        elif self.sound_path(st["file"]) is None:
                            raise Oops("такого звука нет в библиотеке")
                        else:
                            c["file"] = st["file"]
                            if st.get("all"):                   # этот файл — всем заглушкам с такой подписью
                                self.store["sound_map"][c["label"]] = st["file"]
                    if "len" in st:
                        if st["len"] in (None, ""):
                            c.pop("len", None)
                        else:
                            c["len"] = round(min(max(float(st["len"]), 0.05), 3600.0), 3)
                            for key in ("e_uid", "e_at", "e_off"):
                                c.pop(key, None)
                    tt = self.cue_times(c, idx, p["a"], p["b"], meta["total"]) if ("t" in d or "te" in d) else None
                    if "t" in d and tt:
                        t = float(d["t"])
                        if c.get("e_uid"):                       # отрезок едет целиком
                            self._cue_place(c, min(t + (tt[1] - tt[0]), meta["total"]), meta, end=True)
                        self._cue_place(c, t, meta)
                    if "te" in d and tt:
                        te = max(float(d["te"]), tt[0] + 0.1)
                        if c["kind"] == "sfx" and not c.get("e_uid"):
                            c["len"] = round(te - tt[0], 3)
                        else:
                            self._cue_place(c, te, meta, end=True)
                    what = "звук изменён"
            self.save(what, {"cue": c.get("id"), "label": c.get("label")})
            return c

    def cues_scan(self, k: int, kinds=None, fresh=False) -> dict:
        """Распознаватель: места для звуков, фона и музыки по тексту части -> заглушки. Уже предложенное (в том числе
        убранное автором) повторно не предлагается, пока не попросят «найти заново»."""
        try:
            import nox_cues
        except ImportError:
            raise Oops("рядом со студией нет файла nox_cues.py (распознаватель мест для звуков) — скачайте его и "
                       "запустите команду студии ещё раз")
        p = self.open_part(k)
        meta = self.ensure_render(k)
        idx = {u: (t0, t1) for u, t0, t1 in meta["segs"]}

        def dur_of(i):
            tt = idx.get(self.uids[i])
            return (tt[1] - tt[0]) if tt else max(1.0, len(self.rows[i]["text"]) / 14.0)
        found = nox_cues.scan(self.rows, p["a"], p["b"], dur_of, tuple(kinds or CUE_KINDS))
        with self.lock:
            self._cue_undo("поиск мест для звуков")
            here = set(self.uids[p["a"]:p["b"] + 1])
            if fresh:
                self.store["cue_seen"] = [s for s in self.store["cue_seen"] if s.split("|")[0] not in here]
            seen = set(self.store["cue_seen"])
            have = {(c["uid"], c["kind"], c.get("label")) for c in self.store["cues"]}
            new = 0
            for f in found:
                uid = self.uids[f["i"]]
                key = f"{uid}|{f['kind']}|{f['label']}"
                if key in seen or (uid, f["kind"], f["label"]) in have or uid not in idx:
                    continue
                seen.add(key)
                s0, s1 = idx[uid]
                c = dict(id=self.next_id("c"), kind=f["kind"], label=f["label"], uid=uid, at="s", vol=1.0, on=True,
                         src="auto", conf=f["conf"], why=f["why"], loop=f["kind"] != "sfx")
                if f["kind"] == "sfx":
                    c.update(off=round(f["pos"] * (s1 - s0), 3), est=f["len"])
                else:
                    c.update(off=0.0, e_uid=self.uids[min(f["end_i"], p["b"])], e_at="e", e_off=0.0)
                self.store["cues"].append(c)
                new += 1
            if new or fresh:
                self.store["cue_seen"] = sorted(seen)
                self.save("найдены места для звуков", {"part": k, "new": new})
            else:
                self.undo.pop()
            return dict(new=new, found=len(found))

    def cues_clear(self, k: int, what: str) -> int:
        """Убрать заглушки части: «empty» — найденные автоматически и без файла; «all» — все."""
        p = self.open_part(k)
        here = set(self.uids[p["a"]:p["b"] + 1])
        with self.lock:
            self._cue_undo("заглушки убраны")
            keep = []
            for c in self.store["cues"]:
                gone = c["uid"] in here and (what == "all" or (c.get("src") == "auto" and self.cue_file(c) is None))
                if not gone:
                    keep.append(c)
            k0 = len(self.store["cues"]) - len(keep)
            if not k0:
                self.undo.pop()
                return 0
            self.store["cues"] = keep
            self.save("заглушки убраны", {"part": k, "count": k0})
            return k0

    def set_mix(self, d: dict):
        with self.lock:
            self._cue_undo("громкость дорожек")
            mx = self.store["mix"]
            for key in CUE_KINDS:
                if key in d:
                    mx[key] = round(min(max(float(d[key]), 0.0), 2.0), 3)
                if key in (d.get("on") or {}):
                    mx["on"][key] = bool(d["on"][key])
            for v in MIX_VARS:
                if v[0] in (d.get("vars") or {}):
                    mx.setdefault("vars", {})[v[0]] = bool(d["vars"][v[0]])
            self.save("громкость дорожек", {"mix": mx})

    def set_sound_map(self, label: str, ref):
        with self.lock:
            self._cue_undo("звук для подписи")
            if ref in (None, ""):
                self.store["sound_map"].pop(label, None)
            elif self.sound_path(ref) is None:
                raise Oops("такого звука нет в библиотеке")
            else:
                self.store["sound_map"][label] = ref
            self.save("звук для подписи", {"label": label, "file": ref})

    def kit_score(self):
        """Партитура звуков книги (cues_book1.json действующего комплекта) и сведение конвейера (zvuk_k1a.py) — есть ли."""
        return self.kit_active() if self.zvuk() is not None else None

    def score_view(self) -> dict:
        """Партитура для окна: есть ли, сколько её звуков на дорожках, сколько из них правил автор, сколько он убрал,
        не стоят ли они из прежнего комплекта и не распаковывается ли новый."""
        kit, base = self.kit_score(), self.kit_dir()
        ks = [c for c in self.store["cues"] if c.get("src") == "kit"]
        pre = ""
        if kit is not None and base is not None:
            try:
                pre = "kit:" + kit.resolve().relative_to(base.resolve()).as_posix() + "/"
            except (ValueError, OSError):
                pass
        job = self.kit_job
        return dict(have=kit is not None, on_lanes=len(ks), kit=kit.name if kit is not None else "",
                    gen=self.kit_state()["gen"] if kit is not None else 0,
                    mine=sum(1 for c in ks if c.get("mine")), off=len(self.store["kit_off"]),
                    legacy=sum(1 for c in ks if not c.get("kid")),
                    stale=sum(1 for c in ks if pre and str(c.get("file") or "").startswith("kit:")
                              and not str(c["file"]).startswith(pre)),
                    busy=dict(name=job["name"], pct=job["pct"]) if job and not job.get("error") else None,
                    error=(job or {}).get("error") or "",
                    wait=(self.kit_state()["zip"] or {}).get("name", "") if self.zvuk() is not None else "")

    def kit_unpack_start(self) -> dict:
        """Кнопка «Распаковать»: новый архив комплекта появился в Загрузках, пока студия работала."""
        if self._kit_lock.locked() or (self.kit_job is not None and not self.kit_job.get("error")):
            return dict(started=False, busy=True)
        self.kit_job = None
        zp = self.kit_state(True)["zip"]
        if zp is None:
            raise Oops("нового звукового комплекта в Загрузках нет")
        self.kit_job = dict(name=zp["name"], pct=0, error=None)   # сразу: перенос партитуры уже ждёт новый комплект
        threading.Thread(target=self.kit_prepare, daemon=True).start()
        return dict(started=True)

    def kit_import(self, reset: bool = False) -> dict:
        """Партитура звуков книги -> заглушки на дорожках, сразу со звуками из комплекта. Берётся ровно то, что сводит
        конвейер (те же отметки, файлы, громкости, затухания): его расчёт дорожек по собранным частям переносится на
        фразы — и дальше звуки живут в студии: их видно, можно двигать, выключать, заменять. Прежний перенос
        обновляется: что автор в нём менял (сдвиг, длина, громкость, свой файл, подпись, выключение) — остаётся его,
        остальное берётся из партитуры заново; убранные им звуки не возвращаются. reset — забыть эти правки и взять
        партитуру начисто. Звуки, поставленные автором, не трогаются."""
        if self.kit_job is not None and not self.kit_job.get("error"):
            raise Oops(f"новый звуковой комплект ещё распаковывается ({self.kit_job['pct']} %) — повторите через минуту")
        kit = self.kit_score()
        if kit is None:
            raise Oops("партитуры звуков для этой книги нет: нужен звуковой комплект (D:\\nox_audiobook\\zvuk) и "
                       "zvuk_k1a.py в папке книги")
        import types
        z = self.zvuk()
        kd = self.kit_dir().resolve()
        cues = json.loads((kit / "cues_book1.json").read_text(encoding="utf-8"))
        try:
            z.ensure_beds(kit, sorted({x for c in cues if c.get("layer") == "A" and c.get("action") == "bed"
                                       for x in str(c.get("target", "")).split("+") if x}))
        except Exception as e:
            log(f"фоны местности не собраны: {e}")
        T = self.kit_tables(kit)
        music_dur = {}
        for r in T["mus"].values():
            try:
                music_dur[r["трек"]] = float(r["длит_с"] or 60)
            except (ValueError, KeyError, TypeError):
                pass
        parts = z.read_parts(n)
        if not parts:
            raise Oops("конвейер ещё не собрал ни одной части (parts\\part_NN.times.tsv) — партитуру не к чему привязать")
        ff = z.ffmpeg_bin(n)
        id2i = {r["id"]: i for i, r in enumerate(self.rows)}
        starts, off = {}, 0.0
        for num, mp3, tm in parts:
            d = z.probe_dur(ff, mp3)
            if d is None:
                d = max((e for _, e in tm.values()), default=0.0) + 2.0
            for rid, (a0, b0) in tm.items():
                if rid in id2i:
                    starts[id2i[rid]] = (off + a0, off + b0)
            off += d
        a = types.SimpleNamespace(voice=1.0, sfx=MIX_DEF["sfx"], amb=MIX_DEF["amb"], music=MIX_DEF["mus"], duck=0.0)
        nofile = []
        try:
            items, missed = z.build_timeline(self.rows, starts, cues, kit, music_dur, a, nofile)
        except TypeError:                                      # прежний zvuk_k1a.py в папке книги
            items, missed = z.build_timeline(self.rows, starts, cues, kit, music_dur, a)
        order = sorted((t0, t1, i) for i, (t0, t1) in starts.items())
        t0s = [x[0] for x in order]

        def anchor(t):
            j = max(0, bisect.bisect_right(t0s, t) - 1)
            s0, s1, i = order[j]
            return (self.uids[i], "s", round(t - s0, 3)) if t <= s1 else (self.uids[i], "e", round(t - s1, 3))
        kind_of = {"sfx": "sfx", "amb": "amb", "music": "mus"}
        by_id = {str(c.get("id")): c for c in cues}
        new, skipped = [], 0
        t_last = order[-1][1]
        for it in items:
            kind = kind_of.get(it.get("kind"))
            try:
                rel = Path(it["file"]).resolve().relative_to(kd).as_posix()
            except (ValueError, OSError):
                rel = None
            if kind is None or rel is None or not Path(it["file"]).is_file() or it["start"] > t_last:
                skipped += 1
                continue
            uid, at, o = anchor(max(0.0, float(it["start"])))
            note = str(it.get("note") or "")
            kid = str(it.get("cid") or "")
            sc = by_id.get(re.sub(r"(:\d+|r)$", "", kid)) or {}
            if kind == "sfx":                                  # подпись — по-русски, из таблиц комплекта
                game = note.split(" ", 1)[1] if " " in note else note
                label = (T["sfx"].get(game) or {}).get("что_это") or game
            elif kind == "amb":
                game = note
                label = (T["amb"].get(note.lower()) or {}).get("что_это") or note
            else:
                game = note.replace(" (тема части)", "")
                ru = (T["mus"].get(game.lower()) or {}).get("что_это")
                label = (f"{game} — {ru}" if ru else game) + (" (тема части)" if note.endswith("(тема части)") else "")
            label = label or Path(rel).stem
            why = "Из партитуры книги" + (f", отметка {sc['id']}" if sc.get("id") else "") \
                + (f" ({sc['basis']})" if sc.get("basis") else "") \
                + (": " + str(sc["note"]).strip().rstrip(".") + "." if str(sc.get("note") or "").strip() else ".") \
                + (f" В игре — {game}." if game and game != label else "") \
                + (f" Когда звучит: {sc['when']}." if sc.get("when") else "")
            c = dict(kind=kind, label=label[:80], uid=uid, at=at, off=o, file="kit:" + rel,
                     vol=round(float(it.get("gain", MIX_DEF[kind])) / MIX_DEF[kind], 3), on=True, loop=bool(it.get("loop")),
                     src="kit", why=why)
            if kid:
                c["kid"] = kid
            if it.get("offset"):
                c["foff"] = round(float(it["offset"]), 3)
            if it.get("fade_in"):
                c["fin"] = round(float(it["fade_in"]), 3)
            if it.get("fade_out"):
                c["fout"] = round(float(it["fade_out"]), 3)
            if it.get("end") is not None:
                e_uid, e_at, e_o = anchor(min(float(it["end"]), t_last))
                c.update(e_uid=e_uid, e_at=e_at, e_off=e_o)
                if float(it["end"]) > t_last + 0.05:           # конец — дальше собранных частей: пока стоит на последней фразе
                    c["open"] = True
            new.append(c)
        with self.lock:
            self._cue_undo("партитура книги перенесена на дорожки")
            olds = [c for c in self.store["cues"] if c.get("src") == "kit"]
            old_by = {c["kid"]: c for c in olds if c.get("kid")}
            off_set = set() if reset else set(self.store["kit_off"])
            out, kept, gone, fresh_kids = [], 0, 0, set()
            for c in new:
                kid = c.get("kid")
                if kid and kid in off_set:
                    gone += 1
                    continue
                o = old_by.get(kid) if kid else None
                if o is not None and o.get("mine") and not reset:    # то, что менял автор, остаётся его
                    for asp in o["mine"]:
                        for key in KIT_ASPECT.get(asp, ()):
                            if key in o:
                                c[key] = o[key]
                            else:
                                c.pop(key, None)
                    c["mine"] = list(o["mine"])
                    if "end" in o["mine"]:
                        c.pop("open", None)                    # конец поставил автор — он известен
                    kept += 1
                c["id"] = o["id"] if o is not None else self.next_id("c")
                if kid:
                    fresh_kids.add(kid)
                out.append(c)
            if not reset:                                      # правленый звук, которого в новом расчёте нет, — остаётся
                for o in olds:
                    if o.get("mine") and o.get("kid") and o["kid"] not in fresh_kids and o["kid"] not in off_set:
                        out.append(o)
                        kept += 1
            if reset:
                self.store["kit_off"] = []
            self.store["cues"] = [c for c in self.store["cues"] if c.get("src") != "kit"] + out
            self.save("партитура книги перенесена на дорожки", {"new": len(out), "replaced": len(olds), "skipped": skipped,
                                                               "missed": len(missed), "kept": kept, "gone": gone,
                                                               "nofile": len(nofile), "kit": kit.name})
        if missed:                                             # фразу-якорь не нашли в тексте книги: текст в этом месте меняли
            log(f"партитура: {len(missed)} отметок не нашли свою фразу в тексте книги (текст там меняли или главы нет): "
                + "; ".join(f"{c.get('id')} гл. {c.get('chapter')} «{str(c.get('anchor') or '')[:40]}»" for c in missed[:8])
                + ("…" if len(missed) > 8 else ""), "warn", "звуки")
        if nofile:
            log(f"партитура: в комплекте {kit.name} нет файлов для {len(nofile)} имён: "
                + ", ".join(f"{w} {x}" for w, x in nofile[:8]) + ("…" if len(nofile) > 8 else ""), "warn", "звуки")
        return dict(new=len(out), replaced=len(olds), skipped=skipped, missed=len(missed), parts=len(parts), kept=kept,
                    gone=gone, nofile=len(nofile), kit=kit.name)

    def reanchor_cues(self):
        """Заглушки ищут свои фразы после переразбивки книги: та же фраза (по тексту); нет её — «без места»."""
        by_key = {}
        for i, key in enumerate(self.keys):
            by_key.setdefault(key, []).append(i)
        keep, lost = [], 0
        for c in self.store["cues"]:
            if c.get("uid") not in self.idx_of:
                cand = by_key.get(str(c.get("uid", "")).split(".")[0])
                if not cand:
                    self.store["cue_orphans"].append(dict(c, lost=now()))
                    lost += 1
                    continue
                c["uid"] = self.uids[cand[0]]
            if c.get("e_uid") and c["e_uid"] not in self.idx_of:
                cand = by_key.get(str(c["e_uid"]).split(".")[0])
                if cand:
                    c["e_uid"] = self.uids[cand[0]]
                else:
                    for key in ("e_uid", "e_at", "e_off"):
                        c.pop(key, None)
                    c["len"] = c.get("len") or 30.0
            keep.append(c)
        self.store["cues"] = keep
        if lost:
            log(f"звуки: без места осталось {lost} (фразы исчезли из книги)")

    def synth_scope(self, uid: str, scope: str, count: int = 1) -> list:
        """Переозвучка с размахом: фраза (count вариантов), всё предложение (его куски) или весь абзац."""
        with self.lock:
            if uid not in self.idx_of:
                raise Oops("этой фразы больше нет в книге — обновите окно")
            i = self.idx_of[uid]
            r = self.rows[i]
            if scope == "para":
                idxs = [j for j in range(max(0, i - 400), min(len(self.rows), i + 400))
                        if self.rows[j]["chapter"] == r["chapter"] and self.rows[j]["para"] == r["para"]]
            elif scope == "sentence":
                chunk = int(n.PAUSE.get("chunk", 120))
                same = lambda j: self.rows[j]["chapter"] == r["chapter"] and self.rows[j]["para"] == r["para"] \
                    and self.rows[j]["kind"] == r["kind"]
                a = i
                while a > 0 and same(a - 1) and int(self.rows[a]["pause_before_ms"]) == chunk:
                    a -= 1
                b = i
                while b + 1 < len(self.rows) and same(b + 1) and int(self.rows[b + 1]["pause_before_ms"]) == chunk:
                    b += 1
                idxs = list(range(a, b + 1))
            else:
                idxs = [i] * max(1, min(int(count or 1), 5))
            todo = [j for j in idxs if j not in self._abs and not (self.store["segs"].get(self.uids[j]) or {}).get("locked")]
            if not todo:
                raise Oops("всё закреплено — сначала снимите закрепление")
            if len(todo) > 60:
                raise Oops(f"в абзаце {len(todo)} фраз — это слишком много за один раз; переозвучьте по предложениям")
            return [self.synth(self.uids[j]) for j in todo]


    # ------------------------------------------------------------------ правила ударений и слов
    def rule_scan(self, word: str, to: str, scope: str, uid: str) -> list:
        """Где сработает правило (кроме закреплённого и правленного вручную): [(i, часть, текст до, текст после)]."""
        i0 = self.idx_of[uid]
        part = self.part_of(i0)
        if scope == "part":
            if part is None:
                raise Oops("фраза не в открытой части")
            p = self.plan()[part - 1]
            rng = range(i0, p["b"] + 1)
        else:
            rng = range(i0, len(self.rows))
        probe = {word: [dict(id="?", word=word, to=to, scope="book", from_uid=uid, from_i=i0)]}
        ridx = self.rule_index()
        out, skip = [], dict(locked=0, manual=0)
        for i in rng:
            if i in self._abs:                                 # присоединённая — в составе своей головной фразы
                continue
            tx = self.row_text(i).lower()
            if word not in tx.replace("ё", "е") and word not in tx:
                continue
            e = self.store["segs"].get(self.uids[i]) or {}
            if e.get("locked") or e.get("tech") is not None:
                t = (e.get("locked") or {}).get("tech") or e.get("tech") or ""
                if i != i0 and any(bare(m.group(0)) == word for m in WORD_RE.finditer(t)):
                    skip["locked" if e.get("locked") else "manual"] += 1
                continue
            cur = self.auto_tech(i, ridx)[0]
            new, hits = self.apply_rules(cur, i, ridx=probe)
            if hits and norm_t(new) != norm_t(cur):
                out.append((i, self.part_any(i), cur, new))
        self._rule_skip = skip
        return out

    def add_rule(self, word: str, to: str, scope: str, uid: str, dry: bool) -> dict:
        with self.lock:
            word, to = bare(word.strip()), norm_t(plus_to_acute(to))
            to = to[:1].lower() + to[1:]              # заглавная буква восстанавливается по месту (начало фразы, имя)
            if scope not in ("part", "book"):
                raise Oops("область правила: «дальше в этой части» или «дальше по всей книге»")
            if not word or not WORD_RE.fullmatch(to or "") or uid not in self.idx_of:
                raise Oops("правило — это одно слово: как написано в книге и как его читать")
            hits = self.rule_scan(word, to, scope, uid)
            def around(t):
                m = next((x for x in WORD_RE.finditer(t) if bare(x.group(0)) == word), None)
                c = m.start() if m else 0
                return ("…" if c > 34 else "") + t[max(c - 34, 0):c + len(word) + 36] + ("…" if c + len(word) + 36 < len(t) else "")
            view = [dict(uid=self.uids[i], id=self.rows[i]["id"], part=p, before=around(a), after=around(b))
                    for i, p, a, b in hits]
            if dry:
                return dict(count=len(view), hits=view[:200], skipped=self._rule_skip)
            i0 = self.idx_of[uid]
            r = dict(id=self.next_id("r"), word=word, to=to, scope=scope, from_uid=uid, from_i=i0,
                     part=self.part_of(i0) if scope == "part" else None, at=now(), from_id=self.rows[i0]["id"])
            self.store["rules"].append(r)
            e = self.store["segs"].get(uid)                    # здесь правило уже даёт нужный текст — ручная правка не нужна
            if e and e.get("tech") is not None and not e.get("locked"):
                if norm_t(self.auto_tech(i0)[0]) == norm_t(e["tech"]):
                    e.pop("tech")
                    self._tidy(uid)
            self.save("новое правило", {"rule": r, "count": len(view)})
            self.write_rules_dict()
            return dict(rule=r, count=len(view), hits=view[:200])

    def del_rule(self, rid: str) -> int:
        """Удалить правило. Фразы, переозвученные по нему, возвращаются к черновой записи, если та читает как раз
        прежний текст (иначе они остались бы «переозвучить» без нужды). Закреплённое не трогается."""
        with self.lock:
            if not any(r["id"] == rid for r in self.store["rules"]):
                raise Oops("такого правила нет")
            ridx = self.rule_index()
            hit = [i for i in range(len(self.rows)) if rid in self.eff(i, ridx)["hits"]]
            self.store["rules"] = [r for r in self.store["rules"] if r["id"] != rid]
            self.write_rules_dict()
            back = 0
            for i in hit:
                uid = self.uids[i]
                e = self.store["segs"].get(uid)
                if not e or e.get("locked") or not e.get("take"):
                    continue
                ef, side = self.eff(i), self.sidecar(self.wavs[i])
                if side is not None and ef["role"] == self.base_roles[i] and norm_t(side) == norm_t(ef["tech"]):
                    e.pop("take")
                    self._tidy(uid)
                    back += 1
            self.undo.clear()
            self.save("правило удалено", {"rule": rid, "back_to_draft": back})
            return back

    # ------------------------------------------------------------------ граница части
    def set_boundary(self, k: int, uid: str):
        """Закончить часть k на этой фразе (следующая часть начнётся со следующей). Только на границе абзаца и пока обе
        части не закреплены."""
        self.auto_guard("перенести границу части")
        with self.lock:
            self.open_part(k)
            pl = self.plan()
            p, i = pl[k - 1], self.idx_of.get(uid)
            if i is None:
                raise Oops("этой фразы больше нет в книге — обновите окно")
            nxt = pl[k] if k < len(pl) else None
            hi = nxt["b"] - 1 if nxt else len(self.rows) - 1
            if not (p["a"] <= i <= hi):
                raise Oops("границу можно двигать в пределах этой и следующей части")
            if self.store["parts"][k - 1].get("locked") or (nxt and nxt["opened"] and self.store["parts"][k].get("locked")):
                raise Oops("часть закреплена — сначала снимите закрепление")
            i = self.pipe_at(i, p["a"])                    # вставка — граница по её опорной реплике
            t2 = self.ins_tail(i)
            r2 = self.rows[t2 + 1] if t2 + 1 < len(self.rows) else None
            if r2 is not None and not (r2["kind"] == "head" or int(r2["pause_before_ms"]) >= n.PAUSE["para"]):
                raise Oops("часть должна кончаться на конце абзаца — выберите последнюю фразу абзаца")
            rec = self.store["parts"][k - 1]
            rec["last"], rec["last_i"], rec["last_p"], rec["export"] = self.uids[i], i, self.pidx[i], None
            self._parts_cache = None
            self.save(f"граница части {k} перенесена", {"part": k, "uid": uid})

    # ------------------------------------------------------------------ движок (воркеры озвучки)
    def workers(self) -> list:
        out = []
        for f in self.qdir.glob("alive_w*.txt"):
            try:
                age = time.time() - f.stat().st_mtime
                p = f.read_text(encoding="utf-8").split("\t")
                pid = int(p[0]) if p and p[0].isdigit() else 0
                if age < 150 and pid_alive(pid):                   # отметка свежая И сам процесс жив
                    out.append(dict(w=f.stem[7:], mode=p[2] if len(p) > 2 else "book", age=round(age)))
                elif age > 150 or not pid_alive(pid):
                    f.unlink(missing_ok=True)                      # воркер умер или давно молчит — отметку убрать
            except (OSError, ValueError):
                pass
        return out

    def gpu(self, ttl: float = 15.0):
        """Видеокарта (nvidia-smi): имя, память, загрузка, температура. None — карты NVIDIA или её программы нет."""
        if time.time() - self._gpu[0] < ttl:
            return self._gpu[1]
        v = None
        if self._smi is None:
            self._smi = shutil.which("nvidia-smi") or "nvidia-smi"
        if self._smi:
            try:
                r = subprocess.run([self._smi, "--query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu",
                                    "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=6)
                f = [x.strip() for x in r.stdout.strip().splitlines()[0].split(",")]

                def num(x):
                    try:
                        return int(float(x))
                    except ValueError:
                        return None
                a, b = num(f[1]), num(f[2])
                v = dict(name=f[0].replace("NVIDIA ", "").replace("GeForce ", ""), used=a, total=b,
                         free=(b - a) if a is not None and b is not None else None, util=num(f[3]), temp=num(f[4]))
            except FileNotFoundError:
                self._smi = ""                                   # программы нет — больше не искать
            except Exception:
                v = None
        self._gpu = (time.time(), v)
        return v

    def cache_stat(self, force: bool = False) -> dict:
        """Сколько места занимает кэш студии (обработанные фразы и собранные для прослушивания части)."""
        if not force and self._cstat[1] is not None and time.time() - self._cstat[0] < 120:
            return self._cstat[1]
        tot = cnt = 0
        for d in (self.cache_dir, self.render_dir):
            try:
                with os.scandir(d) as it:
                    for e in it:
                        try:
                            if e.is_file():
                                tot += e.stat().st_size
                                cnt += 1
                        except OSError:
                            pass
            except OSError:
                pass
        v = dict(mb=round(tot / 1048576), files=cnt)
        self._cstat = (time.time(), v)
        return v

    def sys_view(self, live: bool = False) -> dict:
        """Состояние компьютера и студии для шапки журнала: видеокарта, память, процессор, диск, кэш, идущие операции."""
        ram, ct = _ram(), _cpu_ticks()
        t0, when, pct = self._cpu
        if ct and t0 and time.time() - when >= 0.8 and ct[1] > t0[1]:
            pct = max(0, min(100, round(100 * (1 - (ct[0] - t0[0]) / (ct[1] - t0[1])))))
            self._cpu = (ct, time.time(), pct)
        elif ct and not t0:
            self._cpu = (ct, time.time(), None)
        try:
            du = shutil.disk_usage(self.root)
            disk = dict(free=du.free >> 20, total=du.total >> 20, drive=(self.root.drive or str(self.root.anchor)).rstrip("\\/"))
        except OSError:
            disk = None
        with EV_LOCK:
            ops = [dict(what=o.what, stage=o.stage_s, done=o.done, total=o.total, sec=round(time.time() - o.t0, 1))
                   for o in sorted(self.ops.values(), key=lambda o: o.id)]
        return dict(gpu=self.gpu(3.0 if live else 30.0), ram=dict(total=ram[0], free=ram[1]) if ram else None, cpu=pct,
                    cores=os.cpu_count(), threads=self.threads, disk=disk, cache=self.cache_stat(), ops=ops)

    # ------------------------------------------------------------------ уборка кэша
    CACHE_GB = (8.0, 4.0, 1.5)

    def housekeeping(self, everything: bool = False) -> dict:
        """Уборка кэша студии. Кэш — уже обработанные фразы (studio\\cache) и части, собранные для прослушивания
        (studio\\render): всё это студия в любой момент пересчитает из записей, удалять безопасно. Правки, дубли,
        закреплённые записи и готовые файлы — НЕ кэш, их уборка не касается. Обычная уборка (сама, раз в несколько
        часов) оставляет свежее в пределах лимита; полная (кнопкой) убирает всё, кроме частей, открытых недавно."""
        if not self.rlock.acquire(blocking=everything, timeout=120 if everything else -1):
            return dict(removed=0, freed=0, left=self.cache_stat()["mb"], skipped=True)
        try:
            removed = freed = 0
            now_ = time.time()
            try:
                free_gb = shutil.disk_usage(self.root).free / 2 ** 30
            except OSError:
                free_gb = 100.0
            lim = self.CACHE_GB                                  # сколько кэша держать: диск свободен / так себе / почти полон
            budget = 0 if everything else int((lim[0] if free_gb > 40 else lim[1] if free_gb > 15 else lim[2]) * 2 ** 30)

            def scan(d):
                out = []
                try:
                    with os.scandir(d) as it:
                        for e in it:
                            try:
                                if e.is_file():
                                    st = e.stat()
                                    out.append((st.st_mtime, st.st_size, e.path, e.name))
                            except OSError:
                                pass
                except OSError:
                    pass
                return out

            def rm(path, size):
                nonlocal removed, freed
                try:
                    os.unlink(path)
                    removed += 1
                    freed += size
                except OSError:
                    pass                                         # файл занят (его сейчас читает плеер) — уйдёт в другой раз

            keep = 0
            for mt, size, path, name in sorted(scan(self.cache_dir), reverse=True):       # от свежих к старым
                if name.endswith(".tmp.npy"):
                    if now_ - mt > 3600:
                        rm(path, size)
                elif keep + size > budget:
                    rm(path, size)
                else:
                    keep += size
            fresh = {k for k, t in self._seen.items() if now_ - t < (600 if everything else 86400)}
            rend = scan(self.render_dir)
            parts = {}
            for mt, size, path, name in rend:
                m = re.match(r"part_(\d+)_[0-9a-f]+\.(wav|pk)$", name)
                if m:
                    parts.setdefault(int(m.group(1)), []).append((mt, size, path))
                elif name.endswith(".tmp.wav"):
                    if now_ - mt > 3600:
                        rm(path, size)
                elif re.match(r"(cue|snd)_.*\.wav$", name) and (everything or now_ - mt > 14 * 86400):
                    rm(path, size)
            order = sorted(parts, key=lambda k: max(x[0] for x in parts[k]), reverse=True)
            for pos, k in enumerate(order):                      # предпросмотр: держать 8 последних частей и открытые сегодня
                if k in fresh or (not everything and pos < 8):
                    continue
                for mt, size, path in parts[k]:
                    rm(path, size)
            for f, lim in ((self.dir / "edits.log", 8_000_000),):
                try:
                    if f.stat().st_size > lim:
                        os.replace(f, f.with_suffix(".old.log"))
                except OSError:
                    pass
            left = self.cache_stat(force=True)["mb"]
            if removed or everything:
                log(f"уборка кэша: убрано файлов {removed}, освободилось {gb(freed / 1048576)}; кэш теперь {gb(left)}"
                    + ("" if everything else f" (лимит {gb(budget / 1048576)})"), "ok")
            return dict(removed=removed, freed=round(freed / 1048576), left=left)
        finally:
            self.rlock.release()

    # ------------------------------------------------------------------ папки в Проводнике
    def open_folder(self, what: str, path: str = "") -> dict:
        """Открыть в Проводнике одну из папок книги (или показать готовый файл). Только свои папки."""
        places = {"gotovo": self.root / "gotovo", "parts": self.final_parts, "parts_mix": self.final_parts_mix,
                  "chapters": self.final_ch, "chapters_mix": self.final_ch_mix, "studio": self.dir,
                  "sounds": self.sounds_dir, "book": self.root, "log": self.dir / "studio.log",
                  "projects": self.proj_dir, "imports": self.dir / "imports", "text": self.root / "gotovo" / "tekst",
                  "engine_log": self.root / "studio_engine.log"}
        if what == "disk":
            d = self.disk_dir(False)
            if not d:
                raise Oops("папка книги на Яндекс Диске не найдена")
            p = Path(d)
        elif what == "project":                              # файл проекта — показать его в папке проектов
            p = self._proj_path(path)
        elif what == "file":
            p = Path(str(path)).resolve()
            roots = [self.root / "gotovo"] + ([Path(self.disk_dir(False))] if self.disk_dir(False) else [])
            if not p.is_file() or not any(r.resolve() in p.parents for r in roots):
                raise Oops("такого готового файла нет")
        else:
            p = places.get(what)
            if p is None:
                raise Oops("нет такой папки")
            if what not in ("log", "engine_log"):
                p.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            raise Oops(f"{p} пока нет")
        opened = False
        if os.name == "nt":
            try:
                if what in ("file", "project"):
                    subprocess.Popen(["explorer", "/select,", str(p)])
                else:
                    os.startfile(str(p))
                opened = True
            except OSError as e:
                raise Oops(f"не открылось: {e}")
        return dict(path=str(p), opened=opened)

    # ------------------------------------------------------------------ самопроверка
    def self_check(self) -> dict:
        """Всё ли на месте: книга, записи, ffmpeg, голоса, движок, видеокарта, звуки, Диск, место. Каждая строка идёт
        в журнал окна; итог виден в шапке журнала."""
        res = []

        def add(lvl, text):
            res.append((lvl, text))
            ev(text, lvl, "проверка")

        def safe(fn, name):
            try:
                fn()
            except Exception as e:
                add("warn", f"{name}: проверить не удалось ({type(e).__name__}: {e})")

        ev("самопроверка: начинаю", "run", "проверка")

        def book():
            st = getattr(n, "NOX_STAGE", 0)
            add("ok", f"книга «{self.title}»: фраз {len(self.rows)}, глав {len(self.chapters)}, частей собрано "
                      f"конвейером {len(self.plan())}")
            add("ok" if st in (20, STAGE_OK) else "warn",
                f"конвейер: этап {st}" + ("" if st in (20, STAGE_OK) else f" — звук студии сверен с этапом {STAGE_OK}; после "
                                          f"первой закреплённой части сравните её на слух с частью конвейера"))
            add("info", f"сборка: темп {n.TEMPO_G:g}, паузы {n.PAUSE_SCALE:g}, файлы {self.fmt} {self.bitrate} "
                        f"({self.asm_src})")
            try:
                names = set(os.listdir(n.WORK / "wav"))
            except OSError:
                names = set()
            have = sum(1 for w in self.wavs if w.name in names)
            miss = len(self.wavs) - have
            add("ok" if not miss else "info", f"черновые записи: {have} из {len(self.wavs)}"
                + (f" — ещё {miss} фраз конвейер не озвучил (эти части откроются позже)" if miss else ""))

        def tools():
            import soundfile
            add("ok", f"Python {sys.version.split()[0]}, numpy {np.__version__}, soundfile {soundfile.__version__}; "
                      f"ядер процессора {os.cpu_count()}, потоков на сборку части: {self.threads}")
            ff = self.ffmpeg()
            try:
                r = subprocess.run([ff, "-version"], capture_output=True, text=True, timeout=15)
                line = (r.stdout.splitlines() or ["?"])[0]
                add("ok" if r.returncode == 0 else "err", "ffmpeg: " + " ".join(line.split()[:3]) + f" ({ff})")
            except (OSError, subprocess.SubprocessError):
                add("err", "ffmpeg не найден — готовые файлы и изменение скорости работать не будут (его ставит go)")

        def voices():
            used = sorted({r for r in self.base_roles if r})
            bad = [r for r in used if not n.voice_files(r, self.stems)[0].exists()]
            add("ok" if not bad else "warn", f"голоса: ролей в книге {len(used)}"
                + ("" if not bad else f"; без образца голоса: {', '.join(bad[:8])} — переозвучить их фразы нельзя"))

        def engine():
            fast = self.root / "nox_fast.py"
            if not fast.exists():
                add("err", "nox_fast.py нет в папке книги — переозвучка не заработает (его кладёт go)")
            elif "studio_queue" not in fast.read_text(encoding="utf-8", errors="replace"):
                add("err", "nox_fast.py в папке книги прежней версии — заявки студии он не понимает; запустите команду "
                           "студии ещё раз, она обновит файл")
            else:
                ws = self.workers()
                add("ok", "движок озвучки: " + (f"на связи воркеров: {len(ws)}" if ws else
                                                "не запущен — запустится кнопкой «Запустить движок» (около минуты)"))
            g = self.gpu(0)
            if g is None:
                add("warn", "видеокарта NVIDIA не найдена (nvidia-smi не отвечает) — переозвучка на лету не заработает")
            else:
                low = g["free"] is not None and g["free"] < 7500 and not self.workers()
                add("warn" if low else "ok", f"видеокарта {g['name']}: память занята {gb(g['used'])} из {gb(g['total'])}"
                    + (f", загрузка {g['util']} %" if g["util"] is not None else "")
                    + (f", {g['temp']} °C" if g["temp"] is not None else "")
                    + (" — свободного мало: движку нужно около 7 ГБ (идёт озвучка книги?)" if low else ""))
            ram = _ram()
            if ram:
                add("ok" if ram[1] > 3000 else "warn", f"оперативная память: свободно {gb(ram[1])} из {gb(ram[0])}")

        def sounds():
            try:
                import nox_cues
                lab = nox_cues.labels()
                add("ok", f"распознаватель звуков: подписей {sum(len(v) for v in lab.values())}")
            except Exception as e:
                add("warn", f"распознаватель звуков не загрузился (nox_cues.py): {e}")
            kit = self.kit_dir()
            mine = sum(1 for f in self.sounds_dir.iterdir() if f.suffix.lower() in SND_EXT) if self.sounds_dir.is_dir() else 0
            if kit is None:
                add("info", f"звуки: своих файлов {mine}; общего комплекта (D:\\nox_audiobook\\zvuk) нет")
            else:
                tot = self.sound_list()["total"] - mine
                ks = self.kit_state()
                act = ks["kit"]
                add("ok", f"звуки: своих файлов {mine}, в комплекте {tot} ({act if act is not None else kit})"
                          + (" — с готовыми петлями фона" if ks["gen"] > 1 else "")
                          + ("; партитура книги есть" if self.kit_score() is not None else ""))
                sv = self.score_view()
                if sv["stale"]:
                    add("info", f"звуков партитуры на дорожках из прежнего комплекта: {sv['stale']} — они звучат как раньше; "
                                "«Обновить партитуру книги» возьмёт их из нового (ваши правки сохранятся)")
                for o in ks["others"]:
                    used = sum(1 for c in self.store["cues"] if str(c.get("file") or "").startswith(
                        "kit:" + o.resolve().relative_to(kit.resolve()).as_posix() + "/"))
                    add("info", f"прежний звуковой комплект {o.name}: " + (f"на его файлы ссылаются звуки на дорожках ({used}) — не удаляйте"
                                                                         if used else "на дорожках больше не используется; папку можно удалить, если не будете "
                                                                         "возвращаться к старым точкам возврата проекта (в них звуки стоят из него)"))
                if (self.root / "zvuk_k1a.py").exists() is False:
                    add("warn", "в папке книги нет zvuk_k1a.py — партитуру книги не перенести; запустите go (он положит файл)")

        def places():
            d = self.disk_dir(False)
            add("ok" if d else "info", f"Яндекс Диск: {d}" if d else
                "Яндекс Диск не найден — готовые файлы останутся только в папке книги (gotovo)")
            du = shutil.disk_usage(self.root)
            cs = self.cache_stat(force=True)
            add("ok" if du.free > 10 * 2 ** 30 else "warn",
                f"диск {(self.root.drive or '').rstrip(chr(92))}: свободно {gb(du.free >> 20)}; кэш студии {gb(cs['mb'])} "
                f"(файлов: {cs['files']})" + ("" if du.free > 10 * 2 ** 30 else " — места мало, очистите кэш кнопкой в журнале"))

        def dubs():
            r = self.heal_silent()
            if r["fixed"] or r["locked"]:
                add("warn", f"дубли без речи: выбор исправлен у фраз — {r['fixed']}; в закреплённых фразах осталось — {r['locked']} "
                            "(подробности — строками выше)")
            else:
                add("ok", "дубли: выбранных записей без речи нет")

        for fn, name in ((book, "книга"), (tools, "программы"), (voices, "голоса"), (engine, "движок"),
                         (sounds, "звуки"), (places, "папки"), (dubs, "дубли")):
            safe(fn, name)
        w, e = sum(1 for x in res if x[0] == "warn"), sum(1 for x in res if x[0] == "err")
        ev("самопроверка: всё на месте" if not (w or e) else
           f"самопроверка: ошибок {e}, замечаний {w} — они выше, красным и жёлтым", "err" if e else "warn" if w else "ok",
           "проверка")
        self.check = dict(at=now(), warn=w, err=e)
        return self.check

    def startup(self):
        """Фоновые дела после запуска: самопроверка и уборка кэша (окно их не ждёт)."""
        try:
            if self._upgraded:
                # первое открытие после обновления студии: точка возврата с правками, какими их оставила прежняя версия
                was, self._upgraded = self._upgraded, None
                try:
                    r = self.project_save(f"перед обновлением студии до {VERSION}", audio=False, kind="auto")
                    log(f"точка возврата «перед обновлением студии» записана: {Path(str(r.get('file') or r.get('path') or '')).name}. "
                        "Все правки, дубли и закрепления на месте", "ok")
                except Exception as e:
                    log(f"точка возврата перед обновлением не записана: {e}", "warn")
                with self.lock:
                    mx = self.store["mix"]
                    if was < "2026-11-12a" or was.startswith("до "):
                        # громкости по умолчанию с 12a: звуки 16 %, музыка 10 % (фон 12 %). Меняется только то, что
                        # стояло на прежнем значении по умолчанию; выставленное автором не трогается
                        for k, old in MIX_WAS.items():
                            if abs(float(mx.get(k, old)) - old) < 0.0005:
                                mx[k] = MIX_DEF[k]
                                log(f"громкость дорожки «{LANE_RU[k]}» по умолчанию теперь {round(MIX_DEF[k] * 100)} % (была "
                                    f"{round(old * 100)} %). Изменить — ползунком в «Звуки и музыка»", "edit", "звуки")
                    self.save("студия обновлена", {"label": f"{was} → {VERSION}"})
            try:
                self.heal_silent()
            except Exception:
                log("сбой проверки пустых дублей:\n" + traceback.format_exc(), "warn")
            if getattr(self, "_auto_was_run", False):
                self._auto_was_run = False
                self.auto_save()
                log(f"озвучка всей книги была прервана закрытием студии (часть {self.auto.get('part') or '?'}). Всё озвученное "
                    "цело — нажмите «Вся книга» → «Продолжить»", "warn", "книга")
            self.write_fixed()
            self.write_rules_dict()
            try:
                self.kit_prepare()
            except Exception:
                log("сбой распаковки звукового комплекта:\n" + traceback.format_exc(), "warn", "звуки")
            self.self_check()
            self.housekeeping()
        except Exception:
            log("сбой самопроверки:\n" + traceback.format_exc())

    # ------------------------------------------------------------------ движок озвучки
    def engine_view(self) -> dict:
        ws = self.workers()
        run = self.engine_proc is not None and self.engine_proc.poll() is None
        return dict(workers=ws, ready=bool(ws), starting=run and not ws, own=run, note="" if ws else self.engine_note,
                    second=self.engine2 is not None and self.engine2.poll() is None,
                    queued=sum(1 for j in self.jobs.values() if j["status"] in ("queued", "running")))

    def _engine_spawn(self, worker: int):
        """Запустить свой движок (nox_fast.py --serve) -> (процесс, команда). Вывод — в studio_engine.log."""
        fa = read_json(n.WORK / "fast_args.json", None) or {}
        st = getattr(n, "SETTINGS", None) or {}
        acc = str(fa.get("accent") or ("none" if st.get("accent") == "base" else st.get("accent") or "acute"))
        if acc not in ("none", "plus", "acute", "sparse", "dbl"):
            acc = "acute"
        model = str(fa.get("model") or ("Qwen/Qwen3-TTS-12Hz-1.7B-Base" if st.get("accent") == "base"
                                        else "shib79/qwen3-tts-12hz-1.7b-ru-stress-cf"))
        py = Path(sys.executable)
        if py.name.lower() == "pythonw.exe":
            py = py.with_name("python.exe")
        fast = self.root / "nox_fast.py"
        if not fast.exists():
            raise Oops(f"нет {fast} — запустите go, он положит файлы на место")
        try:
            if "studio_queue" not in fast.read_text(encoding="utf-8", errors="replace"):
                raise Oops("в папке книги прежняя версия nox_fast.py — она не умеет переозвучивать по заявкам студии. "
                           "Скачайте новый nox_fast.py и запустите команду студии (или go) ещё раз")
        except OSError as e:
            raise Oops(f"не читается {fast}: {e}")
        cmd = [str(py), str(fast), "--serve", "--worker", str(worker), "--model", model, "--accent", acc,
               "--max-new-tokens", str(fa.get("max_new_tokens") or 1500)]
        if fa.get("xvec"):
            cmd.append("--xvec")
        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1", HF_HOME=str(self.root / "hf"))
        env.pop("PYTORCH_CUDA_ALLOC_CONF", None)
        lp = self.root / "studio_engine.log"
        try:                                                   # вывод движка копится — большой файл начать заново
            if lp.stat().st_size > 8_000_000 and not self.workers():
                os.replace(lp, lp.with_suffix(".old.log"))
                self.eng_pos = 0
        except OSError:
            pass
        with open(lp, "ab") as lf:
            proc = subprocess.Popen(cmd, cwd=str(self.root), env=env, stdout=lf, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL)
        return proc, cmd

    def engine_start(self, force=False) -> dict:
        if self.workers():
            return self.engine_view()
        if self.engine_proc is not None and self.engine_proc.poll() is None:
            return self.engine_view()
        g = self.gpu(0)
        if g and g["free"] is not None and g["free"] < 7500 and not force:
            raise Oops(f"в видеопамяти свободно {g['free'] / 1024:.1f} ГБ из {g['total'] / 1024:.0f} — движку нужно около 7. "
                       f"Похоже, идёт озвучка книги прежней версией воркеров: запустите go ещё раз (новые воркеры сами "
                       f"обслуживают редактор), либо дождитесь конца озвучки")
        self.engine_proc, cmd = self._engine_spawn(9)
        self.e1_at = time.time()
        self.engine_note = "движок запускается (загрузка модели ~1 мин)"
        log("движок запускается — модель грузится около минуты: " + " ".join(cmd[2:]), "run", "движок")
        return self.engine_view()

    def engine_tail(self) -> str:
        try:
            b = (self.root / "studio_engine.log").read_bytes()[-1500:]
            return b.decode("utf-8", "replace")
        except OSError:
            return ""

    def engine_pump(self):
        """Новые строки вывода движка (studio_engine.log) -> журнал окна."""
        f = self.root / "studio_engine.log"
        try:
            size = f.stat().st_size
        except OSError:
            return
        if size < self.eng_pos:
            self.eng_pos = 0                                     # файл начат заново
        if size == self.eng_pos:
            return
        try:
            with open(f, "rb") as h:
                h.seek(self.eng_pos)
                data = h.read(min(size - self.eng_pos, 200_000))
        except OSError:
            return
        cut = data.rfind(b"\n")
        if cut < 0:
            if len(data) < 4000:
                return                                           # строка ещё дописывается
            cut = len(data) - 1
        self.eng_pos += cut + 1
        out = []
        for ln in data[:cut].decode("utf-8", "replace").split("\n"):
            ln = ln.rstrip("\r").split("\r")[-1].rstrip()         # полоса загрузки переписывает строку — берём последнее состояние
            if ln.strip():
                out.append(ln)
        if len(out) > 60:
            out = out[:20] + [f"… ещё {len(out) - 40} строк (весь вывод — в studio_engine.log)"] + out[-20:]
        for ln in out:
            low = ln.lower()
            bad = ln.startswith("Traceback") or "error:" in low or "ошибка" in low or "out of memory" in low \
                or "exception" in low
            ev(ln[:500], "err" if bad else "warn" if "warning" in low else "eng", "движок")

    def engine_scale(self):
        """Второй движок. Очередь заявок большая (переозвучка всех изменённых фраз части), книгу в это время никто не
        озвучивает, а видеопамяти свободно с запасом — поднимается второй движок, очередь идёт вдвое шире. Очередь
        опустела — через три минуты он выключается и освобождает видеопамять."""
        queued = sum(1 for j in self.jobs.values() if j["status"] in ("queued", "running"))
        e2 = self.engine2
        if e2 is not None:
            if e2.poll() is not None:
                self.engine2 = None
                log(f"второй движок остановился сам (код {e2.returncode}) — работа идёт на одном", "warn", "движок")
            elif queued:
                self.e2_busy = time.time()
            elif time.time() - self.e2_busy > 180:
                try:
                    e2.terminate()
                except OSError:
                    pass
                self.engine2 = None
                log("очередь пуста — второй движок выключен, видеопамять свободна", "info", "движок")
            return
        own = self.engine_proc is not None and self.engine_proc.poll() is None
        if not own or queued < 12 or time.time() - self.e2_tried < 300 or time.time() - self.e1_at < 150:
            return
        ws = self.workers()
        if len(ws) != 1 or ws[0]["mode"] != "serve":             # книгу озвучивает конвейер — его воркеры сами берут заявки
            return
        g = self.gpu(0)
        if not g or g["free"] is None or g["free"] < 9500:
            return
        self.e2_tried = time.time()
        try:
            self.engine2, _ = self._engine_spawn(8)
        except Exception as e:
            log(f"второй движок не запустился: {e}", "warn", "движок")
            return
        self.e2_busy = time.time()
        log(f"в очереди {queued} фраз, в видеопамяти свободно {g['free'] / 1024:.1f} ГБ — запущен второй движок "
            f"(выключится сам, когда очередь опустеет)", "run", "движок")

    def engine_watch(self):
        """Раз в две секунды: кто из воркеров на связи; не упал ли движок; нужен ли второй."""
        ws = {(w["w"], w["mode"]) for w in self.workers()}
        for w, mode in sorted(ws - self._wseen):
            log(f"воркер {w} на связи: " + ("принимает заявки редактора" if mode == "serve" else
                                            "озвучивает книгу и между фразами берёт заявки редактора"), "ok", "движок")
        for w, mode in sorted(self._wseen - ws):
            log(f"воркер {w} отключился", "info", "движок")
        self._wseen = ws
        if self.engine_proc is not None and self.engine_proc.poll() is not None:
            code = self.engine_proc.returncode
            self.engine_proc = None
            if not ws:
                tail = self.engine_tail().strip().splitlines()[-1:] or [""]
                self.engine_note = "движок остановился: " + tail[0][:200]
                log(f"{self.engine_note} (код {code}; весь вывод — в studio_engine.log)", "err" if code else "info", "движок")
        elif ws:
            self.engine_note = ""
        self.engine_scale()


    # ------------------------------------------------------------------ работа по частям: что закреплено — конвейеру
    def write_fixed(self):
        """Закреплённые части (подряд с первой) -> studio/fixed_parts.json. Конвейер их больше не трогает и берётся
        за следующую часть; дальше неё он ничего не озвучивает."""
        try:
            with self.lock:
                out = []                                 # все закреплённые части; какие из них идут подряд с первой,
                for p in self.plan():                    # конвейер считает сам (остальные тоже не трогает)
                    if p["opened"] and self.store["parts"][p["n"] - 1].get("locked"):
                        own = [j for j in range(p["a"], p["b"] + 1) if self.pidx[j] is not None]     # без вставок студии
                        if own:
                            out.append(dict(n=p["n"], a=self.pidx[own[0]], b=self.pidx[own[-1]],
                                            a_uid=self.uids[own[0]], b_uid=self.uids[own[-1]]))
            f = self.dir / "fixed_parts.json"
            old = read_json(f, None)
            if old is None or old.get("parts") != out:
                write_atomic(f, jd({"ver": 1, "parts": out, "at": now()}))
                pre = 0
                while pre < len(out) and out[pre]["n"] == pre + 1:
                    pre += 1
                log(f"конвейеру: закреплено частей — {len(out)}; он возьмётся за часть {pre + 1}")
        except Exception:
            log("не записан список закреплённых частей:\n" + traceback.format_exc(), "warn")

    def write_rules_dict(self):
        """Правила «дальше по всей книге» -> studio/rules_dict.tsv: конвейер читает их поверх словаря автора, поэтому
        следующие части сразу озвучиваются по ним."""
        try:
            with self.lock:
                rules = {r["word"]: r["to"] for r in self.store["rules"] if r.get("scope") == "book"}
                forms = {}
                if rules:
                    for row in self.rows:
                        for m in WORD_RE.finditer(row["text"]):
                            w = m.group(0)
                            to = rules.get(bare(w))
                            if to is not None and w not in forms:
                                forms[w] = to[:1].upper() + to[1:] if w[:1].isupper() else to
            plus = lambda s: re.sub("([" + VOWELS + "])" + ACUTE, r"+\1", s)
            body = ("# правила студии «дальше по всей книге» — их читает конвейер поверх словаря автора; руками не править\n"
                    + "".join(f"{w}\t{plus(to)}\n" for w, to in sorted(forms.items())))
            f = self.dir / "rules_dict.tsv"
            try:
                old = f.read_text(encoding="utf-8")
            except OSError:
                old = None
            if old != body and (forms or old is not None):
                write_atomic(f, body)
        except Exception:
            log("не записаны правила для конвейера:\n" + traceback.format_exc(), "warn")

    # ------------------------------------------------------------------ проект: работа автора одним файлом
    # Всё, что делается в студии, и так сразу пишется на диск (edits.json, дубли, свои записи). Файл проекта — снимок этой
    # работы одним файлом *.noxproj (обычный zip):
    #   «точка возврата»   — правки, вставки, обрезки, звуки дорожек, закрепления. Без записей: записи со своего диска
    #                        студия не удаляет никогда, поэтому для возврата на этом компьютере они не нужны. Мгновенно.
    #   «проект со звуком» — то же плюс все дубли, свои записи и библиотека звуков: резервная копия и перенос.
    # Загрузка ничего не затирает: нынешнее состояние сперва само сохраняется точкой возврата; записи только добавляются
    # (совпало имя, а звук другой — ложатся рядом под новым именем); книга переразбита — правки ищут свои фразы заново.
    PROJ_EXT, PROJ_FMT = ".noxproj", 1
    TAKE_ARC = re.compile(r"^takes/(s\d+)/\1_(t\d+)\.(wav|json)$")
    PACKED = (".mp3", ".ogg", ".oga", ".m4a", ".aac", ".opus", ".flac", ".wma")

    @property
    def proj_dir(self) -> Path:
        return self.dir / "projects"

    def home_id(self) -> str:
        """Метка этой папки студии. Проект с другой меткой сохранён в другом месте: его записи могут называться так же,
        как здешние, и быть другими — при загрузке такие ложатся отдельно."""
        f = self.dir / "home.id"
        try:
            v = f.read_text(encoding="ascii").strip()
            if re.fullmatch(r"[0-9a-f]{16}", v):
                return v
        except (OSError, UnicodeError):
            pass
        v = secrets.token_hex(8)
        write_atomic(f, v)
        return v

    def book_sig(self) -> str:
        """Отпечаток книги: какие реплики и в каком порядке (вставки студии не в счёт)."""
        return sha("\n".join(u for j, u in enumerate(self.uids) if self.pidx[j] is not None), 16)

    @staticmethod
    def project_counts(st: dict) -> dict:
        segs = st.get("segs") or {}
        return dict(edits=len(segs),
                    takes=sum(1 for e in segs.values() if e.get("take") or (e.get("locked") or {}).get("take")),
                    locked=sum(1 for e in segs.values() if e.get("locked")),
                    ins=len(st.get("ins") or []), rules=len(st.get("rules") or []), parts=len(st.get("parts") or []),
                    fixed=[q.get("n") for q in st.get("parts") or [] if q.get("locked")],
                    cues=len(st.get("cues") or []))

    @staticmethod
    def _sids_of(st: dict) -> list:
        ents = list((st.get("segs") or {}).values()) + [(o.get("e") or {}) for o in st.get("orphans") or []]
        return sorted({str(e["sid"]) for e in ents if isinstance(e, dict) and e.get("sid")})

    def _proj_audio(self, sids: list) -> list:
        """Записи, на которые опирается работа: дубли этих фраз, исходники своих записей, библиотека звуков автора
        -> [(имя в файле проекта, путь, размер)]."""
        out, sidset = [], set(sids)
        for sid in sids:
            d = self.takes_dir / sid
            try:
                names = sorted(x.name for x in os.scandir(d) if x.is_file())
            except OSError:
                continue
            have = set(names)
            for nm in names:
                m = re.fullmatch(re.escape(sid) + r"_(t\d+)\.json", nm)
                if not m or f"{sid}_{m.group(1)}.wav" not in have:
                    continue                                   # дубль без записи или запись без описания — не дубль
                for f in (d / nm, d / f"{sid}_{m.group(1)}.wav"):
                    try:
                        out.append((f"takes/{sid}/{f.name}", f, f.stat().st_size))
                    except OSError:
                        pass
        for d, pfx in ((self.dir / "imports", "imports"), (self.sounds_dir, "sounds")):
            try:
                ents = sorted(os.scandir(d), key=lambda z: z.name)
            except OSError:
                continue
            for x in ents:
                if not x.is_file() or x.name == "index.json" or x.name.endswith((".tmp", ".part")):
                    continue
                if pfx == "imports" and x.name.split("_")[0] not in sidset:
                    continue
                out.append((f"{pfx}/{x.name}", Path(x.path), x.stat().st_size))
        return out

    @staticmethod
    def _sha_file(f: Path) -> str:
        h = hashlib.sha1()
        with open(f, "rb") as src:
            while True:
                b = src.read(1 << 20)
                if not b:
                    break
                h.update(b)
        return h.hexdigest()

    def _proj_info(self, f: Path) -> dict:
        st = f.stat()
        key = (st.st_mtime_ns, st.st_size)
        c = self._pcache.get(str(f))
        if c and c[0] == key:
            return c[1]
        rel = f.relative_to(self.proj_dir).as_posix()
        info = dict(name=rel, file=str(f), size=st.st_size, mtime=st.st_mtime, ok=False, title="",
                    kind="auto" if rel.startswith("auto/") else "manual", note="", audio=False, counts={},
                    created=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)))
        try:
            import zipfile
            with zipfile.ZipFile(f) as zf:
                m = json.loads(zf.read("manifest.json").decode("utf-8"))
            if m.get("format") != "noxproj":
                raise ValueError("не проект")
            info.update(ok=True, title=m.get("title") or "", created=m.get("created") or info["created"],
                        note=m.get("note") or "", audio=bool(m.get("audio")), counts=m.get("counts") or {},
                        studio=m.get("studio"), ver=m.get("ver"), edits_sha=m.get("edits_sha"),
                        audio_bytes=m.get("audio_bytes"), audio_files=m.get("audio_files"))
        except Exception:
            info["note"] = "файл не читается как проект студии"
        if len(self._pcache) > 500:
            self._pcache.clear()
        self._pcache[str(f)] = (key, info)
        return info

    def _proj_infos(self) -> list:
        out = []
        for d in (self.proj_dir, self.proj_dir / "auto"):
            try:
                fs = [Path(x.path) for x in os.scandir(d) if x.is_file() and x.name.endswith(self.PROJ_EXT)]
            except OSError:
                continue
            for f in fs:
                try:
                    out.append(self._proj_info(f))
                except OSError:
                    pass
        out.sort(key=lambda x: (x.get("created") or "", x.get("mtime", 0)), reverse=True)
        return out

    def project_list(self) -> dict:
        out = self._proj_infos()
        with self.lock:
            data = json.dumps(self.store, ensure_ascii=False, indent=1).encode("utf-8")
            cur, sids = self.project_counts(self.store), self._sids_of(self.store)
        esha = hashlib.sha1(data).hexdigest()
        last = next((x for x in out if x["ok"]), None)
        dk = self.disk_dir(False)
        au = self._proj_audio(sids)
        return dict(list=out, dir=str(self.proj_dir), disk=str(Path(dk).parent / "proekt") if dk else None, now=cur,
                    dirty=not (last and last.get("edits_sha") == esha), last=last["name"] if last else None,
                    full_bytes=sum(s for _, _, s in au), full_files=len(au), busy=self._proj_busy_why())

    def _proj_path(self, name: str) -> Path:
        base = self.proj_dir.resolve()
        f = (base / str(name or "")).resolve()
        if f.suffix != self.PROJ_EXT or base not in f.parents or not f.is_file():
            raise Oops("такого файла проекта нет — обновите список")
        return f

    def _proj_prune(self, keep: int = 40, quick: bool = False):
        """Точки возврата копятся. Автоматических остаётся последние 40; quick — быстрых точек автора без заметки
        (Ctrl+S) остаётся последние 50. Точки с заметкой и проекты со звуком не убираются никогда."""
        try:
            if quick:
                fs = [x for x in self._proj_infos() if x["ok"] and x["kind"] != "auto" and not x["audio"] and not x["note"]]
                for x in fs[50:]:                              # список идёт от свежих к старым
                    try:
                        os.unlink(x["file"])
                        self._pcache.pop(x["file"], None)
                    except OSError:
                        pass
                return
            fs = sorted((x for x in os.scandir(self.proj_dir / "auto") if x.is_file() and x.name.endswith(self.PROJ_EXT)),
                        key=lambda z: z.stat().st_mtime)
            for x in fs[:-keep]:
                try:
                    os.unlink(x.path)
                except OSError:
                    pass
        except OSError:
            pass

    def project_save(self, note: str = "", audio: bool = False, kind: str = "manual", disk=None) -> dict:
        """Сохранить проект в файл. audio=False — точка возврата (без записей), True — проект со звуком.
        kind="auto" — служебная точка (перед загрузкой проекта, при закреплении части)."""
        import zipfile
        note = re.sub(r"\s+", " ", str(note or "")).strip()[:80]      # заметка автора — как написана (в имя файла идёт без знаков)
        fnote = safe_file(note)[:60].strip() if note else ""
        if not self.plock.acquire(False):
            raise Oops("проект как раз сохраняется или загружается — подождите")
        try:
            with self.lock:
                data = json.dumps(self.store, ensure_ascii=False, indent=1).encode("utf-8")
                sids, counts = self._sids_of(self.store), self.project_counts(self.store)
                sig, nrows = self.book_sig(), len(self.sidx)
                stress = jd(self.stress_load()).encode("utf-8")
            esha = hashlib.sha1(data).hexdigest()
            d = self.proj_dir / "auto" if kind == "auto" else self.proj_dir
            d.mkdir(parents=True, exist_ok=True)
            if kind != "auto" and not audio and not note:      # сохранение подряд: ничего не менялось — второй файл ни к чему
                last = next((x for x in self._proj_infos() if x["ok"] and x["kind"] != "auto"), None)
                if last and not last["audio"] and last.get("edits_sha") == esha:
                    return dict(last, same=True)
            au = self._proj_audio(sids)
            total = sum(s for _, _, s in au)
            if audio:
                free = shutil.disk_usage(self.dir).free
                if free < total + 300_000_000:
                    raise Oops(f"на диске мало места: проект со звуком займёт около {gb(total >> 20)}, свободно {gb(free >> 20)}")
            base = (safe_file(self.title)[:32].strip() + " " + time.strftime("%Y-%m-%d_%H-%M-%S") + (" " + fnote if fnote else "")
                    + (" со звуком" if audio else "")).strip()
            out, k = d / (base + self.PROJ_EXT), 2
            while out.exists():
                out, k = d / f"{base} ({k}){self.PROJ_EXT}", k + 1
            tmp = out.with_name(out.name + ".part")
            files, need, same = {}, {}, {}
            with Op(self, "проект со звуком: запись файла" if audio else "точка возврата", quiet=not audio) as op:
                try:
                    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
                        def put(arc, path=None, blob=None):
                            h, size = hashlib.sha1(), 0
                            if path is not None and path.suffix.lower() in self.PACKED:      # уже сжато — класть как есть
                                zi = zipfile.ZipInfo(arc, time.localtime()[:6])
                                zi.compress_type = zipfile.ZIP_STORED
                            else:
                                zi = arc
                            with zf.open(zi, "w") as dst:
                                if blob is not None:
                                    dst.write(blob)
                                    h.update(blob)
                                    size = len(blob)
                                else:
                                    with open(path, "rb") as src:
                                        while True:
                                            b = src.read(1 << 20)
                                            if not b:
                                                break
                                            dst.write(b)
                                            h.update(b)
                                            size += len(b)
                            files[arc] = [size, h.hexdigest()]

                        put("edits.json", blob=data)
                        put("stress.json", blob=stress)
                        for arc, path in (("sounds/index.json", self.sounds_dir / "index.json"),
                                          ("ref/roles.json", n.ROLES_PATH), ("ref/segments.csv", n.CSV_PATH)):
                            if path.exists():
                                put(arc, path)
                        if audio:
                            op.stage("записи: дубли, свои записи, звуки", len(au))
                            seen = {}                          # одна запись у нескольких фраз (жёсткие ссылки) — в файл один раз
                            for arc, path, _ in au:
                                op.step()
                                try:
                                    s_ = path.stat()
                                    key = (s_.st_dev, s_.st_ino) if s_.st_nlink > 1 and s_.st_ino else None
                                    if key is not None and key in seen:
                                        same[arc] = seen[key]
                                        continue
                                    put(arc, path)
                                    if key is not None:
                                        seen[key] = arc
                                except FileNotFoundError:      # дубль как раз убрали в корзину
                                    continue
                            vd = getattr(n, "VOICES", None)
                            if vd is not None and Path(vd).is_dir():       # образцы голосов — для справки
                                for f in sorted(Path(vd).iterdir()):
                                    if f.is_file() and f.suffix.lower() in (".wav", ".txt"):
                                        put(f"ref/voices/{f.name}", f)
                        else:
                            need = {arc: size for arc, _, size in au}
                        man = dict(format="noxproj", ver=self.PROJ_FMT, studio=VERSION, created=now(), title=self.title,
                                   book=self.root.name, home=self.home_id(), note=note, kind=kind, audio=bool(audio),
                                   rows=nrows, book_sig=sig, counts=counts, edits_sha=esha, audio_bytes=total,
                                   audio_files=len(au))
                        zf.writestr("files.json", jd({"files": files, "need": need, "same": same}))
                        zf.writestr("manifest.json", jd(man))
                    op.stage("проверка записанного файла")
                    with zipfile.ZipFile(tmp) as zf:
                        bad = zf.testzip()
                    if bad:
                        raise Oops(f"файл проекта записался с ошибкой ({bad}) — проверьте диск и повторите")
                    for q in range(5):
                        try:
                            os.replace(tmp, out)
                            break
                        except OSError:
                            if q == 4:
                                raise
                            time.sleep(0.3 * (q + 1))
                finally:
                    tmp.unlink(missing_ok=True)
                op.note = f"{out.name}, {out.stat().st_size / 1e6:.1f} МБ"
            info = dict(self._proj_info(out))
            if kind == "auto":
                self._proj_prune()
            elif not audio and not note:
                self._proj_prune(quick=True)
            if kind != "auto" and (disk or (disk is None and info["size"] < 60_000_000)):
                info["disk"] = self.project_to_disk(info["name"], quiet=True, sub="tochki" if not audio else "")
            self.journal({"why": "проект сохранён", "file": str(out), "audio": bool(audio), "kind": kind})
            if kind != "auto" and not audio:
                log(f"точка возврата сохранена: {out.name}" + (" (копия — на Яндекс Диске)" if info.get("disk") else ""), "ok")
            return info
        finally:
            self.plock.release()

    def project_to_disk(self, name: str, quiet: bool = False, sub: str = ""):
        """Копия файла проекта в папку книги на Яндекс Диске (proekt). Точки возврата, которые студия кладёт туда сама,
        идут в proekt\\tochki — там остаются последние 30; копии, сделанные кнопкой «На Диск», не убираются никогда."""
        f = self._proj_path(name)
        dk = self.disk_dir()
        if not dk:
            if quiet:
                return None
            raise Oops("папка книги на Яндекс Диске не найдена")
        try:
            dst = Path(dk).parent / "proekt" / sub if sub else Path(dk).parent / "proekt"
            n.copy_to(dst, f)
            if sub:
                old = sorted((x for x in os.scandir(dst) if x.is_file() and x.name.endswith(self.PROJ_EXT)),
                             key=lambda z: z.stat().st_mtime)
                for x in old[:-30]:
                    try:
                        os.unlink(x.path)
                    except OSError:
                        pass
            return str(dst / f.name)
        except Exception as e:
            if quiet:
                log(f"копия проекта на Диск не сделана: {e}", "warn")
                return None
            raise Oops(f"на Диск не скопировано: {e}")

    def project_delete(self, name: str):
        """Убрать файл проекта из списка — в корзину студии (studio\\trash\\projects), не насовсем."""
        f = self._proj_path(name)
        tr = self.dir / "trash" / "projects"
        tr.mkdir(parents=True, exist_ok=True)
        dst, k = tr / f.name, 2
        while dst.exists():
            dst, k = tr / f"{f.stem} ({k}){f.suffix}", k + 1
        os.replace(f, dst)
        self._pcache.pop(str(f), None)
        log(f"файл проекта убран в корзину студии: {f.name}", "edit", "правка")

    def project_adopt(self, tmp: Path, name: str) -> dict:
        """Файл проекта, выбранный автором на компьютере, -> в папку проектов студии (копия; исходный файл не трогается)."""
        import zipfile
        name = Path(str(name).replace("\\", "/")).name
        try:
            with zipfile.ZipFile(tmp) as zf:
                m = json.loads(zf.read("manifest.json").decode("utf-8"))
            if m.get("format") != "noxproj":
                raise ValueError
        except Exception:
            tmp.unlink(missing_ok=True)
            raise Oops(f"«{name}» — не файл проекта студии (нужен файл *.noxproj, сохранённый студией)")
        stem = safe_file(Path(name).stem)[:100].strip() or "proekt"
        size, h = tmp.stat().st_size, None
        for d in (self.proj_dir, self.proj_dir / "auto"):      # этот самый файл уже лежит среди проектов — второй не нужен
            try:
                same = [Path(x.path) for x in os.scandir(d) if x.is_file() and x.name.endswith(self.PROJ_EXT) and x.stat().st_size == size]
            except OSError:
                continue
            for f in same:
                h = h or self._sha_file(tmp)
                if self._sha_file(f) == h:
                    tmp.unlink(missing_ok=True)
                    return self._proj_info(f)
        out, k = self.proj_dir / (stem + self.PROJ_EXT), 2
        while out.exists():
            out, k = self.proj_dir / f"{stem} ({k}){self.PROJ_EXT}", k + 1
        os.replace(tmp, out)
        log(f"файл проекта принят: {out.name}", "ok")
        return self._proj_info(out)

    def _proj_read(self, f: Path):
        import zipfile
        try:
            zf = zipfile.ZipFile(f)
            m = json.loads(zf.read("manifest.json").decode("utf-8"))
            if m.get("format") != "noxproj":
                raise ValueError
        except Exception:
            raise Oops(f"«{f.name}» не читается как файл проекта студии: он повреждён или это другой файл")
        try:
            if int(m.get("ver") or 0) > self.PROJ_FMT:
                raise Oops(f"этот проект сохранён более новой студией ({m.get('studio')}) — обновите студию и загрузите его снова")
            raw = zf.read("edits.json")
            st = json.loads(raw.decode("utf-8"))
            fl = json.loads(zf.read("files.json").decode("utf-8"))
            if not isinstance(st, dict) or not isinstance(st.get("segs"), dict) or not isinstance(fl, dict):
                raise ValueError
            if m.get("edits_sha") and hashlib.sha1(raw).hexdigest() != m["edits_sha"]:
                raise ValueError
        except Oops:
            zf.close()
            raise
        except Exception:
            zf.close()
            raise Oops(f"файл проекта «{f.name}» повреждён: правки в нём не читаются. Ничего не изменено")
        files = fl.get("files") or {}
        for arc, canon in (fl.get("same") or {}).items():     # запись-двойник: в файле лежит один раз, под именем canon
            if canon in files and arc not in files:
                files[arc] = list(files[canon]) + [canon]
        return zf, m, st, files, fl.get("need") or {}

    def _proj_compat(self, m: dict, st: dict) -> dict:
        segs = st.get("segs") or {}
        ins = {x.get("id") for x in st.get("ins") or []}
        keys = {k for j, k in enumerate(self.keys) if self.pidx[j] is not None}
        found = sum(1 for u in segs if u in ins or u.split(".")[0] in keys)
        same = m.get("book_sig") == self.book_sig()
        other = not same and ((len(segs) >= 5 and found < 0.3 * len(segs))
                              or (len(segs) < 5 and (m.get("title") or "") != self.title))
        return dict(same=same, other=other, found=found, lost=len(segs) - found, total=len(segs))

    def _trash_find(self, sid: str, tid: str, ext: str = "wav"):
        c = sorted((self.dir / "trash" / sid).glob(f"{sid}_{tid}__*.{ext}"))
        return c[-1] if c else None

    def _proj_busy_why(self) -> str:
        """Почему проект сейчас нельзя загрузить ('' — можно)."""
        if any(j["status"] in ("queued", "running") for j in self.jobs.values()):
            return "идёт переозвучка фраз — дождитесь дублей"
        if self.auto_on():
            return "идёт озвучка всей книги — поставьте её на паузу («Вся книга» → «Пауза»)"
        if self.go_proc is not None and self.go_proc.poll() is None:
            return "идёт озвучка части — дождитесь её конца или остановите"
        ps = read_json(n.WORK / "part_status.json", None) or {}
        if ps.get("state") == "working":
            try:
                age = time.time() - time.mktime(time.strptime(str(ps.get("at")), "%Y-%m-%d %H:%M:%S"))
            except Exception:
                age = 1e9
            if age < 180:
                return "конвейер сейчас озвучивает часть (работает go) — дождитесь, когда он закончит"
        if self.tool_busy:
            return "идёт работа с голосами — подождите"
        return ""                                          # сверка ударений не мешает: её итоги привязаны к тексту фраз

    def _proj_audio_check(self, files: dict, need: dict) -> dict:
        """Быстрая оценка (по именам и размерам): какие записи проекта уже есть на диске, какие добавятся."""
        r = dict(have=0, add=0, differ=0, trash=0, missing=0)
        for arc, v in list(files.items()) + list(need.items()):
            m = self.TAKE_ARC.match(arc)
            if not m or m.group(3) != "wav":
                continue
            size = v[0] if isinstance(v, list) else v
            dst = self.take_path(m.group(1), m.group(2))
            if dst.exists():
                r["have" if dst.stat().st_size == size else "differ"] += 1
            elif arc in files:
                r["add"] += 1                                  # в том числе запись-двойник: она лежит в файле под другим именем
            elif self._trash_find(m.group(1), m.group(2)):
                r["trash"] += 1
            else:
                r["missing"] += 1
        return r

    @staticmethod
    def _span(ns: list) -> str:
        ns = sorted(x for x in ns if isinstance(x, int))
        if not ns:
            return "нет"
        return f"{ns[0]}–{ns[-1]}" if len(ns) > 1 and ns == list(range(ns[0], ns[-1] + 1)) else ", ".join(map(str, ns))

    def project_check(self, name: str) -> dict:
        """Что будет, если загрузить этот проект: подходит ли он к книге, что изменится, чего не хватает. Ничего не меняет."""
        f = self._proj_path(name)
        zf, m, st, files, need = self._proj_read(f)
        zf.close()
        with self.lock:
            cur, cp = self.project_counts(self.store), self._proj_compat(m, st)
            roles = set(self.roles["roles"])
        pc = self.project_counts(st)
        info = self._proj_info(f)
        if cp["other"]:
            return dict(ok=False, name=name, info=info, now=cur, proj=pc, compat=cp, audio={}, warn=[], busy="",
                        why=f"это проект другой книги («{m.get('title') or '?'}»): из {cp['total']} его правок в этой книге "
                            f"нашлись {cp['found']}. Сюда он не загружается.")
        au = self._proj_audio_check(files, need)
        warn = []
        if not cp["same"] and cp["lost"]:
            warn.append(f"Книга с тех пор переразбита: из {cp['total']} правок проекта свои фразы найдут {cp['found']}, "
                        f"остальные {cp['lost']} сохранятся в списке «без места».")
        elif not cp["same"]:
            warn.append("Книга с тех пор переразбита, но все правки проекта находят свои фразы.")
        if set(cur["fixed"]) - set(pc["fixed"]):
            back = sorted(set(cur["fixed"]) - set(pc["fixed"]))
            warn.append(f"Закреплённых частей станет меньше: сейчас — {self._span(cur['fixed'])}, в проекте — "
                        f"{self._span(pc['fixed'])}. Части {self._span(back)} снова станут рабочими; конвейер при следующем "
                        f"запуске возьмётся за первую незакреплённую.")
        if au["missing"]:
            warn.append(f"Записей не найдено: {au['missing']} (их нет ни в файле, ни на диске, ни в корзине студии). У таких фраз "
                        f"будет пометка «потерян дубль» — их можно переозвучить или вернуть к черновой записи.")
        if au["trash"]:
            warn.append(f"Дублей, удалённых позже, вернётся из корзины студии: {au['trash']}.")
        if au["differ"]:
            warn.append(f"Записей с тем же именем, но другим звуком: {au['differ']} — они лягут рядом под новыми именами, "
                        f"здешние записи не тронутся.")
        gone = sorted({str(e.get("role")) for e in st["segs"].values() if e.get("role") and e.get("role") not in roles})
        if gone:
            warn.append("Ролей из проекта сейчас нет в книге: " + ", ".join(gone[:6]) + (" и др." if len(gone) > 6 else "")
                        + " — у таких фраз останется роль из разметки.")
        if m.get("home") != self.home_id():
            warn.append("Проект сохранён в другой папке или на другом компьютере: его записи лягут отдельно от здешних.")
        return dict(ok=True, name=name, info=info, now=cur, proj=pc, compat=cp, audio=au, warn=warn,
                    busy=self._proj_busy_why(), why="")

    def _zip_extract(self, zf, arc: str, dst: Path, want_sha=None):
        import zipfile
        import zlib
        tmp = dst.with_name(dst.name + ".part")
        h = hashlib.sha1()
        try:
            with zf.open(arc) as src, open(tmp, "wb") as out:
                while True:
                    b = src.read(1 << 20)
                    if not b:
                        break
                    out.write(b)
                    h.update(b)
            if want_sha and h.hexdigest() != want_sha:
                raise zipfile.BadZipFile("не та сумма")
            os.replace(tmp, dst)
            return True
        except (zipfile.BadZipFile, EOFError, KeyError, zlib.error, OSError) as e:
            if isinstance(e, OSError) and not isinstance(e, zipfile.BadZipFile) and getattr(e, "errno", None):
                raise Oops(f"запись «{dst.name}» не легла на диск: {e}. Загрузка отменена — правки студии не изменены")
            log(f"файл проекта: запись «{arc}» повреждена ({type(e).__name__}: {e}) — пропущена", "warn")
            return False
        finally:
            tmp.unlink(missing_ok=True)

    def _trash_back(self, sid: str, tid: str, ext: str) -> bool:
        src = self._trash_find(sid, tid, ext)
        if src is None:
            return False
        dst = self.takes_dir / sid / f"{sid}_{tid}.{ext}"
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".part")
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
        return True

    def project_load(self, name: str) -> dict:
        f = self._proj_path(name)
        if not self.plock.acquire(False):
            raise Oops("проект как раз сохраняется или загружается — подождите")
        try:
            why = self._proj_busy_why()
            if why:
                raise Oops("сейчас проект не загрузить: " + why)
            if not self.rlock.acquire(False):
                raise Oops("сейчас проект не загрузить: идёт сборка части — дождитесь её конца")
            try:
                return self._project_load(f)
            finally:
                self.rlock.release()
        finally:
            self.plock.release()

    def _project_load(self, f: Path) -> dict:
        zf, m, st, files, need = self._proj_read(f)
        try:
            with self.lock:
                cp = self._proj_compat(m, st)
            if cp["other"]:
                raise Oops(f"это проект другой книги («{m.get('title') or '?'}»): из {cp['total']} его правок в этой книге "
                           f"нашлись {cp['found']}. Сюда он не загружается")
            # 1. нынешнее состояние — в точку возврата: загрузку всегда можно отыграть назад
            snap = self.project_save(note="перед загрузкой проекта", kind="auto")
            stat = dict(have=0, added=0, remapped=0, trash=0, missing=0, damaged=0)
            with Op(self, f"загрузка проекта «{f.stem}»") as op:
                names = set(zf.namelist())
                foreign = m.get("home") != self.home_id()
                with self.lock:
                    seq = max(int(self.store.get("seq", 0) or 0), int(st.get("seq", 0) or 0))
                    old_parts = {q.get("n"): (q.get("export") or {}) for q in self.store.get("parts") or []}
                    old_ch = {c: ((q or {}).get("export") or {}) for c, q in (self.store.get("chapters") or {}).items()}
                # 2. записи: только добавляются, ни одна здешняя не заменяется
                by_sid = {}
                for arc, v in list(files.items()) + list(need.items()):
                    mm = self.TAKE_ARC.match(arc)
                    if mm:
                        by_sid.setdefault(mm.group(1), []).append(
                            (arc, mm.group(2), mm.group(3), v[0] if isinstance(v, list) else v,
                             v[1] if isinstance(v, list) else None))
                canon = {arc: v[2] for arc, v in files.items() if isinstance(v, list) and len(v) > 2}
                placed = {}                                        # имя в файле проекта -> где запись легла на диске
                remap = {}
                maps = read_json(self.dir / "sid_map.json", None) or {}       # прошлые загрузки проектов из других папок:
                known = maps.setdefault(str(m.get("home") or "?"), {})        # куда тогда легли записи — туда же и теперь
                sids = sorted(set(self._sids_of(st)) | set(by_sid))
                op.stage("записи: что уже есть, что добавить", len(sids))

                def clash_in(c: str, strict: bool, pf: dict) -> bool:
                    """Занята ли папка записей c другим звуком: файл с тем же номером дубля отличается от проектного;
                    strict (проект из другой папки) — ещё и любой здешний дубль, которого в проекте нет."""
                    try:
                        local = [x for x in os.scandir(self.takes_dir / c) if x.is_file()]
                    except OSError:
                        return False
                    for x in local:
                        mt = re.fullmatch(re.escape(c) + r"_(t\d+)\.wav", x.name)
                        if not mt:
                            continue
                        q = pf.get(mt.group(1))
                        if q is None:
                            if strict:
                                return True
                        elif x.stat().st_size != q[1] or (q[2] and (q[0] in names or canon.get(q[0]) in names)
                                                          and self._sha_file(Path(x.path)) != q[2]):
                            return True
                    return False

                for sid in sids:
                    op.step()
                    items = by_sid.get(sid, [])
                    pf = {tid: (arc, size, hsh) for arc, tid, ext, size, hsh in items if ext == "wav"}
                    tsid = None
                    if known.get(sid) and not clash_in(known[sid], False, pf):
                        tsid = known[sid]
                    elif not clash_in(sid, foreign, pf):
                        tsid = sid
                    if tsid is None:
                        seq += 1
                        tsid = f"s{seq:06d}"
                    if tsid != sid:
                        remap[sid] = known[sid] = tsid
                        stat["remapped"] += 1
                    for arc, tid, ext, size, hsh in items:
                        dst = self.takes_dir / tsid / f"{tsid}_{tid}.{ext}"
                        src_arc = arc if arc in names else canon.get(arc)
                        if dst.exists():
                            stat["have"] += ext == "wav"
                            placed[arc] = dst
                        elif src_arc in names:
                            dst.parent.mkdir(parents=True, exist_ok=True)
                            twin = placed.get(src_arc)             # эта же запись уже легла у другой фразы — ссылка, не копия
                            done = False
                            if twin is not None and twin.exists():
                                try:
                                    os.link(twin, dst)
                                    done = True
                                except OSError:
                                    pass
                            if done or self._zip_extract(zf, src_arc, dst, hsh):
                                stat["added"] += ext == "wav"
                                placed[arc] = dst
                                placed.setdefault(src_arc, dst)
                            else:                                  # запись в файле проекта испорчена: дубль не восстановлен
                                stat["damaged"] += 1
                        elif tsid == sid and self._trash_back(sid, tid, ext):
                            stat["trash"] += ext == "wav"
                        else:
                            stat["missing"] += ext == "wav"
                if remap:
                    write_atomic(self.dir / "sid_map.json", jd(maps))
                op.stage("свои записи и библиотека звуков")
                for arc, v in files.items():
                    kind = arc.split("/", 1)[0]
                    nm = Path(arc).name
                    if kind not in ("imports", "sounds") or not nm or nm == "index.json" or arc not in names:
                        continue
                    dst = (self.dir / "imports" if kind == "imports" else self.sounds_dir) / nm
                    if dst.exists():
                        if dst.stat().st_size == v[0]:
                            continue
                        dst = dst.with_name(f"{dst.stem}__{str(v[1])[:6]}{dst.suffix}")      # имя занято другим файлом
                        if dst.exists():
                            continue
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    if not self._zip_extract(zf, arc, dst, v[1]):
                        stat["damaged"] += 1
                snd_add, pst = {}, {}
                try:
                    if "sounds/index.json" in names:
                        snd_add = json.loads(zf.read("sounds/index.json").decode("utf-8")) or {}
                    if "stress.json" in names:
                        pst = json.loads(zf.read("stress.json").decode("utf-8")) or {}
                except Exception:
                    pass
                # 3. правки проекта: номера и счётчики — так, чтобы новые записи никогда не легли поверх имеющихся
                op.stage("правки проекта")
                new = self.store_defaults(st)
                stale = []
                for e in list(new["segs"].values()) + [(o.get("e") or {}) for o in new["orphans"]]:
                    if not isinstance(e, dict) or not e.get("sid"):
                        continue
                    if e["sid"] in remap:
                        e["sid"] = remap[e["sid"]]
                    mx = 0
                    try:
                        for x in os.scandir(self.takes_dir / e["sid"]):
                            mt = re.match(r"s\d+_t(\d+)\.", x.name)
                            if mt:
                                mx = max(mx, int(mt.group(1)))
                    except OSError:
                        pass
                    for x in (self.dir / "trash" / e["sid"]).glob(f"{e['sid']}_t*__*"):       # и удалённые: их номера заняты
                        mt = re.match(r"s\d+_t(\d+)__", x.name)
                        if mt:
                            mx = max(mx, int(mt.group(1)))
                    if mx > int(e.get("tseq", 0) or 0):
                        e["tseq"] = mx
                for q in new["parts"]:                             # готовые файлы на диске собраны из нынешнего состояния:
                    ex = q.get("export")                           # если оно другое — файл помечается устаревшим
                    if ex and (old_parts.get(q.get("n"), {}).get("ver") != ex.get("ver") or not Path(str(ex.get("file") or "")).exists()):
                        ex["ver"] = ""
                        stale.append(q.get("n"))
                for c, q in new["chapters"].items():
                    ex = (q or {}).get("export")
                    if ex and old_ch.get(c, {}).get("ver") != ex.get("ver"):
                        q["export"] = None
                # 4. сама замена — одним действием. До атомарной записи edits.json любое исключение
                # возвращает и модель, и все производные индексы к прежнему состоянию.
                with self.lock:
                    orph0 = len(new["orphans"])
                    self.backup()
                    new["seq"] = max(seq, int(self.store.get("seq", 0) or 0))
                    old_store = self.store
                    row_attrs = ("rows", "roles", "keys", "uids", "pidx", "sidx", "idx_of", "id2i", "_spans",
                                 "_clock", "stems", "base_roles", "role_cnt", "wavs", "chapters", "rows_sig",
                                 "rows_at", "_abs")
                    missing = object()
                    old_rows = {key: getattr(self, key, missing) for key in row_attrs}
                    old_undo, old_takes, old_jobs = list(self.undo), dict(self._takes), dict(self.jobs)
                    old_parts_cache = self._parts_cache
                    self.store = new
                    try:
                        self.load_rows(force=True, strict=True)     # без промежуточной записи reanchor
                        self.undo.clear()
                        self._takes.clear()
                        self.jobs.clear()
                        self._parts_cache = None
                        self.save("загружен проект", {"file": f.name, "count": len(new["segs"])})
                    except Exception:
                        # save() сначала атомарно заменяет edits.json, а затем пишет только журнал/UI.
                        # Если исключение случилось после замены, новое состояние уже является durable commit.
                        try:
                            committed = (self.dir / "edits.json").read_text(encoding="utf-8") == json.dumps(
                                self.store, ensure_ascii=False, indent=1)
                        except Exception:
                            committed = False
                        if not committed:
                            self.store = old_store
                            for key, value in old_rows.items():
                                if value is missing:
                                    self.__dict__.pop(key, None)
                                else:
                                    setattr(self, key, value)
                            self.undo, self._takes, self.jobs = old_undo, old_takes, old_jobs
                            self._parts_cache = old_parts_cache
                            raise
                        log("проект сохранён, но не все служебные записи обновились:\\n" + traceback.format_exc(), "warn")
                    lost = len(self.store["orphans"]) - orph0
                    if snd_add:
                        try:
                            idx = self.sound_index()
                            add = {k: v for k, v in snd_add.items() if k not in idx and (self.sounds_dir / k).exists()}
                            if add:
                                idx.update(add)
                                write_atomic(self.sounds_dir / "index.json", jd(idx))
                        except Exception:
                            log("проект загружен, но библиотека звуков обновлена не полностью:\\n" + traceback.format_exc(), "warn")
                    if pst:
                        stress_before = None if self.stress is None else dict(self.stress)
                        try:
                            cur_st = self.stress_load()
                            fresh = {u: v for u, v in pst.items() if u not in cur_st}
                            if fresh:
                                cur_st.update(fresh)
                                write_atomic(self.dir / "stress.json", jd(cur_st))
                        except Exception:
                            # stress_load() кеширует словарь; при неудачной записи не оставляем
                            # в памяти импорт, которого нет на диске.
                            self.stress = stress_before
                            log("проект загружен, но словарь ударений обновлён не полностью:\\n" + traceback.format_exc(), "warn")
                    counts = self.project_counts(self.store)
                op.note = (f"правок фраз {counts['edits']}, вставок {counts['ins']}; записей добавлено {stat['added']}, уже было {stat['have']}"
                           + (f", из корзины {stat['trash']}" if stat["trash"] else "")
                           + (f", не найдено {stat['missing']}" if stat["missing"] else "")
                           + (f", повреждено в файле {stat['damaged']}" if stat["damaged"] else "")
                           + (f"; правок без места {lost}" if lost > 0 else ""))
            self.write_fixed()
            self.write_rules_dict()
            log(f"загружен проект «{f.name}» ({m.get('created')}). Прежнее состояние сохранено точкой возврата «{Path(snap['name']).name}» — "
                f"загрузите её так же, если нужно вернуться", "ok")
            return dict(name=f.name, snapshot=snap["name"], counts=counts, lost=max(lost, 0), stale=stale, **stat)
        finally:
            zf.close()

    # ------------------------------------------------------------------ инструменты книги (python конвейера)
    def tool_py(self, which: str = "fast") -> str:
        if which == "design":                              # среда с моделью создания голосов
            pd = self.root / "env" / "python.exe"
            if pd.exists():
                return str(pd)
        ov = os.environ.get("NOX_TOOL_PY")
        if ov:
            return ov
        py = Path(sys.executable)
        return str(py.with_name("python.exe") if py.name.lower() == "pythonw.exe" else py)

    def run_tool(self, args, which="fast", timeout=900, pump=None):
        """Инструмент книги (nox_voices.py, nox_stress.py) — скрыто. -> (код, вывод). pump — имя источника: строки
        вывода идут в журнал по ходу работы."""
        if not (self.root / str(args[0])).exists():
            raise Oops(f"нет {self.root / str(args[0])} — запустите go, он положит файлы на место")
        cmd = [self.tool_py(which), str(self.root / str(args[0]))] + [str(x) for x in args[1:]]
        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1", HF_HOME=str(self.root / "hf"))
        kw = {"creationflags": 0x08000000} if os.name == "nt" else {}
        pr = subprocess.Popen(cmd, cwd=str(self.root), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kw)
        out, t0 = [], time.time()
        killer = threading.Timer(timeout, pr.kill)
        killer.start()
        try:
            for raw in pr.stdout:
                line = raw.decode("utf-8", "replace").rstrip()
                out.append(line)
                if pump and line.strip():
                    log(line.strip()[:400], "eng", pump)
            pr.wait()
        finally:
            killer.cancel()
        return pr.returncode, "\n".join(out)

    # ------------------------------------------------------------------ самопроверка ударений
    def stress_load(self):
        if self.stress is None:
            self.stress = read_json(self.dir / "stress.json", None) or {}
        return self.stress

    def stress_of(self, uid: str, tech: str) -> list:
        """Расхождения второй модели ударений во фразе, ещё не разобранные автором."""
        ent = self.stress_load().get(uid)
        if not ent or not ent.get("f") or ent.get("h") != sha(tech, 16):
            return []
        ok = self.store.get("stress_ok") or {}
        places, forms = set((ok.get("places") or {}).get(uid) or []), ok.get("forms") or {}
        out = []
        for f in ent["f"]:
            if f["k"] in places or forms.get(f["w"]) == bare(f["ours"]) + "|" + str(f["ours"].lower().find(ACUTE)):
                continue
            out.append(dict(k=f["k"], w=f["w"], ours=f["ours"], sil=f["sil"], kind=f.get("kind"),
                            imp=bool(f.get("homo") or f.get("neural") or f.get("kind") == "nomark"),
                            why="омограф: ударение зависит от смысла" if f.get("homo") else
                                "слова нет в словаре ударений" if f.get("neural") else
                                "слово осталось без знака ударения" if f.get("kind") == "nomark" else "обычное слово"))
        return out

    def stress_view(self, k: int, segs: list, auto: bool = False) -> dict:
        st = self.stress_load()
        todo = [s for s in segs if not s["locked"] and (st.get(s["uid"]) or {}).get("h") != sha(s["tech"], 16)]
        imp = sum(1 for s in segs if any(f["imp"] for f in s["st"]))
        rest = sum(1 for s in segs if s["st"] and not any(f["imp"] for f in s["st"]))
        busy = self.stress_busy == k
        have = (self.root / "nox_stress.py").exists() and not os.environ.get("NOX_NO_STRESS")
        if auto and todo and have and not busy and self.stress_busy is None and time.time() - self.stress_run_at.get(k, 0) > 600:
            self.stress_run_at[k] = time.time()
            threading.Thread(target=self.stress_run, args=(k,), daemon=True).start()
            busy = True
        return dict(imp=imp, rest=rest, todo=len(todo), busy=busy, have=have)

    def stress_run(self, k: int):
        """Сверить ударения части второй моделью (nox_stress.py, процессор). Результат — значки «уд.» у фраз."""
        if self.stress_busy is not None:
            return
        self.stress_busy = k
        try:
            with self.lock:
                ridx = self.rule_index()
                items = []
                for i in self.part_range(k):
                    if self.rows[i]["kind"] == "head" and len(self.rows[i]["text"]) <= 3:
                        continue
                    if i in self._abs:
                        continue
                    ef = self.eff(i, ridx)
                    if not ef["locked"]:
                        items.append(dict(key=self.uids[i], raw=self.row_text(i), tech=ef["tech"]))
            st = self.stress_load()
            items = [it for it in items if (st.get(it["key"]) or {}).get("h") != sha(it["tech"], 16)]
            if not items:
                return
            fin, fout = self.cache_dir / f"stress_in_{k}.json", self.cache_dir / f"stress_out_{k}.json"
            write_atomic(fin, jd(items))
            fout.unlink(missing_ok=True)
            with Op(self, f"сверка ударений части {k}", len(items), src="ударения") as op:
                op.stage("вторая модель ударений читает фразы", len(items))
                code, out = self.run_tool(["nox_stress.py", "--root", str(self.root), "--in", str(fin), "--out", str(fout)], timeout=1800)
                res = read_json(fout, None)
                if code != 0 or res is None:
                    op.quiet = True
                    log(f"сверка ударений части {k} не выполнилась (код {code}): " + (out.strip().splitlines() or ["нет вывода"])[-1][:300], "warn", "ударения")
                    return
                with self.lock:
                    for it in items:
                        st[it["key"]] = dict(h=sha(it["tech"], 16), f=(res.get("items") or {}).get(it["key"]) or [])
                    if len(st) > 60000:
                        for u in [u for u in st if u not in self.idx_of][:20000]:
                            st.pop(u, None)
                    write_atomic(self.dir / "stress.json", jd(st))
                nf = sum(1 for it in items if st[it["key"]]["f"])
                op.note = (res.get("note") or f"фраз проверено {len(items)}, с расхождениями {nf}") + " — у таких фраз значок «уд.»"
        except Oops as e:
            log(f"сверка ударений: {e}", "warn", "ударения")
        except Exception:
            log("сбой сверки ударений:\n" + traceback.format_exc(), "warn", "ударения")
        finally:
            self.stress_busy = None

    def stress_keep(self, uid: str, k: int, everywhere: bool):
        """«Оставить»: ударение здесь верное (везде — эта форма слова верна по всей книге)."""
        with self.lock:
            ent = self.stress_load().get(uid) or {}
            f = next((x for x in ent.get("f") or [] if x["k"] == int(k)), None)
            if f is None:
                raise Oops("этого расхождения уже нет — обновите окно")
            ok = self.store.setdefault("stress_ok", {"places": {}, "forms": {}})
            if everywhere:
                ok.setdefault("forms", {})[f["w"]] = bare(f["ours"]) + "|" + str(f["ours"].lower().find(ACUTE))
            else:
                pl = ok.setdefault("places", {}).setdefault(uid, [])
                if int(k) not in pl:
                    pl.append(int(k))
            self.save("ударение: оставить", {"uid": uid, "word": f["w"], "all": bool(everywhere)})

    def stress_set(self, uid: str, k: int, everywhere: bool) -> dict:
        """«Поставить так»: ударение второй модели. Здесь — правка текста фразы; везде — правило по всей книге."""
        with self.lock:
            i = self.idx_of.get(uid)
            ent = self.stress_load().get(uid) or {}
            f = next((x for x in ent.get("f") or [] if x["k"] == int(k)), None)
            if i is None or f is None:
                raise Oops("этого расхождения уже нет — обновите окно")
            ef = self.eff(i)
            if ef["locked"]:
                raise Oops("фраза закреплена — сначала снимите закрепление")
            if everywhere:
                return self.add_rule(f["w"], f["sil"], "book", uid, False)
            ms = list(WORD_RE.finditer(ef["tech"]))
            if int(k) >= len(ms) or bare(ms[int(k)].group(0)) != f["w"]:
                raise Oops("текст фразы с тех пор изменился — запустите сверку ударений ещё раз")
            m = ms[int(k)]
            self.set_seg(uid, {"tech": ef["tech"][:m.start()] + f["sil"] + ef["tech"][m.end():]})
            return {"count": 1}

    # ------------------------------------------------------------------ голоса: банк, подбор, проба
    def bank(self, fresh=False, harvest=False) -> dict:
        if fresh or self._bank is None or time.time() - self._bank[0] > 30:
            code, out = self.run_tool(["nox_voices.py", "list", "--root", str(self.root), "--json"] + (["--harvest"] if harvest else []), timeout=180)
            try:
                d = json.loads(out.strip().splitlines()[-1])
            except Exception:
                raise Oops("банк голосов не прочитан: " + (out.strip().splitlines() or ["нет вывода"])[-1][:300])
            self._bank = (time.time(), d)
        return self._bank[1]

    def _file_sig(self, wav_p: Path, txt_p: Path) -> str:
        """Отпечаток образца (wav + текст образца) — тот же, что у nox_voices.py; по размеру и времени файла не пересчитывается."""
        try:
            a = wav_p.stat()
            key = (str(wav_p), a.st_size, a.st_mtime_ns, txt_p.stat().st_mtime_ns if txt_p.exists() else 0)
        except OSError:
            return ""
        c = self._fsig.get(key[0])
        if c and c[0] == key:
            return c[1]
        try:
            sig = hashlib.sha1(wav_p.read_bytes() + (txt_p.read_bytes() if txt_p.exists() else b"")).hexdigest()[:12]
        except OSError:
            return ""
        self._fsig[key[0]] = (key, sig)
        return sig

    def voices_view(self, fresh=False) -> dict:
        # образцы голосов книги — в банк сразу, тем же запуском инструмента, что и список (обновлённый образец виден без запуска go)
        idle = bool(fresh) and not self.tool_busy and not (self.go_proc is not None and self.go_proc.poll() is None)
        b = self.bank(fresh, harvest=idle)
        self.load_rows()
        with self.lock:
            aliased = set(self.roles.get("aliases", {}))
            cnt = {}
            for r in self.base_roles:
                cnt[r] = cnt.get(r, 0) + 1
            approved = ("narrator", "Джек", "Хорват") if self.root.name.lower().startswith("kniga1") else ()
            bdir = Path(b["bank"]) if b.get("bank") else None
            hist = {}                                          # у каких ролей есть прежние образцы (голос меняли)
            try:
                for nm in os.listdir(n.WORK / "voices_old"):
                    m_ = re.match(r"(.+)__[0-9]{8}-[0-9]{6}(-[0-9]+)?\.wav$", nm)
                    if m_:
                        hist[m_.group(1)] = hist.get(m_.group(1), 0) + 1
            except OSError:
                pass
            here = {}                                          # сколько фраз роли в открытых (взятых в работу) частях
            for p in self.plan():
                if p["opened"]:
                    for i in range(p["a"], p["b"] + 1):
                        here[self.base_roles[i]] = here.get(self.base_roles[i], 0) + 1
            roles = []
            for name, cfg in self.roles["roles"].items():
                if cfg.get("same_as") or name in aliased:
                    continue
                vo = cfg.get("voice") or {}
                pv = (cfg.get("voice_prev") or {}).get("file")
                wv, tx = n.voice_files(name, self.stems)
                upd = False
                if vo.get("bank") and bdir is not None:      # голос роли взят из банка, а там образец с тех пор обновили
                    bw = bdir / (re.sub(r"[^A-Za-z0-9_]", "", str(vo["bank"])) + ".wav")
                    s1, s2 = self._file_sig(wv, tx), self._file_sig(bw, bw.with_suffix(".txt"))
                    upd = bool(s1 and s2 and s1 != s2)
                roles.append(dict(name=name, count=cnt.get(name, 0), gender=cfg.get("gender"), age=cfg.get("age"),
                                  has=wv.exists(), bank=vo.get("bank"), vname=vo.get("name"),
                                  by=vo.get("by"), approved=name in approved, upd=upd, here=here.get(name, 0),
                                  hist=hist.get(wv.stem, 0),
                                  prev=bool(pv) and (n.WORK / "voices_old" / (str(pv) + ".wav")).exists()))
            roles.sort(key=lambda r: (r["name"] != "narrator", -r["count"]))
        return dict(roles=roles, bank=b.get("voices") or [], dir=b.get("bank"), busy=self.tool_busy)

    def voice_path(self, role: str = "", bank: str = "") -> Path:
        if bank:
            d = Path(self.bank().get("bank") or ".")
            return d / (re.sub(r"[^A-Za-z0-9_]", "", bank) + ".wav")
        return n.voice_files(role, self.stems)[0]

    def voices_changed(self):
        """Образец голоса роли сменился: имена черновых записей роли теперь другие — перечитать книгу."""
        try:
            n._VSIG.clear()
        except Exception:
            pass
        self._bank = None
        self._fsig.clear()
        self.tcache_save(True)
        self.load_rows(force=True)
        with self.lock:
            self._parts_cache = None

    def go_busy(self):
        if self.auto_on():
            raise Oops("идёт озвучка всей книги — голоса меняются на паузе («Вся книга» → «Пауза»)")
        if self.go_proc is not None and self.go_proc.poll() is None:
            raise Oops("идёт озвучка части — голоса меняются после неё (или остановите озвучку)")

    def voice_revert(self, role: str, file: str = "", first: bool = False) -> dict:
        """Вернуть роли прежний образец голоса: предыдущий (без ключей), любой из истории (file) или самый первый —
        тот, с которым роль начинала книгу (first: «сбросить голос роли»)."""
        self.go_busy()
        args = ["nox_voices.py", "revert", "--root", str(self.root), "--role", role]
        if first:
            args += ["--first"]
        elif file:
            args += ["--file", re.sub(r"[^A-Za-z0-9_\-]", "", file)]
        code, out = self.run_tool(args, timeout=120)
        last = (out.strip().splitlines() or [""])[-1]
        if code != 0:
            raise Oops((last or "прежний голос не возвращён")[:300])
        same = "ничего не менялось" in last
        self.voices_changed()
        if same:
            log(f"роль «{role}»: этот образец голоса у неё уже стоит — ничего не менялось", "info", "голоса")
        else:
            log(f"роль «{role}»: " + ("голос сброшен к первоначальному (с ним роль начинала книгу)" if first else
                                      "возвращён прежний образец голоса")
                + ". Записи, сделанные этим образцом раньше, снова действуют; остальные фразы роли в незакреплённых частях "
                  "ждут переозвучки. Закреплённые части не изменились", "edit", "голоса")
        return dict(self.voices_view(), same=same)

    def voice_history(self, role: str) -> dict:
        code, out = self.run_tool(["nox_voices.py", "history", "--root", str(self.root), "--role", role, "--json"], timeout=120)
        try:
            return json.loads(out.strip().splitlines()[-1])
        except Exception:
            raise Oops("прежние образцы не прочитаны: " + (out.strip().splitlines() or ["нет вывода"])[-1][:300])

    def voice_old_path(self, role: str, file: str) -> Path:
        stem = n.voice_files(role, self.stems)[0].stem
        name = re.sub(r"[^A-Za-z0-9_\-]", "", file)
        if not name.startswith(stem + "__"):
            raise Oops("это не прежний образец этой роли")
        return n.WORK / "voices_old" / (name + ".wav")

    def voice_tidy(self, apply: bool = False) -> dict:
        """Порядок в голосах: образцы без роли, записи банка без файла, пустые ссылки ролей. apply=False — только отчёт."""
        if apply:
            self.go_busy()
            if self.tool_busy:
                raise Oops(f"уже идёт: {self.tool_busy}")
        code, out = self.run_tool(["nox_voices.py", "tidy", "--root", str(self.root), "--json"] + (["--apply"] if apply else []), timeout=180)
        try:
            d = json.loads(out.strip().splitlines()[-1])
        except Exception:
            raise Oops("порядок в голосах не наведён: " + (out.strip().splitlines() or ["нет вывода"])[-1][:300])
        if apply:
            self.voices_changed()
            nw = len([x for x in d["book"]["orphans"] if str(x).lower().endswith(".wav")])
            log(f"порядок в голосах: образцов без роли убрано в work\\voices_old\\bez_roli — {nw}; записей банка без файла — "
                f"{len(d['bank']['dead'])}; файлов банка без записи — {len(d['bank']['stray'])}. Ничего не удалено", "edit", "голоса")
        return d

    def voice_impact(self, role: str) -> dict:
        """Что затронет смена голоса роли: сколько её фраз в открытых незакреплённых частях ждёт переозвучки, сколько закреплено
        (они не изменятся), сколько ещё не озвучено вовсе. Ничего не меняет."""
        self.load_rows()
        with self.lock:
            if role not in self.roles["roles"]:
                raise Oops("такой роли нет")
            total = redo = locked = later = 0
            parts = {}
            opened = [p for p in self.plan() if p["opened"]]
            in_open = set()
            for p in opened:
                plk = bool(self.store["parts"][p["n"] - 1].get("locked"))
                for i in range(p["a"], p["b"] + 1):
                    if i in self._abs:
                        continue
                    e = self.store["segs"].get(self.uids[i]) or {}
                    r = (e.get("locked") or {}).get("role") or e.get("role") or self.base_roles[i]
                    if r != role:
                        continue
                    in_open.add(i)
                    total += 1
                    if plk or e.get("locked"):
                        locked += 1
                    elif e.get("take") or self.wavs[i].exists():
                        tk = self.takes_of(e).get(e.get("take") or "") or {}
                        if tk.get("src") == "import":
                            continue                         # своя запись автора от голоса роли не зависит
                        redo += 1
                        parts[p["n"]] = parts.get(p["n"], 0) + 1
            for i, r in enumerate(self.base_roles):
                if r == role and i not in in_open and i not in self._abs:
                    total += 1
                    later += 1
            return dict(role=role, total=total, redo=redo, locked=locked, later=later,
                        parts=[dict(n=k, count=v) for k, v in sorted(parts.items())])

    def voice_apply(self, role: str, vid: str) -> dict:
        self.go_busy()
        if role not in self.roles["roles"]:
            raise Oops("такой роли нет")
        code, out = self.run_tool(["nox_voices.py", "apply", "--root", str(self.root), "--role", role, "--voice", vid], timeout=120)
        if code != 0:
            raise Oops((out.strip().splitlines() or ["голос не поставлен"])[-1][:300])
        self.voices_changed()
        log(f"роль «{role}»: поставлен голос из банка ({vid}); прежний образец — в work\\voices_old. Фразы этой роли в "
            f"незакреплённых частях ждут переозвучки («Озвучить изменённые»)", "edit", "голоса")
        return self.voices_view()

    def voice_suggest(self, role: str) -> dict:
        code, out = self.run_tool(["nox_voices.py", "suggest", "--root", str(self.root), "--role", role, "--json"], timeout=180)
        try:
            return (json.loads(out.strip().splitlines()[-1]) or {}).get(role) or {"cand": []}
        except Exception:
            raise Oops("подбор не выполнен: " + (out.strip().splitlines() or ["нет вывода"])[-1][:300])

    PROBE_MIN = 25                                           # букв в пробе: на коротком слове голос не расслышать

    def probe_text(self, role: str, uid: str = ""):
        """Что читать для пробы голоса -> (технический текст, номера фраз, показ) или None. Проба должна показать голос,
        поэтому нужна фраза не короче PROBE_MIN букв: выделенная фраза роли, если она такая; иначе реплика роли на
        40–170 знаков (сначала из открытых частей); у роли одни короткие реплики — несколько первых подряд.
        До 12a проба читала выделенную фразу как есть: на «Наконец-то.» движок молчал, и пустая проба запоминалась."""
        let = lambda s: len(re.sub(r"[^А-Яа-яЁё]", "", s))
        say = lambda s: s.replace(ACUTE, "")                   # что услышит автор: технический текст без знаков ударения
        tech = lambda j: self.eff(j)["tech"].strip()           # длина — по техническому тексту: читает движок его
        i = self.idx_of.get(uid) if uid else None
        if i is not None and i not in self._abs and let(tech(i)) >= self.PROBE_MIN:
            return tech(i), [self.rows[i]["id"]], say(tech(i))
        mine = [j for j, r in enumerate(self.base_roles) if r == role and self.rows[j]["kind"] != "head" and j not in self._abs]
        opened = [p for p in self.plan() if p["opened"]]
        last = opened[-1]["b"] if opened else -1
        cand = [j for j in mine if 40 <= len(self.rows[j]["text"]) <= 170]
        for j in [q for q in cand if q <= last][:40] + cand[:40]:      # сначала из открытых частей
            if 40 <= len(tech(j)) <= 220 and let(tech(j)) >= self.PROBE_MIN:
                return tech(j), [self.rows[j]["id"]], say(tech(j))
        if not mine:
            return None
        big = max(mine[:400], key=lambda q: let(tech(q)))
        if let(tech(big)) >= self.PROBE_MIN:
            return tech(big), [self.rows[big]["id"]], say(tech(big))
        start = mine.index(i) if i in mine else 0
        out, ids, tot = [], [], 0
        for j in (mine[start:] + mine[:start])[:6]:
            s = tech(j)
            if not let(s):
                continue
            out.append(s if s[-1] in ".!?\u2026" else s.rstrip(",;:\u2014\u2013- ") + ".")
            ids.append(self.rows[j]["id"])
            tot += let(s)
            if tot >= self.PROBE_MIN:
                break
        return (" ".join(out), ids, say(" ".join(out))) if out else None

    def voice_try(self, role: str, vid: str, uid: str = "") -> dict:
        """Проба: реплика роли голосом из банка (движок читает её с чужим образцом; в книге ничего не меняется)."""
        with self.lock:
            pr = self.probe_text(role, uid)
            if pr is None:
                raise Oops("у этой роли нет реплик для пробы")
            tech, ids, shown = pr
        wav = self.voice_path(bank=vid)
        if not wav.exists():
            raise Oops("у этой заготовки ещё нет образца — сначала «Создать образец»")
        st_ = wav.stat()
        key = sha(f"p2|{vid}|{tech}|{st_.st_size}|{st_.st_mtime_ns}", 16)      # p2: пробы прежних версий (среди них пустые) не берутся
        d = self.cache_dir / "try"
        d.mkdir(exist_ok=True)
        out = d / f"{key}.wav"
        if not out.exists() and key not in self.tries:
            rid = f"q{int(time.time() * 1000):x}{secrets.token_hex(2)}"
            write_atomic(self.qdir / f"{rid}.req.json", jd(dict(rid=rid, role=role, text=tech, raw=tech.replace(ACUTE, ""), out=str(out),
                                                           variant={}, tries=2, rescue=True,
                                                           ref=dict(wav=str(wav), txt=str(wav.with_suffix(".txt"))))))
            self.tries[key] = dict(rid=rid, t=time.time())
            log(f"проба голоса: роль «{role}», голос {vid}, " + ("фраза № " if len(ids) == 1 else "фразы № ") + ", ".join(map(str, ids))
                + ("" if self.workers() else " — движок запускается, первая проба около минуты"), "run", "голоса")
        return dict(key=key, ready=out.exists() and key not in self.tries, text=shown, id=ids[0], ids=ids)

    def try_state(self, key: str) -> dict:
        key = re.sub(r"[^0-9a-f]", "", key)
        out = self.cache_dir / "try" / f"{key}.wav"
        tr = self.tries.get(key)
        if not tr:
            return dict(ready=out.exists())
        done = read_json(self.qdir / f"{tr['rid']}.done.json", None)
        if done is None and time.time() - tr["t"] <= 600:
            return dict(ready=False)                           # ждём ответ движка (запись появляется чуть раньше ответа)
        for f in self.qdir.glob(f"{tr['rid']}.*"):
            f.unlink(missing_ok=True)
        self.tries.pop(key, None)
        why = [str(w) for w in ((done or {}).get("why") or [])]
        if done is None or not done.get("ok") or not out.exists():
            out.unlink(missing_ok=True)
            return dict(ready=False, error="проба не получилась: " + str((done or {}).get("error") or "движок не ответил"))
        if done.get("silent") or any(w.startswith(DEAD_WHY + WEAK_WHY) for w in why):
            out.unlink(missing_ok=True)                        # тишину не запоминаем: следующая «Проба» попросит движок заново
            log("проба голоса: движок вернул тишину вместо речи (" + (", ".join(why) or "без пояснений")
                + f"; попыток {done.get('tried') or done.get('attempts') or 1}) — нажмите «Проба» ещё раз", "warn", "голоса")
            return dict(ready=False, error="проба не получилась: движок вернул тишину вместо речи. Нажмите «Проба» ещё раз — "
                                           "обычно со второго раза выходит")
        return dict(ready=True)

    def voice_tool(self, what: str, ids: str = "", spec: dict | None = None) -> dict:
        """Долгие дела с голосами в фоне (ход — в журнале): design — создать образцы заготовок; auto — дать голоса ролям
        без образца (из банка, по описанию); custom — свой голос по описанию (spec: name, desc, g, age)."""
        if self.tool_busy:
            raise Oops(f"уже идёт: {self.tool_busy}")
        if what == "auto":
            self.go_busy()
        if what == "custom":
            sp = spec or {}
            name, desc = str(sp.get("name") or "").strip(), str(sp.get("desc") or "").strip()
            if len(name) < 2 or len(desc) < 8:
                raise Oops("нужны название голоса и описание — хотя бы несколько слов (лучше по-английски)")
            g = "f" if sp.get("g") == "f" else "m"
            age = sp.get("age") if sp.get("age") in ("child", "teen", "young", "adult", "mature", "old") else "adult"
            args, label = ["nox_voices.py", "custom", "--root", str(self.root), "--name", name, "--desc", desc, "--g", g,
                           "--age", age], f"создание своего голоса «{name}»"
            return self._voice_bg(args, label)
        if what == "design":
            args, label = ["nox_voices.py", "design", "--root", str(self.root)] + (["--only", ids] if ids else []), \
                "создание образцов заготовок" + ("" if ids else " (все — около 10 минут)")
        elif what == "auto":
            args, label = ["nox_voices.py", "prepare", "--root", str(self.root), "--all"], "подбор голосов ролям без образца"
        else:
            raise Oops("нет такого действия")
        return self._voice_bg(args, label)

    def _voice_bg(self, args, label: str) -> dict:
        self.tool_busy = label

        def work():
            try:
                with Op(self, label, src="голоса") as op:
                    code, out = self.run_tool(args, which="design", timeout=3600, pump="голоса")
                    if code != 0:
                        op.quiet = True
                        log(f"{label}: не выполнено (код {code}) — причина строкой выше", "err", "голоса")
                self.voices_changed()
            except Oops as e:
                log(f"{label}: {e}", "err", "голоса")
            except Exception:
                log(f"{label}: сбой\n" + traceback.format_exc(), "err", "голоса")
            finally:
                self.tool_busy = None
        threading.Thread(target=work, daemon=True).start()
        return dict(busy=label)

    # ================================================================== ВСЯ КНИГА КАК ЕСТЬ (автомат)
    # «Озвучить всю книгу»: студия сама ведёт книгу часть за частью тем же путём, каким автор шёл бы руками:
    #   открытую незакреплённую часть принимает как есть (изменённые фразы сначала переозвучивает), переносит на неё звуки
    #   и музыку партитуры, закрепляет и собирает её файл; главы собирает сразу, как только закреплена их последняя часть;
    #   потом запускает конвейер (go) на следующую часть — и так до конца книги. Голоса — те, что стоят у ролей.
    # Пауза останавливает конвейер (озвученное цело), «Продолжить» идёт с того же места. Состояние лежит в
    # studio\auto_book.json и переживает закрытие студии: после запуска автомат стоит на паузе и ждёт слова автора.
    # Части, принятые автоматом, помечены (parts[k].auto) — видно, что автор их не слушал; любую можно открыть,
    # снять закрепление, поправить и закрепить снова (главу тогда — «Собрать заново»).
    AUTO_DEF = {"v": 1, "state": "idle", "sound": True, "step": "", "part": 0, "err": "", "parts": [], "chapters": [],
                "secs": 0.0, "fails": 0, "since": "", "at": "", "stamp": ""}

    def disk_sync(self) -> int:
        """Дослать на Яндекс Диск готовые файлы, которых там нет (или они другого размера): части, главы, все версии, текст.
        -> сколько файлов скопировано. Диска нет — 0."""
        d = self.disk_dir()
        if not d:
            return 0
        k, src = 0, self.root / "gotovo"
        try:
            subs = [x for x in src.iterdir() if x.is_dir()]
        except OSError:
            return 0
        for sub in subs:
            for f in sorted(sub.iterdir()):
                if not f.is_file() or ".tmp" in f.name or f.name.endswith(".mix.wav"):
                    continue
                dst = Path(d) / sub.name / f.name
                try:
                    if dst.exists() and dst.stat().st_size == f.stat().st_size:
                        continue
                    n.copy_to(dst.parent, f)
                    k += 1
                except Exception as e:
                    log(f"на Диск не скопировано ({sub.name}\\{f.name}): {e}", "warn", "книга")
        return k

    def auto_load(self):
        a = dict(self.AUTO_DEF, **(read_json(self.dir / "auto_book.json", None) or {}))
        self._auto_was_run = a["state"] == "run"
        if a["state"] == "run":                              # студию закрыли посреди работы
            a["state"], a["step"] = "pause", "студия была закрыта — озвучка книги стоит на паузе"
        self.auto = a

    def auto_save(self):
        try:
            write_atomic(self.dir / "auto_book.json", jd(self.auto))
        except OSError as e:
            log(f"состояние озвучки книги не записано: {e}", "warn", "книга")

    def auto_on(self) -> bool:
        return self.auto.get("state") == "run"

    def auto_guard(self, what: str = "это сделать"):
        if self.auto_on():
            raise Oops(f"идёт озвучка всей книги — {what} можно на паузе («Вся книга» → «Пауза»)")

    def _locked_rows(self, pl) -> int:
        return sum(p["b"] - p["a"] + 1 for p in pl if p["opened"] and self.store["parts"][p["n"] - 1].get("locked"))

    def auto_view(self, pl=None) -> dict:
        a = self.auto
        pl = pl if pl is not None else self.plan()
        total = max(1, len(self.rows))
        lk = self._locked_rows(pl)
        run = time.time() - self._auto_t0 if self._auto_t0 else 0.0
        made = lk - self._auto_rows0
        eta = round(run / made * (total - lk)) if (self._auto_t0 and made > 0 and run > 120) else None
        return dict(state=a["state"], step=a.get("step") or "", part=int(a.get("part") or 0), err=a.get("err") or "",
                    sound=bool(a.get("sound", True)), pct=round(100 * lk / total, 1),
                    locked=sum(1 for p in pl if p["opened"] and self.store["parts"][p["n"] - 1].get("locked")),
                    auto_parts=len(a.get("parts") or []), chapters=sum(1 for c in self.chapters
                                                                       if (self.store["chapters"].get(str(c)) or {}).get("export")),
                    chapters_total=len(self.chapters), secs=round(float(a.get("secs") or 0) + run), eta=eta,
                    kit=self.kit_score() is not None, since=a.get("since") or "", ch_busy=self.ch_busy,
                    stamp=a.get("stamp") or "", disk=self.disk_dir(False),
                    vars=[dict(id=v[0], name=v[3], on=bool((self.store["mix"].get("vars") or {}).get(v[0], True))) for v in MIX_VARS],
                    done=bool(pl) and pl[-1]["b"] >= len(self.rows) - 1 and all(
                        p["opened"] and self.store["parts"][p["n"] - 1].get("locked") for p in pl))

    def _auto_set(self, say=True, **kw):
        with self.lock:
            new = kw.get("step") and kw["step"] != self.auto.get("step")
            self.auto.update(kw, at=now())
            self.auto_save()
        if say and new:
            log("вся книга — " + kw["step"], "run", "книга")

    def _auto_acc(self):
        if self._auto_t0:
            self.auto["secs"] = float(self.auto.get("secs") or 0) + (time.time() - self._auto_t0)
            self._auto_t0 = None

    def auto_start(self, sound=None) -> dict:
        with self.lock:
            if self.auto_th is not None and self.auto_th.is_alive():
                if self.auto_on():
                    raise Oops("озвучка всей книги уже идёт")
                raise Oops("озвучка книги ещё останавливается — подождите несколько секунд и нажмите снова")
            if self.tool_busy:
                raise Oops(f"сейчас идёт: {self.tool_busy} — дождитесь конца и нажмите снова")
            if self.auto_view()["done"]:
                raise Oops("вся книга уже озвучена и закреплена. Главы — в «Частях и главах» (кнопка «Собрать готовые главы»)")
            a = self.auto
            if sound is not None:
                a["sound"] = bool(sound)
            a.update(state="run", err="", step="начинаю", fails=0, since=a.get("since") or now(), at=now())
            if not a.get("stamp"):
                a["stamp"] = time.strftime("kniga_%Y-%m-%d_%H-%M")     # папка этой книги на Яндекс Диске
            self.auto_save()
            self._auto_t0, self._auto_rows0 = time.time(), self._locked_rows(self.plan())
            self._auto_redone = {}
            self.auto_th = threading.Thread(target=self._auto_loop, daemon=True)
            self.auto_th.start()
        log("озвучка всей книги начата: части идут одна за другой, голоса — те, что стоят у ролей"
            + ("; звуки и музыка партитуры ставятся сами" if a["sound"] and self.kit_score() is not None else "")
            + ". Остановить — «Пауза»; всё озвученное сохраняется", "run", "книга")
        return self.auto_view()

    def auto_pause(self) -> dict:
        with self.lock:
            if not self.auto_on():
                raise Oops("озвучка всей книги сейчас не идёт")
            self.auto.update(state="pause", step=f"пауза на части {self.auto.get('part') or '?'}: остановлено вами", at=now())
            self._auto_acc()
            self.auto_save()
        log("озвучка всей книги поставлена на паузу. Всё озвученное и закреплённое сохранено; «Продолжить» пойдёт с того "
            "же места", "warn", "книга")
        if self.go_proc is not None and self.go_proc.poll() is None:
            try:
                self.go_stop()
            except Oops:
                pass
        return self.auto_view()

    def _auto_halt(self, msg: str):
        with self.lock:
            self.auto.update(state="error", err=msg[:600], step="остановлено: нужна ваша помощь", at=now())
            self._auto_acc()
            self.auto_save()
        log("озвучка всей книги остановлена: " + msg + " — после исправления нажмите «Вся книга» → «Продолжить»", "err", "книга")

    @staticmethod
    def _stay_awake(on: bool):
        """Пока идёт озвучка книги, компьютер не засыпает сам (экран гаснуть может). Windows; действует для потока автомата."""
        if os.name != "nt":
            return
        try:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | (0x00000001 if on else 0))
        except Exception:
            pass

    def _auto_loop(self):
        self._stay_awake(True)
        try:
            k = self.disk_sync()                              # в папке книги на Диске должно лежать всё, что уже собрано
            if k:
                log(f"на Яндекс Диск досланы готовые файлы, которых там не было: {k} — папка {self.disk_dir(False)}", "ok", "книга")
            while self.auto_on():
                try:
                    if not self._auto_step():
                        break
                except Oops as e:
                    if self.auto_on():
                        self._auto_halt(str(e))
                    break
                except Exception:
                    log("сбой озвучки всей книги:\n" + traceback.format_exc(), "err", "книга")
                    if self.auto_on():
                        self._auto_halt("внутренний сбой студии — подробности в журнале (и в studio\\studio.log)")
                    break
        finally:
            self._stay_awake(False)
            with self.lock:
                self._auto_acc()
                self.auto_save()

    def _auto_step(self) -> bool:
        """Один шаг автомата -> идти ли дальше."""
        self.load_rows()
        with self.lock:
            self._parts_cache = None
            pl = self.plan()
            opened = [q for q in pl if q["opened"]]
            unlocked = next((q["n"] for q in opened if not self.store["parts"][q["n"] - 1].get("locked")), None)
            drafts = [q["n"] for q in pl if not q["opened"]]
            whole = bool(pl) and pl[-1]["b"] >= len(self.rows) - 1
            nofile = next(((q["n"], m) for q in opened if self.store["parts"][q["n"] - 1].get("locked")
                           for m in [self.files_missing(self.store["parts"][q["n"] - 1].get("export"))
                                     or (["звуки меняли после сборки"] if self.export_stale(self.store["parts"][q["n"] - 1].get("export"),
                                                                                              q["a"], q["b"]) else [])] if m), None)
        if nofile is not None:
            # закреплена, а готовых файлов на диске нет (сборка когда-то не удалась, файл удалили, включили новую версию)
            k, miss = nofile
            self._auto_redone[k] = self._auto_redone.get(k, 0) + 1
            if self._auto_redone[k] > 2:
                raise Oops(f"готовые файлы части {k} не удаётся привести в порядок ({', '.join(miss)}) — причина в строках журнала выше")
            self._auto_set(part=k, step=f"часть {k}: закреплена, но её готовые файлы не в порядке ({', '.join(miss)}) — собираю заново")
            self.export_part(k)
            return True
        if unlocked is not None:
            self._auto_accept(unlocked)
            return self.auto_on()
        if drafts:
            self._auto_set(part=drafts[0], step=f"часть {drafts[0]}: собрана конвейером — беру в работу", fails=0)
            self.open_part(drafts[0])
            return True
        if whole:
            self._auto_chapters(final=True)
            k = self.disk_sync()
            if k:
                log(f"на Яндекс Диск досланы готовые файлы: {k}", "ok", "книга")
            with self.lock:
                self.auto.update(state="done", step="вся книга озвучена, закреплена и собрана по главам", err="", at=now())
                self._auto_acc()
                self.auto_save()
            log(f"ВСЯ КНИГА ОЗВУЧЕНА: частей {len(pl)}, глав {len(self.chapters)}. Готовые главы — {self.final_ch}"
                + (f" (со звуком — {self.final_ch_mix})" if self.final_ch_mix.is_dir() else ""), "ok", "книга")
            return False
        return self._auto_go((opened[-1]["n"] + 1) if opened else 1)

    def _auto_wait_jobs(self, rids: list):
        need, t0 = set(rids), time.time()
        limit = max(900.0, 40.0 * len(rids))
        while need and self.auto_on():
            for rid in list(need):
                j = self.jobs.get(rid)
                if j is None or j["status"] in ("done", "error"):
                    need.discard(rid)
            if time.time() - t0 > limit:
                log(f"переозвучка изменённых фраз не закончилась за {limit / 60:.0f} мин (осталось заявок: {len(need)}) — "
                    "движок не отвечает", "warn", "книга")
                break
            time.sleep(0.5)

    def _auto_accept(self, k: int):
        """Принять открытую часть как есть: переозвучить изменённое, поставить звуки партитуры, закрепить, собрать файл."""
        self._auto_set(part=k, step=f"часть {k}: принимаю как есть", fails=0)
        bad = ("stale", "empty", "lost")
        for _ in range(2):
            with self.lock:
                idxs = [i for i in self.part_range(k) if self.state(i) in bad]
            if not idxs:
                break
            self._auto_set(step=f"часть {k}: переозвучиваю изменённые фразы — {len(idxs)}")
            rids = []
            for i in idxs[:600]:
                try:
                    rids.append(self.synth(self.uids[i]))
                except Oops as e:
                    log(f"фраза № {self.rows[i]['id']} не взята в переозвучку: {e}", "warn", "книга")
            self._auto_wait_jobs(rids)
            if not self.auto_on():
                return
        with self.lock:
            left = [self.rows[i]["id"] for i in self.part_range(k) if self.state(i) in ("empty", "lost")]
            old = [self.rows[i]["id"] for i in self.part_range(k) if self.state(i) == "stale"]
        if left:
            raise Oops(f"в части {k} остались фразы без записи: № {', '.join(map(str, left[:10]))}{'…' if len(left) > 10 else ''}. "
                       "Озвучьте их («Озвучить заново») или дайте свою запись")
        if old:
            log(f"часть {k}: {len(old)} изменённых фраз переозвучить не удалось — принимаю с прежними записями: № "
                + ", ".join(map(str, old[:10])) + ("…" if len(old) > 10 else ""), "warn", "книга")
        if self.auto.get("sound", True) and self.kit_score() is not None \
                and not (self.kit_job is not None and not self.kit_job.get("error")):
            self._auto_set(step=f"часть {k}: звуки и музыка партитуры")
            try:
                r = self.kit_import(False)
                log(f"часть {k}: партитура книги перенесена на дорожки — звуков {r['new']}"
                    + (f", ваших правок сохранено {r['kept']}" if r.get("kept") else ""), "ok", "книга")
            except Oops as e:
                log(f"часть {k}: партитура не перенесена ({e}) — файл «со звуком» будет только с теми звуками, что уже стоят",
                    "warn", "книга")
        if not self.auto_on():
            return
        self._auto_set(step=f"часть {k}: закрепляю и собираю готовый файл")
        with self.lock:
            self.store["parts"][k - 1]["auto"] = now()
        ex = self.lock_part(k)
        with self.lock:
            self.auto.setdefault("parts", []).append(k)
            self.auto_save()
        log(f"часть {k} принята и закреплена: {Path(str(ex.get('file') or '')).name}, {mmss(float(ex.get('dur') or 0))}"
            + ("; со звуком — подмешано " + str(ex["mix"]["count"]) if ex.get("mix") else ""), "ok", "книга")
        self._auto_chapters()

    def chapters_due(self, locked_only: bool = True) -> list:
        """Главы, чей файл пора собрать: глава целиком лежит в закреплённых частях (locked_only) либо в открытых, а её
        файла нет или он устарел (после сборки были правки)."""
        with self.lock:
            pl = self.plan()
            last = -1
            for q in pl:
                if q["opened"] and (self.store["parts"][q["n"] - 1].get("locked") or not locked_only):
                    last = q["b"]
                else:
                    break
            cand = [(c, a, b) for c, (a, b, _t) in sorted(self.chapters.items()) if b <= last]
        out = []
        for c, a, b in cand:
            ex = (self.store["chapters"].get(str(c)) or {}).get("export")
            if ex and not self.files_missing(ex) and not self.export_stale(ex, a, b):
                try:
                    if self.timeline(a, b, False)[0]["ver"] == ex.get("ver"):
                        continue
                except Exception:
                    pass
            out.append(c)
        return out

    def _auto_chapters(self, final: bool = False):
        for c in self.chapters_due():
            if not self.auto_on():
                return
            # сверка перед сборкой главы: у каждой её части на диске должны лежать все готовые файлы (голос и версии)
            a, b, _t = self.chapters[c]
            for q in [q for q in self.plan() if q["opened"] and q["b"] >= a and q["a"] <= b]:
                miss = self.files_missing(self.store["parts"][q["n"] - 1].get("export"))
                if miss:
                    self._auto_set(part=q["n"], step=f"глава {c}: у части {q['n']} нет файлов ({', '.join(miss)}) — собираю часть заново")
                    self.export_part(q["n"])
            self._auto_set(step=f"глава {c}: файлы её частей на месте — собираю файл главы" + (" (последние главы книги)" if final else ""))
            try:
                ex = self.export_chapter(c)
                with self.lock:
                    self.auto.setdefault("chapters", []).append(c)
                    self.auto_save()
                log(f"глава {c} собрана: {Path(str(ex.get('file') or '')).name}, {mmss(float(ex.get('dur') or 0))}"
                    + ("; со звуком — подмешано " + str(ex["mix"]["count"]) if ex.get("mix") else ""), "ok", "книга")
            except Oops as e:
                log(f"глава {c} не собрана: {e} — соберите её позже в «Частях и главах»", "warn", "книга")

    def chapters_build(self) -> dict:
        """Кнопка «Собрать готовые главы»: все главы, целиком лежащие в открытых частях, чьих файлов нет или они устарели.
        Идёт в фоне, ход — в журнале."""
        if self.ch_busy:
            raise Oops("главы уже собираются — ход в журнале")
        todo = self.chapters_due(locked_only=False)
        if not todo:
            return dict(count=0)
        self.ch_busy = dict(total=len(todo), done=0)

        def work():
            ok = 0
            try:
                for c in todo:
                    try:
                        ex = self.export_chapter(c)
                        ok += 1
                        log(f"глава {c} собрана: {Path(str(ex.get('file') or '')).name}, {mmss(float(ex.get('dur') or 0))}", "ok", "книга")
                    except Oops as e:
                        log(f"глава {c} не собрана: {e}", "warn", "книга")
                    except Exception:
                        log(f"глава {c} не собрана:\n" + traceback.format_exc(), "err", "книга")
                    self.ch_busy["done"] += 1
            finally:
                self.ch_busy = None
                log(f"готовые главы: собрано {ok} из {len(todo)} — папка {self.final_ch}", "ok" if ok == len(todo) else "warn", "книга")
        threading.Thread(target=work, daemon=True).start()
        return dict(count=len(todo))

    def _auto_go(self, k: int) -> bool:
        """Запустить конвейер на следующую часть и дождаться его. -> идти ли дальше."""
        self._auto_set(part=k, step=f"часть {k}: озвучка, проверка и сборка конвейером (это десятки минут)")
        if not (self.go_proc is not None and self.go_proc.poll() is None):
            self.go_start()
        pr = self.go_proc
        while pr.poll() is None:
            if not self.auto_on():
                return False
            time.sleep(1.0)
        code = pr.returncode
        if not self.auto_on():
            return False
        time.sleep(1.5)                                      # дочитать вывод конвейера и перечитать книгу
        self.load_rows(force=True)
        with self.lock:
            self._parts_cache = None
            got = any(not q["opened"] for q in self.plan())
        if got:
            self._auto_set(say=False, fails=0)
            return True
        fails = int(self.auto.get("fails") or 0) + 1
        self._auto_set(say=False, fails=fails)
        if fails >= 3:
            raise Oops(f"часть {k} не собралась за три запуска конвейера (последний код завершения {code}). Причина — в строках "
                       f"журнала с пометкой «go» и в файлах папки книги: studio_go.log, part.log, work\\failed.txt")
        log(f"часть {k} пока не собрана (код конвейера {code}) — запускаю его ещё раз: он продолжит с того же места "
            f"(попытка {fails + 1} из 3)", "warn", "книга")
        for _ in range(10):
            if not self.auto_on():
                return False
            time.sleep(0.5)
        return True

    # ------------------------------------------------------------------ «Озвучить следующую часть» (go)
    def go_view(self, pl=None) -> dict:
        pl = pl if pl is not None else self.plan()
        run = self.go_proc is not None and self.go_proc.poll() is None
        opened = [p for p in pl if p["opened"]]
        unlocked = next((p["n"] for p in opened if not self.store["parts"][p["n"] - 1].get("locked")), None)
        drafts = [p["n"] for p in pl if not p["opened"]]
        done = bool(pl) and pl[-1]["b"] >= len(self.rows) - 1 and not drafts and unlocked is None
        if run:
            why = "идёт озвучка следующей части — ход в журнале"
        elif unlocked is not None:
            why = f"сначала закрепите часть {unlocked}: конвейер берётся за следующую, только когда предыдущие закреплены"
        elif drafts:
            why = f"часть {drafts[0]} уже озвучена — откройте её («Части и главы»), прослушайте и закрепите"
        elif done:
            why = "вся книга озвучена и закреплена"
        else:
            why = ""
        fixed = [p for p in opened if self.store["parts"][p["n"] - 1].get("locked")]
        total = max(1, len(self.rows))
        return dict(running=run, can=not why, why=why, next=(opened[-1]["n"] + 1) if opened else 1, done=done,
                    since=round(time.time() - self.go_at) if run else None, fixed=len(fixed),
                    pct=round(100 * sum(p["b"] - p["a"] + 1 for p in fixed) / total))

    def go_stop(self) -> dict:
        """Остановить озвучку части: go и всё, что он запустил (воркеры, проверка, сборщик). Озвученное остаётся —
        следующий запуск продолжит с того же места."""
        pr = self.go_proc
        if pr is None or pr.poll() is not None:
            raise Oops("озвучка сейчас не идёт")
        log("озвучка части останавливается по вашей команде…", "warn", "go")
        pg = None
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pr.pid), "/T", "/F"], capture_output=True, timeout=30,
                               creationflags=0x08000000)
                self.kill_book_procs()
            else:
                import signal
                try:
                    pg = os.getpgid(pr.pid)
                    os.killpg(pg, signal.SIGTERM)
                except Exception:
                    pr.terminate()
        except Exception as e:
            log(f"остановка: {type(e).__name__}: {e}", "warn", "go")
        try:
            pr.wait(timeout=20)
        except Exception:
            pass
        if pg is not None:                                   # кто-то из группы успел родиться в миг остановки — добить
            try:
                import signal
                os.killpg(pg, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        return self.go_view()

    def kill_book_procs(self):
        """Процессы конвейера этой книги, которые могли остаться после go (запущены отдельно от его дерева)."""
        ps = ("$r = '" + str(self.root).replace("'", "''") + "'; Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
              "Where-Object { $_.CommandLine -like ('*' + $r + '*') -and ($_.CommandLine -like '*nox_fast*' -or "
              "$_.CommandLine -like '*verify_k1a*' -or $_.CommandLine -like '*nox_part*' -or $_.CommandLine -like '*nox_stress*' "
              "-or $_.CommandLine -like '*nox_voices*' -or $_.CommandLine -like '* cast *') -and $_.CommandLine -notlike '*--serve*' } | "
              "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
        try:
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=60, creationflags=0x08000000)
        except Exception:
            pass

    def go_start(self) -> dict:
        v = self.go_view()
        if not v["can"]:
            raise Oops(v["why"])
        cmd = os.environ.get("NOX_GO_CMD")
        if cmd:
            args = [cmd, str(self.root)]
        else:
            ps1 = Path.home() / "Downloads" / "go_kniga1a.ps1"
            if not ps1.exists():
                raise Oops(f"не найден {ps1} — скачайте go_kniga1a.ps1 в Загрузки")
            args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1), "-Root", str(self.root), "-NoStudio"]
        self.write_fixed()
        kw = {"creationflags": 0x08000000} if os.name == "nt" else {}
        if os.name != "nt":
            kw["start_new_session"] = True                   # своя группа процессов: остановка гасит всё разом
        self.go_proc = subprocess.Popen(args, cwd=str(self.root), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kw)
        self.go_at = time.time()
        log(f"озвучка следующей части (часть {v['next']}): запущен go — подготовка, голоса, озвучка, проверка, сборка; "
            f"ход — здесь, в журнале", "run", "go")

        def pump(pr=self.go_proc):
            try:
                with open(self.root / "studio_go.log", "w", encoding="utf-8") as lf:
                    for raw in pr.stdout:
                        line = raw.decode("utf-8", "replace").rstrip()
                        lf.write(line + "\n")
                        lf.flush()
                        s = line.strip()
                        if s and not s.startswith(">"):
                            lvl = "err" if ("ОШИБКА" in s or "не прош" in s or "упал" in s) else "ok" if "ГОТОВА" in s or "СОБРАНА" in s else "eng"
                            log(s[:400], lvl, "go")
            except Exception:
                pass
            code = pr.wait()
            self.load_rows(force=True)
            with self.lock:
                self._parts_cache = None
            stopped = code != 0 and any("останавливается по вашей команде" in e["msg"] for e in list(EV)[-400:] if e["src"] == "go")
            log(("озвучка части завершена — принимаю её (идёт озвучка всей книги)" if self.auto_on() else
                 "озвучка части завершена — откройте новую часть в «Частях и главах»") if code == 0 else
                "озвучка остановлена. Озвученное сохранено: «Озвучить часть» продолжит с того же места" if stopped else
                f"go завершился с кодом {code} — причина в строках выше (весь вывод: studio_go.log в папке книги)",
                "ok" if code == 0 else "warn" if stopped else "err", "go")
        threading.Thread(target=pump, daemon=True).start()
        return self.go_view()

    # ------------------------------------------------------------------ фон: заявки, сердцебиение, выход
    def tick(self):
        beat = wbeat = 0.0
        hk = time.time() + 6 * 3600
        while True:
            try:
                self.poll_jobs()
                self.engine_pump()                             # вывод движка — в журнал окна, почти сразу
                if (self.tries or any(j["status"] == "queued" and time.time() - j["t"] > 2 for j in self.jobs.values())) \
                        and time.time() - self._auto_eng > 120 and not self.workers() \
                        and (self.engine_proc is None or self.engine_proc.poll() is not None) \
                        and not (self.go_proc is not None and self.go_proc.poll() is None):
                    self._auto_eng = time.time()               # есть заявки, а движка нет — запустить самим
                    try:
                        self.engine_start()
                    except Oops as e:
                        log(f"движок сам не запустился: {e}", "warn", "движок")
                if time.time() - wbeat > 2:
                    wbeat = time.time()
                    self.engine_watch()
                if time.time() - beat > 20:
                    beat = time.time()
                    write_atomic(self.qdir / "editor.txt", now())
                self.tcache_save()
                if time.time() > hk:
                    hk = time.time() + 6 * 3600
                    self.housekeeping()
                idle = time.time() - self.last_ping
                busy = any(j["status"] in ("queued", "running") for j in self.jobs.values()) or self.auto_on()
                # долгое молчание окна: после сна компьютера даём окну полторы минуты подать голос
                self.strikes = self.strikes + 1 if (idle > 45 * 60 and not busy) else 0
                if (self.bye_at and time.time() > self.bye_at and idle > 12) or self.strikes > 180:
                    if self.auto_on():
                        # окно закрыли посреди озвучки всей книги: автомат встаёт на паузу и заканчивает начатое (закрепление,
                        # готовый файл); конвейер дорабатывает текущую часть сам — «Продолжить» потом возьмёт её готовой
                        with self.lock:
                            self.auto.update(state="pause", at=now(),
                                             step=f"окно студии закрыто — пауза на части {self.auto.get('part') or '?'}")
                            self._auto_acc()
                            self.auto_save()
                        log("окно закрыто — озвучка всей книги встала на паузу; конвейер дорабатывает текущую часть сам", "warn", "книга")
                    if not (self.auto_th is not None and self.auto_th.is_alive()):
                        log("окно закрыто — студия выходит")
                        (self.qdir / "editor.txt").unlink(missing_ok=True)
                        (self.dir / "studio.lock").unlink(missing_ok=True)
                        os._exit(0)
            except Exception:
                log("сбой фонового потока:\n" + traceback.format_exc())
            time.sleep(0.5)


# ---------------------------------------------------------------------------------------------- HTTP
S: Studio = None


class Server(ThreadingHTTPServer):
    allow_reuse_address = False      # иначе в Windows второй процесс молча сел бы на тот же порт
    daemon_threads = True

    def handle_error(self, request, client_address):
        e = sys.exc_info()[1]
        if isinstance(e, (ConnectionError, TimeoutError)):       # окно оборвало запрос (перемотка, закрытие) — не новость
            return
        log("сбой соединения:\n" + traceback.format_exc())


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "NoxStudio"

    def log_message(self, *a):
        pass

    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, jd(obj).encode("utf-8"))

    def _host_ok(self) -> bool:
        return (self.headers.get("Host") or "").split(":")[0] in ("127.0.0.1", "localhost")

    def _file(self, path: Path, ctype: str):
        """Файл с поддержкой Range — без неё плеер не умеет перематывать."""
        try:
            size = path.stat().st_size
        except OSError:
            return self._json({"error": "файла нет"}, 404)
        a, b = 0, size - 1
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
        part = bool(m and (m.group(1) or m.group(2)))
        if part:
            if m.group(1):
                a = int(m.group(1))
                b = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:
                a = max(size - int(m.group(2)), 0)
            if a > b or a >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        self.send_response(206 if part else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(b - a + 1))
        self.send_header("Cache-Control", "no-store")
        if part:
            self.send_header("Content-Range", f"bytes {a}-{b}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as f:
            f.seek(a)
            left = b - a + 1
            while left > 0:
                chunk = f.read(min(262144, left))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionError, OSError):
                    return
                left -= len(chunk)

    def _guard(self, fn):
        try:
            if not self._host_ok():
                return self._json({"error": "чужой адрес"}, 403)
            if S is not None and not self.path.startswith("/api/bye"):
                S.last_ping, S.bye_at = time.time(), None      # окно живо (обновили страницу — «прощание» отменяется)
            fn()
        except Oops as e:
            ev("отказ: " + str(e), "warn")
            self._json({"error": str(e)}, 400)
        except (BrokenPipeError, ConnectionError):
            pass
        except Exception as e:
            log("сбой запроса " + self.path + ":\n" + traceback.format_exc())
            try:
                self._json({"error": f"сбой студии: {type(e).__name__}: {e} (подробности в studio\\studio.log)"}, 500)
            except Exception:
                pass

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self._guard(self._get)

    def do_POST(self):
        self._guard(self._post)

    def _get(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        p = u.path
        if p in ("/", "/index.html"):
            html = (Path(__file__).resolve().parent / "nox_studio.html").read_text(encoding="utf-8")
            return self._send(200, html.replace("__NOX_TOKEN__", S.token).encode("utf-8"), "text/html; charset=utf-8")
        if p == "/api/ping":
            S.last_ping, S.bye_at = time.time(), None
            return self._json({"ok": True, "root": str(S.root), "ver": VERSION})
        if p == "/api/state":
            return self._json(S.state_view())
        if p == "/api/part":
            return self._json(S.part_view(int(q.get("n", 1))))
        if p == "/api/jobs":
            S.last_ping = time.time()
            jobs = [dict(rid=j["rid"], uid=j["uid"], tid=j["tid"], status=j["status"], error=j.get("error"),
                         why=j.get("why"), chosen=j.get("chosen"), age=round(time.time() - j["t"])) for j in S.jobs.values()]
            return self._json({"jobs": jobs, "engine": S.engine_view()})
        if p == "/api/log":                                    # журнал для окна: новые строки и состояние компьютера
            after = int(q.get("after") or 0)
            with EV_LOCK:
                evs = [e for e in EV if e["n"] > after][-800:]
                last = EV_N[0]
            return self._json({"ev": evs, "last": last, "sys": S.sys_view(q.get("open") == "1"),
                               "engine": S.engine_view(), "check": S.check, "go": S.go_view(), "auto": S.auto_view()})
        if p == "/api/voices":
            return self._json(S.voices_view(q.get("fresh") == "1"))
        if p == "/api/voice_try":
            return self._json(S.try_state(q.get("key", "")))
        if p == "/api/voice_impact":
            return self._json(S.voice_impact(q.get("role", "")))
        if p == "/api/voice_history":
            return self._json(S.voice_history(q.get("role", "")))
        if p == "/audio/voice":
            if q.get("old"):
                return self._file(S.voice_old_path(q.get("role", ""), q.get("old", "")), "audio/wav")
            return self._file(S.voice_path(q.get("role", ""), q.get("bank", "")), "audio/wav")
        if p == "/audio/try":
            return self._file(S.cache_dir / "try" / (re.sub(r"[^0-9a-f]", "", q.get("key", "")) + ".wav"), "audio/wav")
        if p == "/api/orphans":
            return self._json({"orphans": [dict(uid=o["uid"], at=o.get("at"), e={k: v for k, v in o["e"].items()
                                                                              if k in ("tech", "role", "mark", "ctx")})
                                           for o in S.store["orphans"]]})
        if p in ("/audio/part", "/peaks"):
            k = int(q.get("n", 1))
            ver = re.sub(r"[^0-9a-f]", "", q.get("v", ""))
            wf = S.render_wav(k, ver)
            if not ver or not wf.exists():                     # запрошена не та версия — отдать нынешнюю
                wf = S.render_wav(k, S.ensure_render(k)["ver"])
            if p == "/peaks":
                return self._file(wf.with_suffix(".pk"), "application/octet-stream")
            return self._file(wf, "audio/wav")
        if p == "/api/sounds":
            return self._json(S.sound_list(q.get("q", ""), q.get("kind", "")))
        if p == "/audio/cue":
            return self._file(S.cue_clip(int(q.get("n", 1)), re.sub(r"[^c0-9]", "", q.get("id", ""))), "audio/wav")
        if p == "/audio/sound":
            return self._file(S.sound_preview(q.get("f", "")), "audio/wav")
        if p == "/api/projects":
            return self._json(S.project_list())
        if p == "/api/take_peaks":
            return self._json(S.take_peaks(q.get("uid", ""), re.sub(r"[^t0-9]", "", q.get("tid", ""))))
        if p == "/audio/take":
            e = S.store["segs"].get(q.get("uid", "")) or {}
            return self._file(S.take_path(e.get("sid", "-"), re.sub(r"[^t0-9]", "", q.get("tid", ""))), "audio/wav")
        if p == "/audio/draft":
            i = S.idx_of.get(q.get("uid", ""))
            if i is None:
                return self._json({"error": "фразы нет"}, 404)
            return self._file(S.wavs[i], "audio/wav")
        self._json({"error": "нет такого адреса"}, 404)

    def _post(self):
        # менять что-либо может только наше окно: одноразовый ключ запуска + свой адрес в Origin
        org = self.headers.get("Origin")
        if org and urllib.parse.urlparse(org).hostname not in ("127.0.0.1", "localhost"):
            return self._json({"error": "чужой адрес"}, 403)
        if not hmac.compare_digest(self.headers.get("X-Nox-Token") or "", S.token):
            return self._json({"error": "окно устарело — закройте его и откройте студию заново"}, 403)
        ln = int(self.headers.get("Content-Length") or 0)
        u = urllib.parse.urlparse(self.path)
        p = u.path
        S.last_ping = time.time()
        if p == "/api/sound_upload":                           # тело — сам файл, имя — в адресе
            if ln > 300_000_000:
                return self._json({"error": "файл больше 300 МБ — такие звуки студия не берёт"}, 400)
            name = urllib.parse.parse_qs(u.query).get("name", ["zvuk.wav"])[0]
            data = bytearray()
            while len(data) < ln:
                chunk = self.rfile.read(min(1 << 20, ln - len(data)))
                if not chunk:
                    break
                data += chunk
            if len(data) != ln:
                return self._json({"error": "файл пришёл не целиком — повторите"}, 400)
            return self._json({"sound": S.sound_add(name, bytes(data))})
        if p == "/api/take_upload":                            # своя запись к фразе: тело — файл, имя и фраза — в адресе
            if ln > 300_000_000:
                return self._json({"error": "файл больше 300 МБ — загружайте запись по фразам"}, 400)
            qs = urllib.parse.parse_qs(u.query)
            data = bytearray()
            while len(data) < ln:
                chunk = self.rfile.read(min(1 << 20, ln - len(data)))
                if not chunk:
                    break
                data += chunk
            if len(data) != ln:
                return self._json({"error": "файл пришёл не целиком — повторите"}, 400)
            res = S.take_import(qs.get("uid", [""])[0], qs.get("name", ["zapis.wav"])[0], bytes(data))
            k = int(qs.get("part", ["0"])[0] or 0)
            return self._json({"take": res, "part": S.part_view(k) if k else None})
        if p == "/api/project_upload":                         # файл проекта с компьютера автора: сразу на диск, по кускам
            name = urllib.parse.parse_qs(u.query).get("name", ["proekt.noxproj"])[0]
            S.proj_dir.mkdir(parents=True, exist_ok=True)
            bad = (f"«{name}» — не файл проекта студии (нужен *.noxproj)" if not name.lower().endswith(Studio.PROJ_EXT)
                   else "на диске книги не хватает места для этого файла проекта"
                   if shutil.disk_usage(S.dir).free < ln + 300_000_000 else "")
            if bad:
                left = ln
                while left > 0:
                    chunk = self.rfile.read(min(1 << 20, left))
                    if not chunk:
                        break
                    left -= len(chunk)
                return self._json({"error": bad}, 400)
            tmp = S.proj_dir / f"upload_{secrets.token_hex(4)}.part"
            got = 0
            try:
                with open(tmp, "wb") as f:
                    while got < ln:
                        chunk = self.rfile.read(min(1 << 20, ln - got))
                        if not chunk:
                            break
                        f.write(chunk)
                        got += len(chunk)
                if got != ln:
                    raise Oops("файл пришёл не целиком — повторите")
                info = S.project_adopt(tmp, name)
            finally:
                tmp.unlink(missing_ok=True)
            return self._json({"project": info, "projects": S.project_list()})
        d = json.loads(self.rfile.read(ln).decode("utf-8") or "{}") if ln else {}
        k = int(d.get("part") or 0)
        if p == "/api/bye":
            S.bye_at = time.time() + 8
            return self._json({"ok": True})
        if p == "/api/seg":
            S.set_seg(d["uid"], d.get("set") or {})
        elif p == "/api/synth":
            if d.get("set"):
                S.set_seg(d["uid"], d["set"])
            if d.get("scope") in ("sentence", "para"):         # всё предложение (его куски) или весь абзац
                rids = S.synth_scope(d["uid"], d["scope"])
            else:
                rids = [S.synth(d["uid"], d.get("variant")) for _ in range(max(1, min(int(d.get("count", 1)), 5)))]
            i0 = S.idx_of.get(d["uid"])
            log(f"переозвучка: фраза № {S.rows[i0]['id'] if i0 is not None else '?'} — "
                + ({"sentence": "всё предложение", "para": "весь абзац"}.get(d.get("scope"))
                   or ("один дубль" if len(rids) == 1 else f"вариантов {len(rids)}")) + f"; заявок {len(rids)}"
                + ("" if S.workers() else " — движок не запущен, заявки ждут"), "run", "движок")
            return self._json({"rids": rids, "part": S.part_view(k) if k else None, "engine": S.engine_view()})
        elif p == "/api/synth_stale":
            idxs = [i for i in S.part_range(k) if S.state(i) in ("stale", "empty", "lost")]
            rids = [S.synth(S.uids[i]) for i in idxs[:300]]
            if rids:
                log(f"переозвучка: все изменённые фразы части {k} — заявок {len(rids)}"
                    + (f" (из {len(idxs)}; остальные — следующим заходом)" if len(idxs) > len(rids) else ""), "run", "движок")
            return self._json({"rids": rids, "part": S.part_view(k), "engine": S.engine_view()})
        elif p == "/api/take_trim":
            S.take_trim(str(d.get("uid") or ""), str(d.get("tid") or ""), d.get("t0"), d.get("t1"))
        elif p == "/api/take_next":
            res = S.take_next(str(d.get("uid") or ""), str(d.get("tid") or ""), d.get("t0"), d.get("t1"))
            return self._json(dict(res, part=S.part_view(k) if k else None))
        elif p == "/api/take":
            if d.get("delete"):
                S.delete_take(d["uid"], d["delete"])
            else:
                S.choose_take(d["uid"], d.get("tid"))
        elif p == "/api/lock":
            if d.get("uids"):
                S.lock_segs([S.idx_of[u] for u in d["uids"] if u in S.idx_of], "закреплены фразы")
            elif d.get("whole"):
                S.auto_guard("закрепить часть вручную")
                ex = S.lock_part(k)
                return self._json({"export": ex, "part": S.part_view(k)})
        elif p == "/api/unlock":
            if d.get("uids"):
                S.unlock_segs([S.idx_of[u] for u in d["uids"] if u in S.idx_of], "снято закрепление фраз")
            elif d.get("whole"):
                S.unlock_part(k)
        elif p == "/api/export":
            if d.get("chapter") is not None:
                c = int(d["chapter"])
                ex = S.lock_chapter(c) if d.get("lock") else S.export_chapter(c)
                return self._json({"export": ex, "state": S.state_view()})
            return self._json({"export": S.export_part(k), "part": S.part_view(k)})
        elif p == "/api/rule":
            res = S.add_rule(d.get("word", ""), d.get("to", ""), d.get("scope", ""), d.get("uid", ""), bool(d.get("dry")))
            if d.get("dry"):
                return self._json(res)
            return self._json(dict(res, part=S.part_view(k) if k else None))
        elif p == "/api/rule_delete":
            back = S.del_rule(d.get("id", ""))
            return self._json({"ok": True, "back": back, "part": S.part_view(k) if k else None})
        elif p == "/api/cue":
            c = S.cue_edit(k, d)
            return self._json({"ok": True, "id": c.get("id"), "part": S.part_view(k)})
        elif p == "/api/cues_scan":
            res = S.cues_scan(k, d.get("kinds"), bool(d.get("fresh")))
            return self._json(dict(res, part=S.part_view(k)))
        elif p == "/api/kit_unpack":
            return self._json(S.kit_unpack_start())
        elif p == "/api/cues_kit":
            res = S.kit_import(bool(d.get("reset")))
            return self._json(dict(res, part=S.part_view(k) if k else None))
        elif p == "/api/cues_clear":
            cnt = S.cues_clear(k, d.get("what") or "empty")
            return self._json({"count": cnt, "part": S.part_view(k)})
        elif p == "/api/mix":
            S.set_mix(d)
        elif p == "/api/sound_map":
            S.set_sound_map(str(d.get("label") or ""), d.get("file"))
        elif p == "/api/boundary":
            S.set_boundary(k, d["uid"])
        elif p == "/api/undo":
            uid = S.undo_last()
            return self._json({"uid": uid, "part": S.part_view(k) if k else None})
        elif p == "/api/engine":
            return self._json({"engine": S.engine_start(bool(d.get("force")))})
        elif p == "/api/open":
            return self._json(S.open_folder(str(d.get("what") or ""), str(d.get("path") or "")))
        elif p == "/api/selfcheck":
            threading.Thread(target=S.self_check, daemon=True).start()
            return self._json({"ok": True})
        elif p == "/api/clean":
            return self._json(S.housekeeping(everything=True))
        elif p == "/api/stress":
            if S.stress_busy is None:
                S.stress_run_at[k] = time.time()
                threading.Thread(target=S.stress_run, args=(k,), daemon=True).start()
            return self._json({"ok": True})
        elif p == "/api/stress_keep":
            S.stress_keep(d["uid"], int(d["k"]), bool(d.get("all")))
        elif p == "/api/stress_set":
            res = S.stress_set(d["uid"], int(d["k"]), bool(d.get("all")))
            return self._json({"count": res.get("count", 1), "part": S.part_view(k) if k else None})
        elif p == "/api/voice_apply":
            return self._json(S.voice_apply(str(d.get("role") or ""), str(d.get("voice") or "")))
        elif p == "/api/voice_suggest":
            return self._json(S.voice_suggest(str(d.get("role") or "")))
        elif p == "/api/voice_try":
            return self._json(S.voice_try(str(d.get("role") or ""), str(d.get("voice") or ""), str(d.get("uid") or "")))
        elif p == "/api/voice_tool":
            return self._json(S.voice_tool(str(d.get("what") or ""), str(d.get("ids") or ""), d.get("spec")))
        elif p == "/api/voice_revert":
            return self._json(S.voice_revert(str(d.get("role") or ""), str(d.get("file") or ""), bool(d.get("first"))))
        elif p == "/api/export_text":
            return self._json({"text": S.export_text(str(d.get("upto") or ""), d.get("changes") is not False)})
        elif p == "/api/voice_tidy":
            rep_ = S.voice_tidy(bool(d.get("apply")))
            return self._json({"tidy": rep_, "voices": S.voices_view() if d.get("apply") else None})
        elif p == "/api/go_stop":
            return self._json({"go": S.go_stop()})
        elif p == "/api/seg_reset":
            res = S.reset_seg(str(d.get("uid") or ""))
            return self._json({"ok": True, "reset": res, "part": S.part_view(k) if k else None})
        elif p == "/api/join":
            res = S.join_next(str(d.get("uid") or ""), bool(d.get("force")))
            return self._json({"ok": True, "join": res, "part": S.part_view(k) if k else None})
        elif p == "/api/unjoin":
            res = S.join_split(str(d.get("uid") or ""))
            return self._json({"ok": True, "join": res, "part": S.part_view(k) if k else None})
        elif p == "/api/insert":
            iid = S.insert_add(str(d.get("after") or ""), str(d.get("text") or ""), str(d.get("role") or ""))
            rid = S.synth(iid) if d.get("voice") else None
            return self._json({"uid": iid, "rid": rid, "part": S.part_view(k) if k else None})
        elif p == "/api/insert_edit":
            S.insert_edit(str(d.get("uid") or ""), str(d.get("text") or ""))
        elif p == "/api/insert_delete":
            S.insert_delete(str(d.get("uid") or ""))
        elif p == "/api/go":
            S.auto_guard("запустить озвучку части вручную")
            return self._json({"go": S.go_start()})
        elif p == "/api/auto":                                 # «Вся книга»: start | pause | chapters
            do = str(d.get("do") or "")
            if do == "start":
                S.auto_start(d.get("sound"))
            elif do == "pause":
                S.auto_pause()
            elif do == "chapters":
                return self._json({"chapters": S.chapters_build(), "auto": S.auto_view(), "go": S.go_view()})
            else:
                return self._json({"error": "нет такого действия"}, 400)
            return self._json({"auto": S.auto_view(), "go": S.go_view()})
        elif p == "/api/project_save":
            res = S.project_save(str(d.get("note") or ""), bool(d.get("audio")), disk=d.get("disk"))
            return self._json({"project": res, "projects": S.project_list()})
        elif p == "/api/project_check":
            return self._json(S.project_check(str(d.get("name") or "")))
        elif p == "/api/project_load":
            res = S.project_load(str(d.get("name") or ""))
            return self._json({"loaded": res, "state": S.state_view()})
        elif p == "/api/project_delete":
            S.project_delete(str(d.get("name") or ""))
            return self._json({"projects": S.project_list()})
        elif p == "/api/project_disk":
            return self._json({"disk": S.project_to_disk(str(d.get("name") or ""))})
        else:
            return self._json({"error": "нет такого адреса"}, 404)
        self._json({"ok": True, "part": S.part_view(k) if k else None})


# ---------------------------------------------------------------------------------------------- запуск
def find_edge():
    if os.name != "nt":
        return None
    cand = []
    try:
        import winreg
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe") as k:
                    cand.append(winreg.QueryValue(k, None))
            except OSError:
                pass
    except Exception:
        pass
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LocalAppData")):
        if base:
            cand.append(str(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"))
    return next((c for c in cand if c and Path(c).exists()), None)


def open_window(url: str, profile: Path):
    """Отдельное окно без адресной строки: Edge в режиме приложения со СВОИМ профилем (тогда ключи запуска действуют
    всегда, даже если обычный Edge уже открыт). Нет Edge — вкладка в обычном браузере."""
    exe = find_edge()
    if exe:
        try:
            profile.mkdir(parents=True, exist_ok=True)
            subprocess.Popen([exe, f"--app={url}", f"--user-data-dir={profile}", "--no-first-run",
                              "--no-default-browser-check", "--window-size=1560,980"])
            return
        except OSError:
            pass
    import webbrowser
    webbrowser.open(url)


def alive_instance(root: Path):
    """Уже работающая студия этой книги -> (порт, версия, pid) или None."""
    lk = read_json(root / "studio" / "studio.lock", None)
    if not lk:
        return None
    try:
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{lk['port']}/api/ping", timeout=2) as r:
            d = json.loads(r.read().decode("utf-8"))
            if str(d.get("root", "")).lower() == str(root).lower():
                return int(lk["port"]), str(d.get("ver") or ""), int(lk.get("pid") or 0)
    except Exception:
        pass
    return None


def stop_instance(pid: int) -> bool:
    """Остановить работающую студию ДРУГОЙ версии (файлы обновили, а прежняя ещё открыта): иначе новое окно попало бы
    к старой программе. Правки она пишет на диск сразу, так что терять нечего. True — остановлена."""
    if pid <= 0 or pid == os.getpid():
        return False
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=20)
        else:
            os.kill(pid, 15)
    except Exception:
        pass
    for _ in range(60):
        if not pid_alive(pid):
            return True
        time.sleep(0.25)
    return False


# значок приложения (.ico: 16/32/48/256) — выдаётся ключом --icon, ярлык берёт его
ICON_B64 = (
    "AAABAAQAEBAAAAAAIACWAgAARgAAACAgAAAAACAAfgUAANwCAAAwMAAAAAAgABEJAABaCAAAAAAAAAAAIABkMAAAaxEAAIlQTkcNChoKAAAADU"
    "lIRFIAAAAQAAAAEAgGAAAAH/P/YQAAAl1JREFUeJx1k8tvzVEQxz8z83OJ3t5LxKvVYtWH11oQ/4SEIEHSSBBvsWcr8QihUSsbGwsLEYkIEfGO"
    "jVKRCI1Hw4Lee1ttr3tmLG5Vvc5uzvnO93znOzMCKOCzFrW1ZZpbGZ6mMukoME0hAWMOolrVVL0/0P/qJaACMHdh5zZRPYXQOJEZIALfA4aTk4"
    "mQNyHqb0MefuDz2xc90rSorS1hvSKSRXiqpxECVCOYnzNWF6bxtZa4NTgqKoSIWkS4hCzVmmRrRDWLiARi46pNBRvxsP3NRevumG1NucxGHFUR"
    "i4gkqgq+VomwuuDf6x5OQcf0KWyc3cDFD2XODVTIm+C/kBEhpuOBTCZAwFU53DqTQYej7yvkJ6CTUZD9eVv3SKhVylx4/Z0j1cSb4THmzSiSIv"
    "7C/pNgdHSMzRvW0b5iOVNE6HvWy6XLV5iay/2F/U2XmVGuDLFnVxfnzxzn0Z273L55i7OnjnFo7w7KlSHM7P8EKSXy+Qa2bl7PyOgI9x4+4cHj"
    "pwx/G2LLpvUUC43UarX/lxARmCpmhnsiyzLEnZQcM8NUcfd/KgiALMsolcpcvXaDhul5WlqaWdDcRKGxwNXrN/gyWCLLJv4MAJmzeEmXivSEex"
    "IRc3cEYd/u7bS2LICA9x8HOHG6m5QSZsb4IBkp7ZT5C5e2u3iviFi4J0SICErlMsVCAREYLJUpFhoRUYhAVC0ighTLBGDO4iVdCicRaagvkWCm"
    "1GqpblRmpOREBAhExLdwP/i5v69bxn3wea2dna6sEiFH+tnXn+35FWtQ9RT3Pr3rew7oD+VnCdbqxxrhAAAAAElFTkSuQmCCiVBORw0KGgoAAA"
    "ANSUhEUgAAACAAAAAgCAYAAABzenr0AAAFRUlEQVR4nL2XXYxdVRXHf2vtc8/MvXc+yrTMNLXtTCdBoNonReRLG4MxUQkivQimCRoD+EDUGDVq"
    "lGBMlJemQQIvRokWKKQ1RuWJBFJqrR9PRLEpk8Zm2jAzvTPTlpnOnXvunL2WD/NBO1DuaS2u5CQn52Ot3175r7X3ggst8P7bBTFk1YvYs3FrXy"
    "XRmuO3IX41dsE3l2qOAi5TohyS+YV94+MjU8uxzgcIQOwfuv5OQZ/UoB/AwfH/IfbbJggIuPk40b4xceLI/uWYArUA++LAlg99XpA/geNm+cpf"
    "BSB0VZIcMHzp6ZIPx0U1QYSY+47JE//+HdSCALJ587Y1TYlHVaXf3SJIYS0I0DAnuiMIhtOhQofIu6B7RFRxPytJeu3EsdemEsBbGu8JGvot5j"
    "kiSdHgKnAuOg+u7+KWnk5mo9GbKH+fzXhqbJaKCnYhbsAs15BcFfOFLwOPJwAG2wMrOSu88sycwY6E72zsZU2iuMOCO7vfnLm4K0EcXPDtwOMK"
    "IM5axwWKIwSBRnR29lfpTZTxLGLA946f4fBbGZWwevXno7vg9AFo4SWvctE0Z7AzYce6Km/lxkAaePF0g2frc6xLlViwgC4LQAXmovOlq6tsSA"
    "MKvNnKeWT0LKlSpHAuH0CAlkF/GqitqzATjUoQHhk9y8ksp/wO4V1hABWYM+PB9d18uCtlIA3sqc/xh+kGVyVKfom9q3DJLVt06A7KWXN2j51j"
    "3pxfT8zSFRS7jMZ5yQAhBPI8Z9exCTxbAIFqZ0q1sxMtlYgxvj8AIosVevrMGdb29XH7xz7ClqHNiAj/GT3JGyPHqE9O0dvbg4jgXiwdhQAEcD"
    "OaWYtvPfwQD3x1J8NDQ4yePEGMtnR/kqf3PM8vnvolpSRBVQtBFBKhqDI3P8+uxx7l5z/5EcNDQzy///ds++h2tt3wSZ7es5fBTZt49Iff5cnd"
    "j9HMsiJuiwGEoMzMzPLZz9zO1+7fSaPZwNz47bMv4DhJSHhm7z6iRRrNBvfVvsjdd36OmZkZQmi/pxXIgBAtsuOuO3B3khDIsib1yWk60pQ0LT"
    "F9+gyN+QalJMHdufuuOyja1dsCmBmVcpmhwU2ICCEEsqxFs9lcCdLMMlpZixACIsLQ5o10dVULVUT7DLgjqiRJWPV4WWCLij9fcCEJqFwhEWpQ"
    "ms0m9ckp3B0zI0kSSmmJxabvpKUSSamEmeHuTE5O05hvXBkNCEKeR15+5SAiwsLCApVymTW9vSt7Tk93N13VCq3WAiLCKwf+TJa1UG2vg7YA0Y"
    "zenm6e2buf1/75LyrlKiLKbbd8nNPjp5gaG+fmm24gaEKlXOHoyAi/+s1z9PR0E2P7baltI3J3Qgg0m03uvf8hntj1Mz79qe389Mff54PXDBPz"
    "yFd23gfAq4cO8/C3f8Ds7DnK5TJm7QEEoH9w6wsaQs0tj/DuZ0JVJcsyzIxbb76RW2+6keuvuwYR4ejIMf7y139w8NDfAKezs/M9gnsUTdRj/O"
    "Op0SNfSBady6sC97hz0fI1Mzo6UtzhwMHDvPTyAWxp+1MR0jSlq6uKiLz3yhfPfoJycDkDsmHDdX0x1TdEpM/drN2xXFVRFZarTBaHDmL7lEdE"
    "FPcZF722fvz1ukJNx8aOTrvrA4iIiAbcc/DI4vj0jsvMYp7HFcvzGKPZRb8Hj7jnIhJUVHD5ev3466egtlInAYjrt2yt4fKEBB24kqMZCCJgZl"
    "Nu/s366JHneHs0W7EAxIHh4X6sci/YJwTW+uK0cLkDqi/FnxbhUDT2To4emeC84XS1/d/H8/8Cw1xnlaQCkdsAAAAASUVORK5CYIKJUE5HDQoa"
    "CgAAAA1JSERSAAAAMAAAADAIBgAAAFcC+YcAAAjYSURBVHiczZprrF1FFcd/a2bvfZ63j/s6UMGWFihtaWtpJYJpLsRGSwSLkhpKQwlgBAU/SL"
    "SJkRg0Jho/YDRoiGKDiASUYGJUiBrSWl4tFQjQUor0XW7fve/z2jPLD/uc0xe33J6eVv9fds7sx/n/Z9Zas2bWwOgQ6AkAe4pnzjZMjYOM9sBo"
    "Nwzg6z8Kc+bktE8LTjRCqtpiksdDQ7EqFZepHDj07ruDo3Gq48MEWMABpjBl1lIRlqnqAqAbCM8K6ZMRI3JAVF9DzJN7t739JBAfw62BEwVYwH"
    "VOnjEvMOYXIuYqRUEV1bPb8SdBBBFBELz3G8Dfu2/7O+tgqYU/NkQcK8AC7rzJM67D2D8g5NW5GEFAzLllX4d6FBVrA1RLTlh+YOvGZzhmJOoC"
    "DOC7p86eLepeESSr6mOQoFVURvVC4KPHVp2IWDAVxS/ct23TemoiGj07f/78EO9WiZizQj5WqKhSVT3uWhmTaYpVVQdEqqyaPHlymsShxdDTEw"
    "B+z+HijdbYBercWSE/LhDOjyzdoaUQHr1Oisb6V2LVu9gaO6tE7mZAoccGrFmjAF5ZYUT1lGPdBPmqwsTA8NSMLjoDQ6wgArGH9tDw4J5+fr5n"
    "kLZA8B81GCKiqKroCuBRWOMN4AqFOTlBF6iqgLRs4rICw86ztCvLnGxIygjjAiFrhI+lLG8NV/hV7xBZK4wxyBn1KgLzLrhgZjvgDYCO0wLQOd"
    "avjAUCVDwUIsstXTn6ncdrMiIAfc6zctsRKqpYGYsj1z+rgEyopvR8SKIPrlKOWmn3AEZg0Hm+2JHl4kxIySdtqjDeGn6yq5+3hiu02TGYzokq"
    "RDBVTTUEINLyWSrWxMZvLeQp+qSXncKEwPC3I0VW7RtiYpj4RDOQGuezMkFZgcHYc0N7lhmZkKJLWKaMsLfiuH/7EUJhzHZzKpwVAU6hLTDcVs"
    "hTUUUEvELGCA/s7GN7KSZj5OTMrAm0XICt2f7iiRnm5iKGneIV2gPDEweGePrg8BmZzolouQCvkBJhRa33nULWCltKVX64s5+cMafttKdCSwVY"
    "gQHn+UJHlmvHpwGYEAihCN/d3sfBqiMyLTH9BloaOlUhEmFuPuLlgTIDzjMuMDx3eITn+0pMCAyuxfGupQI8kDbCj3b1JylDo10ZZ6Xl5KHFAo"
    "wIYgyiivEOXzP2yCTt1gjeK963Iv4kaJkAay3FYpGRkSJhFJLP5cikkxVotVplaGiYSqVCJpMmm83ivW/JKu+MBYgIqsrhI0eYMf1Slly/mE9d"
    "OZ+pF02m0N0FCAcPHWTrtp2s3/A6f/7rc7z51iba2vJYY/BnKEIAOqZMnx5gN9falFMvoI4j75wjjmNW3vcN7rnrDtryeQC8d6xe+xLOOXoWXk"
    "UUpgAolkr85tHH+cGPH0S9JwzD0zYpEcF4f8UHO955/QxHQInjmF//8qfctOR6YlelXCkzMDDEzbd9lRdeWocA8+bO5uknfkNnZwdBYLj37q9w"
    "2fRLWH7713DOYYxp2pyangestfT1DfDtb97DTUuup1gaoRrHpKIUTz39J55fvZZCdyeF7i5eefU1fvv7p0hFKWLnGCmOsOjaHr5//0oGBocwpv"
    "npqKk3RYRSqcTF0y7i3rvvJHYxQRA0iLz0yqvkclmc88RxTD6fY/2G1xPhxhCGAdW4wh0rljF39ixGRkaaFtHUW9YYhkeKfHbRNbTl24hdjIhg"
    "jcE5x57evVhr0dp+UmANvXv3U66UsDZZ8DnniaIUn1+8iJFiCWOaW8s2JaDu5Vd8Yk7DdlUVYwzFcpHBwSFsza4VMMYwPDxMsVRq9LTU1lbz5l"
    "5OENhz6wOqShAGdHd11nbPqJESnPNUq1VEjvaoiFCNY+LYUX+6/l53dxepKNWY9M6JgDqBIKiv/48Z/lqvn/D0qNuTYRA0bT7QtBODix2Dg8O1"
    "Fm1cjUl84XgoxlqsNY1n61r6BwZPGrHTQdNRKHaO997fehwZ75Uoishk0njvG6S8V9KpFFEYNWbeuoO/v3Ub5XLlHDuxV6IwZPW/Xkw+YpKdZO"
    "89qShNZ0cHzrmGWOcc7e0TGzmQ1HeeRXh+zQu1iNUU/+YEOO/J53OsfXEdL7y8jihMUY3jRkpw6SXTqFZjwjAgDAKq1SoXT52CEdNIPVJRijc3"
    "buTv/1xNW1se79xH/GsLBQAN8/jWdx6gf2CATCpDXBOx9Es3YK3l0KHDHD7Sh/OOL9+0BO+TiS2dylAql7hv5fcolytJyG2SRyJA9bQN0HtPLp"
    "dl46bNLF1+Jzt37SaXzWOMcNWVn+SxRx6iZ+HVfPrqK1n18M/4zDULMUbIZfPs23+AZbfdxcvrN9DWlsc1sT7QGmcB6Lpw1jRj/eba7tyYs1FI"
    "cqKBwUHO6+7m63fdzo03XMeUj1+InFQTUXbt/oC/PPsPHnr4EXbs3M348eMavnLaMH72vq2b304EdM3Mm6z+B2MKtWB9WiNiraVcrjA0PEx3Vy"
    "eXTb+ECy+YRGdHO4Jw6PBhdu/pZfO777F3336y2QzpdLoZ8snetmq/zfhpH2zefEioVWcKU2Y8K8Z+Tr1zzeyT1nOhSrVKqVRu+AOAGEMYWNLp"
    "dCP/bzJ1cGKM8c6v3b9jUw9gAnp6DGvWeDXymEEWn37/J1BVYuew1pLP52phsn7vaNxv2mRqXxJEEB4HoKfHSJ3uzJkzw0PD+qoYO0fVuVbWCV"
    "oDdSLWqvdbTCUzr7f330U42tdJhXLq5QvU64uCRor+H4lQB2JERAV/Te+2d9ZyQpHPAXbv1rc3eMMyxJTFWItqXLt3jovECWvAoRqLWCsi3nu9"
    "NSG/tFFmPTbWOVhqD2zd+Iz38bWqvGFsEIgxtulM64wgIsZYY4MA1Y3qWbR/x6YnTlXoriNRN3NmVCiZ5Xi9BfwViLRLKyuAp0ByOoAjIrwhyp"
    "M2Hvjd7t27i4zhqEEdxx2smDTpsg6XkfNMNUiJtGpj/MOhGogPqhUT+L29W7YcHI3TWFA/bvM/OmYAQL2OPerQ/xf4Ngrfp30fBwAAAABJRU5E"
    "rkJggolQTkcNChoKAAAADUlIRFIAAAEAAAABAAgGAAAAXHKoZgAAMCtJREFUeJztvXmcnFWV//8+9z619J7uTrqzkY0tNBBlFMX5Kg0KiLuora"
    "LydUZxY9Sv2zjDuAA6jMuMy1ed0R98XVDcJqCjg9vgQiuKyCqQECCQdAgk3emk0+m1qp7nnt8fT1UnYCCdVNVTvdz369XppFL13NrO5557zrnn"
    "gsfj8Xg8Ho/H4/F4PB6Px+PxeDwej8fjmWNIrZ9AAsyH1+ipHlrrJ1BN5opxSPzTI3QPxK+pt9cV/8894aM8nkNj4l/dhm6gt0NhvRILw6wXh9"
    "ksAAa6DXQorI+e5H52xYqTm4fFKOpm8+v1JIUYbVEnzjVMbt/+x4knuaelu1vo7VXiiWbWCcJsM4ii0fdGPObN7rFLj964NAo5RsWtE2QR6DMU"
    "AmCxIItn4WfjqSmCqo4iPAAiAn9W1UEr5hZVHpZ8tm/HjtvGH/uY7gBmlxjMBgEQ6DGw/jFv6uJjurrUmWeK0+coPBN0lYipF4lfkpbuqooqs+"
    "OVemYOCiIU/wBh//dKnYtAHwG5TVV+awy/37llw+3AAZ5ojy0uFWb0EnQmm0Vptg9LNyxe0dWFkRcqeh7wTGOsBVB1qCqAA3XEBi/Fl1eMD3g8"
    "h42CKgilLxgiBjAiAiIIUvr+3YvwE2fkh7se7Lp5/7K0x8J6eIw4zBxmomEY6JHSG9ix+qRO1L1U4G+AZxhjA1VF1QFaXAqIwRu6JzmKAUBVFE"
    "XEioiImKIYsBFlvUG+t6Pvnk3FxwhxQHFGCcFMMhiBblua8TuXrz2JVPBG1F1gjO0sul6gGsZLMkpG7/HMBByq7kAxcM7lEPlPhW8NbLnn+vhu"
    "M2tpMEMMqMeWZvz2VccfnyL4B4XXG2PSTh04FxWN3s/yntlAvCYVscZYYo9Vr1fnPjWw7d5fxXfpDg5c3taKWhtTKcAXta5Z05KO6t6L6HuMtS"
    "0uikqzvZ0Bz9PjORK06BWIGGNQBeXroUSfHNyy6f7ifQw19AZqaFj7Z/3OlSe+FuEyY8xxcYBVo2KwxRu+Z46gEYgYa42L3LDAv+3cmv0U3FY4"
    "0BaSpkYGFrs/7UcdvzRl7Wcw9rWoQ53zM75nbhN7tUG8NHA3q7r/07/13pvhEgOXJV5dmLShldbwbtGqE59vhK8bMUucC6NiwtUk/Hw8nlqgqE"
    "ZibIBq6JS/H+jb8Pni/1kSzBQkaXCGWN1c58oTLrUiPxdY4qIwBLEJPxePp5YIIoFqFClqrTWfW7z6xO8vXbq2HYjiJUFSTyQR4jVOy4qTW7M2"
    "+q4x9vkuikqBD2/4nvmMohoZGwTq3AOEvH7n9g23JJUlSEAAYuNfuGLtEmvMj4y1p7owLCCSqv7YHs8sQTUUawNVN+QiecmubRt+n4QIVHn27Q"
    "5gfbR4+YmnBtbeJsac6qIw9Mbv8TwOkUCjKBKk1Vpu6FjV9RboDenuDqo6bPUuHc/8i5efeKqm9BeCaVWNouJ63+PxHBwHiBgjzrm3DmzdeGU1"
    "04RVEoCi8a8+8VRFfyFIq6rzxu/xTA8FnBhj94tAdZYD1RAAA7jONes6cOE9ImaRn/k9nsNmvwiE0dkD2+79ZTVEoNIxAAF06dK17bjwOm/8Hs"
    "8RI4CoqhNrvte5uusZsfFfUlGbrbQHYIGoc1XX9cbas4o5/qoGMTyeuY06EWtQN+DErBvYcs8AxWK6Sly9cmoSRyujzpUnXGqMPcuFYcEbv8dT"
    "LmJUoxBjOkTd1UBF+1tVSAB6LL29YcdRa88WYy9xLop8qs/jqRQSaBSFxtizOleecCkQVSo9WAklMQCda9YtFBfepSIdxfZJvsLP46kskYgxLo"
    "rOjvsKlJ8eLN9Ie3oEcBKFXxJjO3HOVeS6Ho/ncWjcptTI/9d+/PFNxV6DZU3iZRpqj2X9+mjxiq5XiTU9zoWl7bwej6fiiFGNImPs0XbSfIJ4"
    "41BZNlyOeghgWtc8rTHtJm4XkdWqrtSg0+PxVIe4PgBUnD1tx7a7b4dLBC47oqxAGcbabYEoHY2/1xi7Ju6V7o3f46kyAgrGBJGEnwEULjviif"
    "wIDfYSA73RouVdxyDyHqeR866/x5MUYtVFkTG2e/Hqrh7K6CFwhAKwUQA1Vj9ijG3B+bN3PJ6EEQTUcTldXeliq/HDtsEjEIBLDKx3i1eeeAJi"
    "XuNc5IoNPD0eT2KI0SiKjLXHdo7xOsAdSUDw8A23+wYDqIq70BiTKR7R42d/jydpJG4npOjfxTesP+yGoodruAKwZMlx7VEm2CAii+KTN70AzB"
    "eszIwPW4FoVpy/W3WciKCO5/X3bbjhcIuDDrOcMD66K8oE51ljO1wURj74N39QYE/BzYhzrxVosQYrs+Qc7mqh6sTYQCV8K3ADPRTrg6bH4Yq5"
    "AehYecJNxtpT1UXOb/WdHzggK8IbOhvIGsFp8p5AKcrlgLTAtwfGGCg4UvNbBBQRUDfmVI7d1bdxJ8Vt+dN58GF4AJcYuMwtXL3uONHoqeoc3v"
    "jnB4HEM//LOxv5l1WtjEeKrdE6oKDQmDJct2uMzxZGSM9v4wcQnIuMDRqJonOBb9DdbemdXuOQ6QtA9w2GXpzVwsuMCdLF5p5+u+88IFRoDgxv"
    "7mxkTxgxFiW/2UOI1/x1FjYN53nH5j2giojMdwEoBQNRcT3AN+jtnXZV4PQ/x/iiRlVfqWgxC+mZ6wQC+0LHy9rrWdeQZiKK3e8g4R8rEBjIiP"
    "CxbcPsCR11VmbGGds1R4yqIphnL1yxdgnFxqLTeeR0BcAAbuHqdceI2FNUVb37P/cRYpe7OTC8qbORvGrNtnmGCq2B4cqdo/xy7wStgRDO+6l/"
    "ingZYGxzYMw5AHR3T8s+p/d5dncbAOvCvzbGpItbfj1zHCswGikvL87+Y5FiauD3RQrNgXDzSI5/eXiYZmt8CvAvUQBVPROAjt5pvUPTE4Cpi+"
    "kZBw7mmds4hZRAz8J6whp95EosRDmnXLJ1L3lVn/o7GCLGxQui0+jqSrN+equj6QnAelxcb8yzHA5f+jv3sQIjkXJ6S5anNWUYrVHkP1JYEBi+"
    "9OgIfxzJ0WzFz/4Hx+BUEbO6fdyuJtbIQ9rpdAzZALpgXJcgshytbFNCz8xEFYzAWxY3xnn2GhhdqLAgEP5naILPP7KP1sD4df+T40QknZJoXf"
    "zPnkPa6TQEIL5IRs1qEVNf7PfnBWAOU5r9u1uydLdk2VeD2V+BjIG9oeOSvr1xk3z/rXtyVFVEQOkCoHugAgJQuoi4dfHFazEXeJLkwNnf1Gj2"
    "dwp1Rvj4tmE2jYc02Lj60PMkCKJxRUDsAfQeOhA47bW8wJLiX/3HMId5/Ow/UoPZP1RoTRl+MDjOVf2jtKZ8yu8w6WSa5cCHFoBiVZEqT/MFQH"
    "OfuOJOOa+9npRJftZ1QJ2B7bmQf942TIM1NfFAZidxQRBwXMuKkxcwjYKg6Ufzxa/A5jqmmPdf15jmBW117AsdQQ0+9ZQRLusb5uFcSNZU6Ays"
    "+UVg0xPT2hJ8KAEQQJcvP61O0WXFm7wQzFEMkFflzZ2NLKhBxL1U7ff1naNcMzhGW8pH/Y8ABclSyBTt9ZKyPQDdx746UVmuvvnHnGVq9m9I89"
    "L2eoYTnv0jhUYr3Dte4F+37/PVfkeGxJkAUxc4WR7ftLH8JYAYq4gWyn9+nplKXPcfz/7NCc/+pWq/vCof3DLEcOhIGR9tLgcx07PXw6no8zP/"
    "HMUIjEfQVZ/mJe31jETJzv5hsdrvPx4d4bfDkzQHvtqvAlR0N6BnDmOACed4cVsdCwJDIcGoW1Ss9rt5JMeXHh2hNfCuf5J4AZjnCDDpYEUm4H"
    "UdDYxFLrG8vwIpA8Oh4+ItfqNPLfACMM+Jt/w6Xt/RwIpMwKRLbq0XKTRZw6e37+PWUb/RpxZ4AZjHPH72H01w9j9wo89V/aO0B4aCN/7E8QIw"
    "j7EC45HjDR0NHJXg7F+q9nskH/HBLUNYX1pSM7wAzFMEyDtYlLa8alE940mu/RWyRrh82zB9kyF1vtqvZngBmKeU1v7Pb61jZYKzf6jQnjJcOz"
    "jONYPjvtqvxngBmKcc2Oo7TOiQD6fQUKz2+2jfXuprsNnI81i8AMxDHt/qezSpZp8CFvho314GCo6Mr/arOV4A5hm1avXt23rPTLwAzDNq0erb"
    "t/WeuXgBmGck3erbt/We2XgBmEfUotW3b+s9s/ECMI9IutW3b+s98/ECME9IutW3b+s9O/ACME9IutW3b+s9O/ACMA9IutW3b+s9e/ACMA9Ist"
    "W3b+s9u/ACMMepRatv39Z79uAFYI6TZKtv39Z79uEFYA6TZKtv39Z7duIFYA6TVKtv39Z79uIFYI6SZKtv39Z79uIFYI6SVKtv39Z7duMFYA6S"
    "VKtv39Z79uMFYA6SVKtv39Z79uMFYI6RVKtv39Z7buAFYI6RRKtv39Z77uAFYA6RVKtv39Z77uAFYA6RRKtv39Z7buEFYA5R7Vbfvq333MMLwB"
    "whkVbfvq33nMMLwBwgiVbfvq333MQLwByg2q2+fVvvuYsXgDlANVt9+7becxsvALOcarf69m295zZeAGY51Wz17dt6z328AMxirMC+KrX69m29"
    "5wdBrZ+A58jRYq7/zYsbgdhdr1Ty3yk0WeHDW/eyaTykzXf2nZN4AZilWIHhUHlRWx0vaqtjNFKaKzT9R0CLFa4eGOOb/aO0+mq/OYsXgFmK09"
    "hF3zoZ8up7d5F3VCz1p8SFRXeN5Wmwxlf7zWG8AMxSFEiLcP9EgQ3jeaTChb+KUm8MgU/5zWm8AMxilHhXXr0IFVz+F68V1/l745/beAGY5Sj4"
    "vLzniPFpQI9nHuMFwOOZx3gB8HjmMV4APJ55jBcAj2ce4wXA45nHeAHweOYxXgA8nnmMFwCPZx7jBcDjmcd4AfB45jF+L8AcRkSQYhsfOaCdz/"
    "6/lv6iU7/2bytQVEGLPca0kr3GPDMGLwBzgJKhi8Sbgp0qkYsICxFRFBJGES5yqOr+n6lHl/b+AQdcxxjBGksQWKyNf4wxSLHvoHPOi8IcwAvA"
    "LEQAMQYjglOlUCiQy+cpFAqoQjqVoqGhngVtLbS3t9LS0kzbggU0NzfR2NBAfUMdqSAgCAJEhDCMRWJyMsfY2Bgjo2MMDe1l7/A+9uwZYu/wMK"
    "OjY0xO5nDOEQQB6XSadDpFYC2KF4TZiheAWUQ8A8cGOzE6Rr6QJ51Ks2hRO2tWr6Rr7XEcf9wxHHvMGpYtWcKiRe00NTaSzWSPaLwwKjA2Ps6e"
    "PUPs7N/FQ1v6eGDzQ9x73/3cd/+DPPLoTvYO78MYQ31dlkw6DSJeDGYRXgBmOCKCNYYwihgdHaVQCGltbeFZz3w6f33aqTzrmadyUtfxLFncic"
    "jjY7rxUqAQ5qdc//hWDljs728lcmBsQCQWnOamJlqaWli9chXPesapU1ceGd3Hgw/18afb7uAPN93CzbfezsPbH8FFjvqGejLpNKqKc/7g8JnM"
    "oZrICKAtK05uzZrwQcS0Fr9FvkF0lRERrDVM5vKMjo7R1NjA0//qqbzkhefwvDOfw3HHHo0ckMQJowJRFE2Z8xMFAA+XWDggDgrGsQMjQhAEGL"
    "FT99u1e5A/3XI71/3sen51w+94ePujpFMBDQ0NiEAUeSFICCdijDp9bn/fht9Aj4X10RPd2XsAM4yS4U9M5BgbH2PFUct50wXn85pXvZynnbKO"
    "kvbGBp8H9i8NrLVPcuUjfz6xfjxWRKIoItQQp4qIsKi9jRedew4vOvcc+gcGuO5n1/Od//wBt9x6B04dzU1NAN4jmGF4D2AGYa2lUCiwb2SEo1"
    "ev4m8ueC0XnN/D4s5OAAphHudcMUo/s0o4DnT3M+kMIEQu4vpf3cCXr7yK3/TeCAJNjY0+RlBdvAcw2ygZ9NDevSxsb+NdF13IOy78Gxa2twNK"
    "Lj85dZ9qzPKV4EAPpBAWcM5hreXcs5/HuWc/j5/9zy/59Of+nT/efCuNjY2k0ymi6Am/l56E8AJQY6y15HJ5JiYneOXLXsRH/+kDHHv00SiOyf"
    "wktoJGX5p5S5n/A0MDpbW+MfKY+MGRcKAYlMTrBeecxVlnns6V3/g2n/7sl9g1OEhrSwuR9wZqyszyI+cZQRAwvG8fLS1NfO3Ln+dbX/0yxx59"
    "NJP5ScIwJLC2LEMsEUURqko6lSGTzpJNZ8mkM6RT+38y6SzZTJZ0KoMxZuox5VIqIMrlJxEjXPSWv+XXP72Wc89+LoN7huKg4gxbzswnvAdQA0"
    "ru/ODgbs563ul86TOfZNXKFeQLOQCCCs74IkImHdcBPPDgg9x2+5+5974HeOTRnewbGcE5R11dlo5FCzl6zSpOecrJnPKUk8hm6gBHLp+viAdi"
    "iwVDk7kJ1qxexbXf+Rqf/eJX+PgnPosxhmw245cENcALQMKUZvQ9Q3t590UX8i+XfZggCJjMTRIElVvfR1E0Zfj//dOf89WrvsMtt93J0N69OO"
    "emMgeCTAXwFKiry3Ls0at5xctezIV/83oWtreTy+ewtvxZWoi9nnwhh4jwvne9g5NPPIG3vesD7N49RFNTA2HoRSBJfBYgQYwRIqdMjE9w+aUX"
    "8653vIUwKkwZZKUoGf/Wbdv4wMWX8rNf/Cqu1quvi8t/OfiJPwJEzjGZyzE+PsHRq1fxz5dezCte+iLyhVzFXfUwDMlm6rj3vvt4/d++gwc2b6"
    "GlpZkwDCs6zjzjsLIAfvGVECKCi5TJiUn+4/9+ine94y3k8pOoalWM/9Y77uScl7yan/78lyxY0EJzcxMiEufvo4joID9hcd1fl82waGE7/QMD"
    "vOFN7+DTn/sS6VQGV+FintjzmeCE44/numu/zUknrmXv8DBB4B3TpPACkAAisZs9PjHBV774ad7w2h4mc5PYCgX5SjjnyKSzbLzvPl71+jczMD"
    "BIe1vrlIFP/zpKGIZkMhkWtLTw4Us/wRe+fAXpdOXX6UEQkMtPsnTJEn74vW9wUtda9u3bV7E4iOfJ8QKQAEaEfSOj/OsnLuH8nldWfL0PTHkS"
    "o2OjvPXv3sfuwT00NjZQKMOdLqUN29tb+chln6L3xt+TSWcrLgLWWnL5SRZ3dvL9b17BUUctZ2x8oiJxB8+T49/hKhMEAYN7hvjAey7ibW96Y1"
    "WMH2JjDWyKz33xK/zp1jtYsKAya+mSsIjAxR+9nPGJ8TiiX+HcvbWWyfwkK1es4Oqv/TvZbIZCIayoh+T5S7wAVJHAWoaG9vKyF5/LpR/6e/KF"
    "fFVmNecc6VSaLX19XPG1q1mwYEFFo+lRFNHY2Mjtd9zFd9f/gMCmiKpQ0x9Yy2RuglPWreMLn7mc8fEJLwBVxgtAlTDGMDE5yYoVy/nCv11evF"
    "Wr8oVWVUQMV139fXbtGiSdSlV8hnbOkc1m+fq3vkcunyNlqxOoK8UEes57GW+/8I3sGdrrg4JVxAtAFckXCnz2U5exuLOTQiFflYo3VSWVSjEy"
    "OsqPfvJz6hvqq1JQ45yjvr6Ou++5lz/+6VasDaq2xdcYQyEscNmHP8hT153I6OiYrxasEv5drQKBtQztHeYNr30lLzj7LHL5yapt4nFOMWL545"
    "9uYfODW6jLZqpWW2+MkMvl+OkvfgWAHrSaoHxEBOciGhoa+MTHPlS1cTxeACqOiJDL51myuIMP/+P7cS6q6jq2ZBy/uuHGqgfNnFOymSy/v+lP"
    "5As5UlV0zePMQI4zT38O5/ecx9DevT41WAW8AFQYaw0jo2O88+1vZtmSJRTCQlXd18BawijkT7fcTiadxrnqzZbOOTKZDA8+tJWHtvRhTVDVBh"
    "8i4NTxwfe9k4Xt7eQLBR8UrDBeACqIiDAxMclxx6zhzW98HVEUVtX4S6m/7Y88yoNbtpLJpKu+tTYILMP7Rrjrno1Adc8LMMZQKORZs2oVF7yu"
    "h337RnxtQIXx72YFscYwNj7Bm9/4OlqaWwij6rrkJeO77/4H2LMnjpZXWwCEWHjuvufe4nOo6nDFKkrHW//2DXR0LCLvawMqiheAClFa+x+1fC"
    "mvffV5OK3sBp+DUTK+jZseiJcaCRiGEq/PN92/GYgDg9XEGEO+UGDVypW85IXnMDIygvUZgYrh38kKYa1hdHSMl77o+XQu6qBQyFd9pipd/v4H"
    "HkTEJBIrj4uOArZt285kbiIRr6PEBee/irpstipFSPMVLwAVIoriQpme815SvKX6s7ExBlVH37btpAJbfX+8SCqVYmBwN4O79zymNXi1sNYQRg"
    "We/rRTOOUpJzM+PuHrAiqEfxcrgDGG8YkJTj7pBP7qlKcQRWHVg1WqSmAtY+Pj7OjvJwhSuAQEQFWx1jIyMkJ//66p26pNGEYENuDFLzybXD5X"
    "9aXHfMELQAUoFcicdebppFPpsnbgHQ4i8V6DoaHhqmwweiKMESZzOfoHkhOAksGf/dxuWpqbfeegCuEFoAKU3P/ndj8bAElgdioZ3eCePYyNjR"
    "eXA0lVzMWNRXb2D8TPJYER40alIccfdwwnHH8sE5OTfhlQAfw7WCYiQj6fZ8XyZaw7uQtVl0w0vmjse/YMMZlL1hhE4qrAgV27i08mmXELUUgq"
    "SHPaM59OLueXAZXAC0CZGGOYzOU4+aQumpuaKYTJVqsN7h7CRS7RMVXjJie79+wBDjxUtLqUhnnmqacUexIkM+5cxgtAmQjxfvn43D6qWop7IK"
    "Uv/56hoZqkxcQYhvYOJztm8fTjk088gQUtLYRhmJj4zFW8AJSJK27HPbHreCC52bDE8PC+Yj+AZMc1IgwP7wPKO334cIj3BkQsW7qUZUuXkM/n"
    "D3Ikuudw8O9eGZS67LY0N3PMmtVAcqfclGxu38ho/PcE3WEtngg8NjZOtZqcHAwRIQxD6rJ1HL1mld8cVAG8AJRJoVBg0cI2OjoWopq8Kz46Op"
    "ZYFeCBxB2PJoiqvN358ZRqHdasWkEURn4JUCZeAMrAiFAIQ5YsXkxjfUPVN/88lnic8fHxmhiBMcLkZJ5CcRZO+oDPVSuPAhEfCCwTLwDlUFwC"
    "dHYuQsQkFgAsDg3AxMQkkvBBTaUehPlcjjBM1gMojbR06ZJi8ZNXgHLwAlAmzimLOxcV/5X8lzGXzycfeSQeMl8oEEXJCkDptS5a2Ea6yg1Q5g"
    "NeAMpAJJ4N29vagMT24uwfnDgGYZKOAhIH5AqFQhwDSHLc4u+W5mbqstmpE5A9R4YXgDIoFcQ0NTXFNyQ5ERZ/h2EEkrD4AIjgnCueF5jgEqBo"
    "7PUN9aTTKe8BlIkXgDIRY2hqbIj/nvjoGrvgNRhZiCPyYRVakB8apb4uSzpd/RZocx0vAGUiAplMuiYDK1qT1GMJVa3JafFKvCU5lWAzkrmKF4"
    "CyUASpyck1JZNzqrVwPWI0ma3AB8MYE+8H8FmAsvACUAFqF4TSmmbBYg+kBk9AY+EVEZ8FLBMvAGUhKBBFyTQAOdj4YmpnBIJgalGLXwxAxinI"
    "5IefS3gBKBdVCoXkBUBVEahJAHAKgcQ3IhQpCYBXgPLwAlAmTpWxsYkajV67dbBSWocn/xUS4hbs+XwyrdDnMl4AykAE1DlGRkaAGuTiiTvmUo"
    "s4oCpGTNUOPX3iYeOsw9jYOPkEWq/PdbwAlIkCe4eTbYwB+6PvU335E7YDJT4mzBpTEw9kdHSUyclcwr0Q5x5eAMpANXaD+wcGgdosRzPpdNEA"
    "k98QlEqnsDZZASgZ++49Q1MC4Dly/LtXJtaYqfbYifblK/7OZrOJx+BEBHVKOpXC2qAmWYj+/l0Jb7+em3gBKANVJUgF7Nixk1w+l2yjyuJA9f"
    "V1NXGBVeNW6KmUneoQlMy48e+t27YTRa6WOZA5gReAMlBVUkFA/8Age4aGsCb5/ekNDfU1EQDnlLpsNj6RKMGmpCWd2drXV4w/eMrBC0AZqCpB"
    "ELBn7162PbwdSK4zTmmYpqZGXMJNQePmnI76+jqEZINw1lqiKGTzg30EKb8XoFy8AJSJMYaJ8Qnu3fQAQCLn8x1Ic2krcqLEMYCmpkYgOZ8nPg"
    "8xYNfgbvoefjgOgHoBKAsvABXizj/fAyQXiy/N+G2tC+JIfMJ24FRpXdAS/yMxryfOdty3+UEGB3eTSqW8AJSJF4AyUVUymTS3//luCmEh8Z2B"
    "bW2txXr8ZPsROudoa10AJFcAVWr+ccutd8QpQJ8BKBsvAGXinCOTybD5wS08tGUr1gTJBMWKX/6F7a2kUgl3xtE4FbhoYXtyYwLWxvsO/vin2/"
    "YXQHnKwgtABUgFAXuG9nLjH24GkjkerDT3tbW10lBfl2hvPCWuf+jsiJuhJjFsHHBN8+jOndx+513U1WUTj7fMRbwAVIBSh5rrf90LkMiptVLc"
    "hbewrZWmpsZ4Z1xCqDpSqRSLF3eWnk3Vx3TOIQg3/uFmdvYP+HZgFcILQAVwztFQX8cf/ngrfdu2kQqS+XKGUUhzUzMdHYsohMlUxcXHoTkaGx"
    "vo7Fg4dVsS4wL8909+Udst0HMMLwAVQBXSqRQDuwa57ue/nDKSaiIiRM4RBCmWLV1SPCk3GcMIw5C21gV0LFqI4qq+BHCqpIIU27Y/zG9//0ca"
    "GuoTLT6ay3gBqBBOlWwmzfev+S/yhXzx1JrqosVYwzFrViVWFhufBxCybOkSmhqbEqnHj+MbhvU/+G/6+3eRTvv0X6XwAlAh4mVAPXfceTe/6b"
    "2RwKaqvi4v2d3a44/BiCSSCCwdCHLMMavj49Cq7OmoQsoGjI2P8b31P6Surs7P/hXEC0AlkTgg+OUrvoGqq/pW1dLMe8La42K3uMrGCHG4T4GT"
    "u9YC1a8BiFyEtQE/+K/ruGfjpmLGw8/+lcILQAWJIkdzUxO/7r2R3/z2RlJBuqpegIjg1HHMmtUsW7qYXAIdciLnqK/Lsu7kLqC6GY+49NcyPj"
    "HOf1zxDbKZjE/9VRgvABWmZH+f/LcvEoaFqnbNFREKYYGmxibWnXRi1RtkiAj5fJ5ly5ZwwvHHoVrd2oMocgQ2xVVXf4877rrbB/+qgBeAChNF"
    "jqamRn73h5u5+nvXkEpV1wsoBQJPf85pqKtuINAYw8Rkjr966jpamlsohIWqCYBzjnQqxaM7dvDZL3yFpsbGqmdW5iNeAKqAc46Ghno+/snP8c"
    "iOR0kF6arNXKUZ/7ndz6a9vY1CWN0W5arKOWedAVR356OqYozl0sv/le2P7iCT8YU/1cALQBVQVTLpNDt29vMPH/p4sXFldcYyRsgX8qxZtYr/"
    "ddqpjI6NV6VTb8n9P2rZEs4+83RUHbZKy40wisiks1z7o//mO9//AW0LFsSnIHsqjheAKhFFEW2tLVzzX9fxla9+g0w6Q1il2Tk+IFR44wWvqd"
    "oSwFrDyMgoL33xuXQs6qhaS27nHNl0hi19fXzg4svI+pr/quIFoIpEkWNBSzMfuuQT/PYPN5HN1FVFBKy1hFGBc557Bs/5X6exb99IRb2AUvFP"
    "e3sbb7/wjVUL/jnnMMYyPjHJhRe9l8Hdu33TjyrjBaCKxOvYeK/+hW9/D5sfeohspq4qQcEoigiCFJd86AMEQVDR3YFBEDA0tJd3X3Qhx6xZQ7"
    "6Qr3i2odTsI7AB737/xfz+pltoaW5OdJPTfMQLQJVxLu6eu3NgF6/9329j58AAmXS24l9say35Qo7TTn06H/7H9zI4uAdrbdkikE6l2DW4m3PO"
    "OoP3vPNtFMJCVYxfVUmn0lz80Y/zre9ew8L21qotmTz78QKQAFEU0dzUxKb7N/PK899Ef5VEwBhDvpDj/e++iHf/3YX09w8gwhEtB0SEVCrFwO"
    "BuTnnKSXz1y5+PW3BRWfffaZwdSacyfPSfP8nnv3QFC9vbfNAvIbwAJEQYhixoaebPd93Dy179v+nbto1MOlvxWS4uDsrzmU98jEs/8kHGxibY"
    "NzKCMYbAWowxBzVgEUFEsNYSBJZ8oUD/wC7OPftMfvSf36Szo6PihU2Rc1hjSQVp/vEjH+PTn/132tpavdufIF4AEiQMQxYsaGHDvffxwvNex2"
    "133BkHBqOoYoGuknEXwjwf+vv38uNrvsVfn3Yqo2Nj7N4zxPj4BGHRwIwxU4LgnCOXz7N37zC7dw+xuGMRn//0x7n2O1+nY9FC8oXKVhmGYUgm"
    "lSGfK/Cmd/wfPvelK2j3xp84h/LlBNCWFSe3Zk34IGJap6I1niPGWsv4+AR1dRk+88mPcX7PK3AupBCGFY3eR8V8uuL49Q038pOfXc8tt9/JI4"
    "/sYHRsjFw+P3W4SV1dHYsWtnNi1/E8/6wzeeHzz6K9rY0wKuBUK9aAU1XjPorpLA8+tIW3vPP93HTzLfFYfs1fCZyIMer0uf19G34DPRbWP6Gq"
    "JtvC1gPEhllfn6VQCHnL372PW267g4995B9obGgkl59EjKmIwVlryRVyGCM874zTed4Zp1MIC/QPDLBrcDdjY+NTXY3bWlvp7FxEU0PpnAFHLj"
    "+JtbZixh9GEdl0BhDW//DH/MOHP86uXYPe+GvI4QiAT8ZWkChyWGtpbmriP674OjfdfCuXX/pPPLf7OQBM5iax9uDr9cOhVK2Xy+cAJQgsy5cu"
    "Y/nSZX9xX6cR+UJuKn1ZKW8kiiKstWTTWR7dsYOPfeIzXP29a8hmMjQ3N3njrw7TstdpLerURYJ6b6HSlNzhhe3t3Lvpfl5x/t/yzvf9I1v7tp"
    "HNZEkFKcIoqsg+AmvN1OGlhTBPvpAjl49/8oX4J4qiKcMvV3hUdWo9H2c8HP/vG1dz5gtewVVXf5+W5mZSqeo3TZmvqLpp2et0BEBSE1EBGC5d"
    "+4ifleeghGFIfX09ddksX73q25z5gvO4/NOfY8fOfrLpLOlUBuccUYWChSJSNPT458BgYLk45wijiCCwZNLx0eU//PFPeP5LXsO73n8xu3fvob"
    "29tWKvxfMXiKqGxpmivXY96Zs8nU/cAK5zVdf1YsxZ6qIIpPoN7+YpgbXkCgVGRkZZsXwZ57/6PF73mldw/LHHAqA48vk8QMWMthxKRTyqcWv0"
    "wKYA2Du8l+t+ej1f+9Z3ufmW2whsQFNjA5Fz3vCrR3xOu7o9eVO/Zuih24bZ38TpoExbADpWru01NjjdC0D1ifPxhslcntHRUdrb2njuGc/hVS"
    "9/Mac/+1m0Llgwdd9CmCcq9gEo5fKrJQolw3UuNngRIZ0KkOLXwbmI2++8ix/95Bf8+Lqf88Dmh0ilUjQ2NkwtdzxVRYs53eGJSNbs275xD2UL"
    "QHd3QG9v2LGy6/9aa9/tojBExMcDEqAkBIVCyOjoGCKwZvUqnvPs0zjrzNN5+l89laOWPTaYp0SEYRw3UN3foehAUXgigVCYavKnU7/ja5SKhK"
    "x57Ee/b3Qf92y4j1/f8Dt+3Xsjd9+zkZHRMRrq68hms97wE0WdGGuci24dWNnxLHp7Iw6xZJ+2IQvsKPv5eQ4LVSUM48DcguJJvI88uoOvXfUd"
    "rvrW9+lYtJCuE47jaac8hac+5STWHncsy5YtprmxeTpXf9y/Dz0X5PKT7NjVz4MPbeXP92zg9jvv5s4/38O2h7czPjFJJp2mvr6OhW2tRMWYhS"
    "dRih+q7KK3NyT23ssUgN4OBRD0zxpPB756MGEOjKin02my2SygjIyO0XvjTfzyN7+dSil2dCxi5VHLOGr5Mo5avpSlSxazcGEbba2tNDbUU1eX"
    "JZvNElgbp/lEcFFEFMWVgBMTE4xPTDA8vI/B3XvY2b+L7Y88wsPbH6Vv23Z27OxnaGhv3H/QWrLZDJlMhvq6Olxxtg+94dcGRQXBqLsbgO5uQ2"
    "/vk7pf0/AA4ihiHrM1pZoD0uU/U8+RcqAYBIGlKdU4dSZAGIY8vP0RHnpo61T60BqDsYZUKkUmnSaVSpFKBXHkX0zcytwpTh1hGFEoFMjnC+Ty"
    "OVxUnMWLS5FUKkU6laK+vr64rqe41FBv9DMDAVDkvuk+YBoCcJkC7ImatnYGw7sQs7zYgsZ7AjWmFH0vSbwRIZNOk81kisHA4pJeFVe8by6XZ3"
    "Jysvj4+HEHxgniXYAB6XRq6hpo7EdOjeccflk/AxEx6lwUwR0A9J7hoPfJHzLNS1sg6lzV9UMx5uU+EzB7OXSGQKt+2IenKpRSgI+4MVm7a9fG"
    "UQ6RAYDpzuLd3QJg1PVK6fgbz6zkwLz9wX9q/Qw9R4a6WNzllqLxW6ZRtDc9Aeg9w8VDBDeqOjeV+PV4PDODYgBQlN8CU5P2oTicihHhmGPSnY"
    "XUXVhzXJzc9RkBj2eGoCCFUOUpu/vu2USxgO9QD5q+AXd3WzZvzoH82GDimlSPxzMD0EiMEdXojt1999zHNI0fDkcAivUAiru2uAzws7/HMxMo"
    "uf8i1wJKd/e0bfMwjHh9BMhAg7ld1T0UJ5HVewEeT60Rsc5FYSSpHwFTMbvpcJizeLdl48Y8yrf9MsDjmQkU3X/0d4Nb7toMGLisWgIQK4tz5l"
    "tO3SRGppVq8Hg8VUIhTs3L/wPc4bj/cNgCcJmjp8fuenjDg6j7oYgVVH0NqMdTE9RhjHEufGiAsR8AUtwBOG0OP5C3vjS2uaK4V9R3CPZ4aoHi"
    "jBgR5Jv09U1C92F75EdqvHGTkFUn/MYYe4YvDfZ4EkfjzR5uyOSi43fsuH/31O2HwRGm8nri0mD00ngJ4J0AjydRVJ0RI6p8dseO+weh55B7/w"
    "9GGZYbHzjQuXLtN41NXeA7BXk8SaFOxIqq29xfz0ls3BhS3LB5uFcqo5inSwFxzl7mnNuHMYfceeTxeCqCxnU/fIiNG/NFj/yIbK9M3z32AjpW"
    "nvBP1gaXey/A46k2GokJrEbh9f19955Dcav+kV6tzHLe9Q567ECD/Jtz0R1ibQA+LejxVIk48OfciIvkIkDgkrK87nLr+RW6lI0b85Hqmw8ICP"
    "qlgMdTcTQyYq266OJd2zdujgN/06/6OxgV2NBzmYMeO9h37x3ORR8wxlrvBXg8FUY1MiYInAuv7d+26d/p7g6e7NTf6VK5/F3x/IDOVSdcY0zw"
    "Sh8P8HgqhToRI6humXD26cPb7t5b+o9yr1y5Lb1xCaKZdMFb1EVbfDzA46kIWpqnFX3d8La7h440538wKrmnX+EShrfdPURkXq2qexAjfsuwx3"
    "PEKBCJGOOi6G39W++9uZR5q9QAVSjhK6UG1z7LGPt7jc+YkuqM5fHMZbRgbCoVhfn3DvRt+jx0B9AbVnKEKnT1WR9BdzDQt+kmF0VvE2NK7Yl8"
    "ZsDjmTYaGhukXFi4cqBv0+fjoN/h7fSbDlWclWO16ljV9RZjzBXqXFQcz7cS83ieFC3Exu+u7O/b8Fb2n/FX8Um0isbYG9LdHQxs3Xilc+6tYo"
    "yN+wj6mIDH8wQoEBmbOsD4e0pbfKviQSewLo89gUWrTny+Ef2uiGnVKPIpQo/nMWgEYkUMzul7B/o2fB4uMcWj+aq2fE4oMBeLwOLVJ54K/AiR"
    "JeqiELwIeDyohmJMAIRO9aKBrRuvLAbTqx47S2g93htCd7Bzy4Zbwih6mqr7hbFBQBwc9EsCz3xFUQ2NDQJVHogizhjYuvHKA6r8qh44Tzo1N7"
    "VzafGqrksQcyko6pxfEnjmF6oRItYYi6r7T5OLLnr00U27K53nPxS1yM2XvA63aNWJzzfwBWPMcXGSQH1rMc9cx4GqMdY6p8OgH+nfuvGL8X8l"
    "a/xQ0+KcOC7QumZNS0brPo7q2zEmpW7q5HmfLvTMJRTVSIwNEFCnP4skes/glk33U8U036GodXXe1JKgY+Xa00Tsx0TkbADVKALxdQOe2Y5D1W"
    "FMYMSgzt2jyuX9fRu+F/938rP+gdRaAAAk3tywvigEXecbkX8SY06Kz6t3imrpLMKZ8Hw9nkOhxK4+IsaKGNRFAyBf3Fmvn47beO1fCtfwec4o"
    "g9r/hnR1pTvH9A2IvFlE/jrOjZZiBFA8lnwmPXePJzZ6VUUkEGMQBKfRJpx8X418eWDLPf3xXWs76x/ITDSix/Q461h90tkCF6DuZcbYZo2zBh"
    "TFQA8Qg5n4WjxzGweqKLHRi0FEcC4Kgd8IcvXOIPd9Nm/OxXdPJrd/OMxUoyktC6berGXLTl4eBtHZGHk5qmcYY5sBVGPRBY3iDwIpvizz2Ot5"
    "PEdEyVgdKAd8x4yICCLxTO+iCLhZkB/i9Kc7t23cuP8SM8/wS8wGwyilBae8gmXHnLw8dHq2OD1d4Rmgx4sxVhC0+B7HogDEv32xkedIkNLRd6"
    "V5RUrNOdSh6raB3KoqvzOq//NYo//LSWwmMhsEoISBbgO9j6se7LGLV248TpC1Km6dwjqEhap0CWpBmsUYO3M/As+MRIqTiLohEBQ2i+oQIreg"
    "bBYjG2Qyu2HHjtvGH/uobvuX39GZy2wSgAMpikGHPlEwpXXN01pcmDd14hZrwGIq2kbBM+cJQByjEy54oEWdbNt299AT3NPS3S30zh6jn2sUaw"
    "V6bFxDjcXXDngqjxAbexAXsWGZvRPoFLP+BTwJsv93j8TnmvfU8vl4Zg2l78p62D+r+0Wkx+PxeDwej8fj8Xg8Ho/H4/F4PB6Px+OZDfz/cFaR"
    "+Ae+0xoAAAAASUVORK5CYII="
)


def main():
    global n, np, S, LOG, LOG_IS_STDOUT
    ap = argparse.ArgumentParser(description="Нокс Студия — ручная правка аудиокниги")
    ap.add_argument("--root", default=r"D:\kniga1a", help="папка книги")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true", help="не открывать окно (для проверки)")
    ap.add_argument("--icon", help="только записать значок приложения в этот файл и выйти")
    a = ap.parse_args()
    if a.icon:
        import base64
        Path(a.icon).parent.mkdir(parents=True, exist_ok=True)
        Path(a.icon).write_bytes(base64.b64decode(ICON_B64))
        return
    root = Path(a.root).resolve()
    (root / "studio").mkdir(parents=True, exist_ok=True)
    LOG = root / "studio" / "studio.log"
    try:
        if LOG.exists() and LOG.stat().st_size > 2_000_000:
            os.replace(LOG, LOG.with_suffix(".old.log"))
    except OSError:
        pass
    if sys.stdout is None or sys.stderr is None:               # pythonw: консоли нет — всё в журнал
        sys.stdout = sys.stderr = open(LOG, "a", encoding="utf-8", buffering=1)
        LOG_IS_STDOUT = True
    if os.name == "nt":                                        # ffmpeg и прочее — без мелькающих чёрных окон
        _P = subprocess.Popen

        class _Quiet(_P):
            def __init__(self, *aa, **kk):
                kk.setdefault("creationflags", 0x08000000)
                super().__init__(*aa, **kk)
        subprocess.Popen = _Quiet
    profile = Path(__file__).resolve().parent / "edge-profile"
    alive = alive_instance(root)
    if alive and alive[1] == VERSION:                          # студия этой книги уже работает — просто открыть окно
        if not a.no_open:
            open_window(f"http://127.0.0.1:{alive[0]}/", profile)
        return
    if alive:                                                  # работает прежняя версия — она уступает место новой
        done = stop_instance(alive[2])
        log(f"работала студия версии {alive[1] or '?'} — " + ("остановлена, запускается " + VERSION if done else
            "остановить не удалось: закройте её окно и запустите студию ещё раз"), None if done else "warn")
        if not done:
            if not a.no_open:
                open_window(f"http://127.0.0.1:{alive[0]}/", profile)
            return
        try:
            (root / "studio" / "studio.lock").unlink()
        except OSError:
            pass
    try:
        if not (root / "nox_audiobook.py").exists() or not (root / "work" / "segments.csv").exists():
            raise Oops(f"в {root} нет подготовленной книги (nox_audiobook.py и work\\segments.csv) — сначала запустите go")
        os.chdir(root)
        sys.path.insert(0, str(root))
        os.environ.setdefault("HF_HOME", str(root / "hf"))
        import numpy
        import nox_audiobook
        n, np = nox_audiobook, numpy
        S = Studio(root)
        srv = None
        for p in range(a.port, a.port + 20):
            try:
                srv = Server(("127.0.0.1", p), H)
                break
            except OSError:
                continue
        if srv is None:
            raise Oops("не нашлось свободного порта 8765–8784")
        write_atomic(root / "studio" / "studio.lock", jd({"pid": os.getpid(), "port": srv.server_address[1], "at": now()}))
        threading.Thread(target=S.tick, daemon=True).start()
        url = f"http://127.0.0.1:{srv.server_address[1]}/"
        log(f"Нокс Студия {VERSION}: {root} — {url} (этап конвейера {getattr(n, 'NOX_STAGE', '?')}; сборка {S.asm_src})")
        threading.Thread(target=S.startup, daemon=True).start()
        if not a.no_open:
            open_window(url, profile)
        srv.serve_forever()
    except Exception as e:
        log("студия не запустилась:\n" + traceback.format_exc())
        if os.name == "nt":
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(0, f"Нокс Студия не запустилась:\n\n{e}\n\nЖурнал: {LOG}", "Нокс Студия", 0x10)
            except Exception:
                pass
        sys.exit(1)


if __name__ == "__main__":
    main()
