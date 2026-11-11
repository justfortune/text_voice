#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# NOX_KIT 2026-11-12a
"""
verify_k1a.py — ПРОВЕРКА ДОЗАПИСАННОСТИ РАСПОЗНАВАНИЕМ РЕЧИ (Whisper на процессоре, видеокарту не занимает).

Каждая озвученная реплика (work/wav/<ключ>.wav) распознаётся и сверяется с тем, что должно было прозвучать
(work/wav/<ключ>.txt — текст, который прочёл движок). Если конец фразы не дочитан, слова выброшены или движок
«заговорился» (повторы) — запись откладывается в <ключ>.rejN.wav, и воркер озвучки делает её заново. После
3 попыток берётся лучшая из них (и попадает в work/asr_suspicious.txt — послушать вручную).
Проверенная запись получает метку <ключ>.ok — окно сборки частей (watch) берёт в части только проверенное.
Голос: каждое предложение записи сверяется по высоте голоса с образцом своей роли (те же функции, что у сторожа
голоса в nox_fast.py); выше/ниже образца больше чем в 1,5 раза — «чужой голос», запись переозвучивается.

Запуск (go делает это сам, скрыто):  envfast\\Scripts\\python.exe verify_k1a.py [--model small] [--threads 4]
Лог: verify.log в папке книги; отказы — work/asr_log.txt; под вопросом — work/asr_suspicious.txt.
Сердцебиение: work/asr_active.txt; метка этой проверки — work/asr_okv.txt (пока она есть, сборка частей берёт только
записи, проверенные этой версией проверки, и ждёт её с первой минуты).
Порядок: С НАЧАЛА КНИГИ, и каждые 20 с — снова с начала: переозвученная реплика ранней части проверяется первой,
часть собирается один раз, когда все её реплики проверены (до 3 попыток на реплику, потом берётся лучшая).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
import nox_audiobook as n  # noqa: E402

HEART = n.WORK / "asr_active.txt"
OKV_F = n.WORK / "asr_okv.txt"
RESCAN_S = 20               # раз в 20 с — снова с начала книги: ранние части первыми
SUSP = n.WORK / "asr_suspicious.txt"
LOG = n.WORK / "asr_log.txt"
MIN_LETTERS = 12            # совсем короткие реплики («Да.», «Хм») распознаются ненадёжно — не проверяем
IDLE_EXIT_MIN = 45          # нечего проверять столько минут (озвучка встала) — выходим, сборка перестаёт ждать

# известные «галлюцинации» Whisper на русском (титры, концовки роликов) — это не речь движка
_HALLU = re.compile(r"(субтитр\w*|редактор\w* субтитр\w*|корректор\w*|продолжение следует|спасибо за просмотр\w*|"
                    r"подписывайтесь\w*|dimatorzok|amara\.org|игорь негода)[^.!?]*[.!?]?", re.I)


def _beat():
    while True:
        try:
            HEART.write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
        except OSError:
            pass
        time.sleep(60)


def _numnorm(t: str) -> str:
    f = getattr(n, "_normalize_numbers", None)
    if f:
        try:
            return f(t)
        except Exception:
            pass
    return t


def norm_words(t: str) -> list[str]:
    t = t.replace("́", "").replace("+", "").lower().replace("ё", "е")
    t = re.sub(r"[^а-я0-9a-z]+", " ", t)
    return t.split()


def tail_found(exp: list[str], got: list[str]) -> float:
    """Насколько последние слова ожидаемого текста нашлись в конце распознанного (0..1)."""
    if not exp:
        return 1.0
    tail = " ".join(exp[-2:]) if len(" ".join(exp[-2:])) >= 6 else " ".join(exp[-3:])
    if not got:
        return 0.0
    window = " ".join(got[max(0, len(got) - max(6, len(exp[-3:]) + 4)):])
    L = len(tail)
    best = 0.0
    for i in range(0, max(1, len(window) - L + 1)):
        r = SequenceMatcher(None, tail, window[i:i + L], autojunk=False).ratio()
        if r > best:
            best = r
            if best > 0.95:
                break
    return best


def judge(expected: str, heard: str, strict: float):
    heard = _HALLU.sub(" ", heard)
    e, g = norm_words(_numnorm(expected)), norm_words(_numnorm(heard))
    if len("".join(e)) < MIN_LETTERS:
        # короткие («Иди ко мне.», «Наконец-то,»): раньше не проверялись вовсе — пустая/чужая запись проходила.
        # Теперь: есть ли речь — проверяет звук (speech_dur в main); услышал распознаватель слова — они должны совпасть.
        es, gs = " ".join(e), " ".join(g)
        if not g or len("".join(e)) < 4:
            return True, 0.8, "коротко — проверено по звуку"
        cover = SequenceMatcher(None, es, gs, autojunk=False).ratio()
        if cover < 0.35 and not (set(e) & set(g)):
            return False, cover, f"коротко: услышано «{gs[:30]}» вместо «{es[:30]}»"
        if len(gs) > 2 * len(es) + 10:
            return False, 0.4, "коротко: лишнее/повторы"
        return True, 0.6 + 0.4 * cover, f"коротко — ок ({cover:.2f})"
    es, gs = " ".join(e), " ".join(g)
    cover = SequenceMatcher(None, es, gs, autojunk=False).ratio()
    tail = tail_found(e, g)
    why = []
    if tail < 0.62:
        why.append(f"конец не дочитан (хвост {tail:.2f})")
    if cover < strict:
        why.append(f"пропуски/искажения (сходство {cover:.2f})")
    if len(gs) > 1.5 * len(es) + 12:
        why.append("лишнее/повторы")
    score = 0.6 * cover + 0.4 * tail
    return (not why), score, ", ".join(why) or f"ок ({cover:.2f}/{tail:.2f})"


_TOK = re.compile(r"([А-Яа-яЁёA-Za-z0-9]+)([^А-Яа-яЁёA-Za-z0-9]*)")


def _tok(w: str) -> str:
    return w.lower().replace("ё", "е")


def sent_bounds(expected: str, words) -> list:
    """Границы предложений внутри записи по распознаванию: [[конец последнего слова предложения, начало следующего], ...]
    (секунды). Сборка частей ставит туда паузу, если движок прочитал предложения слитно."""
    t = expected.replace("\u0301", "").replace("+", "")
    ms = list(_TOK.finditer(t))
    exp, ends = [], []
    for i, m in enumerate(ms):
        exp.append(_tok(m.group(1)))
        nxt = ms[i + 1].group(1) if i + 1 < len(ms) else ""
        ends.append(bool(re.search(r"[.!?…]", m.group(2))) and nxt[:1].isupper())
    heard, times = [], []
    for w in words:
        for m in _TOK.finditer(_numnorm(w[0])):
            heard.append(_tok(m.group(1)))
            times.append((float(w[1]), float(w[2])))
    if not exp or not heard:
        return []
    mp = {}
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, exp, heard, autojunk=False).get_opcodes():
        if tag == "equal" or (tag == "replace" and i2 - i1 == j2 - j1):
            for k in range(i2 - i1):
                mp[i1 + k] = j1 + k
    out = []
    for i, is_end in enumerate(ends):
        if not is_end or i not in mp:
            continue
        j = mp[i]
        jn = mp.get(i + 1, j + 1)
        if jn < len(times) and jn > j:
            out.append([round(times[j][1], 3), round(times[jn][0], 3)])
    return out


OKV = "\tv6"            # метка проверки; «\tv2» в .ok остаётся для сборки частей (она ждёт v2)
#   v6: + реплика, где распознаватель не услышал ни слова, должна звучать ГОЛОСОМ (гласные), а не шумом/дыханием;
#       прежние v5-проверки, где слова услышаны и совпали, остаются в силе (метка дописывается без перепроверки),
#       перепроверяются только «по звуку», «не распознано» и «лучшая из N» с серьёзной бедой (нет речи, лишнее)
#   v5: + короткие реплики проверяются (раньше «коротко — не проверяется»), + есть ли в записи речь вообще
#   v3: + пауза между предложениями; v4: + голос своей роли по каждому предложению (все прежние перепроверяются)
PAUSE_MIN_MS = 120       # между предложениями тише этого меньше 120 мс — движок прочёл их слитно («…соседей это хорошо»)


def silence_ms(x, sr, a, b) -> int:
    """Длина тишины (мс) на границе предложений [a, b] (секунды по распознаванию). Метки слов у Whisper неточны
    (±0,1 с), поэтому берётся целиком самый длинный участок тишины, задевающий окно [a−120 мс, b+120 мс]."""
    import numpy as np
    w = int(sr * 0.01)
    k = len(x) // w
    if k < 3:
        return 0
    amax = np.abs(x[:k * w]).reshape(k, w).max(axis=1)
    thr = max(1.5e-3, 0.012 * float(np.percentile(amax, 95)))
    lo, hi = min(a, b), max(a, b)
    c0, c1 = max(0, int((lo - 0.12) * 100)), min(k, int((hi + 0.12) * 100) + 1)
    w0, w1 = max(0, c0 - 150), min(k, c1 + 150)      # тишина может тянуться за окно — меряем её целиком
    q = amax[w0:w1] < thr
    best, i = 0, 0
    while i < len(q):
        if q[i]:
            j = i
            while j < len(q) and q[j]:
                j += 1
            if w0 + j > c0 and w0 + i < c1:              # участок задевает окно вокруг границы
                best = max(best, j - i)
            i = j
        else:
            i += 1
    return best * 10


def sent_spans(gaps, dur):
    """Границы предложений по распознаванию -> отрезки [начало, конец] каждого предложения (секунды)."""
    out, a = [], 0.0
    for g in gaps or []:
        try:
            e, b = float(g[0]), float(g[1])
        except (TypeError, ValueError, IndexError):
            continue
        if e > a:
            out.append((a, e))
        a = max(a, b)
    if dur > a:
        out.append((a, dur))
    return out


WIN_S, WIN_HOP_S, WIN_TOL = 2.5, 1.0, 1.75   # окна по 2,5 с: голос «съехал» посреди предложения (порог шире —
#                                               в коротком окне своя интонация гуляет сильнее, чем в предложении)


def voice_check(nf, x, sr, ref_hz, gaps):
    """Голос своей роли? Высота голоса всей записи, каждого предложения (порог nf.VOICE_TOL) и окон по 2,5 с
    (порог WIN_TOL) против образца роли -> (отношение, где: -1 вся запись / k предложение / «окно 12.5 с», чужой?)
    или (None, None, False), если судить не по чему (коротко, шёпот). Четвёртое — высота всей записи (Гц) или None."""
    import math
    import numpy as np
    cand = []                                            # (отношение, где, порог)
    whole = nf.median_f0(x, sr)
    if whole is not None:
        cand.append((whole / ref_hz, -1, nf.VOICE_TOL))
    spans = sent_spans(gaps, len(x) / sr)
    if len(spans) > 1:
        for k, (a, b) in enumerate(spans):
            f = nf.median_f0(x[int(a * sr):int(b * sr)], sr)
            if f is not None:
                cand.append((f / ref_hz, k, nf.VOICE_TOL))
    W, H = int(WIN_S * sr), int(WIN_HOP_S * sr)
    if len(x) > W + H:
        for a in range(0, len(x) - W + 1, H):
            tr = nf.voice_track(x[a:a + W], sr)
            if tr is not None and int(tr[1].sum()) >= 100:
                cand.append((float(np.median(tr[0][tr[1]])) / ref_hz, f"окно {a / sr:.1f} с", WIN_TOL))
    if not cand:
        return None, None, False, None
    over = [c for c in cand if c[0] > c[2] or c[0] < 1.0 / c[2]]
    pick = max(over or cand, key=lambda c: abs(math.log(c[0])) - math.log(c[2]))
    return pick[0], pick[1], bool(over), whole


def role_ref(vs, ref_hz):
    """Опорная высота роли: пока записей мало — образец; от 8 записей — медиана самих записей роли (у клона голос
    бывает заметно выше/ниже образца — так было у Джека; редкие «съехавшие» на медиану не влияют)."""
    import numpy as np
    return float(np.median(vs[3][-300:])) if len(vs[3]) >= 8 else ref_hz


def speech_dur(x, sr):
    """Сколько секунд в записи звучит речь (кадры 20 мс громче −42 дБ и громче 8 % от пиковых)."""
    import numpy as np
    w = max(1, int(sr * 0.02))
    k = len(x) // w
    if k < 1:
        return 0.0
    rms = np.sqrt(np.mean(np.square(np.asarray(x[:k * w], dtype="float32").reshape(k, w)), axis=1))
    thr = max(10 ** (-42 / 20), 0.08 * float(np.percentile(rms, 95)))
    return float((rms > thr).sum()) * 0.02


def voiced_s(x, sr):
    """Сколько секунд в записи ГОЛОСА — периодичных кадров с основным тоном 70–400 Гц (гласные, звонкие согласные).
    Шум, дыхание, шорох, гул их не дают. Кадры 40 мс, шаг 10 мс; пик автокорреляции после её первого спада ниже нуля.
    Считается на родной частоте записи (без пересчёта частоты — лишних зависимостей нет)."""
    import numpy as np
    x = np.asarray(x, dtype="float32")
    if x.ndim > 1:
        x = x.mean(axis=1)
    sr = int(sr)
    x = np.concatenate([[0.0], np.diff(x)]).astype("float32")     # подъём верхов: гул и низкий рокот не в счёт
    w, hop = int(sr * 0.04), max(1, int(sr * 0.01))
    if len(x) < w or w < 64:
        return 0.0
    nfft = 1 << (2 * w - 1).bit_length()
    k = 1 + (len(x) - w) // hop
    fr = x[np.arange(w)[None, :] + hop * np.arange(k)[:, None]]
    rms = np.sqrt(np.mean(fr ** 2, axis=1))
    gate = max(1e-4, 0.05 * float(np.percentile(rms, 95)))
    fr = fr - fr.mean(axis=1, keepdims=True)
    han = np.hanning(w).astype("float32")
    ac = np.fft.irfft(np.abs(np.fft.rfft(fr * han[None, :], nfft, axis=1)) ** 2, nfft, axis=1)[:, :w]
    ac = ac / (ac[:, :1] + 1e-12)
    wac = np.fft.irfft(np.abs(np.fft.rfft(han, nfft)) ** 2, nfft)[:w]
    ac = ac / np.maximum(wac / wac[0], 0.2)[None, :]
    lo, hi = sr // 400, min(sr // 70, w - 1)
    neg = ac[:, :hi] < 0
    first = np.where(neg.any(axis=1), neg.argmax(axis=1), hi)       # первый спад ниже нуля
    lags = np.arange(hi)[None, :]
    peak = np.where(lags >= np.maximum(first, lo)[:, None], ac[:, :hi], -1.0).max(axis=1)
    return float(((peak >= 0.5) & (rms > gate)).sum()) * hop / sr


def _carry_ok(okt: str) -> bool:
    """v5 -> v6 без перепроверки: распознаватель тогда услышал слова, и они совпали — проверка в силе."""
    t = okt.strip().split("\t")
    if "v5" not in t or "не распознано" in okt or "по звуку" in okt:
        return False
    if "лучшая из" in okt:                 # приняли лучшую из попыток: серьёзная беда (нет речи, лишнее) — переозвучить
        try:                               # умно (на каждой попытке другой способ); прочие — оставить как есть
            return float(t[1]) >= 0.45
        except (IndexError, ValueError):
            return False
    return True


def _put_gaps(p: Path, gaps):
    # границы предложений пишутся, только если изменились: сборка частей смотрит на них
    g = p.with_suffix(".gaps")
    s_ = json.dumps(gaps)
    try:
        if g.read_text(encoding="utf-8") == s_:
            return
    except OSError:
        pass
    g.write_text(s_, encoding="utf-8")


def sig_of(p: Path) -> str:
    st = p.stat()
    return f"{st.st_size}:{st.st_mtime_ns}"


def log(line: str):
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S ") + line + "\n")
    except OSError:
        pass


def load16k(p: Path):
    import numpy as np
    import soundfile as sf
    x, sr = sf.read(str(p), dtype="float32")
    if x.ndim > 1:
        x = x.mean(axis=1)
    if sr != 16000:
        from math import gcd
        from scipy.signal import resample_poly
        g = gcd(16000, sr)
        x = resample_poly(x, 16000 // g, sr // g).astype("float32")
    return np.ascontiguousarray(x, dtype="float32")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=None)
    ap.add_argument("--threads", type=int, default=None)
    args = ap.parse_args()
    st = getattr(n, "SETTINGS", {}) or {}
    model_name = args.model or st.get("asr_model", "small") or "small"
    threads = args.threads or int(st.get("asr_threads", 4) or 4)
    strict = float(st.get("asr_strict", 0.6) or 0.6)
    max_tries = max(1, int(st.get("asr_tries", 3) or 3))
    tail_max = float(st.get("tail_db", -16) or -16)
    cut_db = getattr(n, "cut_tail_db", None)
    vcut = getattr(n, "vowel_cut_ms", None)
    vcut_min = float(st.get("vowel_cut_ms", 20) or 0)
    threading.Thread(target=_beat, daemon=True).start()     # сборка ждёт проверку, пока бьётся сердце
    try:
        OKV_F.write_text(OKV.strip(), encoding="utf-8")       # сборка берёт только записи с этой меткой
    except OSError:
        pass
    nf = None
    if st.get("voice_guard", True):
        try:                                                  # функции сторожа голоса — из nox_fast.py рядом
            import nox_fast as nf
            if not hasattr(nf, "median_f0"):
                print("в папке книги старый nox_fast.py — проверка голоса по высоте выключена", flush=True)
                nf = None
        except Exception as e:
            print(f"проверка голоса по высоте недоступна: {type(e).__name__}: {str(e)[:120]}", flush=True)
            nf = None
    vstat = {}                                                # роль -> [проверено, чужих]; много чужих — виноват образец
    try:
        from faster_whisper import WhisperModel
        print(f"загружаю распознаватель речи Whisper «{model_name}» (процессор, {threads} потоков; "
              f"первый раз скачивается ~0,5 ГБ)…", flush=True)
        model = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=threads)
    except Exception as e:
        print(f"проверка распознаванием недоступна: {type(e).__name__}: {str(e)[:300]}", flush=True)
        HEART.unlink(missing_ok=True)
        OKV_F.unlink(missing_ok=True)                         # сборка не должна ждать проверку, которой не будет
        os._exit(0)
    print("готово; проверяю реплики с начала книги (каждые 20 с — снова с начала: ранние части первыми)", flush=True)
    checked = rejected = accepted_best = 0
    ok_cache: dict[str, str] = {}
    last_work = time.time()
    started_at = time.strftime("%Y-%m-%d %H:%M:%S")
    row_fail: dict[str, int] = {}
    while True:
        try:
            rows = n.load_rows()
            roles = n.load_roles()
            stems = n.voice_stems(roles)
        except Exception as e:                       # сценарий/роли переписываются прямо сейчас — подождём
            print(f"[проверка] список реплик не прочитан ({type(e).__name__}: {str(e)[:100]}) — через 15 с снова", flush=True)
            time.sleep(15)
            continue
        did = 0
        all_ok = True
        pass_t0 = time.time()
        # Части, закреплённые автором в студии, не перепроверяются. В режиме «по частям» проверяется только текущая часть
        # (первая незакреплённая); собрана — проверка своё дело сделала и выходит.
        _fxs, _win = set(), None
        try:
            if hasattr(n, "fixed_set"):
                _fxs = n.fixed_set(rows)
            if hasattr(n, "by_parts") and n.by_parts():
                _win = n.work_window(rows, roles)
        except Exception as e:
            print(f"[проверка] закреплённые части/окно работы не прочитаны ({type(e).__name__}: {str(e)[:100]})", flush=True)
        if _win is not None and _win[3]:
            print(f"[проверка] часть {_win[2]} собрана — проверять больше нечего: проверено {checked}, на переозвучку "
                  f"ушло {rejected}, под вопросом {accepted_best}" if _win[0] < len(rows) else
                  "[проверка] все части закреплены автором — проверять нечего", flush=True)
            HEART.unlink(missing_ok=True)
            os._exit(0)
        if _win is not None:                         # сборщик части сдался (озвучены не все реплики) — не висеть до ночи
            try:
                import json as _json
                _ps = _json.loads((n.WORK / "part_status.json").read_text(encoding="utf-8"))
                if _ps.get("state") == "stalled" and str(_ps.get("at") or "") >= started_at:
                    print(f"[проверка] часть {_win[2]} не собрана: озвучены не все её реплики (причина — в part.log и "
                          f"work\\failed.txt) — выхожу; запустите go ещё раз", flush=True)
                    HEART.unlink(missing_ok=True)
                    os._exit(0)
            except Exception:
                pass
        for _i, r in enumerate(rows):
            try:
                if _i in _fxs or (_win is not None and not (_win[0] <= _i <= _win[1])):
                    continue
                if did and time.time() - pass_t0 > RESCAN_S:   # снова с начала: там могли появиться переозвученные реплики
                    all_ok = False
                    break
                p = n.wav_for(r, roles)
                try:
                    sg = sig_of(p)
                except OSError:
                    all_ok = False
                    continue
                if ok_cache.get(p.name) == sg:
                    continue
                okf = p.with_suffix(".ok")
                try:
                    _okt = okf.read_text(encoding="utf-8")
                    if _okt.split("\t")[0] == sg and OKV in _okt:   # прежние проверки (v2–v4) перепроверяются
                        ok_cache[p.name] = sg
                        continue
                    if _okt.split("\t")[0] == sg and _carry_ok(_okt):   # v5 в силе — дописать метку v6, не слушая заново
                        okf.write_text(_okt + OKV, encoding="utf-8")
                        ok_cache[p.name] = sg
                        continue
                except OSError:
                    pass
                all_ok = False
                _lk = p.with_suffix(".lock")
                if _lk.exists():                             # воркер прямо сейчас пишет — подождём
                    try:                                     # ...но замок старше 10 минут при готовой записи — брошенный
                        if time.time() - _lk.stat().st_mtime < 600:
                            continue
                    except OSError:
                        continue
                side = p.with_suffix(".txt")
                tdb, vc, gaps = -99.0, None, []
                _x = None
                try:
                    expected = side.read_text(encoding="utf-8") if side.exists() else r["text"]
                    if cut_db or vcut:
                        import soundfile as _sf
                        _x, _sr = _sf.read(str(p), dtype="float32")
                        if _x.ndim > 1:
                            _x = _x.mean(axis=1)
                        tdb = cut_db(_x, _sr) if cut_db else -99.0
                        vc = vcut(_x, _sr, r["text"]) if vcut else None
                    segs, _info = model.transcribe(load16k(p), language="ru", beam_size=1, vad_filter=False,
                                                   condition_on_previous_text=False, word_timestamps=True)
                    segs = list(segs)
                    heard = " ".join(s.text for s in segs)
                    try:
                        gaps = sent_bounds(expected, [(w.word, w.start, w.end) for s in segs for w in (s.words or [])])
                    except Exception:
                        gaps = []
                    if gaps and _x is None:
                        import soundfile as _sf
                        _x, _sr = _sf.read(str(p), dtype="float32")
                        if _x.ndim > 1:
                            _x = _x.mean(axis=1)
                except Exception as e:
                    log(f"{r['id']}\tне распознано: {type(e).__name__}: {str(e)[:100]}")
                    heard = None
                try:
                    if sig_of(p) != sg:                      # файл заменили, пока слушали — проверим заново
                        continue
                except OSError:
                    continue
                last_work = time.time()
                if heard is None:
                    okf.write_text(f"{sg}\t-1\tне распознано\tv2{OKV}", encoding="utf-8")
                    continue
                good, score, why = judge(expected, heard, strict)
                try:                                         # есть ли речь вообще и по длине ли она тексту (все реплики)
                    if _x is None:
                        import soundfile as _sf
                        _x, _sr = _sf.read(str(p), dtype="float32")
                        if _x.ndim > 1:
                            _x = _x.mean(axis=1)
                    _lt = len(re.sub(r"[^А-Яа-яЁёA-Za-z0-9]", "", expected.replace("\u0301", "")))
                    _sp = speech_dur(_x, _sr)
                    if _sp < max(0.2, 0.03 * _lt):
                        good = False
                        score = min(score, 0.2)
                        why = (why + ", " if not why.startswith("ок") and not why.startswith("коротко —") else "") \
                            + (f"нет речи (звучит {_sp:.2f} с)" if _sp < 0.2 else f"мало речи для текста ({_sp:.2f} с на {_lt} букв)")
                    elif _lt < 40 and len(_x) / _sr > 1.5 + 0.25 * _lt:
                        good = False
                        score = min(score, 0.4)
                        why = (why + ", " if not why.startswith("ок") and not why.startswith("коротко —") else "") \
                            + f"лишнее (запись {len(_x) / _sr:.1f} с на {_lt} букв)"
                    elif why.startswith("коротко — проверено по звуку") and _lt >= 4:
                        # v6: распознаватель не услышал ни слова — звук должен быть ГОЛОСОМ (гласные), а не шумом/дыханием
                        _nv = len(re.findall(r"[аеёиоуыэюяАЕЁИОУЫЭЮЯ]", expected))
                        _vo = voiced_s(_x, _sr)
                        if _nv >= 2 and _vo < max(0.06, 0.025 * _nv):
                            good = False
                            score = min(score, 0.25)
                            why = f"коротко — не услышано и нет голоса (звучит {_sp:.2f} с, голос {_vo:.2f} с)"
                        else:
                            why += f" (голос {_vo:.2f} с)"
                except Exception as e:
                    log(f"{r['id']}\tречь не измерена: {type(e).__name__}: {str(e)[:100]}")
                if tdb > tail_max:                           # запись кончается на звуке — конец слова обрублен
                    good = False
                    score = min(score, 0.5)
                    why = (why + ", " if not why.startswith("ок") else "") + f"обрублен конец ({tdb:.0f} дБ)"
                if vc is not None and vcut_min and vc < vcut_min:     # гласная на конце оборвана («Возьмит…»)
                    good = False
                    score = min(score, 0.4 + vc / 100)
                    why = (why + ", " if not why.startswith("ок") else "") + f"оборвана гласная на конце ({vc} мс)"
                vr = None
                if nf is not None:                           # голос своей роли? (по каждому предложению)
                    try:
                        role = n.resolve_role(r["speaker"], roles)
                        ref_hz = nf.ref_f0(n.voice_files(role, stems)[0])
                        if ref_hz:
                            if _x is None:
                                import soundfile as _sf
                                _x, _sr = _sf.read(str(p), dtype="float32")
                                if _x.ndim > 1:
                                    _x = _x.mean(axis=1)
                            vs = vstat.setdefault(role, [0, 0, False, []])
                            _adaptive = len(vs[3]) >= 8              # опора — уже медиана записей роли, не образец
                            vr, vk, vw, vwhole = voice_check(nf, _x, _sr, role_ref(vs, ref_hz), gaps)
                            if vr is not None:
                                if vwhole is not None:
                                    vs[3].append(vwhole)
                                if _adaptive:                        # предохранитель считает только против своей медианы
                                    vs[0] += 1
                                    vs[1] += int(bool(vw))
                                if not vs[2] and vs[0] >= 20 and vs[1] / vs[0] > nf.ROLE_TRIP[1]:
                                    vs[2] = True
                                    msg = (f"роль «{role}»: чужим голосом {vs[1]} из {vs[0]} записей — похоже, образец голоса "
                                           f"не похож на её речь; по высоте голоса эту роль больше не браковать (послушайте образец)")
                                    print("[проверка] " + msg, flush=True)
                                    log(msg)
                                    try:
                                        with (n.WORK / "voice_trip.txt").open("a", encoding="utf-8") as f:
                                            f.write(f"{time.strftime('%Y-%m-%d %H:%M')}\t{role}\t{vs[1]}/{vs[0]}\tпроверка\n")
                                    except OSError:
                                        pass
                                if vw and not vs[2]:
                                    good = False
                                    score = min(score, 0.3)
                                    where = "вся запись" if vk == -1 else (vk if isinstance(vk, str) else f"предложение {vk + 1}")
                                    why = ((why + ", " if not why.startswith("ок") else "")
                                           + f"чужой голос ({where}: высота ×{vr:.2f} к образцу)")
                    except Exception as e:
                        log(f"{r['id']}\tголос не проверен: {type(e).__name__}: {str(e)[:100]}")
                if gaps and _x is not None:                  # предложения прочитаны слитно — паузы после точки нет
                    _pz = [silence_ms(_x, _sr, a, b) for a, b in gaps]
                    if min(_pz) < PAUSE_MIN_MS:
                        good = False
                        score = min(score, 0.55)
                        why = (why + ", " if not why.startswith("ок") else "") + f"нет паузы между предложениями ({min(_pz)} мс)"
                why += f"\tконец {tdb:.0f} дБ" + (f", гласная {vc} мс" if vc is not None else "") \
                    + (f", голос ×{vr:.2f}" if vr is not None else "")
                checked += 1
                did += 1
                stem = p.stem
                rej = sorted(p.parent.glob(stem + ".rej*.wav"))
                if good:
                    _put_gaps(p, gaps)
                    okf.write_text(f"{sg}\t{score:.3f}\t{why}\tv2{OKV}", encoding="utf-8")
                    ok_cache[p.name] = sg
                    for f in rej:                            # прежние неудачные попытки больше не нужны
                        f.unlink(missing_ok=True)
                        f.with_suffix(".json").unlink(missing_ok=True)
                else:
                    log(f"{r['id']}\tгл.{r['chapter']}\t{r['speaker']}\t{why}\tждали: {expected[-70:]!r}\t"
                        f"услышано: {heard[-70:]!r}")
                    if len(rej) + 1 >= max_tries:
                        # попыток достаточно — берём лучшую из всех версий
                        cand = [(score, p, gaps)]
                        for f in rej:
                            try:
                                _j = json.loads(f.with_suffix(".json").read_text(encoding="utf-8"))
                                cand.append((_j["score"], f, _j.get("gaps", [])))
                            except Exception:
                                cand.append((0.0, f, []))
                        best_score, best, best_gaps = max(cand, key=lambda c: c[0])
                        if best != p:
                            tmp = p.with_name(stem + ".tmpbest")
                            os.replace(best, tmp)
                            os.replace(tmp, p)
                        for f in list(p.parent.glob(stem + ".rej*")):
                            f.unlink(missing_ok=True)
                        sg2 = sig_of(p)
                        _put_gaps(p, best_gaps)
                        okf.write_text(f"{sg2}\t{best_score:.3f}\tлучшая из {len(cand)} попыток\tv2{OKV}", encoding="utf-8")
                        ok_cache[p.name] = sg2
                        with SUSP.open("a", encoding="utf-8") as f:
                            f.write(f"{r['id']}\tгл.{r['chapter']}\t{r['speaker']}\t{why}\t{r['text'][:90]}\n")
                        accepted_best += 1
                    else:
                        k = len(rej) + 1
                        dst = p.with_name(f"{stem}.rej{k}.wav")
                        os.replace(p, dst)                   # воркер озвучки увидит «нет записи» и сделает заново
                        dst.with_suffix(".json").write_text(json.dumps({"score": score, "why": why, "heard": heard,
                                                                        "gaps": gaps}, ensure_ascii=False), encoding="utf-8")
                        p.with_suffix(".gaps").unlink(missing_ok=True)
                        side.unlink(missing_ok=True)
                        rejected += 1
                if checked % 50 == 0:
                    print(f"[проверка] {time.strftime('%H:%M')}  проверено {checked}, на переозвучку {rejected}, "
                          f"принято лучшее из попыток {accepted_best}; сейчас id {r['id']} (гл. {r['chapter']})", flush=True)
            except Exception as e:                   # сбой на одной реплике (файл занят антивирусом и т. п.) —
                all_ok = False                       # не роняет проверку: реплика будет проверена в следующий проход
                _rid = str(r.get("id", "?"))
                row_fail[_rid] = row_fail.get(_rid, 0) + 1
                if row_fail[_rid] <= 3:                  # повторяющийся сбой — в журнал не чаще трёх раз
                    try:
                        log(f"{_rid}\tсбой проверки: {type(e).__name__}: {str(e)[:120]} — проверю снова")
                    except Exception:
                        pass
                continue
        if all_ok and _win is not None:                  # по частям: окно проверено — ждём сборку части (она может вернуть
            last_work = time.time()                      # пустую запись на переозвучку); собрана — выйдем в начале круга
            time.sleep(5)
            continue
        if all_ok:
            print(f"[проверка] вся книга проверена: {checked} реплик за этот запуск, переозвучено {rejected}, "
                  f"под вопросом {accepted_best} (work/asr_suspicious.txt)", flush=True)
            HEART.unlink(missing_ok=True)
            os._exit(0)
        if time.time() - last_work > IDLE_EXIT_MIN * 60:
            print(f"[проверка] {IDLE_EXIT_MIN} минут нечего проверять (озвучка остановлена?) — выхожу; "
                  f"проверено {checked}, переозвучено {rejected}", flush=True)
            HEART.unlink(missing_ok=True)
            os._exit(0)
        time.sleep(15 if did == 0 else 1)


if __name__ == "__main__":
    main()
