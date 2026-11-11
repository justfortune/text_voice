#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# NOX_KIT 2026-11-12a
"""
nox_part.py — СБОРКА ТЕКУЩЕЙ ЧАСТИ в режиме «по частям».

Порядок работы над книгой: озвучили одну часть — прослушали и закрепили её в «Нокс Студии» — только тогда конвейер берётся
за следующую. Закреплённые части он больше не трогает, а дальше текущей ничего не озвучивает и не проверяет.

Этот файл заменяет окно «watch» прежнего режима: следит за текущей (первой незакреплённой) частью, и когда все её реплики
озвучены и проверены распознаванием — собирает её (те же правила сборки, что и раньше), кладёт черновик на Яндекс Диск
и выходит. Воркеры озвучки и проверка выходят сами, как только часть собрана.

Запускает go (и кнопка «Озвучить следующую часть» в студии). Ключи:
  --init     записать режим работы (work\\run_mode.json), сверить настройки сборки и записи ТЕКУЩЕЙ части (тот ли текст
             в них прочитан: словарь автора, ударения по месту; цел ли файл) — и выйти. Вся книга не просматривается.
  --mode     только записать режим работы и выйти (go вызывает до подбора голосов: по режиму видно, какие роли нужны)
  --all      вместе с --init/--mode: прежний режим «вся книга сразу» (части собирает окно watch)
  --status   напечатать одной строкой JSON, над какой частью идёт работа, и выйти
  --report   то же понятными словами: сколько закреплено, что с текущей частью, сколько осталось (go -Status)
Настройки сборки (темп, паузы, длина части, формат) берутся из ключей, а если ключа нет — из таблицы настроек
(work\\settings.json), как у go.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent


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


_END_RE = re.compile(r"[\s,;:\u2014\u2013\-]+$")


def end_dot(t: str) -> str:
    """Как у воркера озвучки: хвостовая запятая/тире заменяется точкой (так текст и записан рядом с записью)."""
    t2 = _END_RE.sub("", t.rstrip())
    if not t2:
        return t
    return t2 if t2[-1] in ".!?\u2026" else t2 + "."


def accent_mode(arg, st) -> str:
    if arg:
        return arg
    v = str(st.get("accent") or "acute")
    return "none" if v == "base" else ("sparse" if v == "sparse" else "acute")


def audit_window(n, rows, roles, w, accent) -> int:
    """Сверка записей ТЕКУЩЕЙ части (вместо аудита всей книги): запись убирается и озвучится заново, если в ней прочитан
    не нынешний текст (изменился словарь автора, ударение по месту, правила слоя текста), если рядом нет записи о
    прочитанном тексте или файл не читается. Закреплённые части и всё, что дальше текущей, не просматриваются."""
    cache = n.load_durations()
    seen, drop = set(), []
    for r in rows[w[0]:w[1] + 1]:
        p = n.wav_for(r, roles)
        if p.name in seen or not p.exists():
            continue
        seen.add(p.name)
        side, why = p.with_suffix(".txt"), None
        try:
            old = side.read_text(encoding="utf-8")
        except OSError:
            old = None
        if old is None:
            why = "нет записи о прочитанном тексте"
        else:
            try:
                new = n.prepare_text(r["text"], False, accent)
            except Exception as e:
                print(f"  сверка текста не выполнена ({type(e).__name__}: {str(e)[:120]}) — записи части оставлены как есть",
                      flush=True)
                return 0
            if old != new and old != end_dot(new):
                why = "текст для озвучки изменился"
        if why is None:
            try:
                if n.duration_of(p, cache) < 0.15:
                    why = "пустая запись"
            except Exception:
                why = "файл записи не читается"
        if why and n.claim(p):
            try:
                for f in [p, side] + [p.with_suffix(x) for x in (".ok", ".meta", ".gaps")] + list(p.parent.glob(p.stem + ".rej*")):
                    try:
                        f.unlink()
                    except OSError:
                        pass
                drop.append((r["id"], why))
            finally:
                n.release(p)
    if drop:
        print(f"  сверка записей части {w[2]}: на переозвучку {len(drop)} — "
              + "; ".join(f"№ {i} ({y})" for i, y in drop[:6]) + ("…" if len(drop) > 6 else ""), flush=True)
        try:
            with open(n.WORK / "audit_purged.txt", "a", encoding="utf-8") as f:
                f.write("".join(f"{i}\tчасть {w[2]}\t{y}\t{time.strftime('%Y-%m-%d %H:%M')}\n" for i, y in drop))
        except OSError:
            pass
    return len(drop)


def sync_fixed(n):
    """Список закреплённых частей (studio\\fixed_parts.json) сверяется с правками студии (studio\\edits.json) — они первичны.
    Нужен, когда студия прежней версии закрепляла части, не сообщая конвейеру, и когда рукопись переразбита, а студия
    с тех пор не запускалась (границы частей ищутся по меткам реплик). Расчёт — тот же, что у студии."""
    f = n.FIXED_PATH
    ed = f.parent / "edits.json"
    if not ed.exists():
        return
    try:
        st = json.loads(ed.read_text(encoding="utf-8"))
        rows = n.load_rows()
        uids = n.row_uids(rows)
        pos = {u: i for i, u in enumerate(uids)}
        out, start = [], 0
        for k, p in enumerate(st.get("parts") or [], 1):
            if start >= len(rows):
                break
            b = pos.get(p.get("last"))
            if b is None or b < start:                   # last_p — номер реплики у конвейера (last_i считан с вставками студии)
                lp = p.get("last_p") if isinstance(p.get("last_p"), int) else p.get("last_i", start)
                b = min(max(int(lp or start), start), len(rows) - 1)
            if p.get("locked"):
                out.append(dict(n=k, a=start, b=b, a_uid=uids[start], b_uid=uids[b]))
            start = b + 1
        try:
            old = json.loads(f.read_text(encoding="utf-8")).get("parts")
        except Exception:
            old = None
        if old == out:
            return
        f.parent.mkdir(exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps({"ver": 1, "parts": out, "at": time.strftime("%Y-%m-%d %H:%M:%S"), "by": "nox_part"},
                                  ensure_ascii=False), encoding="utf-8")
        replace_retry(tmp, f)
        print(f"  закреплённые в студии части: {len(out)} — список для конвейера обновлён по правкам студии", flush=True)
    except Exception as e:
        print(f"  правки студии не прочитаны ({type(e).__name__}: {str(e)[:120]}) — список закреплённых частей оставлен как есть",
              flush=True)


def report(n, minutes: float) -> int:
    """Где книга сейчас — понятными словами (go -Status). Ничего не меняет."""
    rows, roles = n.load_rows(), n.load_roles()
    fx = n.fixed_prefix(rows)
    fxs = n.fixed_set(rows, None)
    letters = lambda rr: sum(len(n.RE_RU_LETTERS.sub("", r["text"])) for r in rr)
    all_l = letters(rows) or 1
    done_l = letters([rows[i] for i in sorted(fxs)])
    w = n.work_window(rows, roles)
    print(f"Книга: реплик {len(rows)}. Закреплено частей: {len(fx)}"
          + (f" (реплики № {rows[fx[0][0]]['id']}–{rows[fx[-1][1]]['id']})" if fx else "") + f" — {100 * done_l / all_l:.0f} % текста.")
    if len(fxs) > sum(b - a + 1 for a, b in fx):
        print("  Есть закреплённые части не подряд (перед ними — незакреплённая): конвейер вернётся к первой незакреплённой.")
    if w[0] >= len(rows):
        print("Все части закреплены — книга готова.")
        return 0
    seg = rows[w[0]:w[1] + 1]
    have = sum(1 for r in seg if n.wav_for(r, roles).exists())
    gate = n._asr_gate()
    chk = sum(1 for r in seg if n._seg_ok(r, roles, True)) if gate else None
    f = n.PARTS / f"part_{w[2]:02d}"
    print(f"Текущая часть — {w[2]}: реплики № {rows[w[0]]['id']}–{rows[w[1]]['id']} ({len(seg)} шт.), озвучено {have}"
          + (f", проверено распознаванием {chk}" if chk is not None else "") + ". "
          + ("Собрана — прослушайте и закрепите её в студии." if w[3] else "Ещё не собрана — запустите go."))
    rest_l = letters(rows[w[1] + 1:])
    cur_l = letters(seg) or 1
    est = rest_l / cur_l                                  # сколько ещё таких частей впереди
    print(f"После неё останется примерно {est:.0f} част(ей) по ~{minutes:g} мин — {100 * rest_l / all_l:.0f} % текста.")
    try:
        st = json.loads((n.WORK / "part_status.json").read_text(encoding="utf-8"))
        if st.get("state") == "stalled" and st.get("part") == w[2]:
            print(f"Последний запуск ({st.get('at')}) часть не собрал: озвучено {st.get('have')} из {st.get('total')} реплик — "
                  f"причины в part.log и work\\failed.txt.")
    except Exception:
        pass
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--dest", default="", help="папка на Яндекс Диске для черновика части")
    ap.add_argument("--parts", type=float, default=None, help="длина части, мин")
    ap.add_argument("--tempo", type=float, default=None)
    ap.add_argument("--pauses", type=float, default=None)
    ap.add_argument("--no-trim-pauses", action="store_true")
    ap.add_argument("--no-equalize", action="store_true")
    ap.add_argument("--format", default=None, choices=["mp3", "m4b"])
    ap.add_argument("--bitrate", default=None)
    ap.add_argument("--title", default=None)
    ap.add_argument("--author", default=None)
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--every", type=float, default=15.0, help="период проверки, с")
    ap.add_argument("--accent", default=None, help="режим ударений, как у воркеров: acute | sparse | none")
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--mode", action="store_true", help="только записать режим работы (и длину части) и выйти")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--report", action="store_true", help="где книга сейчас — словами (go -Status); ничего не меняет")
    a = ap.parse_args()

    root = Path(a.root).resolve()
    os.chdir(root)
    sys.path.insert(0, str(root))
    try:
        import nox_audiobook as n
    except Exception as e:
        print(f"не загрузился конвейер книги ({root}\\nox_audiobook.py): {type(e).__name__}: {e}", flush=True)
        return 3
    if int(getattr(n, "NOX_STAGE", 0) or 0) < 21 or not hasattr(n, "work_window"):
        print(f"конвейер в папке книги — этап {getattr(n, 'NOX_STAGE', '?')}, а работа по частям появилась на этапе 21: "
              "скачайте новый prep_kniga1a.ps1 и запустите go", flush=True)
        return 3
    st = getattr(n, "SETTINGS", None) or {}

    def cfg(arg, key, dflt):
        if arg is not None:
            return arg
        v = st.get(key)
        return dflt if v is None or v == "" else v

    minutes = float(cfg(a.parts, "part_min", 19))
    n.TEMPO_G = float(cfg(a.tempo, "tempo", 1.12))
    n.PAUSE_SCALE = float(cfg(a.pauses, "pauses", 0.85))
    n.TRIM_PAUSES = not a.no_trim_pauses and st.get("trim", True) is not False
    n.TEMPO_EQ = not a.no_equalize and st.get("equalize", True) is not False
    fmt = str(cfg(a.format, "format", "mp3"))
    # название и автор — те же значения по умолчанию, что у go (иначе подпись сборки разошлась бы с окном watch режима -All)
    dflt_title = "Посох Забвения" if root.name.lower().startswith("kniga1") else root.name
    ns = argparse.Namespace(parts=minutes, format=fmt, bitrate=str(cfg(a.bitrate, "bitrate", "96k")),
                            title=str(cfg(a.title, "title", dflt_title)), author=str(cfg(a.author, "author", "Д.")),
                            speed=float(a.speed or 1.0))
    dest = Path(a.dest) if a.dest else None

    def window():
        rows, roles = n.load_rows(), n.load_roles()
        return rows, roles, n.work_window(rows, roles)

    def counts(rows, roles, w):
        gate = n._asr_gate()
        seg = rows[w[0]:w[1] + 1]
        have = sum(1 for r in seg if n.wav_for(r, roles).exists())
        chk = sum(1 for r in seg if n._seg_ok(r, roles, True)) if gate else None
        return have, chk, len(seg)

    if not a.status:
        sync_fixed(n)
    if a.report:
        return report(n, minutes)
    if a.status:
        rows, roles, w = window()
        have, chk, total = counts(rows, roles, w) if w[0] < len(rows) else (0, None, 0)
        print(json.dumps(dict(part=w[2], i0=w[0], i1=w[1], closed=bool(w[3]), done_all=w[0] >= len(rows),
                              fixed=len(n.fixed_prefix(rows)), rows=len(rows), have=have, checked=chk, total=total,
                              by_parts=n.by_parts()), ensure_ascii=False), flush=True)
        return 0

    # режим работы — для воркеров и проверки: они берут из этого файла и сам режим, и длину части в секундах записи
    mode = dict(by_parts=not a.all, minutes=minutes, part_sec=round(minutes * 60 * max(n.TEMPO_G, 1.0), 1),
                at=time.strftime("%Y-%m-%d %H:%M:%S"))
    tmp = n.MODE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(mode, ensure_ascii=False), encoding="utf-8")
    replace_retry(tmp, n.MODE_PATH)
    if a.mode:
        return 0
    if a.all:
        print("режим: вся книга сразу (части собирает окно watch); закреплённые в студии части не трогаются", flush=True)
        return 0

    # настройки сборки: та же сверка, что делает watch (изменились темп/паузы/роли — черновики незакреплённых частей
    # убираются и соберутся заново в свою очередь; закреплённые части остаются)
    sent_f = n.WORK / "sent.json"
    try:
        sent = json.loads(sent_f.read_text(encoding="utf-8"))
    except Exception:
        sent = {}
    sent = n._asm_setup(ns, dest, sent)
    rows, roles, w = window()
    nfx = len(n.fixed_prefix(rows))
    if a.init and w[0] < len(rows):
        dropped = audit_window(n, rows, roles, w, accent_mode(a.accent, st))
        if dropped:
            rows, roles, w = window()
    if a.init:
        if w[0] >= len(rows):
            print(f"режим: по частям. Все части ({nfx}) закреплены — книга готова", flush=True)
        else:
            have, chk, total = counts(rows, roles, w)
            print(f"режим: по частям. Закреплено автором частей: {nfx}; работа — над частью {w[2]} "
                  f"(реплики № {rows[w[0]]['id']}–{rows[w[1]]['id']}, {total} шт., озвучено {have})"
                  + (" — она уже собрана: прослушайте и закрепите её в студии" if w[3] else ""), flush=True)
        return 0

    status_f = n.WORK / "part_status.json"

    def put_status(**kw):
        try:
            t_ = status_f.with_suffix(".tmp")
            t_.write_text(json.dumps(dict(kw, at=time.strftime("%Y-%m-%d %H:%M:%S")), ensure_ascii=False), encoding="utf-8")
            replace_retry(t_, status_f)
        except OSError:
            pass

    def to_disk(num):
        f = n.PARTS / f"part_{num:02d}.{fmt}"
        if not dest or not f.exists():
            return
        try:
            s_ = f.stat()
            sg = f"{s_.st_size}:{s_.st_mtime_ns}"
            if sent.get(f.name) == sg and (dest / f.name).exists():
                return
            n.copy_to(dest, f)
            sent[f.name] = sg
            sent_f.write_text(json.dumps(sent), encoding="utf-8")
            print(f"[часть] черновик {f.name} — на Яндекс Диске: {dest}", flush=True)
        except OSError as e:
            print(f"[часть] копия на Диск не удалась ({type(e).__name__}: {e}) — файл в {f}", flush=True)

    stall = float(os.environ.get("NOX_STALL_WAIT") or 180)    # сколько ждать без движения, прежде чем сдаться

    def workers_alive() -> bool:
        q = n.WORK / "studio_queue"
        try:
            return any(time.time() - f.stat().st_mtime < min(120.0, stall) for f in q.glob("alive_w*.txt"))
        except OSError:
            return False

    def verify_alive() -> bool:
        try:
            return time.time() - (n.WORK / "asr_active.txt").stat().st_mtime < 600
        except OSError:
            return False

    if w[0] >= len(rows):
        print("[часть] все части закреплены автором — собирать нечего", flush=True)
        put_status(state="done_all", part=w[2])
        return 0
    print(f"[часть] работа над частью {w[2]}: закреплено частей {nfx}; длина части ~{minutes:g} мин, темп {n.TEMPO_G:g}; "
          f"соберу, когда все её реплики будут озвучены" + (" и проверены распознаванием" if n._asr_gate() else ""), flush=True)
    last, last_change, shown, shown_t = None, time.time(), None, 0.0
    while True:
        try:
            rows, roles, w = window()
        except Exception as e:                           # сценарий/роли переписываются прямо сейчас
            print(f"[часть] список реплик не прочитан ({type(e).__name__}: {str(e)[:100]}) — повторю", flush=True)
            time.sleep(a.every)
            continue
        num = w[2]
        if w[0] >= len(rows):
            print("[часть] все части закреплены автором", flush=True)
            put_status(state="done_all", part=num)
            return 0
        if not w[3]:
            try:
                n.build_parts(rows, roles, minutes, ns.title, ns.author, ns.bitrate, fmt, quiet=True, speed=ns.speed,
                              only=num, upto=w[1])
            except SystemExit as e:
                print(f"[часть] сборка: {e}", flush=True)
            except Exception as e:
                print(f"[часть] сборка не удалась ({type(e).__name__}: {str(e)[:160]}) — повторю", flush=True)
            w = n.work_window(rows, roles)
        have, chk, total = counts(rows, roles, w)
        if w[3]:
            f = n.PARTS / f"part_{num:02d}.{fmt}"
            to_disk(num)
            print(f"[часть] ЧАСТЬ {num} СОБРАНА: {f} (реплики № {rows[w[0]]['id']}–{rows[w[1]]['id']}). Прослушайте и "
                  f"закрепите её в студии — после этого go возьмётся за часть {num + 1}", flush=True)
            put_status(state="done", part=num, file=str(f), have=have, checked=chk, total=total)
            return 0
        cur = (num, w[1], have, chk)
        if cur != last:
            last, last_change = cur, time.time()
        if cur != shown and time.time() - shown_t > 30:
            shown, shown_t = cur, time.time()
            print(f"[часть] {time.strftime('%H:%M')}  часть {num}: озвучено {have} из {total} реплик"
                  + (f", проверено {chk}" if chk is not None else ""), flush=True)
        put_status(state="working", part=num, have=have, checked=chk, total=total)
        idle = time.time() - last_change
        pending = chk is not None and chk < have             # проверке ещё есть что проверять
        if idle > stall and not workers_alive() and (not pending or not verify_alive() or idle > 900):
            miss = [r for r in rows[w[0]:w[1] + 1] if not n._seg_ok(r, roles, n._asr_gate())][:8]
            print(f"[часть] часть {num} не собрана: воркеры озвучки не работают, а готовы не все реплики "
                  f"(озвучено {have} из {total}" + (f", проверено {chk}" if chk is not None else "") + "). Не готовы: "
                  + ", ".join(f"№ {r['id']}" for r in miss) + ("…" if len(miss) == 8 else "")
                  + ". Причины — в work\\failed.txt и work\\asr_log.txt; запустите go ещё раз", flush=True)
            put_status(state="stalled", part=num, have=have, checked=chk, total=total)
            return 2
        time.sleep(a.every)


if __name__ == "__main__":
    sys.exit(main())
