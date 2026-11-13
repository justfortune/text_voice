#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# NOX_KIT 2026-11-12a
"""
nox_fast.py — быстрый воркер озвучки для nox_audiobook на движке faster-qwen3-tts (CUDA-графы).

Чем отличается от `nox_audiobook.py synth`: та же модель Qwen3-TTS-1.7B-Base, те же голоса, тот же
wav-кэш по хешу текста, но каждый шаг генерации выполняется одним воспроизведением CUDA-графа вместо
сотен мелких запусков ядер из Python. На Windows (WDDM) именно запуск ядер и был узким местом:
RTF 2–4 при загрузке GPU 3–5 %. Батчи не нужны — воркер работает по одной реплике, в порядке книги.

Запускается из окружения envfast (torch + faster-qwen3-tts + qwen-tts-hf/transformers 5), лежит рядом
с nox_audiobook.py и использует его функции (очередь, замки, кэш, словарь ударений, паузы).
Несколько воркеров уживаются через те же .lock-замки. Ctrl+C безопасен.

  python nox_fast.py --worker 1                 # основной воркер
  python nox_fast.py --worker 2 --mem-fraction 0.47   # второй, с лимитом VRAM
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import nox_audiobook as n  # noqa: E402  (ставит HF_HOME на D:\nox_audiobook\hf)

RE_LETTERS = re.compile(r"[^А-Яа-яЁё]")


def abrupt_end(x, sr) -> bool:
    """Обрыв: последние 80 мс почти так же громки, как вся фраза (нет затухания) — запись оборвана."""
    import numpy as np
    k = int(sr * 0.08)
    if len(x) < 4 * k:
        return False
    tail = float(np.sqrt(np.mean(np.square(x[-k:]))))
    body = float(np.sqrt(np.mean(np.square(x))))
    return body > 1e-4 and tail > 0.5 * body
RATE_MAX = 14.8     # букв/с на фразах от 60 букв: быстрее — почти наверняка проглочены слова (норма 11-13.5)

# ---------------------------------------------------------------- СПОСОБ ОЗВУЧКИ (общий с опытом probe_k1a.py)
# Опыт на видеокарте вызывает ЭТИ ЖЕ функции — проверяется ровно то, чем потом озвучивается книга.
REF_SILENCE = 0.5   # с тишины в конце образца голоса. Так задумано в самой библиотеке (faster_qwen3_tts,
#   _load_ref_audio_with_silence): подсказка модели кончается последними звуками образца, и первый звук новой фразы
#   «продолжает» их — отсюда «тык» в начале каждой записи. С тишиной в конце образца продолжать нечего.
SENT_SPLIT = re.compile(r"(?<=[.!?…])\s+(?=[«\"“(—–-]?\s*[А-ЯЁA-Z])")
SENT_MIN_LETTERS = 6    # по предложениям: кусок короче (междометие «Да.», «Ну?») озвучивается вместе с соседним


def padded_ref(wav_p, secs=REF_SILENCE, cache_dir=None):
    """Образец голоса + secs тишины в конце -> путь к wav (кэш в work/ref_pad; при сбое — исходный образец)."""
    wav_p = Path(wav_p)
    if not secs or secs <= 0:
        return str(wav_p)
    try:
        import numpy as np
        import soundfile as sf
        d = Path(cache_dir) if cache_dir else n.WORK / "ref_pad"
        st_ = wav_p.stat()                                  # имя кэша — по версии образца: сменили голос роли — новый кэш
        out = d / f"{wav_p.stem}__{st_.st_size:x}_{st_.st_mtime_ns & 0xffffffffff:x}__sil{int(round(secs * 1000))}.wav"
        if out.exists():
            return str(out)
        a, sr0 = sf.read(str(wav_p), dtype="float32", always_2d=False)
        if a.ndim > 1:
            a = a.mean(axis=1)
        a = np.concatenate([a, np.zeros(int(sr0 * secs), dtype="float32")])
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f"{out.stem}.{os.getpid()}.tmp.wav"
        sf.write(str(tmp), a, sr0, subtype="PCM_16")
        os.replace(tmp, out)
        return str(out)
    except Exception as e:
        print(f"  образец {wav_p.name}: тишина не добавлена ({type(e).__name__}: {e}) — беру как есть", flush=True)
        return str(wav_p)


def sent_pieces(text: str, min_letters: int = SENT_MIN_LETTERS) -> list:
    """Текст -> предложения; короткое (<min_letters букв) склеивается с соседом, чтобы не озвучивать «Да.» отдельно."""
    parts = [p.strip() for p in SENT_SPLIT.split(text) if p.strip()]
    out = []
    for p in parts:
        if out and len(RE_LETTERS.sub("", out[-1])) < min_letters:
            out[-1] = out[-1] + " " + p
        else:
            out.append(p)
    if len(out) > 1 and len(RE_LETTERS.sub("", out[-1])) < min_letters:
        last = out.pop()
        out[-1] = out[-1] + " " + last
    return out or [text]


# Модель озвучки закреплена на проверенной версии: если автор модели выложит новую, озвучка книги посреди работы
# не начнёт звучать иначе. Грузится прямо из папки этой версии в кэше (сеть не нужна); нет папки — как раньше.
MODEL_PIN = {"shib79/qwen3-tts-12hz-1.7b-ru-stress-cf": "66338b83519a05e3d4f923e0f9b4cd2ce686f138"}


def pinned_model(model_id: str) -> str:
    rev = MODEL_PIN.get(model_id)
    if not rev:
        return model_id
    hub = os.environ.get("HF_HUB_CACHE") or os.path.join(
        os.environ.get("HF_HOME") or os.path.join(os.path.expanduser("~"), ".cache", "huggingface"), "hub")
    p = Path(hub) / ("models--" + model_id.replace("/", "--")) / "snapshots" / rev
    return str(p) if (p / "config.json").exists() and (p / "model.safetensors").exists() else model_id


def load_tts(model_id: str, max_seq_len: int = 2048):
    """FasterQwen3TTS закреплённой версии; не загрузилась из папки — по имени модели (как раньше)."""
    from faster_qwen3_tts import FasterQwen3TTS
    src = pinned_model(model_id)
    if src != model_id:
        try:
            m = FasterQwen3TTS.from_pretrained(src, max_seq_len=max_seq_len)
            print(f"  модель: закреплённая версия {MODEL_PIN[model_id][:7]}", flush=True)
            return m
        except Exception as e:
            print(f"  закреплённая версия не загрузилась ({type(e).__name__}: {str(e)[:120]}) — гружу по имени", flush=True)
    return FasterQwen3TTS.from_pretrained(model_id, max_seq_len=max_seq_len)


_NONSTREAM_OK = [True]
_MINTOK_OK = [True]
# Минимальная длина записи. Движок сам решает, когда речь кончилась («конец» — такой же кадр, как звук), и библиотека
# запрещает ему «конец» только на первых двух кадрах (0,16 с). На очень коротких репликах («Наконец-то.», «Иди ко мне.»)
# модель иногда ставит «конец» сразу после них: текст в подсказке целиком лёг на запись образца, и ей кажется, что всё
# уже сказано. Выходила запись в 0,2 с без единого звука. Теперь «конец» запрещён, пока не выдано кадров на
# 0,6 × число букв: самая быстрая речь — 15 букв/с, это 0,83 кадра на букву (кадров 12,5 в секунду), так что обычные
# записи это не меняет — меняются только те, что обрывались, не начавшись.
MIN_TOK_PER_LETTER = 0.6
MIN_TOK_CAP = 48


def min_tokens(text: str) -> int:
    """Сколько кадров движок обязан выдать, прежде чем ему разрешён «конец речи» (2 — как в библиотеке)."""
    letters = len(RE_LETTERS.sub("", text))
    return max(2, min(MIN_TOK_CAP, int(letters * MIN_TOK_PER_LETTER)))


def gen_once(model, text, prompt, cap, nonstream=False):
    """Один вызов движка -> (wav, sr). nonstream: весь текст модели сразу (если версия библиотеки умеет)."""
    kw = dict(text=text, language="Russian", voice_clone_prompt=prompt, max_new_tokens=cap)
    mt = min_tokens(text)
    if mt > 2 and _MINTOK_OK[0]:
        kw["min_new_tokens"] = max(2, min(mt, int(cap) - 1))
    if nonstream and _NONSTREAM_OK[0]:
        kw["non_streaming_mode"] = True
    for _ in range(2):                                   # библиотека постарше может не знать одного из двух ключей
        try:
            wavs, sr = model.generate_voice_clone(**kw)
            return wavs[0], sr
        except TypeError as e:
            s = str(e)
            if "non_streaming_mode" in s and "non_streaming_mode" in kw:
                _NONSTREAM_OK[0] = False
                kw.pop("non_streaming_mode")
                print("  (эта версия движка не умеет «весь текст сразу» — работаю как обычно)", flush=True)
            elif "min_new_tokens" in s and "min_new_tokens" in kw:
                _MINTOK_OK[0] = False
                kw.pop("min_new_tokens")
                print("  (эта версия движка не умеет «минимальную длину записи» — короткие реплики защищает только "
                      "переозвучка)", flush=True)
            else:
                raise
    wavs, sr = model.generate_voice_clone(**kw)
    return wavs[0], sr


# ---------------------------------------------------------------- СТОРОЖ ГОЛОСА: тот ли голос прозвучал
# Движок изредка «съезжает» на чужой голос (у рассказчика вдруг женский). Каждый кусок (предложение) сверяется с
# образцом голоса своей роли по двум признакам:
#  1) высота голоса (медиана частоты основного тона): выше или ниже образца больше чем в 1,5 раза (~7 полутонов) —
#     чужой голос. Свой голос от фразы к фразе гуляет в пределах ±30 % (рассказчик 126–157 Гц), женский — 180–250 Гц;
#  2) «отпечаток голоса» тем же датчиком, которым движок снимает образец (speaker encoder модели): сходство ниже 0,30.
# Чужой кусок озвучивается заново (до 2 раз), берётся самый похожий. Если у роли чужими выходят больше трети кусков —
# дело не в сбое, а в образце: сторож для этой роли умолкает и пишет об этом (переделывать всё подряд бессмысленно).
# Те же функции использует проверка verify_k1a.py (по высоте голоса — она работает без видеокарты).
VOICE_TOL = 1.5
VOICE_COS_MIN = 0.30
VOICE_RETRY = 2
VOICED_MIN = 40          # звонких кадров по 10 мс (0,4 с): меньше — по высоте не судим
COS_MIN_SEC = 1.2        # отпечаток голоса — с кусков не короче 1,2 с
ROLE_TRIP = (20, 0.35)   # опора роли: образец, а с 8 кусков — медиана её кусков (у клона голос бывает выше/ниже
#                          образца, как у Джека); против медианы чужими больше 35 % из 20+ -> сторож роли умолкает
SPEECH_MIN_S, SPEECH_PER_LETTER = 0.2, 0.03   # меньше речи в записи — «нет речи» (короткие реплики пропадали)
_F0_OK = [True]
_SPK_OK = [True]
_REF_F0 = {}


def voice_track(x, sr):
    """-> (частоты основного тона по кадрам 10 мс, маска звонких громких кадров) или None (нет librosa)."""
    if not _F0_OK[0]:
        return None
    try:
        import numpy as np
        import librosa
        from math import gcd
        from scipy.signal import resample_poly
    except Exception as e:
        _F0_OK[0] = False
        print(f"  (высота голоса не измеряется: {type(e).__name__}: {str(e)[:80]})", flush=True)
        return None
    y = np.asarray(x, dtype="float32")
    if y.ndim > 1:
        y = y.mean(axis=1)
    if sr != 16000:
        g = gcd(16000, int(sr))
        y = resample_poly(y, 16000 // g, int(sr) // g).astype("float32")
    if len(y) < 4800:
        return None
    f0 = librosa.yin(y, fmin=60, fmax=500, sr=16000, frame_length=1024, hop_length=160)
    rms = librosa.feature.rms(y=y, frame_length=1024, hop_length=160)[0]
    k = min(len(f0), len(rms))
    f0, rms = f0[:k], rms[:k]
    ok = (rms > 0.15 * float(np.percentile(rms, 95))) & (f0 > 62) & (f0 < 490)
    return f0, ok


def median_f0(x, sr):
    """Медиана высоты голоса (Гц) по звонким кадрам; коротко/нет звонких — None."""
    import numpy as np
    tr = voice_track(x, sr)
    if tr is None or int(tr[1].sum()) < VOICED_MIN:
        return None
    return float(np.median(tr[0][tr[1]]))


def ref_f0(wav_p):
    """Высота голоса образца роли (кэш по файлу: сменился образец — пересчёт)."""
    p = Path(wav_p)
    try:
        st_ = p.stat()
    except OSError:
        return None
    key = (str(p), st_.st_size, st_.st_mtime_ns)
    if key not in _REF_F0:
        try:
            import soundfile as sf
            a, s0 = sf.read(str(p), dtype="float32", always_2d=False)
            _REF_F0[key] = median_f0(a, s0)
        except Exception:
            _REF_F0[key] = None
    return _REF_F0[key]


def prompt_emb(prompt):
    """Отпечаток образца, который движок снял при подготовке голоса (ref_spk_embedding) или None."""
    try:
        it = prompt[0] if isinstance(prompt, (list, tuple)) else prompt
        e = getattr(it, "ref_spk_embedding", None)
        if e is None and isinstance(it, dict):
            e = it.get("ref_spk_embedding")
            e = e[0] if isinstance(e, (list, tuple)) else e
        return e
    except Exception:
        return None


def spk_cos(model, x, sr, ref_emb):
    """Сходство отпечатка голоса куска с образцом (-1..1) датчиком самого движка; недоступно — None."""
    if ref_emb is None or not _SPK_OK[0] or len(x) < sr * COS_MIN_SEC:
        return None
    try:
        import numpy as np
        import torch
        core = model.model.model                     # FasterQwen3TTS -> Qwen3TTSModel -> ядро со speaker_encoder
        ssr = int(getattr(core, "speaker_encoder_sample_rate", 24000) or 24000)
        y = np.ascontiguousarray(x, dtype="float32")
        if int(sr) != ssr:
            from math import gcd
            from scipy.signal import resample_poly
            g = gcd(ssr, int(sr))
            y = np.ascontiguousarray(resample_poly(y, ssr // g, int(sr) // g), dtype="float32")
        e = core.extract_speaker_embedding(audio=y, sr=ssr)
        a = e.float().flatten()
        b = ref_emb.to(a.device).float().flatten()
        return float(torch.nn.functional.cosine_similarity(a, b, dim=0).item())
    except Exception as ex:
        _SPK_OK[0] = False
        print(f"  (отпечаток голоса недоступен: {type(ex).__name__}: {str(ex)[:100]} — сверяю по высоте голоса)",
              flush=True)
        return None


def voice_why(ratio, cos=None, tol=None, cos_min=None):
    """Замечания «чужой голос» по высоте (отношение к образцу) и сходству отпечатка."""
    tol = VOICE_TOL if tol is None else tol
    cos_min = VOICE_COS_MIN if cos_min is None else cos_min
    why = []
    if ratio is not None and (ratio > tol or ratio < 1.0 / tol):
        why.append(f"чужой голос: высота ×{ratio:.2f} к образцу")
    if cos is not None and cos < cos_min:
        why.append(f"чужой голос: сходство {cos:.2f}")
    return why


_ROLE_STAT = {}


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


VOWELS_RE = re.compile(r"[аеёиоуыэюяАЕЁИОУЫЭЮЯ]")


def voiced_need(vowels: int) -> float:
    """Сколько секунд голоса нужно реплике (те же пороги, что у проверки распознаванием)."""
    return max(0.06, 0.025 * vowels)


def role_ref(role, ref_hz):
    """Опорная высота роли: пока кусков мало — образец; от 20 кусков — медиана самих кусков роли (у клона голос бывает
    чуть выше/ниже образца; редкие «съехавшие» куски на медиану не влияют)."""
    import numpy as np
    st_ = _ROLE_STAT.get(role) if role is not None else None
    f = st_.get("f0") if st_ else None
    return float(np.median(f[-300:])) if f and len(f) >= 8 else ref_hz


def role_guard_on(role):
    st_ = _ROLE_STAT.get(role)
    return not (st_ and st_.get("off"))


def role_guard_note(role, alien_first, f0=None):
    """Учёт по роли: чужими с первого раза выходит слишком много — сторож для роли умолкает (виноват образец)."""
    st_ = _ROLE_STAT.setdefault(role, {"n": 0, "bad": 0, "off": False, "f0": []})
    adaptive = len(st_["f0"]) >= 8                 # опора уже медиана кусков роли — считаем только против неё
    if f0:
        st_["f0"].append(f0)                       # все первые попытки: медиана — настоящий голос клона
    if adaptive:
        st_["n"] += 1
        st_["bad"] += int(bool(alien_first))
    if not st_["off"] and st_["n"] >= 20 and st_["bad"] / st_["n"] > ROLE_TRIP[1]:
        st_["off"] = True
        print(f"  !! роль «{role}»: чужим голосом выходит {st_['bad']} из {st_['n']} предложений — похоже, образец голоса "
              f"не похож на её речь; сторож голоса для неё выключен. Послушайте образец (work\\voices).", flush=True)
        try:
            with (n.WORK / "voice_trip.txt").open("a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M')}\t{role}\t{st_['bad']}/{st_['n']}\n")
        except OSError:
            pass


# ---------------------------------------------------------------- УМНАЯ ПЕРЕОЗВУЧКА: каждая следующая попытка — иначе
# Проверка распознаванием отбраковала запись -> воркер делает её заново, но НЕ тем же способом, а с поправкой по причине:
#   нет/мало речи, нет голоса, не распознано, короткая реплика -> конец «Наконец-то,» читается как «Наконец-то.»
#       (запятая в конце заставляла движок ждать продолжения — выходила тишина); третья запись короткой «немой»
#       реплики — голос по «отпечатку» образца (x-vector) с первой попытки, вперемежку с обычным;
#   лишнее (запись длиннее текста) -> предел длины туже;  слитно (нет паузы между предложениями) -> каждое отдельно;
#   прочее (недочитано, чужой голос) -> другой режим движка (весь текст сразу / потоком).
# Третья запись — последняя: проверка берёт лучшую из трёх и отпускает реплику (всё в work/asr_suspicious.txt).
_END_RE = re.compile(r"[\s,;:\u2014\u2013\-]+$")
_SPEECH_WHY = re.compile(r"нет речи|мало речи|нет голоса|не распознано|не дочитан|пропуск|услышано|сборка: ")
_ABSENT_WHY = re.compile(r"нет речи|мало речи|нет голоса|не услышано|сборка: ")     # речи/голоса нет вовсе


def end_dot(t: str) -> str:
    """«Наконе́ц то,» -> «Наконе́ц то.»; есть точка/!/?/… — без изменений."""
    t2 = _END_RE.sub("", t.rstrip())
    if not t2:
        return t
    return t2 if t2[-1] in ".!?\u2026" else t2 + "."


def redo_plan(p, letters: int) -> dict:
    """Как переозвучивать запись, которую отбраковала проверка (по последней причине из <ключ>.rejN.json)."""
    import json
    rej = sorted(p.parent.glob(p.stem + ".rej*.json"))
    if not rej:
        return {}
    k = len(rej)
    try:
        why = str(json.loads(rej[-1].read_text(encoding="utf-8")).get("why", ""))
    except Exception:
        why = ""
    plan = {"k": k, "why": why[:120], "flip": k % 2 == 1}
    speech = bool(_SPEECH_WHY.search(why))
    if speech or letters < 12:
        plan["end_dot"] = True
    if k >= 2 and letters < 20 and _ABSENT_WHY.search(why):
        plan["xvec"] = True                      # последняя запись короткой «немой» реплики: голос по отпечатку образца
    if "лишнее" in why:
        plan["cap"] = 0.75
    if "паузы между" in why:
        plan["min_letters"] = 1
    return plan


def synth_text(model, text, prompt, cap, nonstream=False, suffix="", split=False, pause_s=0.35,
               min_letters=SENT_MIN_LETTERS, ref_hz=None, role=None, info=None):
    """Озвучить готовый (с ударениями) текст -> (запись без тишины по краям, sr, сырая длина с, предел упёрт?,
    вставлено тишины между предложениями с).
    split: каждое предложение отдельно, между ними pause_s тишины (паузу ставит программа, а не движок).
    ref_hz: высота голоса образца роли — включает сторож голоса (чужой кусок переозвучивается, см. выше);
    info (dict): сюда пишется, что нашёл сторож: voice — по кускам, alien — сколько кусков так и остались чужими."""
    import math
    import numpy as np
    pieces = sent_pieces(text, min_letters) if split else [text]
    out, sr, raw, hit = [], 24000, 0.0, False
    grow = max(1.0, cap / (len(text) * 2.4 + 40))          # при повторе после «лимита» предел растёт и у кусков
    guard = ref_hz is not None and (role is None or role_guard_on(role))
    ref_eff = role_ref(role, ref_hz) if guard else None
    emb = prompt_emb(prompt) if guard else None
    vlog, alien = [], 0
    for i, p in enumerate(pieces):
        c = cap if len(pieces) == 1 else int(min(cap, (len(p) * 2.4 + 40) * grow))
        best = None
        for k in range(1 + (VOICE_RETRY if guard else 0)):
            w, sr = gen_once(model, p.rstrip() + suffix, prompt, c, nonstream)
            h = len(w) / sr >= 0.97 * c / n.FRAME_RATE
            x = np.asarray(n.trim_silence(w, sr), dtype="float32")
            ratio = cos = None
            why = []
            if guard:
                f = median_f0(x, sr)
                ratio = f / ref_eff if f and ref_eff else None
                cos = spk_cos(model, x, sr, emb)
                why = voice_why(ratio, cos)
                if k == 0 and role is not None and (ratio is not None or cos is not None):
                    role_guard_note(role, bool(why), f)
            dev = abs(math.log(ratio)) if ratio else 0.0
            score = (len(why), -(cos if cos is not None else 1.0), dev)
            if best is None or score < best[0]:
                best = (score, x, len(w) / sr, h, ratio, cos, why, k + 1)
            if not why:
                break
        _, x, rw, h, ratio, cos, why, tries = best
        raw += rw
        hit = hit or h
        if guard and (why or tries > 1 or ratio is not None):
            vlog.append({"i": i, "f0r": None if ratio is None else round(ratio, 2),
                         "cos": None if cos is None else round(cos, 2), "tries": tries, "why": why})
        alien += int(bool(why))
        if i:
            out.append(np.zeros(int(sr * pause_s), dtype="float32"))
        out.append(x)
    if info is not None:
        info["voice"] = vlog
        info["alien"] = alien
        info["retried"] = sum(1 for v in vlog if v["tries"] > 1)
    return (np.concatenate(out) if out else np.zeros(1, "float32")), sr, raw, hit, pause_s * (len(pieces) - 1)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--worker", type=int, default=1, help="номер воркера (для подписи строк)")
    ap.add_argument("--model", default="shib79/qwen3-tts-12hz-1.7b-ru-stress-cf",
                    help="ru-stress: Qwen3-TTS 1.7B Base, дообученный читать знак ударения U+0301; базовая: Qwen/Qwen3-TTS-12Hz-1.7B-Base")
    ap.add_argument("--max-new-tokens", type=int, default=1500)
    ap.add_argument("--max-seq-len", type=int, default=2048)
    ap.add_argument("--xvec", action="store_true",
                    help="клонировать только тембр (быстрее и короче префилл, но интонация чуть менее похожа на образец)")
    ap.add_argument("--mem-fraction", type=float, default=0.0,
                    help="доля VRAM этому процессу (0 = без лимита); при нескольких воркерах ~1/N-0.03")
    ap.add_argument("--chapter", type=int, nargs="*", help="только эти главы (номера из CSV)")
    ap.add_argument("--reverse", action="store_true",
                    help="идти с конца книги (по умолчанию все воркеры идут с начала: ранние части собираются первыми)")
    ap.add_argument("--accent", default="acute", choices=["none", "plus", "acute", "sparse", "dbl"],
                    help="acute: ruaccent+словарь -> знак ударения (для ru-stress модели); none: чистый текст (для базовой); dbl: удвоение (даёт искажения)")
    ap.add_argument("--serve", action="store_true",
                    help="режим редактора «Нокс Студия»: книгу не озвучивать, только заявки на переозвучку фраз "
                         "(work/studio_queue); выходит сам, когда редактор закрыт")
    args = ap.parse_args()
    tag = f"[f{args.worker}]"

    import torch
    if args.mem_fraction > 0:
        try:
            torch.cuda.set_per_process_memory_fraction(min(args.mem_fraction, 1.0), 0)
        except Exception as e:
            print(f"{tag} лимит VRAM не установлен ({e}) — продолжаю без него", flush=True)
    logging.getLogger("faster_qwen3_tts").setLevel(logging.WARNING)

    rows = n.load_rows()
    roles = n.load_roles()
    n.WAVS.mkdir(parents=True, exist_ok=True)
    # «Настройки озвучки.xlsx» -> work/settings.json: попыток при обрыве и порог «слишком быстро»
    global RATE_MAX
    st = getattr(n, "SETTINGS", None)
    if st is None:
        try:
            import json
            st = json.loads((n.WORK / "settings.json").read_text(encoding="utf-8"))
        except Exception:
            st = {}
    attempts = max(1, min(int(st.get("attempts", 3) or 3), 6))
    tail_max = float(st.get("tail_db", -16) or -16)          # конец громче этого (дБ к речи) — обрублен, переделать
    cut_db = getattr(n, "cut_tail_db", None)
    vcut = getattr(n, "vowel_cut_ms", None)                  # гласная на конце оборвана («Возьмит…»)
    vcut_min = float(st.get("vowel_cut_ms", 20) or 0)
    RATE_MAX = float(st.get("rate_max", RATE_MAX) or RATE_MAX)
    # способ озвучки (work/settings.json; по умолчанию — проверенное: образец с тишиной, остальное как раньше)
    ref_sil = st.get("ref_silence")
    ref_sil = REF_SILENCE if ref_sil is None else float(ref_sil)
    nonstream = bool(st.get("nonstream", False))
    tail_sfx = str(st.get("tail_suffix", "") or "")
    split_sent = bool(st.get("split_sent", True))      # по опыту 05.11 (способ E): каждое предложение отдельно
    split_min = int(st.get("split_min", SENT_MIN_LETTERS) or SENT_MIN_LETTERS)
    vguard = bool(st.get("voice_guard", True))         # сторож голоса (см. «СТОРОЖ ГОЛОСА» выше)
    _pz = st.get("pause") or {}
    sent_pause = float(_pz.get("sentence", 420)) / 1000.0 * float(st.get("pauses", 1.0) or 1.0)
    method = f"ref+{ref_sil:g}s" + ("|nonstream" if nonstream else "") + (f"|sfx{tail_sfx.strip()}" if tail_sfx else "") \
        + (f"|split{split_min}" if split_sent else "")
    print(f"{tag} способ: образец голоса +{ref_sil:.1f} с тишины"
          + ("; весь текст сразу" if nonstream else "") + (f"; хвост «{tail_sfx.strip()}»" if tail_sfx else "")
          + (f"; по предложениям, пауза {sent_pause:.2f} с" if split_sent else "")
          + ("; сторож голоса включён" if vguard else ""), flush=True)
    if args.chapter:
        rows = [r for r in rows if int(r["chapter"]) in args.chapter]
    total_all = len(rows)
    # части, закреплённые автором в студии, конвейер не трогает (ни в каком режиме)
    try:
        fxs = n.fixed_set(rows) if hasattr(n, "fixed_set") and not args.chapter else set()
    except Exception as e:
        print(f"{tag} список закреплённых частей не прочитан ({type(e).__name__}) — считаю, что их нет", flush=True)
        fxs = set()
    BP = bool(hasattr(n, "by_parts") and n.by_parts() and not args.chapter and not args.reverse and not args.serve)
    todo = [r for i, r in enumerate(rows) if i not in fxs and not n.wav_for(r, roles).exists()]
    hb = n.WORK / "asr_active.txt"

    def asr_alive() -> bool:
        # проверка распознаванием (verify_k1a.py) работает: её сердцебиение не старше 10 минут
        if not st.get("asr_check"):
            return False
        try:
            return time.time() - hb.stat().st_mtime < 600
        except OSError:
            return False

    if not todo and not asr_alive() and not args.serve:
        print(tag, "все сегменты уже озвучены")
        return
    order = [r for i, r in enumerate(rows) if i not in fxs]
    if args.reverse:
        order.reverse()
    print(f"{tag} " + (f"режим «по частям»: работаю только над первой незакреплённой частью и после неё останавливаюсь "
                       f"(во всей книге не озвучено {len(todo)} из {total_all})" if BP else f"осталось {len(todo)}/{total_all} сегментов")
          + (f" (закреплено автором и не трогается: {len(fxs)})" if fxs else "")
          + ("; проверка распознаванием включена — отбракованное переозвучиваю сам" if asr_alive() else ""), flush=True)

    state = {"model": None}
    stems = n.voice_stems(roles)
    prompts = {}
    roles_sig = [None]

    def roles_fresh():
        """В студии роли могли дать другой голос (work/roles.json и образец в work/voices) — перечитать, если изменилось."""
        nonlocal roles, stems
        try:
            sg = n.ROLES_PATH.stat().st_mtime_ns
        except OSError:
            return
        if roles_sig[0] is None:
            roles_sig[0] = sg
        elif sg != roles_sig[0]:
            try:
                roles, stems = n.load_roles(), None
                stems = n.voice_stems(roles)
                roles_sig[0] = sg
                print(f"{tag} роли книги изменились — перечитал", flush=True)
            except Exception as e:
                print(f"{tag} роли не перечитаны ({type(e).__name__}) — работаю с прежними", flush=True)

    def voice_key(role):
        wav_p, txt_p = n.voice_files(role, stems)
        try:
            a = wav_p.stat()
            return (str(wav_p), a.st_size, a.st_mtime_ns, txt_p.stat().st_mtime_ns if txt_p.exists() else 0)
        except OSError:
            return None

    def fresh_vsig(role):
        """Отпечаток образца голоса роли по его нынешнему файлу. У конвейера отпечаток 20 секунд берётся из памяти: сразу после
        смены голоса в студии дубль читался уже новым образцом, а помечался прежним."""
        if not hasattr(n, "voice_sig"):
            return None
        c, k = getattr(n, "_VSIG", {}).get(role), voice_key(role)
        if c is not None and (k is None or tuple(c[1] or ()) != tuple(k[1:])):
            n._VSIG.pop(role, None)
        return n.voice_sig(role, roles)

    def get_model():
        # модель грузится только когда реально есть что озвучивать (воркер, ждущий отбраковку, VRAM не занимает)
        if state["model"] is None:
            print(f"{tag} загружаю модель и снимаю CUDA-графы (~1 мин)…", flush=True)
            t_load = time.time()
            m = load_tts(args.model, args.max_seq_len)
            m.warmup(prefill_len=100)
            print(f"{tag} модель и графы готовы за {time.time() - t_load:.0f} с", flush=True)
            state["model"] = m
        return state["model"]

    def prompt_for(role):
        key = voice_key(role)                           # образец роли сменился — подсказка голоса строится заново
        if role not in prompts or prompts[role][0] != key:
            wav_p, txt_p = n.voice_files(role, stems)
            if not wav_p.exists():
                raise FileNotFoundError(f"нет образца голоса {wav_p} — сначала cast")
            ref_text = txt_p.read_text(encoding="utf-8").strip() if txt_p.exists() else ""
            if role in prompts:
                print(f"{tag} у роли «{role}» новый образец голоса — дальше озвучиваю им", flush=True)
            prompts[role] = (key, get_model().model.create_voice_clone_prompt(
                ref_audio=padded_ref(wav_p, ref_sil), ref_text=ref_text,
                x_vector_only_mode=args.xvec or not ref_text))
            prompts_xv.pop(role, None)
            refhz.pop(role, None)
        return prompts[role][1]

    prompts_xv = {}

    def prompt_xv(role):
        # голос по «отпечатку» образца (x-vector), без его записи — запасной путь для коротких реплик, где выходила тишина
        key = voice_key(role)
        if role not in prompts_xv or prompts_xv[role][0] != key:
            wav_p, _ = n.voice_files(role, stems)
            prompts_xv[role] = (key, get_model().model.create_voice_clone_prompt(
                ref_audio=padded_ref(wav_p, ref_sil), ref_text="", x_vector_only_mode=True))
        return prompts_xv[role][1]

    refhz = {}

    def ref_hz_for(role):
        # высота голоса образца роли (Гц) — для сторожа голоса; нет образца/не измерить — сторож для роли молчит
        if not vguard:
            return None
        if role not in refhz:
            wav_p, _ = n.voice_files(role, stems)
            refhz[role] = ref_f0(wav_p)
            if refhz[role] is None:
                print(f"{tag} роль «{role}»: высоту голоса образца не измерить — сторож голоса для неё не работает",
                      flush=True)
        return refhz[role]

    failed_log = n.WORK / "failed.txt"
    susp_log = n.WORK / "suspicious.txt"
    retry_log = n.WORK / "retries.txt"
    cnt = dict(done=0, skipped=0, fails=0, retries=0, redo=0)
    fail_row = Counter()
    audio_sec, done_chars = [0.0], [0]
    todo_chars = sum(len(r["text"]) for r in todo) or 1
    t0 = time.time()

    def synth_row(r) -> str:
        """Озвучить одну реплику -> "done" | "busy" (занята другим/уже есть) | "fail"."""
        p = n.wav_for(r, roles)
        if p.exists():
            return "busy"
        if not n.claim(p):
            return "busy"
        try:
            if p.exists():
                return "busy"
            redo = any(p.parent.glob(p.stem + ".rej*.wav"))   # проверка распознаванием отбраковала прежнюю запись
            try:
                # подготовка текста (словарь, ruaccent) — ВНУТРИ try: сбой одного сегмента
                # пишется в failed.txt, а не валит воркер на всю ночь
                model = get_model()
                role = n.resolve_role(r["speaker"], roles)
                text0 = n.prepare_text(r["text"], False, args.accent)
                letters = len(RE_LETTERS.sub("", text0))
                vowels = len(VOWELS_RE.findall(text0))
                plan = redo_plan(p, letters) if redo else {}       # умная переозвучка: иначе, чем в прошлый раз
                cap = int(min(args.max_new_tokens, (len(text0) * 2.4 + 40) * plan.get("cap", 1.0)))
                ns0 = (not nonstream) if plan.get("flip") else nonstream
                mlet = int(plan.get("min_letters", split_min))
                speech_fail = False
                # ПРОВЕРЯЛЬЩИК ДОЗАПИСАННОСТИ: фраза генерируется заново (до 3 попыток), если
                #   «лимит» — упёрлась в предел длины (обрезана), «обрыв» — кончается на громком звуке без
                #   затухания (выгружена раньше времени), «быстро» — темп >14.8 букв/с на фразе от 60 букв
                #   (выброшены слова). Берётся лучшая попытка (меньше замечаний, затем медленнее темп).
                #   Сверх этого verify_k1a.py слушает готовую запись распознаванием речи и отбраковывает
                #   недочитанное — тогда реплика приходит сюда снова.
                best, cap_try = None, cap
                for attempt in range(attempts):
                    vinfo = {}
                    _ns = ns0 if (attempt % 2 == 0 or letters >= 40) else (not ns0)   # короткой — иначе
                    _dot = plan.get("end_dot") or (letters < 40 and attempt >= 2 and speech_fail)
                    text = end_dot(text0) if _dot else text0
                    _xv = ((bool(plan.get("xvec")) and attempt % 2 == 0)
                           or (speech_fail and letters < 24 and attempt >= 2)) and not state.get("xv_bad")
                    try:
                        x, sr, raw, hit, ins = synth_text(model, text, prompt_xv(role) if _xv else prompt_for(role),
                                                          cap_try, nonstream=_ns, suffix=tail_sfx, split=split_sent,
                                                          pause_s=sent_pause, min_letters=mlet, ref_hz=ref_hz_for(role),
                                                          role=role, info=vinfo)
                    except Exception as e_xv:
                        if not _xv:
                            raise
                        # запасной путь не сработал на этой машине — не теряем реплику: обычный голос, и больше не пробуем
                        state["xv_bad"] = True
                        print(f"{tag} голос по отпечатку образца недоступен ({type(e_xv).__name__}: {str(e_xv)[:80]}) "
                              f"— дальше без него", flush=True)
                        _xv, vinfo = False, {}
                        x, sr, raw, hit, ins = synth_text(model, text, prompt_for(role), cap_try, nonstream=_ns,
                                                          suffix=tail_sfx, split=split_sent, pause_s=sent_pause,
                                                          min_letters=mlet, ref_hz=ref_hz_for(role), role=role,
                                                          info=vinfo)
                    rate = letters / max(len(x) / sr - ins, 0.05)     # темп по речи, вставленные паузы не в счёт
                    why = []
                    if hit:
                        why.append("лимит")
                        cap_try = int(min(cap_try * 1.6, args.max_new_tokens))
                    if abrupt_end(x, sr):
                        why.append("обрыв")
                    tdb = cut_db(x, sr) if cut_db else -99.0
                    if tdb > tail_max and "обрыв" not in why:
                        why.append(f"обрублен конец {tdb:.0f} дБ")   # запись кончается на звуке, без затухания
                    vc = vcut(x, sr, r["text"]) if vcut else None
                    if vc is not None and vcut_min and vc < vcut_min:
                        why.append(f"оборвана гласная {vc} мс")
                    if letters >= 60 and rate > RATE_MAX:
                        why.append(f"быстро {rate:.1f}")
                    _sp = speech_dur(x, sr)                   # речь вообще есть? (у коротких реплик бывала пустота)
                    if _sp < max(SPEECH_MIN_S, SPEECH_PER_LETTER * letters):
                        why.append(f"нет речи {_sp:.2f} с" if _sp < 0.2 else f"мало речи {_sp:.2f} с на {letters} букв")
                        speech_fail = True
                    elif letters < 40 and len(x) / sr - ins > 1.5 + 0.25 * letters:
                        why.append(f"лишнее {len(x) / sr:.1f} с")
                    elif 4 <= letters < 40 and vowels >= 2:
                        _vo = voiced_s(x, sr)             # звук есть, но голос ли это (гласные), а не шум/дыхание?
                        if _vo < voiced_need(vowels):
                            why.append(f"нет голоса {_vo:.2f} с")
                            speech_fail = True
                    score = (len(why), rate)
                    if best is None or score < best[0]:
                        best = (score, x, raw, why, text)
                        best_meta = dict(method=method, attempt=attempt + 1, raw_s=round(raw, 2), dur_s=round(len(x) / sr, 2),
                                         rate=round(rate, 1), tail_db=tdb, vowel_ms=vc, why=why, role=role,
                                         vsig=n.voice_sig(role, roles) if hasattr(n, "voice_sig") else None,
                                         ref_hz=None if refhz.get(role) is None else round(refhz[role], 1),
                                         voice=vinfo.get("voice"), alien=vinfo.get("alien", 0),
                                         variant={"nonstream": _ns, "end_dot": bool(_dot), "xvec": _xv}, redo=plan or None)
                    if not why:
                        break
                    cnt["retries"] += 1
                    with retry_log.open("a", encoding="utf-8") as f:
                        f.write(f"{r['id']}\tпопытка {attempt + 1}: {', '.join(why)}\t{r['text'][:80]}\n")
                (score, x, raw, why, text) = best
                critical = [w for w in why if w == "лимит" or w == "обрыв"
                            or w.startswith(("обрублен конец", "оборвана гласная", "мало речи", "нет речи", "быстро"))]
                if critical:
                    with failed_log.open("a", encoding="utf-8") as f:
                        f.write(f"{r['id']}\\tнеполная/сомнительная запись после {attempts} попыток: "
                                f"{', '.join(critical)}\\t{r['text'][:80]}\\n")
                    with susp_log.open("a", encoding="utf-8") as f:
                        f.write(f"{r['id']}\\tНЕ ПРИНЯТА: {', '.join(critical)}\\t{r['text'][:80]}\\n")
                    return "fail"                         # не превращаем заведомый обрыв в готовый WAV           # что прочитано в лучшей попытке — то и в .txt для проверки
                if why:
                    with susp_log.open("a", encoding="utf-8") as f:
                        f.write(f"{r['id']}\tпосле {attempts} попыток: {', '.join(why)}\t{r['text'][:80]}\n")
                if best_meta.get("alien"):              # сторож голоса: кусок так и остался чужим (проверка переспросит)
                    with susp_log.open("a", encoding="utf-8") as f:
                        f.write(f"{r['id']}\tчужой голос в {best_meta['alien']} предл. (роль {role})\t{r['text'][:80]}\n")
                try:   # что именно было прочитано (после словаря/ударений) — для проверки и точечной переозвучки
                    p.with_suffix(".txt").write_text(text, encoding="utf-8")
                except Exception:
                    pass
                n.write_wav_atomic(p, x, sr)
                p.with_suffix(".ok").unlink(missing_ok=True)
                p.with_suffix(".gaps").unlink(missing_ok=True)       # границы фраз прежней записи не годятся
                try:   # для диагностики: сколько попыток, темп, затухание конца (diag_k1a.py это показывает)
                    import json as _json
                    best_meta["attempts"] = attempt + 1
                    p.with_suffix(".meta").write_text(_json.dumps(best_meta, ensure_ascii=False), encoding="utf-8")
                except Exception:
                    pass
                audio_sec[0] += len(x) / sr
                done_chars[0] += len(r["text"])
                if redo:
                    cnt["redo"] += 1
                return "done"
            except Exception as e:
                cnt["fails"] += 1
                fail_row[r["id"]] += 1
                try:
                    import torch
                    torch.cuda.empty_cache()
                except Exception:
                    pass
                print(f"{tag} !! сегмент {r['id']}: {type(e).__name__}: {str(e)[:140]}", flush=True)
                with failed_log.open("a", encoding="utf-8") as f:
                    f.write(f"{r['id']}\t{r['text'][:80]}\n")
                return "fail"
        finally:
            n.release(p)

    def report():
        d = cnt["done"]
        if d <= 3 or d % 10 == 0:
            el = time.time() - t0
            rtf = el / max(audio_sec[0], 1e-6)
            eta = max(todo_chars - done_chars[0], 0) * el / max(done_chars[0], 1) / 60
            extra = f" | переозвучено после проверки {cnt['redo']}" if cnt["redo"] else ""
            if d % 50 == 0:
                ready = sum(1 for rr in rows if n.wav_for(rr, roles).exists())
                extra += f" | всего готово {ready}/{total_all}"
            # по частям оценка «осталось» на всю книгу только путает: воркер остановится после текущей части
            tail = "" if BP else f"| этому воркеру осталось ~{eta:.0f} мин"
            print(f"{tag} {d:>5} сегм | аудио {audio_sec[0]/60:6.1f} мин за {el/60:5.1f} мин | RTF={rtf:.2f} "
                  f"{tail}{extra}", flush=True)

    # ------------------------------------------------------------ ЗАЯВКИ РЕДАКТОРА «НОКС СТУДИЯ»: переозвучка на лету
    # Редактор кладёт заявку work/studio_queue/<rid>.req.json: {"role": "Хекубах", "text": "Наконе́ц то.", "out": "…wav",
    # "variant": {"nonstream": bool, "xvec": bool}, "tries": 2, "raw": "исходный текст"}. Текст — уже ТЕХНИЧЕСКИЙ (с
    # ударениями), он читается как есть. Заявку берёт один воркер (атомарное переименование), озвучивает тем же способом и
    # с теми же проверками, что и книгу, пишет запись в out и ответ <rid>.done.json. Заявки идут вне очереди — между
    # репликами книги, так что отдельная модель в видеопамяти редактору не нужна.
    QDIR = n.WORK / "studio_queue"
    alive_f = QDIR / f"alive_w{args.worker}.txt"
    qstat = {"served": 0, "beat": 0.0}

    def take_report(x, sr, text, raw_text, hit, ins, vinfo):
        """Проверки готовой записи (те же, что у озвучки книги) -> (замечания, показатели)."""
        letters = len(RE_LETTERS.sub("", text))
        vowels = len(VOWELS_RE.findall(text))
        rate = letters / max(len(x) / sr - ins, 0.05)
        why = []
        if hit:
            why.append("лимит")
        if abrupt_end(x, sr):
            why.append("обрыв")
        tdb = cut_db(x, sr) if cut_db else -99.0
        if tdb > tail_max and "обрыв" not in why:
            why.append(f"обрублен конец {tdb:.0f} дБ")
        vc = vcut(x, sr, raw_text) if vcut else None
        if vc is not None and vcut_min and vc < vcut_min:
            why.append(f"оборвана гласная {vc} мс")
        if letters >= 60 and rate > RATE_MAX:
            why.append(f"быстро {rate:.1f}")
        sp, vo = speech_dur(x, sr), None
        if sp < max(SPEECH_MIN_S, SPEECH_PER_LETTER * letters):
            why.append(f"нет речи {sp:.2f} с" if sp < 0.2 else f"мало речи {sp:.2f} с на {letters} букв")
        elif letters < 40 and len(x) / sr - ins > 1.5 + 0.25 * letters:
            why.append(f"лишнее {len(x) / sr:.1f} с")
        elif 4 <= letters < 40 and vowels >= 2:
            vo = voiced_s(x, sr)
            if vo < voiced_need(vowels):
                why.append(f"нет голоса {vo:.2f} с")
        if vinfo.get("alien"):
            why.append(f"чужой голос в {vinfo['alien']} предл.")
        return why, dict(rate=round(rate, 1), tail_db=round(float(tdb), 1), vowel_ms=vc, speech_s=round(sp, 2),
                         voiced_s=None if vo is None else round(vo, 2), letters=letters)

    prompts_ref, prompts_ref_xv = {}, {}

    def serve_one(req: dict) -> dict:
        roles_fresh()
        role = str(req.get("role", ""))
        ref = req.get("ref") or None                   # проба голоса: образец указан явно (банк голосов), не из ролей книги
        if ref:
            rw = Path(str(ref.get("wav") or ""))
            if not rw.is_file():
                raise ValueError(f"нет файла образца голоса {rw}")
            st_ = rw.stat()
            rkey = (str(rw), st_.st_size, st_.st_mtime_ns)
            if rkey not in prompts_ref:
                rt = Path(str(ref.get("txt") or rw.with_suffix(".txt")))
                ref_text = rt.read_text(encoding="utf-8").strip() if rt.is_file() else ""
                if len(prompts_ref) >= 12:
                    prompts_ref.clear()
                    prompts_ref_xv.clear()
                prompts_ref[rkey] = get_model().model.create_voice_clone_prompt(
                    ref_audio=padded_ref(rw, ref_sil), ref_text=ref_text, x_vector_only_mode=args.xvec or not ref_text)
        elif role not in roles["roles"]:
            raise ValueError(f"нет роли «{role}» в ролях книги")
        text = str(req.get("text", "")).strip()
        if req.get("prep"):                            # фраза, вставленная автором в студии: ударения ставим здесь, как книге
            try:
                text = n.prepare_text(str(req.get("raw") or text), False, args.accent).strip() or text
            except Exception as e:
                print(f"{tag} текст вставки не подготовлен ({type(e).__name__}: {e}) — читаю как есть", flush=True)
        if not RE_LETTERS.sub("", text):
            raise ValueError("в тексте нет букв")
        var = req.get("variant") or {}
        ns = bool(var["nonstream"]) if "nonstream" in var else nonstream
        xv = bool(var.get("xvec")) and not state.get("xv_bad")
        tries = max(1, min(int(req.get("tries", 2) or 2), 4))
        raw_text = str(req.get("raw") or text.replace("\u0301", ""))
        model = get_model()
        cap = int(min(args.max_new_tokens, len(text) * 2.4 + 40))
        # Что пробуем. Сначала — как просили: tries раз, режимы движка вперемежку. Если речи так и нет (тишина, обрывок,
        # шум без голоса) — «спасательные» попытки: голос по отпечатку образца (x-vector — без записи образца в подсказке
        # модели нечего «продолжать», и короткая реплика начинается с чистого листа), потом то же с точкой на конце
        # («Наконец-то,» -> «Наконец-то.»). Берётся первая запись без замечаний; иначе лучшая, причём запись с речью
        # всегда лучше записи без неё. Заявка может запретить это ключом "rescue": false.
        steps = [dict(ns=ns if a % 2 == 0 else (not ns), xv=xv, dot=False, extra=False) for a in range(tries)]
        if req.get("rescue", True) is not False:
            steps += [dict(ns=ns, xv=True, dot=False, extra=True), dict(ns=not ns, xv=True, dot=False, extra=True),
                      dict(ns=ns, xv=True, dot=False, extra=True),
                      dict(ns=ns, xv=False, dot=True, extra=True), dict(ns=not ns, xv=True, dot=True, extra=True)]
        text_dot = end_dot(text)
        best, done_k = None, 0
        for st_ in steps:
            if st_["extra"] and not (best is not None and _ABSENT_WHY.search(", ".join(best[2]))):
                break                                       # речь есть — спасать нечего
            if st_["xv"] and state.get("xv_bad"):
                continue
            if st_["dot"] and text_dot == text:
                continue                                    # на конце и так точка, ! или ? — такая попытка уже была
            t_use = text_dot if st_["dot"] else text
            vinfo = {}
            try:
                if ref:
                    if st_["xv"] and rkey not in prompts_ref_xv:
                        prompts_ref_xv[rkey] = model.model.create_voice_clone_prompt(
                            ref_audio=padded_ref(rw, ref_sil), ref_text="", x_vector_only_mode=True)
                    x, sr, raw, hit, ins = synth_text(model, t_use, prompts_ref_xv[rkey] if st_["xv"] else prompts_ref[rkey],
                                                      cap, nonstream=st_["ns"], suffix=tail_sfx, split=split_sent and not bool(req.get("atomic")),
                                                      pause_s=sent_pause, min_letters=split_min,
                                                      ref_hz=ref_f0(rw) if vguard else None, role="проба:" + rw.stem,
                                                      info=vinfo)
                else:
                    x, sr, raw, hit, ins = synth_text(model, t_use, prompt_xv(role) if st_["xv"] else prompt_for(role), cap,
                                                      nonstream=st_["ns"], suffix=tail_sfx, split=split_sent and not bool(req.get("atomic")),
                                                      pause_s=sent_pause, min_letters=split_min, ref_hz=ref_hz_for(role),
                                                      role=role, info=vinfo)
            except Exception as e_xv:
                if not st_["xv"] or best is None:
                    raise
                # запасной путь на этой машине не работает — остаёмся с тем, что есть, и больше его не пробуем
                state["xv_bad"] = True
                print(f"{tag} голос по отпечатку образца недоступен ({type(e_xv).__name__}: {str(e_xv)[:80]}) — дальше без него",
                      flush=True)
                continue
            done_k += 1
            why, met = take_report(x, sr, t_use, raw_text, hit, ins, vinfo)
            if hit:
                cap = int(min(cap * 1.6, args.max_new_tokens))
            score = (1 if _ABSENT_WHY.search(", ".join(why)) else 0, len(why))
            if best is None or score < best[0]:
                best = (score, x, why, met, dict(st_), done_k, sr)
            if not why:
                break
        score, x, why, met, st_, att, sr = best
        critical = [w for w in why if w == "лимит" or w == "обрыв"
                    or w.startswith(("обрублен конец", "оборвана гласная", "мало речи", "нет речи", "быстро"))]
        if critical:
            return dict(ok=False, error="запись не принята: " + ", ".join(critical), why=why,
                        attempts=att, tried=done_k, role=role, text=text, worker=args.worker)
        how = ", ".join(w for w, on in (("голос по отпечатку образца", st_["xv"] and not xv),
                                        ("точка на конце", st_["dot"])) if on) if st_["extra"] else ""
        out = Path(str(req["out"]))
        out.parent.mkdir(parents=True, exist_ok=True)
        n.write_wav_atomic(out, x, sr)
        return dict(ok=True, dur=round(len(x) / sr, 3), sr=sr, why=why, attempts=att, tried=done_k, method=method, role=role,
                    text=text, variant={"nonstream": st_["ns"], "xvec": bool(st_["xv"]), "end_dot": bool(st_["dot"])},
                    rescue=how, silent=bool(score[0]), worker=args.worker,
                    vsig=None if ref else fresh_vsig(role), **met)

    def serve_queue() -> int:
        """Обслужить заявки редактора (если есть) -> сколько обслужено. Заодно — сердцебиение «воркер умеет заявки»."""
        if not QDIR.is_dir():
            return 0
        import json
        now = time.time()
        if now - qstat["beat"] > 5:
            qstat["beat"] = now
            try:
                alive_f.write_text(f"{os.getpid()}\t{now:.0f}\t{'serve' if args.serve else 'book'}", encoding="utf-8")
            except OSError:
                pass
        try:
            reqs = sorted(QDIR.glob("*.req.json"))
        except OSError:
            return 0
        k = 0
        for rq in reqs:
            rid = rq.name[:-len(".req.json")]
            run = rq.with_name(f"{rid}.run{args.worker}.json")
            try:
                os.replace(rq, run)                       # атомарно: заявку берёт один воркер
            except OSError:
                continue
            t_ = time.time()
            try:
                res = serve_one(json.loads(run.read_text(encoding="utf-8")))
            except Exception as e:
                res = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
                try:
                    import torch
                    torch.cuda.empty_cache()
                except Exception:
                    pass
            res["rid"] = rid
            res["sec"] = round(time.time() - t_, 2)
            done = rq.with_name(f"{rid}.done.json")
            tmp = rq.with_name(f"{rid}.done.tmp")
            try:
                tmp.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, done)
            except OSError as e:
                print(f"{tag} заявка {rid}: ответ не записан ({type(e).__name__})", flush=True)
            try:
                run.unlink()
            except OSError:
                pass
            k += 1
            qstat["served"] += 1
            print(f"{tag} заявка редактора {rid}: {'готово' if res.get('ok') else 'ошибка: ' + str(res.get('error'))}"
                  + (f" ({', '.join(res['why'])})" if res.get("why") else "")
                  + (f"; попыток {res['tried']}" if int(res.get("tried") or 1) > 1 else "")
                  + (f"; помогло: {res['rescue']}" if res.get("rescue") and not res.get("silent") else "")
                  + (" — РЕЧИ НЕТ" if res.get("silent") else "") + f" за {res['sec']:.1f} с", flush=True)
        return k

    if args.serve:
        QDIR.mkdir(parents=True, exist_ok=True)
        print(f"{tag} режим редактора: жду заявки на переозвучку (work\\studio_queue); книга не озвучивается", flush=True)
        ed, idle = QDIR / "editor.txt", time.time()
        try:
            serve_queue()                                 # сердцебиение — сразу, ещё до загрузки модели
            get_model()                                   # прогрев заранее: первая заявка не ждёт загрузку
            while True:
                if serve_queue():
                    idle = time.time()
                try:
                    ed_age = time.time() - ed.stat().st_mtime
                except OSError:
                    ed_age = 1e9
                if ed_age > 180 and time.time() - idle > 60:
                    print(f"{tag} редактор закрыт — выхожу (обслужено заявок: {qstat['served']})", flush=True)
                    break
                time.sleep(0.3)
        except KeyboardInterrupt:
            pass
        finally:
            try:
                alive_f.unlink()
            except OSError:
                pass
        return

    def stale_drop(r, p):
        """Запись есть, но прочитан в ней прежний текст (изменились словарь, ударения по месту или правила слоя текста) —
        убрать её: реплика озвучится заново. Так аудит всей книги не нужен: проверяется только текущая часть."""
        side = p.with_suffix(".txt")
        try:
            old = side.read_text(encoding="utf-8")
            new = n.prepare_text(r["text"], False, args.accent)
        except Exception:
            return
        if old == new or old == end_dot(new):
            return
        if not n.claim(p):
            return
        try:
            for f in [p, side] + [p.with_suffix(x) for x in (".ok", ".meta", ".gaps")]:
                f.unlink(missing_ok=True)
            print(f"{tag} реплика {r['id']}: текст для озвучки изменился — озвучиваю заново", flush=True)
        finally:
            n.release(p)

    def run_window():
        """РЕЖИМ «ПО ЧАСТЯМ». Работа идёт над одной частью — первой, которую автор ещё не закрепил в студии. Реплики за
        её пределами не озвучиваются. Часть собрана (её собирает nox_part.py) — воркер выходит."""
        shown, idle_t, checked = None, time.time(), set()
        while True:
            serve_queue()
            try:
                w = n.work_window(rows, roles)
            except Exception as e:
                print(f"{tag} окно работы не определить ({type(e).__name__}: {str(e)[:100]}) — через 10 с снова", flush=True)
                time.sleep(10)
                continue
            if w[0] >= len(rows):
                print(f"{tag} все части закреплены автором — озвучивать нечего", flush=True)
                return
            if w[3]:
                print(f"{tag} часть {w[2]} собрана — дальше не озвучиваю: следующая часть пойдёт, когда эта будет "
                      f"закреплена в студии", flush=True)
                return
            if w[:3] != shown:
                shown = w[:3]
                left = sum(1 for r in rows[w[0]:w[1] + 1] if not n.wav_for(r, roles).exists())
                print(f"{tag} часть {w[2]}: реплики № {rows[w[0]]['id']}–{rows[w[1]]['id']} ({w[1] - w[0] + 1} шт.), "
                      f"не озвучено {left}", flush=True)
            did, t_eval = False, time.time()
            for r in rows[w[0]:w[1] + 1]:
                if fail_row[r["id"]] >= 2:
                    continue
                p = n.wav_for(r, roles)
                if p.exists() and p.name not in checked:
                    checked.add(p.name)
                    stale_drop(r, p)                    # тот ли текст прочитан в готовой записи
                if p.exists():
                    continue
                serve_queue()
                res = synth_row(r)
                if res == "done":
                    cnt["done"] += 1
                    did = True
                    report()
                elif res == "fail" and cnt["fails"] >= 5 and cnt["done"] == 0:
                    print(f"{tag} пять ошибок подряд в самом начале — останавливаюсь, посмотрите лог", flush=True)
                    return
                if res == "done" and time.time() - t_eval > 4:   # окно уточняется по настоящим длинам записей
                    break
            if did:
                idle_t = time.time()
                continue
            # реплики, которые этому воркеру не даются (дважды ошибка: нет образца голоса роли и т.п.): часть без них не
            # собрать — не ждать вечно, а выйти с понятным сообщением (go покажет, какие реплики не готовы)
            blocked = [r for r in rows[w[0]:w[1] + 1] if fail_row[r["id"]] >= 2 and not n.wav_for(r, roles).exists()]
            if blocked and time.time() - idle_t > float(os.environ.get("NOX_BLOCK_WAIT") or 180):
                print(f"{tag} часть {w[2]}: не удаётся озвучить реплики № " + ", ".join(str(r["id"]) for r in blocked[:8])
                      + ("…" if len(blocked) > 8 else "") + " — причина выше и в work\\failed.txt. Выхожу: исправьте причину "
                      "(чаще всего у роли нет образца голоса) и запустите go ещё раз", flush=True)
                return
            # всё в окне озвучено: идёт проверка и сборка; отбракованное вернётся сюда как «нет записи»
            if time.time() - idle_t > 30 * 60 and not asr_alive():
                print(f"{tag} часть {w[2]} не собрана за полчаса простоя (сборка nox_part.py не работает?) — выхожу", flush=True)
                return
            for _ in range(10):
                serve_queue()
                time.sleep(0.5)

    def missing_behind(i):
        # реплики позади (уже пройденные), которые проверка отбраковала — их первыми: часть на Диск ждёт именно их
        return [rr for rr in order[:i] if fail_row[rr["id"]] < 2 and not n.wav_for(rr, roles).exists()]

    try:
        if BP:
            run_window()
            order = []                                  # обычный проход по всей книге в этом режиме не делается
        i, last_scan = 0, time.time()
        while i < len(order):
            serve_queue()                               # заявки редактора — вне очереди, между репликами
            if time.time() - last_scan > 15:            # отбракованное в ранних частях — первым (часть ждёт его)
                last_scan = time.time()
                for rr in missing_behind(i):
                    if synth_row(rr) == "done":
                        cnt["done"] += 1
                        report()
            r = order[i]
            i += 1
            if fail_row[r["id"]] >= 2:
                continue
            res = synth_row(r)
            if res == "done":
                cnt["done"] += 1
                report()
            elif res == "fail" and cnt["fails"] >= 5 and cnt["done"] == 0:
                print(f"{tag} пять ошибок подряд в самом начале — останавливаюсь, посмотрите лог", flush=True)
                return
            elif res == "busy" and not n.wav_for(r, roles).exists():
                cnt["skipped"] += 1
        # книга пройдена; пока работает проверка распознаванием — доозвучиваем то, что она отбраковала
        if asr_alive() and not BP:
            print(f"{tag} проход по книге закончен; жду проверку распознаванием и переозвучиваю отбракованное…",
                  flush=True)
        while asr_alive() and not BP:
            did = False
            for rr in missing_behind(len(order)):
                if synth_row(rr) == "done":
                    cnt["done"] += 1
                    did = True
                    report()
            if not did:
                for _ in range(20):                     # 10 с ожидания, заявки редактора — каждые полсекунды
                    serve_queue()
                    time.sleep(0.5)
    except KeyboardInterrupt:
        print(f"\n{tag} прервано — всё готовое сохранено, повторный запуск продолжит с места остановки", flush=True)
        try:
            alive_f.unlink()
        except OSError:
            pass
        return

    el = time.time() - t0
    print(f"{tag} ГОТОВО: озвучено {cnt['done']} (аудио {audio_sec[0]/60:.1f} мин за {el/60:.1f} мин, "
          f"RTF={el/max(audio_sec[0], 1e-6):.2f}), пропущено занятых другими: {cnt['skipped']}, ошибок: {cnt['fails']}, "
          f"перегенераций (лимит/обрыв/быстро): {cnt['retries']}, переозвучено после проверки: {cnt['redo']}", flush=True)
    try:
        alive_f.unlink()                                  # редактор не должен ждать воркер, который закончил
    except OSError:
        pass


if __name__ == "__main__":
    main()
