#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# NOX_KIT 2026-11-11g
"""
nox_stress.py — САМОПРОВЕРКА УДАРЕНИЙ В ЧАСТИ. Ничего в книге не меняет.

Ударения в тексте для озвучки ставит ruaccent (по контексту) + словарь автора + ударения по месту. Здесь каждое слово
собранной части проверяет вторая, независимая модель — Silero Stress. Где модели расходятся — кандидаты в ошибки:
  • «стоит послушать» — омографы (за́мок/замо́к: ударение зависит от смысла), слова, которых нет в словаре ударений
    (имена, редкие слова — там первая модель гадала), и слова, оставшиеся без знака ударения;
  • «прочие» — обычные словарные слова: здесь чаще ошибается вторая модель, их можно не смотреть.
Решения автора не проверяются: слова из словаря accent_dict.csv, ударения по месту и то, что уже разобрано в студии.

Запуск (его делает go, когда часть собрана):  nox_stress.py --root D:\\kniga1a --part 3
Студия проверяет свои тексты сама:            nox_stress.py --root D:\\kniga1a --in вход.json --out выход.json
Разбор — в «Нокс Студии»: у фразы значок «уд.», в её окне — «Оставить» или «Поставить так» одним нажатием.
Видеокарта не нужна. Первый раз ставит пакет silero-stress (~40 МБ). Повторная проверка тех же фраз — из кэша.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

VOW = "аеёиоуыэюяАЕЁИОУЫЭЮЯ"
ACUTE = "\u0301"
WORD = re.compile(r"[А-Яа-яЁё\u0301+]+")
CACHE_VER = 2


def replace_retry(src, dst, tries: int = 8):
    """os.replace с повторами: в Windows файл нельзя подменить в тот миг, когда его читает другой процесс."""
    for k in range(tries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if k == tries - 1:
                raise
            time.sleep(0.05 * (k + 1))


def stress_of(w: str):
    """слово с ударением (U+0301 после гласной или + перед ней) -> (слово без знаков, номер ударной гласной | None)"""
    letters, idx, vi, pending = [], None, -1, False
    for ch in w:
        if ch == "+":
            pending = True
            continue
        if ch == ACUTE:
            if vi >= 0:
                idx = vi
            continue
        letters.append(ch)
        if ch in VOW:
            vi += 1
            if pending:
                idx = vi
            if ch in "ёЁ" and idx is None:
                idx = vi
        pending = False
    return "".join(letters), idx


def mark(word: str, idx):
    if idx is None:
        return word
    out, vi = [], -1
    for ch in word:
        out.append(ch)
        if ch in VOW:
            vi += 1
            if vi == idx:
                out.append(ACUTE)
    return "".join(out)


def load_silero(work: Path):
    try:
        from silero_stress import load_accentor
    except ImportError:
        print("  ставлю silero-stress (разово; уже стоящие пакеты не меняются)…", flush=True)
        freeze = subprocess.run([sys.executable, "-m", "pip", "freeze"], capture_output=True, text=True).stdout
        cons = work / "pip_constraints_stress.txt"
        cons.write_text("\n".join(l for l in freeze.splitlines() if re.match(r"^[A-Za-z0-9_.\-]+==", l)) + "\n",
                        encoding="ascii", errors="ignore")
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "silero-stress", "-c", str(cons)], check=False)
        from silero_stress import load_accentor
    return load_accentor()


def pairs(ow, sw):
    """Пары слов «наш текст — вторая модель». Число слов совпало — по порядку; нет — выравнивание по самим словам."""
    if len(ow) == len(sw):
        return list(zip(range(len(ow)), ow, sw))
    a = [w.lower().replace("ё", "е") for w, _ in ow]
    b = [w.lower().replace("ё", "е") for w, _ in sw]
    out = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            out += [(i1 + d, ow[i1 + d], sw[j1 + d]) for d in range(i2 - i1)]
    return out


class Checker:
    def __init__(self, n, accent: str, cache_path: Path | None):
        self.n, self.accent, self.cache_path = n, accent, cache_path
        self.acc = None
        self.homo = None
        self.cache = {}
        self.dirty = False
        self.ran = 0
        if cache_path and cache_path.exists():
            try:
                c = json.loads(cache_path.read_text(encoding="utf-8"))
                if c.get("ver") == CACHE_VER and c.get("accent") == accent:
                    self.cache = c.get("items") or {}
            except Exception:
                self.cache = {}

    def _models(self):
        if self.acc is None:
            self.acc = load_silero(self.n.WORK)
            self.homo = set()
            try:
                self.homo |= {k.lower() for k in self.acc.homosolver.homodict}
            except Exception:
                pass
            if self.accent != "none":
                try:
                    self.n.prepare_text("проверка", False, self.accent)      # грузит ruaccent, как у озвучки
                    self.homo |= {k.lower() for k in getattr(self.n, "_ACCENTIZER").omographs}
                except Exception:
                    pass

    def check(self, raw: str, tech: str | None) -> list:
        """Расхождения во фразе: [{k, w, ours, sil, kind: diff|nomark, homo, neural, ctx}] — без учёта решений автора."""
        if tech is None:
            tech = self.n.prepare_text(raw, False, self.accent)
        key = hashlib.sha1(tech.encode("utf-8")).hexdigest()[:16]
        if key in self.cache:
            return self.cache[key]
        self._models()
        if raw and self.accent != "none":
            try:
                self.n.prepare_text(raw, False, self.accent)                # отмечает слова вне словаря ударений
            except Exception:
                pass
        neural = getattr(self.n, "_NEURAL_WORDS", None) or {}
        base = tech.replace(ACUTE, "").replace("+", "")
        try:
            sil = self.acc(base)
        except Exception:
            return []
        self.ran += 1
        toks = list(WORD.finditer(tech))
        ow = [stress_of(m.group(0)) for m in toks]
        sw = [stress_of(w) for w in WORD.findall(sil)]
        out = []
        for k, (w1, i1), (w2, i2) in pairs(ow, sw):
            low = w1.lower()
            if low.replace("ё", "е") != w2.lower().replace("ё", "е") or i2 is None:
                continue
            if sum(c in VOW for c in w1) < 2 or "ё" in low or i1 == i2:
                continue
            if i1 is None and self.accent != "acute":
                continue                                 # «только трудные слова»: слово без знака — так и задумано
            pos = toks[k].start()
            out.append(dict(k=k, w=low, ours=toks[k].group(0), sil=mark(w1, i2), kind="nomark" if i1 is None else "diff",
                            homo=low in self.homo, neural=low in neural,
                            ctx=("…" if pos > 40 else "") + base[max(0, pos - 40):pos + len(low) + 40].replace("\n", " ")))
        self.cache[key] = out
        self.dirty = True
        return out

    def save(self):
        if not self.cache_path or not self.dirty:
            return
        if len(self.cache) > 60000:                      # кэш не растёт без конца
            self.cache = dict(list(self.cache.items())[-40000:])
        tmp = self.cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(dict(ver=CACHE_VER, accent=self.accent, items=self.cache), ensure_ascii=False), encoding="utf-8")
        replace_retry(tmp, self.cache_path)
        self.dirty = False


def author_decisions(n) -> dict:
    """Что автор уже решил сам: слова словаря, ударения по месту."""
    d = {}
    try:
        d["dict"] = {re.sub(r"[+\u0301]", "", k).lower() for k in n.load_word_dict()}
    except Exception:
        d["dict"] = set()
    try:
        d["fixes"] = {h: {int(k) for k, _, _ in v} for h, v in n._load_stress_fixes().items()}
    except Exception:
        d["fixes"] = {}
    return d


def visible(f: dict, raw: str, dec: dict) -> bool:
    if f["w"] in dec["dict"]:
        return False
    if raw and f["k"] in dec["fixes"].get(hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12], ()):
        return False
    return True


def important(f: dict) -> bool:
    return bool(f.get("homo") or f.get("neural") or f.get("kind") == "nomark")


def part_rows(n, rows, roles, num: int):
    """Реплики части N: по карте времени собранной части, иначе по закреплению, иначе — текущее окно."""
    idpos = {str(r["id"]): i for i, r in enumerate(rows)}
    tsv = n.PARTS / f"part_{num:02d}.times.tsv"
    if tsv.exists():
        idx = []
        for line in tsv.read_text(encoding="utf-8").splitlines():
            p = line.split("\t")
            if len(p) > 2 and p[2] in idpos:
                idx.append(idpos[p[2]])
        if idx:
            return list(range(min(idx), max(idx) + 1))
    if hasattr(n, "fixed_prefix"):
        fx = n.fixed_prefix(rows)
        if 1 <= num <= len(fx):
            return list(range(fx[num - 1][0], fx[num - 1][1] + 1))
        w = n.work_window(rows, roles)
        if w[2] == num and w[0] < len(rows):
            return list(range(w[0], w[1] + 1))
    return []


def accent_mode(arg, st) -> str:
    if arg:
        return arg
    v = str(st.get("accent") or "acute")
    return "none" if v == "base" else ("sparse" if v == "sparse" else "acute")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent))
    ap.add_argument("--part", type=int, default=0)
    ap.add_argument("--accent", default=None)
    ap.add_argument("--in", dest="inp", default=None, help="json: [{key, raw, tech}] — фразы студии")
    ap.add_argument("--out", default=None)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    root = Path(a.root).resolve()
    if a.inp:
        a.inp = str(Path(a.inp).resolve())
    if a.out:
        a.out = str(Path(a.out).resolve())
    os.chdir(root)
    sys.path.insert(0, str(root))
    try:
        import nox_audiobook as n
    except Exception as e:
        print(f"  самопроверка ударений: не загрузился конвейер книги ({type(e).__name__}: {e})", flush=True)
        return 3
    st = getattr(n, "SETTINGS", None) or {}
    accent = accent_mode(a.accent, st)
    if accent == "none":
        msg = "в этой книге ударения не ставятся (базовая модель) — сверять нечего"
        if a.out:
            Path(a.out).write_text(json.dumps(dict(ok=True, note=msg, items={}), ensure_ascii=False), encoding="utf-8")
        print("  самопроверка ударений: " + msg)
        return 0
    ck = Checker(n, accent, n.WORK / "stress_cache.json")
    dec = author_decisions(n)

    if a.inp:                                            # студия: её тексты (с правками и правилами)
        items = json.loads(Path(a.inp).read_text(encoding="utf-8"))
        res, words = {}, 0
        t0 = time.time()
        for k, it in enumerate(items, 1):
            tech = it.get("tech") or ""
            words += sum(1 for m in WORD.finditer(tech) if sum(c in VOW for c in m.group(0)) >= 2)
            fs = [f for f in ck.check(it.get("raw") or "", tech) if visible(f, it.get("raw") or "", dec)]
            if fs:
                res[it["key"]] = fs
            if k % 50 == 0:
                print(f"  …{k}/{len(items)}", flush=True)
        ck.save()
        out = dict(ok=True, items=res, phrases=len(items), words=words, ran=ck.ran, sec=round(time.time() - t0, 1))
        if a.out:
            tmp = Path(a.out).with_suffix(".tmp")
            tmp.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
            replace_retry(tmp, a.out)
        print(f"  самопроверка ударений: фраз {len(items)}, с расхождениями {len(res)}", flush=True)
        return 0

    rows, roles = n.load_rows(), n.load_roles()
    idx = part_rows(n, rows, roles, a.part) if a.part else list(range(len(rows)))
    if not idx:
        print(f"  самопроверка ударений: часть {a.part} не найдена (ещё не собрана)")
        return 0
    seen, found, words, changed = set(), [], 0, 0
    t0 = time.time()
    for i in idx:
        r = rows[i]
        if r["kind"] == "head" and len(r["text"]) <= 3:
            continue
        p = n.wav_for(r, roles)
        try:
            tech = p.with_suffix(".txt").read_text(encoding="utf-8")          # что на самом деле прочитано в записи
        except OSError:
            tech = None
        if tech is None:
            tech = n.prepare_text(r["text"], False, accent)
        key = (r["text"], tech)
        if key in seen:
            continue
        seen.add(key)
        words += sum(1 for m in WORD.finditer(tech) if sum(c in VOW for c in m.group(0)) >= 2)
        for f in ck.check(r["text"], tech):
            if visible(f, r["text"], dec):
                found.append((r["id"], f))
    ck.save()
    imp = [(i, f) for i, f in found if important(f)]
    rest = [(i, f) for i, f in found if not important(f)]
    tag = f"часть {a.part}" if a.part else "вся книга"

    def line(i, f):
        why = "омограф" if f.get("homo") else "нет в словаре ударений" if f.get("neural") else "без знака ударения" if f["kind"] == "nomark" else "обычное слово"
        return f"№ {i}\t{f['ours']}\t{f['sil']}\t{why}\t{f['ctx']}"

    name = f"stress_part_{a.part:02d}" if a.part else "stress_book"
    rep = [f"Самопроверка ударений, {tag}: фраз {len(seen)}, слов с ударением {words}; расхождений со второй моделью {len(found)}.",
           "Формат: № реплики | как озвучено | как ставит вторая модель | почему в списке | где", "",
           f"СТОИТ ПОСЛУШАТЬ ({len(imp)}):"] + [line(i, f) for i, f in imp] + ["", f"ПРОЧИЕ — обычные слова, чаще ошибается вторая модель ({len(rest)}):"] \
        + [line(i, f) for i, f in rest]
    (n.WORK / f"{name}.txt").write_text("\n".join(rep) + "\n", encoding="utf-8")
    (n.WORK / f"{name}.json").write_text(json.dumps(dict(part=a.part, at=time.strftime("%Y-%m-%d %H:%M:%S"), phrases=len(seen), words=words,
                                                     important=[dict(f, id=i) for i, f in imp], rest=[dict(f, id=i) for i, f in rest]),
                                                ensure_ascii=False), encoding="utf-8")
    if a.json:
        print(json.dumps(dict(part=a.part, phrases=len(seen), words=words, important=len(imp), rest=len(rest)), ensure_ascii=False))
        return 0
    agree = 100.0 * (1 - len(found) / words) if words else 100.0
    print(f"  {tag}: фраз {len(seen)}, слов с ударением {words}; вторая модель согласна в {agree:.1f} % ({time.time() - t0:.0f} с)")
    if imp:
        print(f"  СТОИТ ПОСЛУШАТЬ — омографы и слова, которых нет в словаре ударений: {len(imp)}")
        for i, f in imp[:12]:
            print(f"    № {i}: «{f['ours']}» — вторая модель: «{f['sil']}»   {f['ctx']}")
        if len(imp) > 12:
            print(f"    … и ещё {len(imp) - 12}")
    else:
        print("  спорных ударений, которые стоит послушать, не найдено")
    if rest:
        print(f"  прочие расхождения (обычные слова — чаще ошибается вторая модель): {len(rest)}")
    print(f"  весь список: {n.WORK / (name + '.txt')}")
    if imp:
        print("  в студии у таких фраз значок «уд.»: в окне фразы — «Оставить» или «Поставить так» одним нажатием")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
