# -*- coding: utf-8 -*-
# nox_import.py — загрузка книги для озвучки: DOCX, TXT, EPUB, FB2 (и .fb2.zip) -> чистый book.docx + отчёт.
# NOX_STUDIO 2026-11-11g
#
# Зачем: конвейер озвучки читает book.docx (абзацы, заголовки стилем «Заголовок»). Книги приходят какими угодно:
# текст с переносами строк посреди фразы, EPUB с оглавлением и сносками, FB2 в старой кодировке, DOCX с таблицами
# и исправлениями рецензента. Здесь всё это приводится к одному виду и проверяется:
#   * кодировка TXT/FB2 определяется сама (UTF-8, UTF-16, Windows-1251, KOI8-R, DOS-866);
#   * строки, разорванные посреди абзаца, склеиваются; переносы «сло-\nво» убираются; колонтитулы и номера страниц —
#     тоже; сноски, оглавление, обложка, картинки не попадают в озвучку;
#   * ГЛАВЫ: берутся из разметки книги (стили DOCX, заголовки EPUB/FB2); если разметки нет — ищутся по смыслу:
#     «Глава 5», «ЧАСТЬ ВТОРАЯ», «Пролог», «1. Название», одинокие номера «12» или «XII», строки ПРОПИСНЫМИ.
#     Ничего надёжного не нашлось — глав нет, и это не ошибка: книга пойдёт одним текстом, а на части её разрежет
#     сборка (по времени);
#   * РОЛИ: кто говорит в диалогах (по словам автора: «— …, — сказал Джек»), сколько реплик, мужской/женский род —
#     список действующих лиц для подбора голосов;
#   * отчёт import_report.txt называет уязвимые места: латиница, числа, сокращения, странные знаки, слишком
#     длинные абзацы — всё, на чём синтез речи обычно спотыкается.
# Исходный файл не меняется. Зависимостей нет (только стандартная библиотека Python).
#
# Запуск:  python nox_import.py "C:\путь\книга.epub" --out D:\kniga4
#          (появятся D:\kniga4\book.docx, import_report.txt, roles_scan.tsv)
import argparse
import html
import io
import re
import sys
import zipfile
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree as ET

VERSION = "2026-11-11c"


class Bad(Exception):
    """Понятная автору причина, почему книгу не удалось загрузить."""


def B(t, text, level=1):
    return {"t": t, "text": text, "level": level}        # t: "h" заголовок, "p" абзац, "scene" разрыв сцены


RU = "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
RU_FREQ = "оеаинтсрвлкмдпуяыьгзбчйхжшюцщэфъё"          # по убыванию частоты
SCENE_RE = re.compile(r"^[\s*#~·•❦✦✧☙❧⁂※◊◆◇♦_=–—-]{1,40}$")
WS_RE = re.compile(r"[ \t\u00a0\u2000-\u200a\u202f\u205f\u3000]+")


def clean(s: str) -> str:
    """Невидимые и типографские мелочи: мягкие переносы, нулевые пробелы, неразрывные пробелы."""
    s = s.replace("\u00ad", "").replace("\u200b", "").replace("\u200c", "").replace("\u200d", "").replace("\ufeff", "")
    s = s.replace("\u2028", "\n").replace("\u2029", "\n").replace("\r", "")
    return "\n".join(WS_RE.sub(" ", x).strip() for x in s.split("\n")).strip("\n")


# ---------------------------------------------------------------------------------------------- TXT
def decode_text(data: bytes):
    """Байты -> (текст, название кодировки). Сначала явные признаки (BOM), потом — на какой кодировке текст
    больше всего похож на русский."""
    if data[:3] == b"\xef\xbb\xbf":
        return data[3:].decode("utf-8", "replace"), "UTF-8"
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", "replace"), "UTF-16"
    if len(data) >= 4 and data[1:2] == b"\x00" and data[3:4] == b"\x00" and data[0:1] != b"\x00":
        return data.decode("utf-16-le", "replace"), "UTF-16 LE"
    if len(data) >= 4 and data[0:1] == b"\x00" and data[2:3] == b"\x00" and data[1:2] != b"\x00":
        return data.decode("utf-16-be", "replace"), "UTF-16 BE"
    try:
        return data.decode("utf-8"), "UTF-8"
    except UnicodeDecodeError:
        pass
    sample = data[:400000]
    best = None
    for enc, name in (("cp1251", "Windows-1251"), ("koi8-r", "KOI8-R"), ("cp866", "DOS-866"), ("mac_cyrillic", "Mac Cyrillic")):
        try:
            t = sample.decode(enc)
        except UnicodeDecodeError:
            continue
        low = t.lower()
        cnt = Counter(c for c in low if c in RU)
        total = sum(cnt.values())
        if not total:
            continue
        # похоже на русский: частые буквы — действительно частые, строчных больше прописных, мало «мусора»
        top = sum(cnt[c] for c in "оеаинтс") / total
        caps = sum(1 for c in t if c.lower() in RU and c.isupper()) / total
        junk = sum(1 for c in t if ord(c) > 127 and c.lower() not in RU and c not in "«»—–…№„“”’") / max(1, len(t))
        score = top - caps * 0.8 - junk * 3
        if best is None or score > best[0]:
            best = (score, enc, name)
    if best is None:
        return data.decode("utf-8", "replace"), "UTF-8 (с потерями)"
    return data.decode(best[1], "replace"), best[2]


END_PUNCT = ".!?…:;»\"”)"
KEEP_AFTER = {"то", "либо", "нибудь", "таки", "ка", "де", "с", "за", "под", "на"}


def join_hyph(a: str, b: str) -> str:
    """Склейка двух строк абзаца. «сло-» + «во» -> «слово»; но «кое-» + «как», «кто-» + «то», «по-» + «русски» —
    настоящие дефисы, они остаются."""
    m = re.search(r"([А-Яа-яЁёA-Za-z]+)-$", a)
    n = re.match(r"[а-яёa-z]+", b)
    if not m or not n:
        return a + " " + b
    left, right = m.group(1).lower(), n.group(0)
    keep = right in KEEP_AFTER or left == "кое" \
        or (left == "по" and re.search(r"(?:ски|цки|ьи|ему|ому)$", right)) \
        or (left in ("в", "во") and re.search(r"(?:ых|их)$", right)) \
        or (left == "из" and right in ("за", "под"))
    return a + b if keep else a[:-1] + b


def txt_blocks(text: str, notes: list) -> list:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x0c", "\n")
    lines = [WS_RE.sub(" ", x.replace("\ufeff", "")).rstrip() for x in text.split("\n")]
    if not any(x.strip() for x in lines):
        raise Bad("в файле нет текста")
    # колонтитулы и номера страниц (остатки PDF и сканов) — на уровне строк, ДО склейки абзацев: «- 12 -», одинокие
    # номера страниц по порядку, одна и та же короткая строка много раз. Абзац, разорванный страницей, срастается.
    cnt = Counter(x.strip() for x in lines if x.strip() and len(x.strip()) <= 60)
    rep = {x for x, k in cnt.items() if k >= 6 and not SCENE_RE.match(x) and x[:1] not in "—–-" and not looks_heading(x)}
    nums = [int(x) for x in lines if re.fullmatch(r"\s*\d{1,4}\s*", x or "")]
    pages = len(nums) >= 12 and sum(1 for a, b in zip(nums, nums[1:]) if 0 < b - a <= 3) >= len(nums) * 0.8
    kept, dropped = [], 0
    for x in lines:
        t = x.strip()
        if t and (t in rep or re.fullmatch(r"[-–—]\s*\d{1,4}\s*[-–—]", t) or (pages and re.fullmatch(r"\d{1,4}", t))):
            dropped += 1
            while kept and not kept[-1].strip():        # пустые строки вокруг номера страницы
                kept.pop()
            if kept and kept[-1].strip()[-1:] not in ".!?…»\"”":
                kept.append("\x00")                     # фраза продолжается на следующей странице — абзац не рвём
            else:
                kept.append("")
            continue
        if kept and kept[-1] == "\x00":
            if not t:
                continue
            kept.pop()
        kept.append(x)
    lines = [x for x in kept if x != "\x00"]
    if dropped:
        notes.append(f"убрано колонтитулов и номеров страниц: {dropped}" + (f" (повторялись: {', '.join(sorted(rep))[:120]})" if rep else ""))
    full = [x for x in lines if x.strip()]
    if not full:
        raise Bad("в файле нет текста")
    blank_sep = sum(1 for i in range(1, len(lines)) if not lines[i].strip() and lines[i - 1].strip())
    lens = sorted(len(x) for x in full)
    p90 = lens[int(len(lens) * 0.9)]
    med = lens[len(lens) // 2]
    not_end = sum(1 for x in full if x.strip()[-1] not in END_PUNCT) / len(full)
    indented = sum(1 for x in full if x[:1] in " \t") / len(full)
    # продолжения: строка после «незаконченной» строки начинается со строчной буквы — признак разорванной прозы
    # (у стихов строки тоже без точки в конце, но начинаются с прописной — их склеивать нельзя)
    follow = [(a.strip(), b.strip()) for a, b in zip(lines, lines[1:]) if a.strip() and b.strip() and a.strip()[-1] not in END_PUNCT]
    low_start = sum(1 for a, b in follow if b[:1].islower() or b[:1].isdigit()) / max(1, len(follow))
    # три вида текста: (а) абзац = строка; (б) абзацы через пустую строку, внутри строки разорваны;
    # (в) строки разорваны по ширине, абзац начинается с отступа
    wrapped = p90 <= 100 and not_end > 0.28 and low_start > 0.5 and (med >= 35 or len(follow) >= 3)
    mode = "строка = абзац"
    if wrapped and blank_sep >= len(full) * 0.04:
        mode = "абзацы через пустую строку, строки внутри склеены"
    elif wrapped and 0.03 < indented < 0.7:
        mode = "абзац с отступа, строки внутри склеены"
    elif wrapped:
        mode = "строки склеены по смыслу (нет ни отступов, ни пустых строк)"
    notes.append(f"вид текста: {mode} (строк {len(full)}, типичная длина {med}, без точки в конце {not_end:.0%})")
    paras, cur = [], []

    def flush():
        if cur:
            s = cur[0]
            for x in cur[1:]:
                s = join_hyph(s, x)
            paras.append(s)
            cur.clear()

    if mode == "строка = абзац":
        for x in lines:
            paras.append(x.strip())                    # пустая строка — тоже значима (вокруг заголовков)
    else:
        for i, x in enumerate(lines):
            s = x.strip()
            if not s:
                flush()
                paras.append("")
                continue
            new_para = False
            if "отступа" in mode and x[:1] in " \t":
                new_para = True
            if SCENE_RE.match(s) or (len(s) <= 60 and looks_heading(s)):
                flush()
                paras.append(s)
                continue
            if "по смыслу" in mode and cur and cur[-1][-1:] in ".!?…»" and (s[:1] in "—–-«" or len(cur[-1]) < p90 * 0.72):
                new_para = True
            if "по смыслу" in mode and s[:1] in "—–" and cur and cur[-1][-1:] in ".!?…":   # реплика диалога — новый абзац
                new_para = True
            if new_para:
                flush()
            cur.append(s)
        flush()
    out = paras
    blocks, blank = [], 2
    for p in out:
        if not p:
            blank += 1
            continue
        if SCENE_RE.match(p):
            blocks.append(B("scene", "* * *"))
        else:
            b = B("p", clean(p))
            b["blank_before"] = blank
            blocks.append(b)
        blank = 0
    for i, b in enumerate(blocks):                      # сколько пустых строк после — для поиска заголовков
        b["blank_after"] = blocks[i + 1].get("blank_before", 1) if i + 1 < len(blocks) else 2
    return blocks


# ---------------------------------------------------------------------------------------------- DOCX
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def docx_blocks(data: bytes, notes: list) -> list:
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        doc = ET.fromstring(z.read("word/document.xml"))
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as e:
        raise Bad(f"это не документ Word (.docx) или он повреждён: {e}")
    styles = {}
    try:
        for st in ET.fromstring(z.read("word/styles.xml")).iter(W + "style"):
            sid = st.get(W + "styleId")
            nm = st.find(W + "name")
            ol = st.find(f"{W}pPr/{W}outlineLvl")
            styles[sid] = ((nm.get(W + "val") if nm is not None else sid) or "", int(ol.get(W + "val")) if ol is not None else None)
    except (KeyError, ET.ParseError):
        pass
    body = doc.find(W + "body")
    if body is None:
        raise Bad("в документе Word нет текста")
    stat = Counter()
    blocks = []

    def para_text(p):
        out = []

        def walk(e):
            tag = e.tag
            if tag in (W + "del", W + "moveFrom"):                    # удалённое рецензентом — не читаем
                stat["del"] += 1
                return
            if tag == W + "ins":
                stat["ins"] += 1
            if tag == W + "t":
                out.append(e.text or "")
            elif tag == W + "tab":
                out.append(" ")
            elif tag in (W + "br", W + "cr"):
                out.append("\n" if e.get(W + "type") not in ("page", "column") else " ")
            elif tag == W + "noBreakHyphen":
                out.append("-")
            elif tag in (W + "footnoteReference", W + "endnoteReference"):
                stat["notes"] += 1
            elif tag in (W + "drawing", W + "pict", W + "object"):
                stat["pics"] += 1
                return
            elif tag == W + "instrText" or tag == W + "fldSimple" and "TOC" in (e.get(W + "instr") or ""):
                return
            for ch in e:
                if ch.tag == W + "pPr" or ch.tag == W + "rPr":
                    continue
                walk(ch)
        walk(p)
        return clean("".join(out))

    def handle(p, in_table):
        ppr = p.find(W + "pPr")
        sid, lvl = "", None
        if ppr is not None:
            ps = ppr.find(W + "pStyle")
            if ps is not None:
                sid = ps.get(W + "val") or ""
            ol = ppr.find(W + "outlineLvl")
            if ol is not None:
                lvl = int(ol.get(W + "val"))
        name, slvl = styles.get(sid, (sid, None))
        low = name.lower()
        if lvl is None:
            lvl = slvl
        text = para_text(p)
        if not text:
            return
        if "toc" in low or "оглавлен" in low or "contents" in low:
            stat["toc"] += 1
            return
        if text.rstrip(".:").strip().lower() in ("содержание", "оглавление"):
            stat["toc"] += 1
            return
        if in_table:
            stat["table"] += 1
        if "scene" in low or SCENE_RE.match(text):
            blocks.append(B("scene", "* * *"))
            return
        m = re.match(r"(?:heading|заголовок)\s*(\d)", low)
        if m or low in ("title", "название", "subtitle", "подзаголовок") or (lvl is not None and lvl <= 2):
            level = int(m.group(1)) if m else (1 if low in ("title", "название") else (lvl + 1 if lvl is not None else 2))
            b = B("h", " ".join(x.strip() for x in text.split("\n") if x.strip()), min(level, 3))
            b["src"] = "стиль"
            blocks.append(b)
            return
        b = B("p", text)
        if any(k in low for k in ("quote", "цитат", "block", "эпиграф", "epigraph", "стих", "poem", "verse")):
            b["block"] = True
        blocks.append(b)

    def walk_body(e, in_table=False):
        for ch in e:
            if ch.tag == W + "p":
                handle(ch, in_table)
            elif ch.tag == W + "tbl":
                walk_body(ch, True)
            elif ch.tag in (W + "tr", W + "tc", W + "sdt", W + "sdtContent", W + "ins", W + "smartTag", W + "customXml"):
                walk_body(ch, in_table)
    walk_body(body)
    if stat["ins"] or stat["del"]:
        notes.append(f"в документе были исправления рецензента: вставок {stat['ins']} приняты, удалений {stat['del']} отброшены")
    if stat["table"]:
        notes.append(f"текст из таблиц вынут в обычные абзацы: {stat['table']} (обычный конвейер таблицы пропускает)")
    if stat["notes"]:
        notes.append(f"сноски не озвучиваются: убрано знаков сноски {stat['notes']}")
    if stat["pics"]:
        notes.append(f"картинки пропущены: {stat['pics']}")
    if stat["toc"]:
        notes.append(f"оглавление пропущено: {stat['toc']} строк")
    return blocks


# ---------------------------------------------------------------------------------------------- EPUB
class _Html(HTMLParser):
    BLOCK = {"p", "div", "li", "blockquote", "tr", "dd", "dt", "section", "article", "td", "pre", "figcaption", "body"}
    SKIP = {"script", "style", "head", "nav", "svg", "math", "sup", "rt", "rp", "title", "figure", "table" if False else "iframe"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.buf, self.skip, self.h, self.links, self.chars, self.notes = [], [], 0, None, 0, 0, 0
        self.stack = []

    def flush(self):
        s = clean("".join(self.buf))
        self.buf = []
        if not s:
            return
        if self.h:
            self.out.append(B("h", " ".join(s.split()), self.h))
        elif SCENE_RE.match(s):
            self.out.append(B("scene", "* * *"))
        else:
            self.out.append(B("p", s))

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        hidden = "display:none" in (a.get("style") or "").replace(" ", "") or a.get("hidden") is not None \
            or a.get("aria-hidden") == "true"
        et = (a.get("epub:type") or "") + " " + (a.get("role") or "") + " " + (a.get("class") or "")
        note = tag == "a" and ("noteref" in et or re.search(r"\b(footnote|note)-?ref", et) is not None)
        aside = tag in ("aside", "section", "div") and re.search(r"\b(footnotes?|endnotes?|rearnotes?|toc|pagebreak)\b", et) is not None
        if self.skip or tag in self.SKIP or hidden or note or aside:
            if tag not in ("br", "hr", "img", "meta", "link", "input"):
                self.skip += 1
                self.stack.append(tag)
            if note:
                self.notes += 1
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.flush()
            self.h = min(int(tag[1]), 3)
        elif tag in self.BLOCK:
            self.flush()
        elif tag == "br":
            self.buf.append("\n")
        elif tag == "hr":
            self.flush()
            self.out.append(B("scene", "* * *"))
        elif tag == "a":
            self.links += 1

    def handle_endtag(self, tag):
        if self.skip:
            if self.stack and self.stack[-1] == tag:
                self.stack.pop()
                self.skip -= 1
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.flush()
            self.h = None
        elif tag in self.BLOCK:
            self.flush()

    def handle_data(self, data):
        if not self.skip:
            self.buf.append(data if self.buf and self.buf[-1].endswith("\n") else re.sub(r"\s+", " ", data))
            self.chars += len(data.strip())


def html_blocks(text: str):
    p = _Html()
    p.feed(text)
    p.close()
    p.flush()
    return p.out, p


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def epub_blocks(data: bytes, notes: list) -> list:
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise Bad("файл .epub повреждён (это не архив)")
    names = {n.lower(): n for n in z.namelist()}
    if "meta-inf/encryption.xml" in names:
        enc = z.read(names["meta-inf/encryption.xml"]).decode("utf-8", "replace")
        algs = re.findall(r'Algorithm="([^"]+)"', enc)
        if any("embedding" not in a and "font" not in a.lower() for a in algs if "EncryptionMethod" or True):
            if any(k in enc for k in ("adept", "fairplay", "kobo", "lcp", "aes")) or not algs:
                raise Bad("книга защищена от копирования (DRM) — такой файл прочитать нельзя. Нужен EPUB без защиты")
    try:
        cont = ET.fromstring(z.read(names["meta-inf/container.xml"]))
        opf_path = next(e.get("full-path") for e in cont.iter() if _local(e.tag) == "rootfile")
        opf = ET.fromstring(z.read(opf_path))
    except (KeyError, StopIteration, ET.ParseError) as e:
        raise Bad(f"в EPUB не найдено описание книги (container.xml / .opf): {e}")
    base = opf_path.rsplit("/", 1)[0] + "/" if "/" in opf_path else ""
    manifest, nav_ids = {}, set()
    for e in opf.iter():
        if _local(e.tag) == "item":
            manifest[e.get("id")] = (e.get("href") or "", e.get("media-type") or "", e.get("properties") or "")
            if "nav" in (e.get("properties") or ""):
                nav_ids.add(e.get("id"))
    skip_href = set()
    for e in opf.iter():
        if _local(e.tag) == "reference" and (e.get("type") or "").lower() in ("toc", "cover", "copyright-page", "title-page", "loi", "lot"):
            skip_href.add((e.get("href") or "").split("#")[0])
    meta = {}
    for e in opf.iter():
        if _local(e.tag) in ("title", "creator") and e.text and _local(e.tag) not in meta:
            meta[_local(e.tag)] = e.text.strip()
    spine = [e.get("idref") for e in opf.iter() if _local(e.tag) == "itemref" and (e.get("linear") or "yes") != "no"]
    if not spine:
        raise Bad("в EPUB пустой порядок чтения (spine)")
    from urllib.parse import unquote
    from posixpath import normpath
    blocks, skipped, fn = [], [], 0
    for idref in spine:
        href, mt, props = manifest.get(idref, ("", "", ""))
        if not href or idref in nav_ids or href in skip_href:
            skipped.append(href or idref)
            continue
        path = normpath(base + unquote(href.split("#")[0]))
        try:
            raw = z.read(path)
        except KeyError:
            alt = names.get(path.lower())
            if not alt:
                notes.append(f"в EPUB нет файла {path} — пропущен")
                continue
            raw = z.read(alt)
        m = re.search(rb'encoding=["\']([\w-]+)', raw[:200])
        try:
            text = raw.decode(m.group(1).decode() if m else "utf-8", "replace")
        except LookupError:
            text = raw.decode("utf-8", "replace")
        bl, p = html_blocks(text)
        fn += p.notes
        words = sum(len(b["text"]) for b in bl)
        if bl and p.links >= 5 and words / max(1, p.links) < 60:      # одни ссылки — оглавление
            skipped.append(href)
            continue
        if not any(b["t"] == "h" for b in bl) and blocks and blocks[-1]["t"] != "scene" and bl:
            pass
        blocks += bl
    if skipped:
        notes.append("пропущены служебные страницы (обложка, оглавление): " + ", ".join(skipped)[:160])
    if fn:
        notes.append(f"сноски не озвучиваются: убрано знаков сноски {fn}")
    for k in ("title", "creator"):
        if meta.get(k):
            notes.append(("название в файле: " if k == "title" else "автор в файле: ") + meta[k])
    return blocks


# ---------------------------------------------------------------------------------------------- FB2
def fb2_blocks(data: bytes, notes: list) -> list:
    if data[:2] == b"PK":
        try:
            z = zipfile.ZipFile(io.BytesIO(data))
            name = next(n for n in z.namelist() if n.lower().endswith(".fb2"))
            data = z.read(name)
        except (zipfile.BadZipFile, StopIteration):
            raise Bad("в архиве нет файла .fb2")
    m = re.search(rb'encoding=["\']([\w-]+)', data[:300])
    enc = m.group(1).decode("ascii", "replace") if m else "utf-8"
    try:
        text = data.decode(enc, "replace")
    except LookupError:
        text, enc = decode_text(data)
    text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text.lstrip("\ufeff"), count=1)
    text = re.sub(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)", "&amp;", text)      # «голые» амперсанды
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise Bad(f"файл FB2 повреждён (ошибка разметки: {e})")
    blocks, stat = [], Counter()

    def txt(e):
        out = []

        def walk(x):
            tag = _local(x.tag)
            if tag == "a" and (x.get("type") == "note" or re.fullmatch(r"\s*\[?\d+\]?\s*", "".join(x.itertext()) or "")):
                stat["notes"] += 1
            elif tag not in ("image",):
                out.append(x.text or "")
                for ch in x:
                    walk(ch)
            out.append(x.tail or "")
        out.append(e.text or "")
        for ch in e:
            walk(ch)
        return clean(re.sub(r"\s+", " ", "".join(out)))

    def section(e, depth):
        for ch in e:
            tag = _local(ch.tag)
            if tag == "title":
                t = " ".join(x for x in (txt(p) for p in ch) if x) or txt(ch)
                if t:
                    b = B("h", t, min(depth, 3))
                    b["src"] = "разметка"
                    blocks.append(b)
            elif tag == "section":
                section(ch, depth + 1)
            elif tag in ("p", "subtitle", "text-author"):
                t = txt(ch)
                if not t:
                    continue
                if tag == "subtitle" and SCENE_RE.match(t):
                    blocks.append(B("scene", "* * *"))
                else:
                    blocks.append(B("p", t))
            elif tag == "empty-line":
                pass
            elif tag in ("epigraph", "cite", "annotation"):
                section(ch, depth)
            elif tag == "poem":
                for st in ch:
                    if _local(st.tag) == "stanza":
                        lines = [txt(v) for v in st if _local(v.tag) == "v"]
                        if lines:
                            b = B("p", "\n".join(x for x in lines if x))
                            b["block"] = True
                            blocks.append(b)
                    elif _local(st.tag) in ("title", "text-author"):
                        t = txt(st)
                        if t:
                            blocks.append(B("p", t))
            elif tag == "image":
                stat["pics"] += 1

    bodies = [e for e in root if _local(e.tag) == "body"]
    main = [b for b in bodies if (b.get("name") or "").lower() not in ("notes", "footnotes", "comments")]
    if not main:
        raise Bad("в файле FB2 нет текста книги")
    for b in main:
        section(b, 1)
    if len(bodies) > len(main):
        notes.append("раздел примечаний (сноски) не озвучивается")
    if stat["notes"]:
        notes.append(f"убрано знаков сноски: {stat['notes']}")
    if stat["pics"]:
        notes.append(f"картинки пропущены: {stat['pics']}")
    ti = next((e for e in root.iter() if _local(e.tag) == "title-info"), None)
    if ti is not None:
        bt = next((e for e in ti if _local(e.tag) == "book-title"), None)
        if bt is not None and bt.text:
            notes.append("название в файле: " + bt.text.strip())
        au = next((e for e in ti if _local(e.tag) == "author"), None)
        if au is not None:
            nm = " ".join((x.text or "").strip() for x in au if _local(x.tag) in ("first-name", "middle-name", "last-name") and x.text)
            if nm:
                notes.append("автор в файле: " + nm)
    notes.append(f"кодировка FB2: {enc}")
    return blocks


# ---------------------------------------------------------------------------------------------- главы
NUM_WORDS = (r"перв\w+|втор\w+|трет\w+|четв[её]рт\w+|пят\w+|шест\w+|седьм\w+|восьм\w+|девят\w+|десят\w+|одиннадцат\w+|"
             r"двенадцат\w+|тринадцат\w+|четырнадцат\w+|пятнадцат\w+|шестнадцат\w+|семнадцат\w+|восемнадцат\w+|"
             r"девятнадцат\w+|двадцат\w+|тридцат\w+|сорок\w*|пятидесят\w+|последн\w+")
H_KEY = re.compile(r"^(?:глава|часть|книга|том|раздел|chapter|part|book)\s+(?:\d{1,3}|[IVXLCМ]{1,8}|" + NUM_WORDS + r")\b.{0,90}$", re.I)
H_NUMWORD = re.compile(r"^(?:" + NUM_WORDS + r")\s+(?:глава|часть|книга)\b.{0,60}$", re.I)
H_NAMED = re.compile(r"^(?:пролог|эпилог|предисловие|послесловие|интерлюдия|вступление|введение|заключение|от автора|"
                     r"вместо (?:предисловия|послесловия|эпилога|пролога)|примечания|приложени[ея]|"
                     r"prologue|epilogue|foreword|afterword|interlude)\b.{0,70}$", re.I)
H_NUM_TITLE = re.compile(r"^(\d{1,3})[.)]\s+[А-ЯЁA-Z«\"].{0,80}$")
H_NUM = re.compile(r"^(\d{1,3})\.?$")
H_ROMAN = re.compile(r"^([IVXLC]{1,7})\.?$")


def roman(s):
    v = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}
    t = 0
    for a, b in zip(s, s[1:] + " "):
        t += -v[a] if b in v and v[b] > v[a] else v[a]
    return t


def looks_heading(s: str) -> bool:
    return bool(H_KEY.match(s) or H_NUMWORD.match(s) or H_NAMED.match(s) or H_NUM_TITLE.match(s) or H_NUM.match(s)
                or H_ROMAN.match(s))


def _caps(s: str) -> bool:
    letters = [c for c in s if c.isalpha()]
    return len(letters) >= 3 and all(c.isupper() for c in letters) and len(s) <= 70 and s[-1:] not in ".,;:" \
        and s[:1] not in "—–-«\""


def find_chapters(blocks: list, notes: list) -> str:
    """Расставляет заголовки. Возвращает, как найдены главы (для отчёта)."""
    heads = [b for b in blocks if b["t"] == "h"]
    paras = sum(1 for b in blocks if b["t"] == "p")
    if len(heads) >= 2 and len(heads) <= max(3, paras // 2):
        return f"из разметки книги ({len(heads)} заголовков)"
    if len(heads) > max(3, paras // 2):                  # «заголовком» размечено всё подряд — разметке верить нельзя
        notes.append(f"в книге стилем заголовка размечено слишком многое ({len(heads)} из {len(blocks)} абзацев) — "
                     "разметка заголовков не учитывается, главы ищутся по смыслу")
        for b in heads:
            b["t"] = "p"
    cand = {"key": [], "numtitle": [], "num": [], "roman": [], "caps": []}
    for i, b in enumerate(blocks):
        if b["t"] != "p" or "\n" in b["text"]:
            continue
        s = b["text"].strip()
        if len(s) > 100 or s[:1] in "—–-":
            continue
        lonely = b.get("blank_before", 1) >= 1 and b.get("blank_after", 1) >= 1
        if H_KEY.match(s) or H_NUMWORD.match(s) or H_NAMED.match(s):
            m = re.match(r"^\S+\s+\S+?[.:—–-]?(?:\s+|$)", s)                 # «Глава первая.» — а дальше название
            rest = s[m.end():] if m and not H_NAMED.match(s) else s
            if s[-1:] not in ",;" and not re.search(r"[.!?…]\s+\S", rest):         # в названии нет второго предложения
                cand["key"].append(i)
            continue
        m = H_NUM_TITLE.match(s)
        if m and s[-1:] not in ".,;:" and len(s) <= 70:
            cand["numtitle"].append((i, int(m.group(1))))
            continue
        m = H_NUM.match(s)
        if m and lonely:
            cand["num"].append((i, int(m.group(1))))
            continue
        m = H_ROMAN.match(s)
        if m and lonely:
            cand["roman"].append((i, roman(m.group(1))))
            continue
        if _caps(s) and lonely:
            cand["caps"].append(i)

    def seq(items):
        """Самая длинная цепочка номеров 1, 2, 3… (не обязательно с единицы) — отсекает случайные числа в тексте."""
        best, cur = [], []
        for i, v in items:
            if cur and v == cur[-1][1] + 1:
                cur.append((i, v))
            else:
                if len(cur) > len(best):
                    best = cur
                cur = [(i, v)]
        if len(cur) > len(best):
            best = cur
        return best
    how, idx = "", []
    named = [i for i in cand["key"] if H_NAMED.match(blocks[i]["text"].strip())]
    keyed = [i for i in cand["key"] if i not in named]
    st, sn, sr = seq(cand["numtitle"]), seq(cand["num"]), seq(cand["roman"])
    if len(keyed) >= 2:
        how, idx = "по словам «Глава», «Часть»", cand["key"]
    elif len(st) >= 3:
        how, idx = "по нумерованным названиям («1. Название»)", [i for i, _ in st] + named
    elif len(sn) >= 3:
        how, idx = "по одиноким номерам глав («1», «2», «3»)", [i for i, _ in sn] + named
    elif len(sr) >= 3:
        how, idx = "по римским номерам глав («I», «II», «III»)", [i for i, _ in sr] + named
    elif len(cand["caps"]) >= 3 and len(cand["caps"]) <= max(3, paras // 8):
        how, idx = "по строкам ПРОПИСНЫМИ буквами", cand["caps"] + named
    elif named:
        how, idx = "только по словам «Пролог», «Эпилог» и подобным", named
    if not idx:
        return ""
    for i in sorted(set(idx)):
        b = blocks[i]
        s = b["text"].strip()
        b["t"] = "h"
        b["level"] = 1 if re.match(r"(?:часть|книга|том|part|book)\b", s, re.I) or re.match(NUM_WORDS + r"\s+(?:часть|книга)", s, re.I) else 2
        b["src"] = "по смыслу"
        # «Глава 5» + следующей строкой короткое название -> один заголовок «Глава 5. Название»
        if i + 1 < len(blocks) and blocks[i + 1]["t"] == "p" and re.fullmatch(r"(?:глава|часть|книга|том)\s+\S+\.?|\d{1,3}\.?|[IVXLC]{1,7}\.?", s, re.I):
            nx = blocks[i + 1]["text"].strip()
            if len(nx) <= 70 and nx[-1:] not in ".!?…,;:" and nx[:1] not in "—–-«\"" and "\n" not in nx \
                    and nx[:1].isupper() and blocks[i + 1].get("blank_after", 1) >= 1:
                b["text"] = s.rstrip(".") + ". " + nx
                blocks[i + 1]["t"] = "drop"
    blocks[:] = [b for b in blocks if b["t"] != "drop"]
    return how + f" ({sum(1 for b in blocks if b['t'] == 'h')} заголовков)"


# ---------------------------------------------------------------------------------------------- роли
SAY = (r"сказал|спросил|ответил|крикнул|закричал|прошептал|шепнул|буркнул|пробормотал|проворчал|воскликнул|"
       r"вздохнул|усмехнулся|хмыкнул|фыркнул|заметил|добавил|продолжил|возразил|согласился|кивнул|повторил|"
       r"произн[её]с|проговорил|пробурчал|прорычал|рявкнул|простонал|взмолился|потребовал|приказал|велел|"
       r"предложил|объяснил|пояснил|уточнил|напомнил|признался|перебил|отозвался|откликнулся|поинтересовался|"
       r"удивился|засмеялся|рассмеялся|улыбнулся|нахмурился|пожал плечами|начал|закончил|позвал|окликнул|"
       r"выдохнул|процедил|прохрипел|взвизгнул|всхлипнул|огрызнулся|отрезал|бросил|протянул|подтвердил|"
       r"сообщил|доложил|объявил|заявил|рассудил|подумал|решил|сказала|спросила|ответила|крикнула|закричала|"
       r"прошептала|шепнула|буркнула|пробормотала|проворчала|воскликнула|вздохнула|усмехнулась|хмыкнула|"
       r"фыркнула|заметила|добавила|продолжила|возразила|согласилась|кивнула|повторила|произнесла|проговорила|"
       r"взмолилась|потребовала|приказала|велела|предложила|объяснила|пояснила|уточнила|напомнила|призналась|"
       r"перебила|отозвалась|поинтересовалась|удивилась|засмеялась|рассмеялась|улыбнулась|нахмурилась|начала|"
       r"закончила|позвала|окликнула|выдохнула|процедила|всхлипнула|огрызнулась|отрезала|бросила|протянула|"
       r"подтвердила|сообщила|объявила|заявила|подумала|решила")
NAME = r"[А-ЯЁ][а-яё]+(?:[- ][А-ЯЁ][а-яё]+)?"
R_VERB_NAME = re.compile(r"[—–-]\s*(?:\w+\s+){0,2}?(" + SAY + r")\s+(?:\w+\s+){0,2}?(" + NAME + r")\b")
R_NAME_VERB = re.compile(r"[—–-]\s*(" + NAME + r")\s+(?:\w+\s+){0,2}?(" + SAY + r")\b")
NOT_NAME = set("Он Она Они Оно Я Мы Вы Ты Тот Та Те Это Этот Эта Кто Что Все Всё Никто Каждый Один Одна Другой Другая "
               "Первый Вторая Второй Третий Старик Мальчик Девочка Мужчина Женщина Человек Незнакомец Незнакомка Голос "
               "Тогда Потом Затем Наконец Вдруг Снова Опять Только Просто Здесь Там Тут Да Нет Но И А Ну Ах Ох Эй Он-то "
               "Господи Боже Чёрт Черт Конечно Может Наверное Кажется Правда Ладно Хорошо Спасибо Пожалуйста".split())


def scan_roles(blocks: list) -> list:
    """Действующие лица по словам автора при репликах -> [(имя, реплик, род, пример)]."""
    cnt, gender, sample = Counter(), {}, {}
    speech = 0
    for b in blocks:
        if b["t"] != "p":
            continue
        s = b["text"]
        if not (s[:1] in "—–" or (s[:1] in "«\"„" and re.search(r"[»\"“],?\s*[—–]", s))):
            continue
        speech += 1
        hits = [(m.group(2), m.group(1)) for m in R_VERB_NAME.finditer(s)] + [(m.group(1), m.group(2)) for m in R_NAME_VERB.finditer(s)]
        for name, verb in hits[:1]:
            first = name.split()[0].split("-")[0]
            if first in NOT_NAME or len(first) < 2:
                continue
            cnt[name] += 1
            g = "ж" if re.search(r"(?:ла|лась)$", verb.split()[0]) else "м"
            gender.setdefault(name, Counter())[g] += 1
            sample.setdefault(name, s[:110])
    out = [(n, k, gender[n].most_common(1)[0][0], sample[n]) for n, k in cnt.most_common() if k >= 2]
    return out, speech


# ---------------------------------------------------------------------------------------------- проверка текста
def audit(blocks: list) -> list:
    """Уязвимые места для синтеза речи — в отчёт (ничего не меняет)."""
    out = []
    text = "\n".join(b["text"] for b in blocks if b["t"] != "scene")
    paras = [b["text"] for b in blocks if b["t"] == "p"]
    lat = Counter(w for w in re.findall(r"[A-Za-z][A-Za-z'’-]{1,}", text) if not re.fullmatch(r"[IVXLCDM]+", w))   # не римские числа
    if lat:
        out.append(f"латиница: {sum(lat.values())} слов ({len(lat)} разных) — синтез читает её по буквам или с акцентом; "
                   "частые: " + ", ".join(f"{w} ×{k}" for w, k in lat.most_common(12)))
    nums = re.findall(r"\d[\d  .,:/-]*\d|\d", text)
    if nums:
        out.append(f"числа цифрами: {len(nums)} — читаются словами автоматически, но падеж бывает неверным "
                   "(«в 1920 году»); примеры: " + ", ".join(dict.fromkeys(nums[:40]))[:160])
    abbr = Counter(m.lower() for m in re.findall(r"\b(?:т\.\s?е\.|т\.\s?к\.|т\.\s?д\.|т\.\s?п\.|и\s?др\.|им\.|св\.|гг?\.|вв?\.|"
                                                 r"стр\.|см\.|руб\.|коп\.|тыс\.|млн\.?|млрд\.?|кг\b|км\b|см\b|мм\b|°C)", text, re.I))
    if abbr:
        out.append("сокращения: " + ", ".join(f"{a} ×{k}" for a, k in abbr.most_common(12)) + " — лучше раскрыть словами")
    odd = Counter(c for c in text if ord(c) > 127 and c.lower() not in RU and c not in "«»—–…№„“”’‘\u0301\n ")
    if odd:
        out.append("необычные знаки: " + " ".join(f"{c}×{k}" for c, k in odd.most_common(20)))
    long_p = [p for p in paras if len(p) > 2500]
    if long_p:
        out.append(f"очень длинные абзацы (больше 2500 знаков): {len(long_p)} — возможно, в исходнике потеряны абзацы; "
                   "первый начинается: «" + long_p[0][:70] + "…»")
    short = sum(1 for p in paras if len(p) < 40 and p[-1:] not in END_PUNCT)
    if paras and short > len(paras) * 0.25:
        out.append(f"много коротких абзацев без точки в конце ({short} из {len(paras)}) — возможно, строки остались "
                   "разорванными; проверьте исходный файл")
    caps = Counter(w for w in re.findall(r"\b[А-ЯЁ]{4,}\b", text))
    if caps:
        out.append("слова ПРОПИСНЫМИ: " + ", ".join(f"{w} ×{k}" for w, k in caps.most_common(10)) +
                   " — синтез может прочитать их по буквам")
    yo = text.count("ё") + text.count("Ё")
    if len(text) > 20000 and yo == 0:
        out.append("в книге нет буквы «ё» — ударения и чтение «все/всё», «небо/нёбо» расставятся автоматически, "
                   "но ошибок будет больше; если есть издание с «ё», возьмите его")
    stress = text.count("\u0301")
    if stress:
        out.append(f"в тексте уже стоят знаки ударения: {stress} — они сохранены и будут соблюдены")
    return out


# ---------------------------------------------------------------------------------------------- запись DOCX
def _x(s):
    return html.escape(s, quote=False)


def write_docx(blocks: list, out: Path, title: str = "", author: str = ""):
    """Минимальный, но честный .docx: абзацы, «Заголовок 1/2/3», разрывы сцен «* * *». Его читает и Word, и конвейер."""
    body = []
    for b in blocks:
        if b["t"] == "h":
            body.append(f'<w:p><w:pPr><w:pStyle w:val="Heading{min(max(int(b.get("level", 1)), 1), 3)}"/></w:pPr>'
                        f'<w:r><w:t xml:space="preserve">{_x(b["text"])}</w:t></w:r></w:p>')
        elif b["t"] == "scene":
            body.append('<w:p><w:pPr><w:jc w:val="center"/></w:pPr><w:r><w:t>* * *</w:t></w:r></w:p>')
        else:
            runs = "<w:r><w:br/></w:r>".join(f'<w:r><w:t xml:space="preserve">{_x(x)}</w:t></w:r>' for x in b["text"].split("\n"))
            st = '<w:pPr><w:pStyle w:val="Quote"/></w:pPr>' if b.get("block") else ""
            body.append(f"<w:p>{st}{runs}</w:p>")
    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    doc = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document {ns}><w:body>' + "".join(body) +
           '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134" '
           'w:header="708" w:footer="708" w:gutter="0"/></w:sectPr></w:body></w:document>')

    def style(sid, name, size, bold, outline=None, based="Normal", extra=""):
        ol = f'<w:outlineLvl w:val="{outline}"/>' if outline is not None else ""
        return (f'<w:style w:type="paragraph" w:styleId="{sid}"><w:name w:val="{name}"/><w:basedOn w:val="{based}"/>'
                f'<w:next w:val="Normal"/><w:qFormat/><w:pPr><w:keepNext/><w:spacing w:before="360" w:after="160"/>{ol}{extra}</w:pPr>'
                f'<w:rPr>{"<w:b/>" if bold else ""}<w:sz w:val="{size}"/></w:rPr></w:style>')
    styles = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:styles {ns}>'
              '<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Cambria" w:hAnsi="Cambria" w:cs="Cambria"/>'
              '<w:sz w:val="24"/><w:lang w:val="ru-RU"/></w:rPr></w:rPrDefault>'
              '<w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="300" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>'
              '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/></w:style>'
              + style("Heading1", "heading 1", 36, True, 0) + style("Heading2", "heading 2", 30, True, 1)
              + style("Heading3", "heading 3", 26, True, 2)
              + '<w:style w:type="paragraph" w:styleId="Quote"><w:name w:val="Quote"/><w:basedOn w:val="Normal"/><w:qFormat/>'
                '<w:pPr><w:ind w:left="567"/></w:pPr><w:rPr><w:i/></w:rPr></w:style></w:styles>')
    ct = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
          '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
          '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
          '</Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
            '</Relationships>')
    drels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
             '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
             '</Relationships>')
    core = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/">'
            f'<dc:title>{_x(title)}</dc:title><dc:creator>{_x(author)}</dc:creator></cp:coreProperties>')
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", doc)
        z.writestr("word/styles.xml", styles)
        z.writestr("word/_rels/document.xml.rels", drels)
        z.writestr("docProps/core.xml", core)
    import os
    os.replace(tmp, out)


# ---------------------------------------------------------------------------------------------- всё вместе
def load(path: Path):
    """Файл книги -> (абзацы, заметки о том, что сделано, название формата)."""
    path = Path(path)
    if not path.is_file():
        raise Bad(f"нет файла {path}")
    data = path.read_bytes()
    if not data:
        raise Bad("файл пустой")
    notes = []
    name = path.name.lower()
    if name.endswith(".docx") or name.endswith(".docm"):
        return docx_blocks(data, notes), notes, "DOCX"
    if name.endswith(".epub"):
        return epub_blocks(data, notes), notes, "EPUB"
    if name.endswith(".fb2") or name.endswith(".fb2.zip") or name.endswith(".fbz"):
        return fb2_blocks(data, notes), notes, "FB2"
    if name.endswith(".doc") or name.endswith(".rtf") or name.endswith(".odt") or name.endswith(".pdf") or name.endswith(".mobi") \
            or name.endswith(".azw3") or name.endswith(".djvu"):
        raise Bad(f"формат {path.suffix} напрямую не читается. Откройте книгу в Word или LibreOffice и сохраните как .docx "
                  "(для PDF — «Сохранить как текст» или распознавание), затем загрузите снова")
    if data[:2] == b"PK":                                # архив без понятного имени — пробуем как книгу
        names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        if "word/document.xml" in names:
            return docx_blocks(data, notes), notes, "DOCX"
        if any(n.lower().endswith(".opf") for n in names):
            return epub_blocks(data, notes), notes, "EPUB"
        if any(n.lower().endswith(".fb2") for n in names):
            return fb2_blocks(data, notes), notes, "FB2"
        raise Bad("это архив, но книги (docx, epub, fb2) в нём нет")
    if data.lstrip()[:200].lower().find(b"<fictionbook") >= 0:
        return fb2_blocks(data, notes), notes, "FB2"
    text, enc = decode_text(data)
    probe = text[:30000]
    junk = sum(1 for c in probe if (ord(c) < 32 and c not in "\n\r\t\x0c") or c == "\ufffd")
    if junk > max(3, len(probe) * 0.02):
        raise Bad("это не текстовый файл (двоичные данные). Нужны docx, txt, epub или fb2")
    notes.append(f"кодировка текста: {enc}")
    if text.lstrip()[:300].lower().startswith(("<!doctype html", "<html")):
        bl, _ = html_blocks(text)
        return bl, notes, "HTML"
    return txt_blocks(text, notes), notes, "TXT"


def convert(src, out_dir, title: str = "", author: str = "") -> dict:
    """Книга -> out_dir/book.docx + import_report.txt + roles_scan.tsv. Возвращает сводку."""
    src, out_dir = Path(src), Path(out_dir)
    blocks, notes, fmt = load(src)
    blocks = [b for b in blocks if b["t"] == "scene" or b["text"].strip()]
    # подряд идущие разрывы сцен и разрывы по краям — лишние
    tidy = []
    for b in blocks:
        if b["t"] == "scene" and (not tidy or tidy[-1]["t"] in ("scene", "h")):
            continue
        tidy.append(b)
    while tidy and tidy[-1]["t"] == "scene":
        tidy.pop()
    blocks = tidy
    if sum(len(b["text"]) for b in blocks) < 200:
        raise Bad("в файле почти нет текста — это точно книга?")
    how = find_chapters(blocks, notes)
    for b in blocks:                                     # заголовок без единой буквы — не заголовок
        if b["t"] == "h" and not re.search(r"[A-Za-zА-Яа-яЁё0-9]", b["text"]):
            b["t"] = "scene"
    heads = [b for b in blocks if b["t"] == "h"]
    roles, speech = scan_roles(blocks)
    chars = sum(len(b["text"]) for b in blocks if b["t"] != "scene")
    paras = sum(1 for b in blocks if b["t"] == "p")
    if not title:
        title = next((n.split(": ", 1)[1] for n in notes if n.startswith("название в файле: ")), "") or src.stem
    if not author:
        author = next((n.split(": ", 1)[1] for n in notes if n.startswith("автор в файле: ")), "")
    out_dir.mkdir(parents=True, exist_ok=True)
    write_docx(blocks, out_dir / "book.docx", title, author)
    hours = chars / 14.5 / 3600                          # ~14,5 знака в секунду при обычной скорости чтения
    rep = [f"Загрузка книги для озвучки — отчёт ({VERSION})", "",
           f"Исходный файл: {src}", f"Формат: {fmt}", f"Название: {title}" + (f"   Автор: {author}" if author else ""),
           f"Абзацев: {paras}, знаков: {chars}, реплик диалога: {speech}",
           f"Примерная длительность озвучки: {int(hours)} ч {int(hours % 1 * 60)} мин", "",
           "ГЛАВЫ: " + (how if how else "не найдены. Это не ошибка: книга пойдёт одним текстом, на части её разрежет "
                        "сборка (по времени). Если главы в книге есть — назовите их «Глава N» или выделите стилем "
                        "«Заголовок 1» в book.docx и загрузите заново.")]
    for k, b in enumerate(heads[:400], 1):
        rep.append(f"  {k:3d}. {'    ' * (int(b.get('level', 1)) - 1)}{b['text'][:100]}")
    if notes:
        rep += ["", "ЧТО СДЕЛАНО ПРИ ЗАГРУЗКЕ:"] + ["  - " + x for x in notes]
    aud = audit(blocks)
    rep += ["", "УЯЗВИМЫЕ МЕСТА (на чём синтез речи обычно спотыкается):"] + (["  - " + a for a in aud] or ["  не найдено"])
    rep += ["", f"ДЕЙСТВУЮЩИЕ ЛИЦА (по словам автора при репликах; подробно — roles_scan.tsv): {len(roles)}"]
    for n, k, g, s in roles[:40]:
        rep.append(f"  {n:<24} реплик {k:4d}   {'женский' if g == 'ж' else 'мужской'} голос")
    if not roles:
        rep.append("  не найдено (в книге нет диалогов с указанием говорящего — всё прочитает рассказчик)")
    rep += ["", "Дальше: проверьте book.docx (заголовки глав выделены стилем), при необходимости поправьте его в Word —",
            "и запускайте подготовку книги. Исходный файл не изменён."]
    (out_dir / "import_report.txt").write_text("\ufeff" + "\r\n".join(rep) + "\r\n", encoding="utf-8")
    (out_dir / "roles_scan.tsv").write_text(
        "\ufeffимя\tреплик\tрод\tпример\r\n" + "".join(f"{n}\t{k}\t{g}\t{s}\r\n" for n, k, g, s in roles), encoding="utf-8")
    return dict(format=fmt, title=title, author=author, paras=paras, chars=chars, chapters=len(heads), how=how,
                roles=len(roles), speech=speech, hours=round(hours, 2), notes=notes, audit=aud,
                out=str(out_dir / "book.docx"), report=str(out_dir / "import_report.txt"))


def main():
    ap = argparse.ArgumentParser(description="Загрузка книги (docx, txt, epub, fb2) для озвучки: чистый book.docx и отчёт")
    ap.add_argument("book", help="файл книги")
    ap.add_argument("--out", default=None, help="папка книги (по умолчанию — рядом с исходным файлом, подпапка nox_import)")
    ap.add_argument("--title", default="")
    ap.add_argument("--author", default="")
    a = ap.parse_args()
    src = Path(a.book)
    out = Path(a.out) if a.out else src.parent / "nox_import"
    try:
        r = convert(src, out, a.title, a.author)
    except Bad as e:
        print(f"Книга не загружена: {e}")
        sys.exit(2)
    print(f"Книга загружена: {r['title']}" + (f" ({r['author']})" if r["author"] else ""))
    print(f"  формат {r['format']}, абзацев {r['paras']}, знаков {r['chars']}, примерно {r['hours']:.1f} ч озвучки")
    print("  главы: " + (r["how"] or "не найдены — книга пойдёт одним текстом, деление только на части"))
    print(f"  действующих лиц с репликами: {r['roles']} (реплик диалога всего {r['speech']})")
    for n in r["audit"][:6]:
        print("  ! " + n[:150])
    print(f"  чистый текст: {r['out']}")
    print(f"  отчёт:        {r['report']}")


if __name__ == "__main__":
    main()
