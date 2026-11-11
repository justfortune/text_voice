#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# NOX_KIT 2026-11-11i
"""
zvuk_k1a.py — СВЕДЕНИЕ ДОРОЖЕК: голос + эффекты + фон местности + музыка, синхронно по таймингам частей.

Голос НЕ меняется: берутся готовые части книги (parts/part_NN.mp3) и их карты времени (parts/part_NN.times.tsv —
где в части начинается и кончается каждая реплика). Звук ставится по партитуре cues_book1.json (539 отметок,
якоря — дословные фразы книги) — те же места, что теги ⟦music/amb/sfx⟧ в Разметке_книги.txt.

Дорожки и громкость (каждая — своим параметром; громкость — ОТНОСИТЕЛЬНО ГОЛОСА: каждый звук сначала выравнивается
по громкости с голосом части, потом умножается на число; 0.33 = втрое тише голоса, 0.10 = вдесятеро):
  --voice 1.0     голос
  --sfx   0.33    точечные эффекты (заклинания, дверь, хруст яблока…); «книги, страницы, огоньки» — ещё вдвое тише
  --amb   0.12    фон местности (море, птицы, капель, толпа) — входит в дорожку «эффекты»
  --music 0.10    музыка (играет фоном по партитуре, из главы в главу)
  --duck  0.0     0…0.9: насколько приглушать музыку под голосом (0 — ровно; 0.5 — в паузах громче вдвое)
Варианты (--variants, через запятую):  effekty = голос+эффекты+фон,  muzyka = голос+музыка,  vse = всё вместе,
  golos = голос отдельно (обычные части и так голос отдельно; этот вариант нужен, только если --voice не 1).
Результат: D:\\kniga1a\\parts_zvuk\\<вариант>\\part_NN.mp3 (главы и названия — из исходных частей) и копия на Яндекс
Диск в папку ПОСЛЕДНЕГО запуска книги: <папка издания>\\<дата_время>\\<вариант>\\. Таймлайн каждой части (секунды по
каждой дорожке, для любого звукового редактора) — parts_zvuk\\timeline\\part_NN_tl.csv.
Часть пересводится, только если изменилась она сама, её звук или громкости (отпечаток в .sig рядом).

Звуковой комплект (из другого чата) живёт в D:\\nox_audiobook\\zvuk, каждый — в своей папке. Берётся лучший: новее
поколением (с готовыми петлями фона и справочником имён), затем по дате архива. Если в Загрузках лежит архив комплекта
(Nox_*.zip с партитурой cues_book1.json; архив с картинками не подходит) новее всех распакованных — он распаковывается
сам, разово (~310 МБ), через временную папку: оборванная распаковка не оставит половину комплекта. Прежний комплект не
удаляется. Музыка и фоны ищутся по имени без расширения: mp3 или WAV, готовая петля flac или своя сборка wav.

Запуск (видеокарта не нужна; можно, пока идёт озвучка — сводятся уже готовые части):
  D:\\kniga1a\\envfast\\Scripts\\python.exe "%USERPROFILE%\\Downloads\\zvuk_k1a.py"
  … --parts 1-3   только эти части;   … --music 0.07 --sfx 0.25   свои громкости;   … --no-disk   без Диска
go запускает его сам, скрыто, рядом с озвучкой (--watch): части сводятся по мере сборки, ход — zvuk.log в папке книги.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

VARIANTS = {"effekty": ("голос + эффекты + фон", ("sfx", "amb")),
            "muzyka": ("голос + музыка", ("music",)),
            "vse": ("голос + эффекты + фон + музыка", ("sfx", "amb", "music")),
            "golos": ("голос отдельно", ())}
SR = 24000
CHUNK = SR * 20
RUN_DIR = re.compile(r"^\d{4}-\d\d-\d\d_\d\d-\d\d$")
_ORD = sorted({"одиннадц": 11, "двенадц": 12, "перв": 1, "втор": 2, "трет": 3, "четв": 4, "пят": 5, "шест": 6,
               "седьм": 7, "восьм": 8, "девят": 9, "десят": 10}.items(), key=lambda kv: -len(kv[0]))


# ------------------------------------------------------------------------------------------------ текст и якоря
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


def loose(s: str) -> str:
    """Только буквы и цифры (как в разметке): якорь может переходить из авторской речи в реплику."""
    out = []
    for ch in s:
        c = ch.lower().replace("ё", "е")
        if not c.isalnum():
            if not out or out[-1] == " ":
                continue
            c = " "
        out.append(c)
    return "".join(out).strip()


def secs(note, word, dflt):
    m = re.search(word + r"\s*(\d+(?:[.,]\d+)?)\s*c", note or "", re.I)
    return float(m.group(1).replace(",", ".")) if m else dflt


# ------------------------------------------------------------------------------------------------ звук
def ffmpeg_bin(n=None):
    f = getattr(n, "find_ffmpeg", None) if n else None
    p = f() if f else None
    return p or shutil.which("ffmpeg") or "ffmpeg"


def decode(ff, path, sr=SR):
    """Любой звук (mp3, WAV IMA ADPCM, PCM) -> моно float32 sr."""
    import numpy as np
    r = subprocess.run([ff, "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "1", "-ar", str(sr), "-"],
                       capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"не читается {path}: {r.stderr.decode('utf-8', 'replace')[:200]}")
    return np.frombuffer(r.stdout, dtype="<f4").astype("float32")


def active_rms(x, sr=SR):
    """Громкость звучащей части (кадры 50 мс громче 10 % от пиковых) — чтобы паузы не занижали громкость."""
    import numpy as np
    w = int(sr * 0.05)
    k = len(x) // w
    if k < 1:
        return float(np.sqrt(np.mean(np.square(x)))) if len(x) else 0.0
    rms = np.sqrt(np.mean(np.square(x[:k * w].reshape(k, w)), axis=1))
    act = rms[rms > 0.1 * float(np.percentile(rms, 95))]
    return float(np.sqrt(np.mean(np.square(act)))) if len(act) else 0.0


class Assets:
    def __init__(self, ff):
        self.ff, self.cache, self.level = ff, {}, {}

    def get(self, path):
        if path not in self.cache:
            if len(self.cache) > 60:                     # музыка длинная — держим немного
                self.cache.pop(next(iter(self.cache)))
            self.cache[path] = decode(self.ff, path)
        return self.cache[path]

    def rms(self, path):
        if path not in self.level:
            self.level[path] = active_rms(self.get(path))
        return self.level[path]


# ------------------------------------------------------------------------------------------------ комплект
KIT_MARK = ".nox_kit.json"
NAMES_JSON = "файлы_по_именам.json"
MUSIC_EXT = (".mp3", ".wav", ".flac", ".ogg", ".m4a")          # в комплекте музыка в mp3, в папке игры — WAV
BED_EXT = (".wav", ".flac", ".mp3", ".ogg")                    # своя сборка фона (.wav) главнее готовой петли (.flac)
SFX_EXT = (".wav", ".flac", ".mp3", ".ogg")


def kit_base() -> Path:
    return Path(os.environ.get("NOX_ZVUK") or r"D:\nox_audiobook\zvuk")


def kit_downloads() -> Path:
    return Path(os.environ.get("NOX_DL") or (Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Downloads"))


def kit_ok(d: Path) -> bool:
    return (d / "cues_book1.json").is_file() and (d / "music").is_dir()


def _crc(path: Path) -> int:
    import zlib
    c = 0
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            c = zlib.crc32(b, c)
    return c & 0xFFFFFFFF


def kit_info(d: Path) -> dict:
    """Распакованный комплект: поколение (2 — с готовыми петлями фона и справочником имён), из какого архива, когда."""
    try:
        m = json.loads((d / KIT_MARK).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        m = {}
    try:
        tm = (d / "cues_book1.json").stat().st_mtime
    except OSError:
        tm = 0.0
    return dict(dir=d, name=d.name, gen=2 if (d / NAMES_JSON).is_file() else 1, zip=m.get("zip"), sig=m.get("sig"),
                at=float(m.get("zip_mtime") or tm), marked=bool(m))


def kits_unpacked(base=None) -> list:
    """Все распакованные комплекты общей папки, лучший — первым: новее поколением, затем архивом, затем по имени."""
    base = Path(base) if base else kit_base()
    out = []
    if base.is_dir():                                    # комплект — сама папка, её подпапка или подпапка подпапки
        cands = [base / "cues_book1.json"] + sorted(base.glob("*/cues_book1.json")) + sorted(base.glob("*/*/cues_book1.json"))
        for cj in cands:
            if kit_ok(cj.parent) and not any(p.name.startswith("_raspakovka") for p in cj.parents):
                out.append(kit_info(cj.parent))
    out.sort(key=lambda k: (-k["gen"], -k["at"], k["name"]))
    return out


def zip_name(i) -> str:
    name = i.filename
    if not (i.flag_bits & 0x800):
        try:
            name = name.encode("cp437").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return name.replace("\\", "/")


def zip_probe(z: Path):
    """Архив из Загрузок — звуковой комплект? -> сведения о нём (без распаковки) или None. Архив с картинками и любой
    другой Nox_*.zip без партитуры и музыки сюда не подходит."""
    try:
        with zipfile.ZipFile(z) as zf:
            infos = zf.infolist()
            names = [zip_name(i) for i in infos]
            cj = [k for k, nm in enumerate(names) if nm.rsplit("/", 1)[-1] == "cues_book1.json"]
            if not cj:
                return None
            k = min(cj, key=lambda q: (names[q].count("/"), names[q]))
            top = names[k].rsplit("/", 1)[0] if "/" in names[k] else ""
            pre = top + "/" if top else ""
            if not any(nm.startswith(pre + "music/") and not nm.endswith("/") for nm in names):
                return None
            st = z.stat()
            return dict(zip=z, name=z.name, top=top, gen=2 if (pre + NAMES_JSON) in names else 1,
                        sig=f"{st.st_size}:{infos[k].CRC:08x}:{len(infos)}", crc=infos[k].CRC, at=st.st_mtime,
                        size=sum(i.file_size for i in infos), files=len(infos))
    except (OSError, zipfile.BadZipFile, ValueError, RuntimeError):
        return None


def kit_zips(dl=None) -> list:
    """Архивы звукового комплекта в Загрузках, лучший — первым («(1)»-копии — те же архивы, годится любая)."""
    dl = Path(dl) if dl else kit_downloads()
    out = []
    if dl.is_dir():
        for z in dl.glob("Nox_*.zip"):
            p = zip_probe(z)
            if p:
                out.append(p)
    out.sort(key=lambda p: (-p["gen"], -p["at"], p["name"]))
    return out


def kit_pick(base=None, dl=None) -> dict:
    """Какой комплект брать — ничего не распаковывая. -> dict(kit=папка лучшего распакованного или None, zip=архив из
    Загрузок, который лучше всего распакованного и ждёт распаковки, или None, others=остальные распакованные)."""
    kits = kits_unpacked(base)
    best = kits[0] if kits else None
    wait = None
    for z in kit_zips(dl)[:1]:
        same = next((k for k in kits if k["sig"] and k["sig"] == z["sig"]), None)
        if same is None:                                 # распакован прежней версией (без метки): узнаём по партитуре
            for k in kits:
                if not k["marked"] and k["gen"] == z["gen"]:
                    try:
                        if _crc(k["dir"] / "cues_book1.json") == z["crc"]:
                            same = k
                            break
                    except OSError:
                        pass
        if same is None and (best is None or (z["gen"], z["at"]) > (best["gen"], best["at"])):
            wait = z
    return dict(kit=best["dir"] if best else None, zip=wait, others=[k["dir"] for k in kits[1:]])


def _pid_alive(pid: int) -> bool:
    """Жив ли процесс (не знаем — считаем, что жив)."""
    try:
        if os.name == "nt":
            import ctypes
            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(0x1000, False, int(pid))         # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return ctypes.get_last_error() == 5 or k32.GetLastError() == 5     # «отказано в доступе» — процесс есть
            code = ctypes.c_ulong()
            ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
            k32.CloseHandle(h)
            return bool(ok) and code.value == 259                # STILL_ACTIVE
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, ValueError):
        return False
    except Exception:
        return True


def kit_unpack(z: dict, base=None, progress=None) -> Path:
    """Архив комплекта -> своя папка в общей папке звуков. Распаковка идёт во временную папку и переименовывается в
    конце: оборванная распаковка не оставит половину комплекта под видом целого. Прежние комплекты не трогаются
    (на их файлы могут ссылаться звуки, уже стоящие на дорожках студии). Один архив распаковывается один раз, даже
    если за него взялись сразу студия и сведение go: второй ждёт первого и берёт готовую папку."""
    base = Path(base) if base else kit_base()
    base.mkdir(parents=True, exist_ok=True)
    lock, t0 = base / "_raspakovka.lock", time.time()
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()} {time.time():.0f}".encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
                owner = int(lock.read_text(encoding="ascii", errors="ignore").split()[0])
            except (OSError, ValueError, IndexError):
                age, owner = 0.0, None
                if not lock.exists():
                    continue
            # брошенный замок: распаковку оборвали (её процесса уже нет) или замок давно не обновлялся —
            # живая распаковка трогает его каждые 200 файлов
            if (owner is not None and owner != os.getpid() and not _pid_alive(owner)) or age > 120:
                try:
                    lock.unlink()
                except OSError:
                    pass
                continue
            if time.time() - t0 > 900:
                raise RuntimeError("другая распаковка звукового комплекта не закончилась за 15 минут")
            time.sleep(1.0)
    tmp = base / f"_raspakovka_{os.getpid()}"
    try:
        same = next((k for k in kits_unpacked(base) if k["sig"] and k["sig"] == z["sig"]), None)
        if same is not None:                             # пока ждали, этот архив распаковал другой процесс
            return same["dir"]
        free = shutil.disk_usage(str(base)).free
        need = z["size"] + 200 * 1024 * 1024
        if free < need:
            raise RuntimeError(f"на диске мало места: нужно {need / 1e6:.0f} МБ, свободно {free / 1e6:.0f} МБ")
        for old in base.glob("_raspakovka_*"):           # остатки оборванных распаковок
            if old.is_dir():
                shutil.rmtree(old, ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir()
        root, done, count = tmp.resolve(), 0, 0
        with zipfile.ZipFile(z["zip"]) as zf:
            for i in zf.infolist():
                name = zip_name(i)
                parts = [p for p in name.split("/") if p]
                if name.endswith("/") or not parts:
                    continue
                if name.startswith("/") or any(p in ("..", ".") or ":" in p for p in parts):
                    continue                             # путь наружу из папки комплекта — не распаковывается
                out = tmp.joinpath(*parts)
                if root not in out.resolve().parents:
                    continue
                out.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(i) as src, open(out, "wb") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
                done += i.file_size
                count += 1
                if count % 200 == 0:
                    os.utime(lock, None)                 # замок жив: его не примут за брошенный
                if progress:
                    progress(done, z["size"])
        src = tmp.joinpath(*z["top"].split("/")) if z["top"] else tmp
        if not kit_ok(src):
            raise RuntimeError("в архиве нет партитуры cues_book1.json или папки music")
        (src / KIT_MARK).write_text(json.dumps(dict(zip=z["name"], sig=z["sig"], zip_mtime=z["at"], files=z["files"],
                                                     unpacked=time.strftime("%Y-%m-%d %H:%M:%S")), ensure_ascii=False),
                                    encoding="utf-8")
        name = (z["top"].rsplit("/", 1)[-1] if z["top"] else re.sub(r"\s*\(\d+\)$", "", Path(z["name"]).stem)) or "komplekt"
        dst, k = base / name, 1
        while dst.exists():
            k += 1
            dst = base / f"{name}_{k}"
        for attempt in range(6):                         # антивирус или индексатор может секунду держать свежие файлы
            try:
                os.replace(src, dst)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(1.5)
        _KIX.pop(str(dst), None)
        return dst
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            lock.unlink()
        except OSError:
            pass


def find_kit(arg=None, unpack=True, say=None):
    """Папка звукового комплекта: указанная; иначе лучшая из распакованных; а если в Загрузках лежит архив комплекта
    лучше всех распакованных — он распаковывается (разово) и берётся."""
    say = say or (lambda s: print(s, flush=True))
    if arg and kit_ok(Path(arg)):
        return Path(arg)
    pk = kit_pick()
    z = pk["zip"]
    if z is not None and unpack:
        say(f"  распаковываю звуковой комплект {z['name']} -> {kit_base()} (разово, ~{z['size'] / 1e6:.0f} МБ)…")
        try:
            return kit_unpack(z)
        except Exception as e:
            say(f"  {z['name']}: не распаковался ({type(e).__name__}: {e})"
                + (f" — беру прежний комплект {pk['kit'].name}" if pk["kit"] is not None else ""))
    return pk["kit"]


_KIX = {}


def kit_index(kit: Path, fresh=False) -> dict:
    """Что лежит в комплекте: музыка и фоны — по имени без расширения и регистра (mp3 или WAV, flac или wav — всё равно),
    эффекты — по логическому имени игры (sfx\\<раздел>\\Имя__файл.wav), сырые файлы игры — по имени (sfx_all)."""
    key = str(kit)
    ix = None if fresh else _KIX.get(key)
    if ix is not None:
        return ix
    ix = dict(music={}, beds={}, sfx={}, raw={}, names={})
    for sub, order in (("music", MUSIC_EXT), ("beds", BED_EXT), ("sfx_all", SFX_EXT)):
        d = kit / sub
        if d.is_dir():
            tgt = ix["raw" if sub == "sfx_all" else sub]
            for f in sorted(d.iterdir()):
                e = f.suffix.lower()
                if e in order and f.is_file():
                    k = f.stem.lower()
                    if k not in tgt or order.index(e) < order.index(tgt[k].suffix.lower()):
                        tgt[k] = f
    d = kit / "sfx"
    if d.is_dir():
        for f in d.rglob("*__*"):
            if f.suffix.lower() in SFX_EXT and f.is_file():
                ix["sfx"].setdefault(f.name.split("__", 1)[0], []).append(f)
        for v in ix["sfx"].values():
            v.sort(key=str)
    try:
        nj = json.loads((kit / NAMES_JSON).read_text(encoding="utf-8"))
        for nm, v in (nj.get("sfx") or {}).items():
            if isinstance(v, dict):
                ix["names"][nm] = [str(x) for x in (v.get("варианты") or [])]
    except (OSError, ValueError, AttributeError):
        pass
    try:
        with open(kit / "sfx_dictionary.csv", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f, delimiter=";"):
                ix["names"].setdefault(r["логическое_имя"], [f"sfx_all/{x}.wav" for x in (r.get("файлы") or "").split()])
    except (OSError, KeyError):
        pass
    _KIX[key] = ix
    return ix


def kit_file(kit: Path, kind: str, name: str):
    """Имя из партитуры -> файл комплекта. kind: music | bed -> Path или None; sfx -> список вариантов (может быть пуст)."""
    ix = kit_index(kit)
    if kind == "music":
        return ix["music"].get(str(name).lower())
    if kind == "bed":
        return ix["beds"].get(str(name).lower())
    hits = list(ix["sfx"].get(name) or [])
    if not hits:
        for rel in ix["names"].get(name) or []:
            p = kit / rel
            if not p.is_file():
                p = ix["raw"].get(Path(rel).stem.lower())
            if p is not None and p.is_file():
                hits.append(p)
    return hits


def ensure_beds(kit: Path, used):
    """Фоны местности: в комплекте с готовыми петлями (beds\\*.flac) делать нечего; в прежнем они собираются разово
    его же tools\\make_ambient_bed.py. Собирается, только если чего-то из нужного нет."""
    beds = kit / "beds"
    missing = [b for b in used if kit_file(kit, "bed", b) is None]
    tool = kit / "tools" / "make_ambient_bed.py"
    if missing and tool.exists():
        print(f"  собираю фоны местности ({len(missing)} шт., разово)…", flush=True)
        subprocess.run([sys.executable, str(tool), "--all-used", "--seconds", "120"], cwd=str(kit))
        kit_index(kit, fresh=True)
        missing = [b for b in used if kit_file(kit, "bed", b) is None]
    if missing:
        print(f"  !! в комплекте нет фонов местности: {', '.join(missing[:8])}{'…' if len(missing) > 8 else ''}", flush=True)
    return beds


# ------------------------------------------------------------------------------------------------ части и время
def read_parts(n):
    """-> [(номер, mp3, {id: (начало, конец)})] по порядку; секунды — в части после ускорения."""
    out = []
    for tf in sorted(n.PARTS.glob("part_*.times.tsv")):
        m = re.match(r"part_(\d+)\.times", tf.name)
        if not m:
            continue
        mp3 = [p for p in tf.parent.glob(f"part_{m.group(1)}.*") if p.suffix.lower() in (".mp3", ".m4b")]
        if not mp3:
            continue
        tm = {}
        for line in tf.read_text(encoding="utf-8").splitlines():
            p = line.split("\t")
            if len(p) < 3:
                continue
            try:
                if len(p) >= 8:
                    a, b = float(p[6]), float(p[7])
                else:
                    f = lambda v: int(v.split(":")[0]) * 60 + float(v.split(":")[1])
                    a, b = f(p[0]), f(p[1])
            except (ValueError, IndexError):
                continue
            tm[p[2]] = (a, b)
        out.append((m.group(1), mp3[0], tm))
    return out


def probe_dur(ff, path):
    fp = str(Path(ff).with_name("ffprobe" + Path(ff).suffix)) if Path(ff).name.startswith("ffmpeg") else "ffprobe"
    try:
        r = subprocess.run([fp, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                           capture_output=True, text=True)
        return float(r.stdout.strip())
    except Exception:
        return None


# ------------------------------------------------------------------------------------------------ партитура -> дорожки
def build_timeline(rows, starts, cues, kit, music_dur, a, nofile=None):
    """Отметки партитуры -> предметы дорожек в ОБЩЕМ времени книги: dict(kind, file, start, end, offset, loop, gain,
    fade_in, fade_out, note, cid). starts: индекс реплики -> (начало, конец) в общем времени (только собранные).
    cid — от какой отметки партитуры предмет (C041:0 — её первый звук; C020r — музыка, вернувшаяся после темы части).
    nofile (список) — сюда пишутся имена, для которых в комплекте нет файла: такой предмет на дорожку не ставится."""
    nofile = nofile if nofile is not None else []

    def lost(what, name):
        if (what, name) not in nofile:
            nofile.append((what, name))
            print(f"  !! в комплекте нет файла: {what} {name}", flush=True)
    amb_vol = {}
    try:
        with open(kit / "ambient_loops.csv", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f, delimiter=";"):
                amb_vol[r["петля"]] = float(r.get("громкость_в_игре_0_50") or 20)
    except (OSError, ValueError):
        pass
    by_ch, heads, order = {}, {}, []
    for i, r in enumerate(rows):
        lab = ch_label(r["ch_title"])
        if lab:
            if lab not in by_ch:
                order.append(lab)
            by_ch.setdefault(lab, []).append(i)
        if r["kind"] == "head" and part_no(r["text"]):
            heads[f"ЧАСТЬ {part_no(r['text'])}"] = i
    texts = {}

    def ch_text(lab):
        if lab not in texts:
            parts, offs, pos = [], [], 0
            for i in by_ch[lab]:
                t = loose(rows[i]["text"])
                offs.append((pos, i, len(t)))
                parts.append(t)
                pos += len(t) + 1
            texts[lab] = (" ".join(parts), offs)
        return texts[lab]

    def at(offs, pos):
        """Время символа: внутри реплики — пропорционально числу букв (между её началом и концом)."""
        for o, i, ln in reversed(offs):
            if pos >= o:
                if i not in starts:
                    return None
                t0, t1 = starts[i]
                return t0 + (t1 - t0) * min(1.0, max(0.0, (pos - o) / max(1, ln)))
        return None

    events, missed = [], []
    cursor = {}
    for c in cues:
        ch = c.get("chapter", "")
        if ch.startswith("ЧАСТЬ"):
            i = heads.get(ch)
            if i is not None and i in starts:
                events.append((starts[i][0], starts[i][0], c))
            continue
        if ch not in by_ch:
            missed.append(c)
            continue
        text, offs = ch_text(ch)
        key = loose(c.get("anchor", ""))
        p = text.find(key[:60], cursor.get(ch, 0))
        if p < 0:
            p = text.find(key[:30], cursor.get(ch, 0))
        back = p < 0
        if back:                                         # якорь раньше курсора: порядок сбит правкой текста — ставим
            p = text.find(key[:60])
            if p < 0:
                p = text.find(key[:30])
        if p < 0 or not key:
            missed.append(c)
            continue
        if not back:
            cursor[ch] = p
        pe = p + len(key)
        t0, t_end = at(offs, p), at(offs, pe)
        if t0 is None or t_end is None:
            continue                                     # эта часть ещё не собрана
        # конец фразы, в которой кончается якорь: по исходному тексту реплики
        k = next(i for o, i, ln in reversed(offs) if pe - 1 >= o)
        raw = rows[k]["text"]
        full = loose(raw)
        o_k = next(o for o, i, ln in offs if i == k)
        rel = (pe - o_k) / max(1, len(full))
        m = re.search(r"[.!?…](?=\s|$|[»\")])", raw[int(rel * len(raw)):])
        rel2 = (int(rel * len(raw)) + m.end()) / max(1, len(raw)) if m else 1.0
        tk0, tk1 = starts.get(k, (t_end, t_end))
        t_sent = tk0 + (tk1 - tk0) * min(1.0, rel2)
        note = (c.get("note") or "") + " " + (c.get("when") or "")
        events.append((t0, t_end if "слог" in note else t_sent, c))
    events.sort(key=lambda e: e[0])
    # тема части звучит целиком: музыкальные отметки, попавшие на неё, сдвигаются на её конец
    for i, (t0, t1, c) in enumerate(list(events)):
        if c["layer"] == "M" and c["action"] == "jingle":
            jend = t0 + music_dur.get(c["target"], 34)
            for j in range(i + 1, len(events)):
                tj, t1j, cj = events[j]
                if cj["layer"] == "M" and tj < jend:
                    events[j] = (jend, max(t1j, jend), cj)
    events.sort(key=lambda e: e[0])
    # после Эпилога (приложения: действующие лица, словарь) — тишина
    stop_at = None
    seen_epi = False
    for i, r in enumerate(rows):
        lab = ch_label(r["ch_title"])
        seen_epi = seen_epi or lab == "Эпилог"
        if seen_epi and not lab and i in starts:
            stop_at = starts[i][0]
            break

    items = []
    cur, beds, stack = None, None, []

    def mpath(t):                                        # в комплекте музыка в mp3, в папке игры — WAV: что есть
        p = kit_file(kit, "music", t)
        if p is None:
            lost("музыка", t)
        return str(p) if p is not None else None
    posat = lambda cu, t: (cu["offset"] + t - cu["start"]) % max(1.0, music_dur.get(cu["track"], 60))

    def end_music(t, fo):
        nonlocal cur
        if cur and t - cur["start"] > 0.05:
            mf = mpath(cur["track"])
            if mf:
                items.append(dict(kind="music", file=mf, start=cur["start"], end=t + fo,
                                  offset=cur["offset"], loop=1, gain=a.music, fade_in=cur["fi"], fade_out=fo,
                                  note=cur["track"], cid=cur["cid"]))
        cur = None

    def start_music(track, t, offset=0.0, fi=1.0, cid=""):
        nonlocal cur
        cur = dict(track=track, start=t, offset=offset, fi=fi, cid=cid)

    def end_beds(t):
        nonlocal beds
        if beds and t > beds["start"]:
            for k, nm in enumerate(beds["names"]):
                g = a.amb * min(2.5, max(0.25, amb_vol.get(nm, 20) / 20.0))
                bf = kit_file(kit, "bed", nm)            # готовая петля (.flac) или своя сборка (.wav)
                if bf is None:
                    lost("фон", nm)
                    continue
                items.append(dict(kind="amb", file=str(bf), start=beds["start"], end=t + 2,
                                  offset=0, loop=1, gain=g, fade_in=2.0, fade_out=2.0, note=nm, cid=f"{beds['cid']}:{k}"))
        beds = None

    def find_sfx(name):
        return [str(p) for p in kit_file(kit, "sfx", name)]

    for t0, t1, c in events:
        if stop_at is not None and t0 >= stop_at:
            break
        L, act, tgt, note = c["layer"], c["action"], c.get("target", ""), (c.get("note") or "")
        when = c.get("when") or ""
        if L == "M":
            fo, fi = secs(note, "Затухание", 1.0), secs(note, "Fade-in", 1.0)
            if act in ("start", "change"):
                if cur and cur["track"] == tgt:
                    continue
                end_music(t0, 1.0)
                start_music(tgt, t0, 0.0, fi, c["id"])
            elif act == "stop":
                end_music(t0, fo)
                if "стек" in note:
                    stack.clear()
            elif act == "push":
                stack.append((cur["track"], posat(cur, t0)) if cur else (None, 0.0))
                end_music(t0, 1.0)
                start_music(tgt, t0, 0.0, fi, c["id"])
            elif act == "pop":
                prev = stack.pop() if stack else (None, 0.0)
                end_music(t0, 1.0)
                if prev[0]:
                    start_music(prev[0], t0, prev[1], fi, c["id"])
            elif act == "jingle":
                saved = (cur["track"], posat(cur, t0)) if cur else None
                end_music(t0, 1.0)
                jend = t0 + music_dur.get(tgt, 34)
                jf = mpath(tgt)
                if jf:
                    items.append(dict(kind="music", file=jf, start=t0, end=jend, offset=0, loop=0, gain=a.music,
                                      fade_in=0.5, fade_out=1.0, note=tgt + " (тема части)", cid=c["id"]))
                if saved and saved[0]:
                    start_music(saved[0], jend, saved[1], 1.0, c["id"] + "r")
        elif L == "A":
            names = [x for x in tgt.split("+") if x] if act == "bed" else []
            if beds and names == beds["names"]:
                continue
            end_beds(t0)
            if names:
                beds = dict(names=names, start=t0, cid=c["id"])
        elif L == "S":
            ts = max(0.0, t0 - 0.5) if "ДО" in (note + when) else t1 + 0.05
            for k, nm in enumerate([x for x in tgt.split("+") if x]):
                hits = find_sfx(nm)
                if not hits:
                    lost("эффект", nm)
                    continue
                items.append(dict(kind="sfx", file=random.Random(c["id"]).choice(hits), start=ts + 0.6 * k, end=None,
                                  offset=0, loop=0, gain=a.sfx * (0.5 if "тихо" in note else 1.0), fade_in=0,
                                  fade_out=0, note=f"{c['id']} {nm}", snap="ДО" not in (note + when), cid=f"{c['id']}:{k}"))
    # книга собрана не вся — у последней собранной части звук не гасим (он продолжится в следующей части)
    complete = len(starts) >= len(rows)
    t_end = stop_at if stop_at is not None else \
        ((max(s[1] for s in starts.values()) + 3 if starts else 0) if complete else 1e9)
    end_music(t_end, 3.0 if stop_at is not None else 0.0)
    end_beds(t_end - 2)
    items.sort(key=lambda it: it["start"])
    return items, missed


# ------------------------------------------------------------------------------------------------ сведение части
def snap_to_pause(voice, t, sr=SR, win=0.8, min_ms=120):
    """Эффект «после фразы»: ближайшая пауза в голосе (тишина от 120 мс) в пределах ±0,8 с — эффект в её начало."""
    import numpy as np
    w = int(sr * 0.01)
    i0, i1 = max(0, int((t - win) * sr)), min(len(voice), int((t + win) * sr))
    seg = voice[i0:i1]
    k = len(seg) // w
    if k < 5:
        return t
    amax = np.abs(seg[:k * w]).reshape(k, w).max(axis=1)
    ref = float(np.percentile(np.abs(voice[::50]), 99)) or 1.0
    sil = amax < max(2e-3, 0.02 * ref)
    best, i = None, 0
    while i < k:
        if sil[i]:
            j = i
            while j < k and sil[j]:
                j += 1
            if (j - i) * 10 >= min_ms:
                ts = (i0 + i * w) / sr + 0.05
                if best is None or abs(ts - t) < abs(best - t):
                    best = ts
            i = j
        else:
            i += 1
    return best if best is not None else t


def render(bus, item, assets, off, ref_rms, sr=SR):
    """Предмет дорожки -> в шину части (off — начало части в общем времени). Громкость = gain × голос/звук."""
    import numpy as np
    a = assets.get(item["file"])
    if not len(a):
        return
    lvl = assets.rms(item["file"])
    g = item["gain"] * (min(ref_rms / lvl, 10.0) if lvl > 1e-6 else 0.0)    # тихий файл не раздувать сверх +20 дБ
    s = item["start"]
    e = item["end"] if item["end"] is not None else s + len(a) / sr
    i0, i1 = max(0, int((s - off) * sr)), min(len(bus), int((e - off) * sr))
    fi, fo = float(item["fade_in"] or 0), float(item["fade_out"] or 0)
    for c0 in range(i0, i1, CHUNK):
        c1 = min(i1, c0 + CHUNK)
        tg = np.arange(c0, c1) / sr + off                 # общее время
        pos = ((tg - s + float(item["offset"] or 0)) * sr).astype(np.int64)
        if item["loop"]:
            pos %= len(a)
            ok = slice(None)
            y = a[pos]
        else:
            ok = (pos >= 0) & (pos < len(a))
            y = np.zeros(c1 - c0, dtype="float32")
            y[ok] = a[pos[ok]]
        env = np.ones(c1 - c0, dtype="float32")
        if fi > 0:
            env *= np.clip((tg - s) / fi, 0, 1)
        if fo > 0 and item["end"] is not None:
            env *= np.clip((e - tg) / fo, 0, 1)
        bus[c0:c1] += (y * env * g).astype("float32")


def duck_env(voice, amount, sr=SR):
    """Огибающая приглушения музыки под голосом: 1 в паузах, (1 − amount) под речью, плавно (0,2 с / 0,9 с)."""
    import numpy as np
    if amount <= 0:
        return None
    w = int(sr * 0.02)
    k = len(voice) // w + 1
    v = np.pad(voice, (0, k * w - len(voice)))
    rms = np.sqrt(np.mean(np.square(v.reshape(k, w)), axis=1))
    act = (rms > 0.1 * float(np.percentile(rms, 95))).astype("float32")
    sm = np.zeros(k, dtype="float32")
    up, down = 1 - math.exp(-0.02 / 0.2), 1 - math.exp(-0.02 / 0.9)
    s = 0.0
    for i in range(k):
        s += (act[i] - s) * (up if act[i] > s else down)
        sm[i] = s
    return np.repeat(1.0 - amount * sm, w)[:len(voice)]


def soft_limit(x, knee=0.89):
    import numpy as np
    m = np.abs(x) > knee
    if m.any():
        y = x.copy()
        y[m] = np.sign(x[m]) * (knee + (1 - knee) * np.tanh((np.abs(x[m]) - knee) / (1 - knee)))
        return y
    return x


def encode(ff, x, src, out, title, bitrate):
    import soundfile as sf
    out.parent.mkdir(parents=True, exist_ok=True)
    wav = out.with_suffix(".mix.wav")
    sf.write(str(wav), x, SR, subtype="PCM_16")
    tmp = out.with_name(out.stem + ".tmp" + out.suffix)
    cmd = [ff, "-y", "-hide_banner", "-loglevel", "error", "-i", str(wav), "-i", str(src),
           "-map", "0:a", "-map_metadata", "1", "-map_chapters", "1", "-metadata", f"title={title}",
           "-ac", "1", "-b:a", bitrate]
    cmd += ["-c:a", "libmp3lame", "-id3v2_version", "3", "-f", "mp3"] if out.suffix.lower() == ".mp3" else \
        ["-c:a", "aac", "-movflags", "+faststart", "-f", "ipod"]
    r = subprocess.run(cmd + [str(tmp)])
    wav.unlink(missing_ok=True)
    if r.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg не собрал {out.name}")
    os.replace(tmp, out)


def disk_run_dir(st, root, arg):
    """Папка последнего запуска книги на Яндекс Диске (<издание>\\<дата_время>); нет — новая с нынешним временем."""
    if arg:
        return Path(arg)
    name = str(st.get("dest") or "")
    if root.name.lower() == "kniga1a" and name in ("", "Posoh_izdanie2"):
        name = "Posoh_izdanie3"
    base = Path(name) if re.match(r"^[A-Za-z]:\\", name) else \
        Path(os.environ.get("USERPROFILE", str(Path.home()))) / "YandexDisk" / (name or root.name)
    runs = sorted(d for d in base.glob("*") if d.is_dir() and RUN_DIR.match(d.name)) if base.is_dir() else []
    return runs[-1] if runs else base / time.strftime("%Y-%m-%d_%H-%M")


def main():
    ap = argparse.ArgumentParser(description="Сведение голоса с эффектами, фоном и музыкой по частям книги")
    ap.add_argument("--root", default=r"D:\kniga1a")
    ap.add_argument("--kit", default=None, help="папка звукового комплекта (по умолчанию D:\\nox_audiobook\\zvuk)")
    ap.add_argument("--voice", type=float, default=None)
    ap.add_argument("--sfx", type=float, default=None)
    ap.add_argument("--amb", type=float, default=None)
    ap.add_argument("--music", type=float, default=None)
    ap.add_argument("--duck", type=float, default=None)
    ap.add_argument("--variants", default=None, help="effekty,muzyka,vse[,golos]")
    ap.add_argument("--parts", default="", help="например 1-3,7")
    ap.add_argument("--dest", default=None, help="папка на Диске (по умолчанию — последнего запуска книги)")
    ap.add_argument("--no-disk", action="store_true")
    ap.add_argument("--dry", action="store_true", help="только таймлайны, без сведения")
    ap.add_argument("--watch", action="store_true",
                    help="работать рядом с озвучкой: каждые 5 минут сводить новые и пересобранные части; выйти, когда "
                         "проверка закончила, книга собрана целиком и 90 минут нет новых частей (так go запускает его "
                         "сам, скрыто; ход — zvuk.log в папке книги)")
    a = ap.parse_args()
    root = Path(a.root).resolve()
    sys.path.insert(0, str(root))
    os.chdir(root)
    import numpy as np
    import nox_audiobook as n
    st = getattr(n, "SETTINGS", None) or {}
    if not st:
        try:
            st = json.loads((n.WORK / "settings.json").read_text(encoding="utf-8"))
        except Exception:
            st = {}
    z = st.get("zvuk") or {}                          # громкости из таблицы настроек (если есть), иначе по умолчанию
    for k, d in (("voice", 1.0), ("sfx", 0.33), ("amb", 0.12), ("music", 0.10), ("duck", 0.0)):
        if getattr(a, k) is None:
            setattr(a, k, float(z.get(k, d)))
    variants = [v.strip() for v in (a.variants or z.get("variants") or "effekty,muzyka,vse").split(",") if v.strip()]
    bad = [v for v in variants if v not in VARIANTS]
    if bad:
        sys.exit(f"неизвестные варианты: {bad}; есть: {', '.join(VARIANTS)}")
    print(f"=== Сведение дорожек: голос {a.voice:g}, эффекты {a.sfx:g}, фон {a.amb:g}, музыка {a.music:g}"
          + (f", приглушение под голосом {a.duck:g}" if a.duck else "") + " (громкость относительно голоса)", flush=True)
    print("    варианты: " + "; ".join(f"{v} = {VARIANTS[v][0]}" for v in variants), flush=True)
    kit = find_kit(a.kit)
    if kit is None:
        sys.exit("звуковой комплект не найден: положите Nox_…zip в Загрузки или распакуйте в D:\\nox_audiobook\\zvuk")
    _ki = kit_info(kit)
    print(f"  звуковой комплект: {kit}" + (" (с готовыми петлями фона)" if _ki["gen"] > 1 else " (прежний: фоны собираются на месте)"),
          flush=True)
    cues = json.loads((kit / "cues_book1.json").read_text(encoding="utf-8"))
    used_beds = sorted({x for c in cues if c["layer"] == "A" and c["action"] == "bed" for x in c["target"].split("+") if x})
    ensure_beds(kit, used_beds)
    music_dur = {}
    try:
        with open(kit / "music_tracks.csv", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f, delimiter=";"):
                music_dur[r["трек"]] = float(r["длит_с"] or 60)
    except (OSError, ValueError):
        pass
    ff = ffmpeg_bin(n)
    assets = Assets(ff)
    if a.watch:
        print(f"  работаю рядом с озвучкой: проверка каждые 5 минут; выход, когда книга собрана целиком и 90 минут нет новых частей", flush=True)
        idle_since = time.time()
        hb_gone = None                                  # с какого момента проверки распознаванием нет
        while True:
            try:
                k = run_once(a, n, np, st, kit, cues, music_dur, ff, assets, variants, root)
            except Exception as e:
                print(f"  !! проход не удался ({type(e).__name__}: {str(e)[:160]}) — попробую через 5 минут", flush=True)
                k = 0
            try:                                        # проверка распознаванием ещё работает — части ещё будут
                _hb = time.time() - (n.WORK / "asr_active.txt").stat().st_mtime < 600
            except OSError:
                _hb = False
            hb_gone = None if _hb else (hb_gone or time.time())
            if k:
                idle_since = time.time()
            elif time.time() - idle_since > 90 * 60 and hb_gone and time.time() - hb_gone > 15 * 60:
                # проверка ушла больше 15 минут назад (последние проверенные части сборка успевает собрать за 3 мин),
                # книга собрана целиком (book.*) — всё сведено; иначе ждём до 6 часов тишины
                _book = any((root / f"book.{e}").exists() for e in ("mp3", "m4b"))
                if _book or time.time() - idle_since > 6 * 3600:
                    print("=== новых частей больше нет — сведение закончено", flush=True)
                    return
            time.sleep(300)
    if run_once(a, n, np, st, kit, cues, music_dur, ff, assets, variants, root) is None:
        sys.exit("собранных частей с картой времени ещё нет (parts\\part_NN.times.tsv) — сведение после сборки частей")


def run_once(a, n, np, st, kit, cues, music_dur, ff, assets, variants, root):
    """Один проход: тайминги всех собранных частей -> дорожки -> сведение изменившихся частей. -> сколько свели
    (None — частей ещё нет)."""
    rows = n.load_rows()
    parts = read_parts(n)
    if not parts:
        return None
    # общее время книги: части подряд
    id2i = {r["id"]: i for i, r in enumerate(rows)}
    starts, offsets, durs, off = {}, {}, {}, 0.0
    for num, mp3, tm in parts:
        d = probe_dur(ff, mp3)
        if d is None:
            d = len(decode(ff, mp3)) / SR
        offsets[num], durs[num] = off, d
        for rid, (s, e) in tm.items():
            if rid in id2i:
                starts[id2i[rid]] = (off + s, off + e)
        off += d
    nofile = []
    items, missed = build_timeline(rows, starts, cues, kit, music_dur, a, nofile)
    out_root = root / "parts_zvuk"
    (out_root / "timeline").mkdir(parents=True, exist_ok=True)
    if missed:
        (out_root / "timeline" / "ne_naydeno.txt").write_text(
            "\n".join(f"{c.get('id')}\t{c.get('chapter')}\t{c.get('anchor', '')[:80]}" for c in missed), encoding="utf-8")
    print(f"  частей с картой времени: {len(parts)} ({off / 60:.0f} мин); предметов на дорожках: {len(items)}; "
          f"якорей не найдено: {len(missed)}" + (f"; НЕТ ФАЙЛОВ в комплекте: {len(nofile)}" if nofile else ""), flush=True)
    want = set()
    for chunk in a.parts.split(","):
        if "-" in chunk:
            x, y = chunk.split("-", 1)
            want |= {f"{k:02d}" for k in range(int(x), int(y) + 1)}
        elif chunk.strip():
            want.add(f"{int(chunk):02d}")
    dest_run = None if a.no_disk else disk_run_dir(st, root, a.dest)
    title = st.get("title") or root.name
    bitrate = "128k" if str(st.get("bitrate", "96k")) in ("96k", "") else str(st.get("bitrate"))
    kits_sig = hashlib.sha1(json.dumps([a.voice, a.sfx, a.amb, a.music, a.duck]).encode()).hexdigest()[:8]
    done = 0
    for num, mp3, tm in parts:
        if want and num not in want:
            continue
        o, d = offsets[num], durs[num]
        mine = [it for it in items if it["start"] < o + d
                and (it["end"] if it["end"] is not None else it["start"] + 15.0) > o]   # эффект — до 15 с хвоста
        tl = out_root / "timeline" / f"part_{num}_tl.csv"
        with tl.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["kind", "file", "start_s", "end_s", "offset_s", "loop", "volume", "fade_in", "fade_out", "note"])
            for it in mine:
                w.writerow([it["kind"], it["file"], f"{it['start'] - o:.2f}", "" if it["end"] is None or it["end"] > 1e8 else f"{it['end'] - o:.2f}",
                            f"{it['offset']:.2f}", it["loop"], f"{it['gain']:.3f}", it["fade_in"], it["fade_out"], it["note"]])
        if a.dry:
            continue
        stv = mp3.stat()
        try:
            stt = (n.PARTS / f"part_{num}.times.tsv").stat().st_mtime_ns
        except OSError:
            stt = 0
        clip = lambda v: round(min(max(v - o, -1.0), d + 1.0), 2)       # отпечаток — только то, что внутри части
        sig = hashlib.sha1((f"{stv.st_size}:{stv.st_mtime_ns}:{stt}|{kits_sig}|" + json.dumps(
            [(it["kind"], it["file"], clip(it["start"]), None if it["end"] is None else clip(it["end"]), it["gain"],
              round(it["offset"], 2)) for it in mine])).encode()).hexdigest()[:16]
        todo = [v for v in variants
                if not ((out_root / v / f"part_{num}{mp3.suffix}").exists()
                        and (out_root / v / f"part_{num}.sig").exists()
                        and (out_root / v / f"part_{num}.sig").read_text(encoding="utf-8") == sig)]
        if todo:
            t0 = time.time()
            voice = decode(ff, mp3)
            ref = active_rms(voice)
            buses = {}
            for kind in ("sfx", "amb", "music"):
                if not any(kind in VARIANTS[v][1] for v in todo):
                    continue
                bus = np.zeros(len(voice), dtype="float32")
                for it in mine:
                    if it["kind"] != kind:
                        continue
                    if kind == "sfx" and it.get("snap"):
                        it = dict(it, start=snap_to_pause(voice, it["start"] - o) + o)
                    try:
                        render(bus, it, assets, o, ref)
                    except Exception as e:
                        print(f"  !! {Path(it['file']).name}: {type(e).__name__}: {str(e)[:120]}", flush=True)
                if kind == "music":
                    env = duck_env(voice, a.duck)
                    if env is not None:
                        bus *= env
                buses[kind] = bus
            for v in todo:
                x = voice * a.voice
                for kind in VARIANTS[v][1]:
                    x = x + buses[kind]
                out = out_root / v / f"part_{num}{mp3.suffix}"
                encode(ff, soft_limit(x), mp3, out, f"{title} — часть {num} ({VARIANTS[v][0]})", bitrate)
                (out_root / v / f"part_{num}.sig").write_text(sig, encoding="utf-8")
            print(f"  часть {num}: сведено {', '.join(todo)} за {time.time() - t0:.0f} с "
                  f"(эффектов {sum(1 for it in mine if it['kind'] == 'sfx')}, фонов {sum(1 for it in mine if it['kind'] == 'amb')}, "
                  f"музыки {sum(1 for it in mine if it['kind'] == 'music')})", flush=True)
            done += 1
        if dest_run is not None:
            for v in variants:
                src = out_root / v / f"part_{num}{mp3.suffix}"
                dst = dest_run / v / src.name
                if src.exists() and (not dst.exists() or dst.stat().st_size != src.stat().st_size
                                     or dst.stat().st_mtime < src.stat().st_mtime):
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    tmp = dst.with_name(dst.name + ".copying")
                    shutil.copy2(src, tmp)
                    os.replace(tmp, dst)
    if done or not a.watch:
        print(f"=== {time.strftime('%H:%M')} сведено частей {done}; файлы: {out_root}\\<вариант>; таймлайны: "
              f"{out_root}\\timeline" + (f"; на Диске: {dest_run}\\<вариант>" if dest_run is not None and not a.dry else ""),
              flush=True)
    return done


if __name__ == "__main__":
    main()
