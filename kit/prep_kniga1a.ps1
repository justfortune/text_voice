# NOX_KIT 2026-11-11g
# prep_kniga1a.ps1 — проект АУДИОРЕДАКЦИИ первой книги (полной) в папке D:\kniga1a.
# Текущие папки (D:\nox_audiobook, D:\kniga2) не трогаются. GPU не нужен, ~1 минута.
#
# Что делает:
#   1) D:\kniga1a + ссылки (junction) на env/envfast/hf/pip_cache/tools — ничего не перекачивается;
#   2) копирует конвейер и ВСЕ голоса; кладёт словарь произношения (169 словоформ: поосох=по́сох,
#      Хорваат=Хорва́т, Галаава, Хекуубах, фаартук...) — применяется на лету, текст книги не меняется;
#   3) правит копию nox_audiobook.py: дефис внутри слова -> пробел (конец паузам в «архи-волшебник»,
#      «кто-то»), пропуск оглавления, разрывы сцен -> пауза, двухстрочные заголовки;
#   4) рукопись: $HOME\Downloads\kniga1.docx (или -Docx путь) -> разбивка по ролям.
#
# Подготовка: скопируйте ПОЛНУЮ первую книгу в Загрузки под именем kniga1.docx. Затем:
#   powershell -NoProfile -ExecutionPolicy Bypass -File "$HOME\Downloads\prep_kniga1a.ps1"

param(
    [string]$Src  = "D:\nox_audiobook",
    [string]$Root = "D:\kniga1a",
    [string]$Docx = "$env:USERPROFILE\Downloads\kniga1.docx",
    [string]$Model = "shib79/qwen3-tts-12hz-1.7b-ru-stress-cf",   # Qwen3-TTS 1.7B Base, дообученный читать знак ударения (Apache-2.0)
    [string]$Accent = "acute",                                    # acute = знак U+0301 после ударной гласной; none = чистый текст
    [string]$Dict = "",                                           # путь к accent_dict.csv (иначе ищется в Загрузках)
    [string]$FastPy = "",                                         # путь к nox_fast.py (иначе Src/Загрузки)
    [string]$Overrides = "",                                      # сценарий по ролям speaker_overrides.tsv (иначе ищется в Загрузках)
    [int]$MaxChars = 200,                                         # длина куска текста для озвучки (таблица настроек)
    [string]$SeedFrom = "",                                       # новая книга: роли и голоса взять из этой книги (D:\kniga1a)
    [string]$Title = ""                                           # название книги — проверка, что рукопись та самая
)

$ErrorActionPreference = "Continue"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
chcp 65001 | Out-Null
$env:PYTHONUTF8 = "1"; $env:PYTHONIOENCODING = "utf-8"

function Step($m) { Write-Host ""; Write-Host ("=== " + $m) -ForegroundColor Cyan }

if (-not (Test-Path "$Src\nox_audiobook.py")) { Write-Host "Нет $Src" -ForegroundColor Red; exit 1 }
if (-not (Test-Path -LiteralPath $Docx) -and (Test-Path "$Root\book.docx")) { $Docx = "$Root\book.docx" }   # рукопись уже в проекте
if (-not (Test-Path -LiteralPath $Docx)) {
    Write-Host ("Нет рукописи: " + $Docx) -ForegroundColor Red
    Write-Host "Положите рукопись в Загрузки (книга 1 — kniga1.docx, книга 2 — kniga2.docx) или укажите путь в таблице настроек" -ForegroundColor Yellow
    exit 1
}
New-Item -ItemType Directory -Force -Path $Root, "$Root\work" | Out-Null

Step "Ссылки на окружение (модели и python не копируются)"
foreach ($d in @("env", "envfast", "hf", "pip_cache", "tools")) {
    if ((Test-Path "$Src\$d") -and -not (Test-Path "$Root\$d")) {
        cmd /c mklink /J "$Root\$d" "$Src\$d" | Out-Null
        Write-Host ("  " + $d + " -> " + $Src + "\" + $d)
    }
}

Step "Конвейер, роли, голоса, словарь произношения"
Copy-Item "$Src\nox_audiobook.py" "$Root\nox_audiobook.new.py" -Force      # правится копия; на место встаёт уже готовой
if ($FastPy -and (Test-Path -LiteralPath $FastPy)) { Copy-Item -LiteralPath $FastPy "$Root\nox_fast.py" -Force }
elseif (Test-Path "$Src\nox_fast.py") { Copy-Item "$Src\nox_fast.py" "$Root\nox_fast.py" -Force }
elseif (Test-Path "$env:USERPROFILE\Downloads\nox_fast.py") { Copy-Item "$env:USERPROFILE\Downloads\nox_fast.py" "$Root\nox_fast.py" -Force }
$seed = $Src
if ($SeedFrom -and (Test-Path "$SeedFrom\work\roles.json")) { $seed = $SeedFrom }   # вторая книга: голоса из аудиоредакции первой
if (-not (Test-Path "$Root\work\roles.json")) { Copy-Item "$seed\work\roles.json" "$Root\work\roles.json"; Write-Host ("  роли взяты из " + $seed) }
if (-not (Test-Path "$Root\work\voices")) { Copy-Item "$seed\work\voices" "$Root\work\voices" -Recurse; Write-Host ("  голоса взяты из " + $seed) }
# ВНИМАНИЕ: в PowerShell $dict и параметр $Dict — одна и та же переменная (регистр не важен), а $Dict объявлен [string]:
# найденный файл превращался в строку без пути, и словарь из Загрузок НЕ копировался. Отсюда отдельное имя $dictFile.
if ($Dict -and (Test-Path -LiteralPath $Dict)) { $dictFile = Get-Item -LiteralPath $Dict }
else { $dictFile = Get-ChildItem "$env:USERPROFILE\Downloads\accent_dict*.csv" -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -notlike "*draft*" } | Sort-Object LastWriteTime -Descending | Select-Object -First 1 }
if ($dictFile) { Copy-Item -LiteralPath $dictFile.FullName "$Root\work\accent_dict.csv" -Force; Write-Host ("  словарь: " + $dictFile.FullName) }
elseif (-not (Test-Path "$Root\work\accent_dict.csv")) { Write-Host "  ВНИМАНИЕ: accent_dict.csv не найден в Загрузках!" -ForegroundColor Yellow }
else { Write-Host "  accent_dict.csv в Загрузках нет — остаётся прежний словарь книги" -ForegroundColor Yellow }
if (Test-Path "$Root\work\accent_dict.csv") {      # какая версия словаря реально работает (строка «# vN» в его шапке)
    $dv = Get-Content "$Root\work\accent_dict.csv" -Encoding UTF8 -TotalCount 40 | Where-Object { $_ -match '^# v\d+' } | Select-Object -Last 1
    if ($dv) { Write-Host ("  версия словаря: " + $dv.Substring(2, [Math]::Min(70, $dv.Length - 2))) }
}
$ovr = $null
$leaf = (Split-Path $Root -Leaf).ToLower()
if ($Overrides -and (Test-Path -LiteralPath $Overrides)) { $ovr = Get-Item -LiteralPath $Overrides }
else {
    # сценарий именно ЭТОЙ книги: первая строка «# book: kniga2a»; файлы без пометки — сценарий первой книги (kniga1a)
    foreach ($f in (Get-ChildItem "$env:USERPROFILE\Downloads\speaker_overrides*.tsv" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending)) {
        $first = (Get-Content -LiteralPath $f.FullName -TotalCount 1 -Encoding UTF8)
        $book = if ($first -match '^#\s*book:\s*(\S+)') { $Matches[1].ToLower() } else { "kniga1a" }
        if ($book -eq $leaf) { $ovr = $f; break }
    }
}
if ($ovr) { Copy-Item $ovr.FullName "$Root\work\speaker_overrides.tsv" -Force; Write-Host ("  сценарий по ролям: " + $ovr.FullName) }
elseif (Test-Path "$Root\work\speaker_overrides.tsv") { Write-Host "  сценарий по ролям: уже в проекте (work\speaker_overrides.tsv)" }
$sw = Get-ChildItem "$env:USERPROFILE\Downloads\stress_words*.txt" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($sw) { Copy-Item $sw.FullName "$Root\work\stress_words.txt" -Force; Write-Host ("  слова с ударением по контексту: " + $sw.FullName) }
Write-Host ("  голосов: " + (Get-ChildItem "$Root\work\voices\*.wav").Count)

Step "Правки копии nox_audiobook.py (дефисы, оглавление, разрывы сцен, заголовки)"
$patchPy = @'
# -*- coding: utf-8 -*-
"""Правки nox_audiobook.py для второй книги: оглавление -> пропуск, ❦/*** -> пауза, заголовки в одну строку."""
import py_compile, sys
from pathlib import Path

p = Path(sys.argv[1] if len(sys.argv) > 1 else "nox_audiobook.py")
s = p.read_text(encoding="utf-8")


EDITS = [
(
'''        style = (p.style.name or "") if p.style is not None else ""
        is_head = style.startswith("Heading") or style.startswith("Заголовок") or bool(HEAD_RE.match(txt))
        is_block = "Block" in style or "Quote" in style or "Цитата" in style
        out.append((txt, is_head, is_block))''',
'''        style = (p.style.name or "") if p.style is not None else ""
        low = style.lower()
        if "toc" in low or "оглавлен" in low or "contents" in low:
            continue                                    # строки оглавления не озвучиваем
        if "scene" in low or not any(ch.isalnum() for ch in txt):
            out.append(("<<<SCENE>>>", False, False))   # разрыв сцены (❦, ***) -> пауза, не текст
            continue
        is_head = style.startswith("Heading") or style.startswith("Заголовок") or bool(HEAD_RE.match(txt))
        if is_head and "\\n" in txt:
            txt = ". ".join(x.strip() for x in txt.split("\\n") if x.strip())
        is_block = "Block" in style or "Quote" in style or "Цитата" in style
        out.append((txt, is_head, is_block))'''
),
(
'''    chapter, ch_title = 0, "Начало"
    narr_run = 0            # подряд идущая проза без диалога (символов) — для сброса сцены
    seg_id = 0''',
'''    chapter, ch_title = 0, "Начало"
    narr_run = 0            # подряд идущая проза без диалога (символов) — для сброса сцены
    seg_id = 0
    extra_pause = 0         # добавочная тишина после разрыва сцены'''
),
(
'''    def add(kind, speaker, conf, pause, text, para_idx):
        nonlocal seg_id
        seg_id += 1''',
'''    def add(kind, speaker, conf, pause, text, para_idx):
        nonlocal seg_id, extra_pause
        pause = int(pause) + extra_pause
        extra_pause = 0
        seg_id += 1'''
),
(
'''    for pi, (txt, is_head, is_block) in enumerate(paras):
        if is_head:''',
'''    for pi, (txt, is_head, is_block) in enumerate(paras):
        if txt == "<<<SCENE>>>":
            tracker.reset_scene()
            narr_run = 0
            extra_pause = max(extra_pause, PAUSE["heading"])
            continue
        if is_head:'''
),
(
        "2b/дефис внутри слова -> пробел",
        '''def prepare_text(text: str, use_num2words: bool, accent: str) -> str:
    t = text.replace("\u00ab", "").replace("\u00bb", "").replace("\u201c", "").replace("\u201d", "")
    t, ph = apply_word_dict(t)''',
        '''def prepare_text(text: str, use_num2words: bool, accent: str) -> str:
    t = text.replace("\u00ab", "").replace("\u00bb", "").replace("\u201c", "").replace("\u201d", "")
    t = re.sub(r"(?<=[\u0410-\u044f\u0401\u0451])-(?=[\u0410-\u044f\u0401\u0451])", " ", t)  # дефис внутри слова -> пробел
    t, ph = apply_word_dict(t)''',
),
]
applied = 0
for item in EDITS:
    name, a, b = (item if len(item) == 3 else ("", item[0], item[1]))
    if b in s or (name.startswith("2b") and "NOX_PT_V" in s):   # уже применено (или функция заменена этапом 4)
        applied += 1
        continue
    if s.count(a) != 1:
        if "NOX_PT_V" in s:        # файл уже патчен ранее, якорь перекрыт поздними этапами — пропускаем
            applied += 1
            continue
        sys.exit(f"правка «{name}»: якорь найден {s.count(a)} раз — файл отличается, патч не применён")
    s = s.replace(a, b)
    applied += 1

# ---- этап 2: ударения-омографы (кодовые трансформации) ----
if "HOMOGRAPHS" not in s:
    anchor = 'WORD_DICT_PATH = WORK / "accent_dict.csv"\n_WORD_DICT = None'
    homo = (
        "\n\n# слова-омографы: ударение зависит от смысла; ruaccent решает по контексту, а мы помечаем\n"
        "# УДВОЕНИЕМ гласной только их (остальным словам движок ставит ударение сам)\n"
        "HOMOGRAPHS = set(\"\"\"\n"
        "замок замка замку замком замке замки замков замкам замками замках\n"
        "дома стоит стоят духи мука орган органа органе полки белки кружки стрелки гвоздики\n"
        "ирис атлас атласе хлопок жаркое пора потом села сели село жила пила вести пропасть\n"
        "берега города леса луга снега окна стены земли воды горы реки доски ноги руки головы дороги\n"
        "целую дорого крою строю звонок? печи свечи\n"
        "ночи болото болота болоту болотом болоте болот болотам болотами болотах\n"
        "болотный болотная болотное болотные болотной болотного болотную болотных болотным болотными\n"
        "\"\"\".split())\n"
        "HOMOGRAPHS.discard(\"звонок?\")"
    )
    assert s.count(anchor) == 1
    s = s.replace(anchor, anchor + homo, 1)
if 'accent == "dbl"' not in s:
    start = s.index("def prepare_text")
    ra = s.index("RUAccent", start)
    old_exc = "        except ImportError:\n            pass\n"
    i = s.index(old_exc, ra)
    warn = (
        "        except ImportError:\n"
        "            if accent == \"dbl\":\n"
        "                global _NO_RUACCENT\n"
        "                if \"_NO_RUACCENT\" not in globals():\n"
        "                    _NO_RUACCENT = True\n"
        "                    print(\"ruaccent не установлен: омографы (замок/дома/уже...) пойдут без контекстных ударений\")\n"
    )
    s = s[:i] + warn + s[i + len(old_exc):]
    j = s.index('    if accent == "acute":', i)
    k = s.index("    return t", j)
    dbl = (
        "    elif accent == \"dbl\":\n"
        "        def _dbl(m):\n"
        "            w = m.group(0)\n"
        "            i = w.index(\"+\")\n"
        "            bare = w.replace(\"+\", \"\")\n"
        "            if i == 0 or bare.lower() not in HOMOGRAPHS:   # только омографы и не первая буква\n"
        "                return bare\n"
        "            return bare[:i] + bare[i] + bare[i:]\n"
        "        t = re.sub(r\"[А-Яа-яЁё]*\\+[А-Яа-яЁё]+\", _dbl, t)\n"
        "        t = t.replace(\"+\", \"\")\n"
    )
    s = s[:k] + dbl + s[k:]
s = s.replace('--accent", default="none", choices=["none", "plus", "acute"]',
              '--accent", default="acute", choices=["none", "plus", "acute", "sparse", "dbl"]', 1)

# ---- этап 3: ruaccent 1.5.8 + transformers 5 ----
# Символьный токенизатор ruaccent под transformers 5 не отдаёт token_type_ids, а его onnx-модель
# ударений (вызывается на каждом незнакомом слове = на всех именах книги) их требует ->
# ValueError валил воркер на первом же имени. Подставляем недостающие входы нулями.
# Плюс общий предохранитель: любой сбой ruaccent -> сегмент идёт без контекстных ударений, воркер живёт.
if "_fix_ruaccent_onnx" not in s:
    helper = (
        "def _fix_ruaccent_onnx(acc):\n"
        "    # ruaccent 1.5.8 + transformers 5: токенизатор не отдаёт token_type_ids, onnx-модель их требует.\n"
        "    # Оборачиваем session.run каждой модели: недостающие входы дополняем нулями, лишние убираем.\n"
        "    import numpy as np\n"
        "    for name in (\"accent_model\", \"omograph_model\", \"stress_usage_model\", \"yo_homograph_model\"):\n"
        "        sess = getattr(getattr(acc, name, None), \"session\", None)\n"
        "        if sess is None or getattr(sess, \"_nox_fixed\", False):\n"
        "            continue\n"
        "        need = [i.name for i in sess.get_inputs()]\n"
        "        orig = sess.run\n"
        "\n"
        "        def run(output_names, feed, *a, _orig=orig, _need=need, **k):\n"
        "            feed = dict(feed)\n"
        "            ref = feed.get(\"input_ids\")\n"
        "            for nm in _need:\n"
        "                if nm not in feed and ref is not None:\n"
        "                    feed[nm] = np.zeros_like(ref)\n"
        "            for nm in list(feed):\n"
        "                if nm not in _need:\n"
        "                    del feed[nm]\n"
        "            return _orig(output_names, feed, *a, **k)\n"
        "\n"
        "        sess.run = run\n"
        "        sess._nox_fixed = True\n"
        "\n"
        "\n"
    )
    i = s.index("def prepare_text")
    s = s[:i] + helper + s[i:]
    old = '                _ACCENTIZER.load(omograph_model_size="turbo", use_dictionary=True)\n'
    if s.count(old) != 1:
        sys.exit("этап 3: строка _ACCENTIZER.load не найдена — файл отличается")
    s = s.replace(old, old + "                _fix_ruaccent_onnx(_ACCENTIZER)\n", 1)
if "_RUACCENT_ERRS" not in s:
    anchor = '                    print("ruaccent не установлен: омографы (замок/дома/уже...) пойдут без контекстных ударений")\n'
    if s.count(anchor) != 1:
        sys.exit("этап 3: блок except ImportError не найден — файл отличается")
    guard = (
        "        except Exception as e:                   # ruaccent споткнулся: сегмент без контекстных ударений, воркер живёт\n"
        "            global _RUACCENT_ERRS\n"
        "            _RUACCENT_ERRS = globals().get(\"_RUACCENT_ERRS\", 0) + 1\n"
        "            if _RUACCENT_ERRS <= 3:\n"
        "                print(f\"ruaccent: {type(e).__name__}: {str(e)[:100]} -> сегмент без контекстных ударений\", flush=True)\n"
    )
    s = s.replace(anchor, anchor + guard, 1)

# ---- этап 4: prepare_text v2 — без плейсхолдеров ----
# Старый порядок: слова словаря -> метки \ue000N\ue001 -> ruaccent -> вернуть слова. Но ruaccent вырезает все
# незнакомые символы, от метки оставалась голая цифра, слово не возвращалось -> имена читались как «0», «1».
# Новый порядок: ruaccent по живому тексту (полный контекст) -> словарь поверх его разметки ('+' внутри слова
# допускается) -> удвоение ударной гласной у контекстных слов. «…» и «–» заменяются на понятные ruaccent.
if "NOX_PT_V3" not in s:
    start = s.index("def prepare_text(")
    end = s.index("\n    return t\n", start) + len("\n    return t\n")
    new_pt = (
        "TEXT_LAYER_VERSION = \"k1a-v5\"   # v5: ударения знаком U+0301 для дообученной модели; v4 = чистый текст\n"
        "VOWELS_RU = \"аеёиоуыэюяАЕЁИОУЫЭЮЯ\"\n"
        "\n"
        "\n"
        "def _ruaccent_marked(acc, t):\n"
        "    # ruaccent точнее на тексте в нижнем регистре (совет автора stress-модели) -> размечаем lower(),\n"
        "    # а регистр букв возвращаем из оригинала пословно; если слова не сошлись — надёжный путь без хитростей\n"
        "    low = t.lower()\n"
        "    marked = acc.process_all(low)\n"
        "    if low == t:\n"
        "        return marked\n"
        "    orig = re.findall(r\"[А-Яа-яЁё]+\", t)\n"
        "    got = re.findall(r\"[а-яё+]+\", marked)\n"
        "    if len(orig) != len(got) or any(a.lower() != b.replace(\"+\", \"\") for a, b in zip(orig, got)):\n"
        "        return acc.process_all(t)\n"
        "    it = iter(orig)\n"
        "\n"
        "    def recase(m):\n"
        "        o = next(it)\n"
        "        out, j = [], 0\n"
        "        for ch in m.group(0):\n"
        "            if ch == \"+\":\n"
        "                out.append(ch)\n"
        "            else:\n"
        "                out.append(o[j])\n"
        "                j += 1\n"
        "        return \"\".join(out)\n"
        "    return re.sub(r\"[а-яё+]+\", recase, marked)\n"
        "\n"
        "\n"
        "def _plus_to_acute(t, sparse=False):\n"
        "    # '+' ruaccent -> U+0301 после ударной гласной (формат stress-модели); ё и односложные не метим.\n"
        "    # sparse: метим только слова от 3 слогов и контекстные (омографы ruaccent); словарь имён помечен всегда\n"
        "    omo = set()\n"
        "    if sparse:\n"
        "        try:\n"
        "            omo = set(getattr(_ACCENTIZER, \"omographs\", {}).keys())\n"
        "        except Exception:\n"
        "            omo = set()\n"
        "\n"
        "    def f(m):\n"
        "        w = m.group(0)\n"
        "        bare = w.replace(\"+\", \"\")\n"
        "        nv = sum(ch in VOWELS_RU for ch in bare)\n"
        "        if \"ё\" in bare.lower() or nv < 2 or (sparse and nv < 3 and bare.lower() not in omo):\n"
        "            return bare\n"
        "        return re.sub(r\"\\+([\" + VOWELS_RU + r\"])\", \"\\\\1\\u0301\", w).replace(\"+\", \"\")\n"
        "    return re.sub(r\"[А-Яа-яЁё]*\\+[А-Яа-яЁё]+\", f, t)\n"
        "\n"
        "\n"
        "def _tune_ruaccent(acc):\n"
        "    # 1) ё: автор пишет ё последовательно (ещё/её/всё/идёт — 7.6 тыс. слов с ё, без пропусков), а модель\n"
        "    #    ruaccent сама меняла е->ё по догадке (небо->нёбо, все->всё) -> её ё-преобразование выключаем совсем\n"
        "    acc._process_yo = lambda words, sentence: words\n"
        "    # 2) считаем слова, которых нет в словаре ruaccent (ударение гадала нейросеть) — для аудита имён\n"
        "    global _NEURAL_WORDS\n"
        "    _NEURAL_WORDS = {}\n"
        "    m = getattr(acc, \"accent_model\", None)\n"
        "    if m is not None and hasattr(m, \"put_accent\"):\n"
        "        orig = m.put_accent\n"
        "\n"
        "        def put_accent(word, _orig=orig):\n"
        "            _NEURAL_WORDS[word.lower()] = _NEURAL_WORDS.get(word.lower(), 0) + 1\n"
        "            return _orig(word)\n"
        "\n"
        "        m.put_accent = put_accent\n"
        "\n"
        "\n"
        "def _dict_pattern():\n"
        "    # регэксп словаря, терпимый к '+' ruaccent внутри слова: Хорв+ата -> ключ Хорвата\n"
        "    global _DICT_RE\n"
        "    if \"_DICT_RE\" not in globals():\n"
        "        d = load_word_dict()\n"
        "        if not d:\n"
        "            _DICT_RE = None\n"
        "        else:\n"
        "            alts = [\"\\\\+?\".join(re.escape(c) for c in w) for w in sorted(d, key=len, reverse=True)]\n"
        "            _DICT_RE = re.compile(r\"(?<![А-Яа-яЁё])\\+?(\" + \"|\".join(alts) + r\")(?![А-Яа-яЁё+])\")\n"
        "    return _DICT_RE\n"
        "\n"
        "\n"
        "def apply_word_dict_marked(t: str) -> str:\n"
        "    # словарь автора главнее ruaccent: заменяем слово целиком вместе с его '+'\n"
        "    rx = _dict_pattern()\n"
        "    if rx is None:\n"
        "        return t\n"
        "    d = load_word_dict()\n"
        "    return rx.sub(lambda m: d[m.group(1).replace(\"+\", \"\")], t)\n"
        "\n"
        "\n"
        "def _extend_homographs(acc):\n"
        "    # контекстные слова самого ruaccent (ночи/дома/стоит/руки...) + work/stress_words.txt автора\n"
        "    stop = set(\"уже потом большая большие большое большую большой большим большими большом мою твою свою\".split())\n"
        "    try:\n"
        "        HOMOGRAPHS.update(w for w in getattr(acc, \"omographs\", {}) if w.isalpha() and w not in stop)\n"
        "    except Exception:\n"
        "        pass\n"
        "    p = WORK / \"stress_words.txt\"\n"
        "    if p.exists():\n"
        "        for line in p.read_text(encoding=\"utf-8\").splitlines():\n"
        "            w = line.split(\"#\")[0].strip().lower()\n"
        "            if w:\n"
        "                HOMOGRAPHS.add(w)\n"
        "\n"
        "\n"
        "def prepare_text(text: str, use_num2words: bool, accent: str) -> str:\n"
        "    # NOX_PT_V3/v5: ruaccent (без его ё, в нижнем регистре) -> словарь автора -> знак ударения (acute) или удвоение (dbl)\n"
        "    t = text.replace(\"\\u00ab\", \"\").replace(\"\\u00bb\", \"\").replace(\"\\u201c\", \"\").replace(\"\\u201d\", \"\")\n"
        "    t = re.sub(r\"(?<=[А-яЁё])-(?=[А-яЁё])\", \" \", t)      # дефис внутри слова -> пробел (кто-то -> кто то)\n"
        "    t = t.replace(\"\\u2026\", \"...\").replace(\"\\u2013\", \"\\u2014\")   # «…» и «–» ruaccent вырезает — заменяем\n"
        "    if use_num2words:\n"
        "        try:\n"
        "            from num2words import num2words\n"
        "            t = re.sub(r\"\\d+\", lambda m: num2words(int(m.group(0)), lang=\"ru\"), t)\n"
        "        except ImportError:\n"
        "            pass\n"
        "    if accent != \"none\":\n"
        "        try:\n"
        "            global _ACCENTIZER\n"
        "            if \"_ACCENTIZER\" not in globals():\n"
        "                from ruaccent import RUAccent\n"
        "                _ACCENTIZER = RUAccent()\n"
        "                _ACCENTIZER.load(omograph_model_size=\"turbo\", use_dictionary=True)\n"
        "                _fix_ruaccent_onnx(_ACCENTIZER)\n"
        "                _extend_homographs(_ACCENTIZER)\n"
        "                _tune_ruaccent(_ACCENTIZER)\n"
        "            t = _ruaccent_marked(_ACCENTIZER, t).replace(\" ~ \", \" \\u2014 \")   # '+' перед ударной гласной\n"
        "        except ImportError:\n"
        "            global _NO_RUACCENT\n"
        "            if \"_NO_RUACCENT\" not in globals():\n"
        "                _NO_RUACCENT = True\n"
        "                print(\"ruaccent не установлен: контекстные ударения (замок/дома/ночи...) не ставятся\", flush=True)\n"
        "        except Exception as e:                   # ruaccent споткнулся: сегмент без контекстных ударений, воркер живёт\n"
        "            global _RUACCENT_ERRS\n"
        "            _RUACCENT_ERRS = globals().get(\"_RUACCENT_ERRS\", 0) + 1\n"
        "            if _RUACCENT_ERRS <= 3:\n"
        "                print(f\"ruaccent: {type(e).__name__}: {str(e)[:100]} -> сегмент без контекстных ударений\", flush=True)\n"
        "    t = apply_word_dict_marked(t)                      # словарь автора — поверх ruaccent\n"
        "    if accent in (\"acute\", \"sparse\"):\n"
        "        t = _plus_to_acute(t, accent == \"sparse\")\n"
        "    elif accent == \"dbl\":\n"
        "        def _dbl(m):\n"
        "            w = m.group(0)\n"
        "            i = w.index(\"+\")\n"
        "            bare = w.replace(\"+\", \"\")\n"
        "            if i == 0 or bare[i] in \"ёЁ\" or bare.lower() not in HOMOGRAPHS:   # контекстные слова; не первая буква; ё и так ударная\n"
        "                return bare\n"
        "            return bare[:i] + bare[i] + bare[i:]\n"
        "        t = re.sub(r\"[А-Яа-яЁё]*\\+[А-Яа-яЁё]+\", _dbl, t)\n"
        "        t = t.replace(\"+\", \"\")\n"
        "    elif accent == \"none\":                            # чистый текст: ни '+', ни знаков ударения (и из словаря тоже)\n"
        "        t = t.replace(\"+\", \"\").replace(\"\\u0301\", \"\")\n"
        "    return t\n"
    )
    s = s[:start] + new_pt + s[end:]

# ---- этап 6: сценарий по ролям (work/speaker_overrides.tsv) + мягкая обрезка тишины ----
# speaker_overrides.tsv: строки «para<TAB>*<TAB>Роль» (все реплики абзаца) или «para<TAB>префикс<TAB>Роль» (конкретная
# реплика по первым символам). Применяется при разбивке: kind=speech получает роль из сценария, conf=script.
if "SPEAKER_OVERRIDES" not in s:
    s = s.replace('def trim_silence(wav, sr, thresh_db=-42.0, pad_ms=60):',
                  'def trim_silence(wav, sr, thresh_db=-48.0, pad_ms=150):   # мягче: не срезать тихие концы слов', 1)
    anchor6 = "    extra_pause = 0         # добавочная тишина после разрыва сцены\n"
    if s.count(anchor6) != 1:
        sys.exit("этап 6: якорь extra_pause не найден")
    load_ovr = (
        "    extra_pause = 0         # добавочная тишина после разрыва сцены\n"
        "    SPEAKER_OVERRIDES = {}  # para -> [(prefix|'*', role), ...]\n"
        "    ovr_path = WORK / \"speaker_overrides.tsv\"\n"
        "    if ovr_path.exists():\n"
        "        for line in ovr_path.read_text(encoding=\"utf-8\").splitlines():\n"
        "            if not line.strip() or line.startswith(\"#\"):\n"
        "                continue\n"
        "            parts = line.rstrip(\"\\n\").split(\"\\t\")\n"
        "            if len(parts) < 3:\n"
        "                continue\n"
        "            try:\n"
        "                SPEAKER_OVERRIDES.setdefault(int(parts[0]), []).append((parts[1].strip(), parts[2].strip()))\n"
        "            except ValueError:\n"
        "                continue\n"
        "        print(f\"  сценарий по ролям: {len(SPEAKER_OVERRIDES)} абзацев из {ovr_path.name}\")\n"
        "    ovr_last = {}\n"
        "\n"
        "    def _norm(t):\n"
        "        return re.sub(r\"[^А-Яа-яЁёA-Za-z0-9]\", \"\", t).lower()\n"
    )
    s = s.replace(anchor6, load_ovr, 1)
    anchor_add = (
        "    def add(kind, speaker, conf, pause, text, para_idx):\n"
        "        nonlocal seg_id, extra_pause\n"
        "        pause = int(pause) + extra_pause\n"
        "        extra_pause = 0\n"
    )
    if s.count(anchor_add) != 1:
        sys.exit("этап 6: якорь add() не найден")
    apply_ovr = anchor_add + (
        "        if kind == \"speech\" and para_idx in SPEAKER_OVERRIDES:\n"
        "            role = None\n"
        "            nt = _norm(text)\n"
        "            for pref, rl in SPEAKER_OVERRIDES[para_idx]:\n"
        "                if pref == \"*\":\n"
        "                    role = rl\n"
        "                    break\n"
        "                np_ = _norm(pref)\n"
        "                if np_ and (np_ in nt or (len(nt) >= 12 and nt in np_)):   # начало реплики внутри куска\n"
        "                    role = rl\n"
        "                    break\n"
        "            if role is None:\n"
        "                role = ovr_last.get(para_idx)        # продолжение той же реплики (разрезана по длине)\n"
        "            if role:\n"
        "                speaker, conf = role, \"script\"\n"
        "                ovr_last[para_idx] = role\n"
    )
    s = s.replace(anchor_add, apply_ovr, 1)

# ---- этап 8: сценарий может задать вид абзаца без тире ----
# «para<TAB>!<TAB>Роль» — весь абзац прозы читает Роль (прямая речь без тире: записанные показания, письмо);
# «para<TAB>—<TAB>Роль» — абзац без тире разбирать как реплику с ремарками («Ты пришёл, — сказал Голос.»), говорящий — Роль.
if "NOX_PT_V8" not in s:
    a8 = "        is_dialog = txt[0] in DASHES\n"
    if s.count(a8) != 1:
        sys.exit("этап 8: якорь is_dialog не найден")
    b8 = a8 + (
        "        # NOX_PT_V8: принудительный вид абзаца из сценария («!» — весь абзац речь Роли; «—» — разобрать как реплику)\n"
        "        force_kind, force_role = None, None\n"
        "        for pref, rl in SPEAKER_OVERRIDES.get(pi, []):\n"
        "            if pref in (\"!\", \"—\", \"–\", \"-\") and rl:\n"
        "                force_kind, force_role = pref, rl\n"
        "        if force_kind == \"!\":\n"
        "            narr_run = 0\n"
        "            tracker.note_narration(txt)\n"
        "            for li, line in enumerate(lines):\n"
        "                pause = PAUSE[\"dialog\"] if li == 0 else PAUSE[\"poem_line\"]\n"
        "                for j, ch in enumerate(chunk_text(line, max_chars)):\n"
        "                    add(\"speech\", force_role, \"script\", pause if j == 0 else PAUSE[\"chunk\"], ch, pi)\n"
        "            continue\n"
        "        if force_kind is not None:\n"
        "            is_dialog = True\n"
    )
    s = s.replace(a8, b8, 1)
    a8b = (
        "        if speaker is None:\n"
        "            speaker, conf = tracker.by_alternation()\n"
        "        tracker.commit(speaker)\n"
    )
    if s.count(a8b) != 1:
        sys.exit("этап 8: якорь by_alternation не найден")
    b8b = (
        "        if speaker is None:\n"
        "            speaker, conf = tracker.by_alternation()\n"
        "        if force_kind is not None and force_role:        # NOX_PT_V8: говорящий задан сценарием\n"
        "            speaker, conf = force_role, \"script\"\n"
        "        tracker.commit(speaker)\n"
    )
    s = s.replace(a8b, b8b, 1)

# ---- этап 9: привязка строк сценария к абзацам по отпечатку текста (переживает правки книги) ----
# 4-я колонка speaker_overrides.tsv — первые 24 буквы/цифры абзаца; если абзац с таким номером другой (автор вставил
# или убрал абзацы), строка переезжает к ближайшему абзацу с таким же началом; не найденные — отбрасываются с сообщением.
if "NOX_PT_V9" not in s:
    a9 = "    SPEAKER_OVERRIDES = {}  # para -> [(prefix|'*', role), ...]\n"
    if s.count(a9) != 1:
        sys.exit("этап 9: якорь SPEAKER_OVERRIDES не найден")
    s = s.replace(a9, a9 + "    OVR_FP = {}             # para -> отпечаток текста абзаца (NOX_PT_V9)\n", 1)
    a9b = "                SPEAKER_OVERRIDES.setdefault(int(parts[0]), []).append((parts[1].strip(), parts[2].strip()))\n"
    if s.count(a9b) != 1:
        sys.exit("этап 9: якорь загрузки сценария не найден")
    s = s.replace(a9b, a9b + (
        "                if len(parts) > 3 and parts[3].strip():\n"
        "                    OVR_FP[int(parts[0])] = parts[3].strip()\n"), 1)
    a9c = "    ovr_last = {}\n"
    if s.count(a9c) != 1:
        sys.exit("этап 9: якорь ovr_last не найден")
    s = s.replace(a9c, a9c + (
        "    if SPEAKER_OVERRIDES and OVR_FP:   # NOX_PT_V9: сверка номеров абзацев с отпечатками текста\n"
        "        def _fp(t):\n"
        "            return re.sub(r\"[^А-Яа-яЁёA-Za-z0-9]\", \"\", t).lower()[:24]\n"
        "        fps = [_fp(p_[0]) for p_ in paras]\n"
        "        W = 150\n"
        "        cand = {p0: [i for i in range(max(0, p0 - W), min(len(fps), p0 + W)) if fps[i] == fp]\n"
        "                for p0, fp in OVR_FP.items()}\n"
        "        # сдвиг определяем по однозначным строкам, неоднозначные («Да.», «Хм.») берут сдвиг ближайшей однозначной\n"
        "        offs = {p0: c[0] - p0 for p0, c in cand.items() if len(c) == 1}\n"
        "        known = sorted(offs)\n"
        "        import bisect\n"
        "        def _local_off(p0):\n"
        "            if not known:\n"
        "                return 0\n"
        "            k = bisect.bisect_left(known, p0)\n"
        "            near = min(known[max(0, k - 1):k + 1], key=lambda q: abs(q - p0))\n"
        "            return offs[near]\n"
        "        remap, lost = {}, []\n"
        "        for p0, c in cand.items():\n"
        "            if not c:\n"
        "                lost.append(p0)\n"
        "                continue\n"
        "            want = p0 + _local_off(p0)\n"
        "            tgt = want if want in c else min(c, key=lambda i: abs(i - want))\n"
        "            if tgt != p0:\n"
        "                remap[p0] = tgt\n"
        "        if remap or lost:\n"
        "            new_ovr = {}\n"
        "            for p0, lst in SPEAKER_OVERRIDES.items():\n"
        "                if p0 in lost:\n"
        "                    continue\n"
        "                new_ovr.setdefault(remap.get(p0, p0), []).extend(lst)\n"
        "            SPEAKER_OVERRIDES.clear()\n"
        "            SPEAKER_OVERRIDES.update(new_ovr)\n"
        "            print(f\"  сценарий: книга правилась — строк переехало к другим абзацам: {len(remap)}, не найдено: {len(lost)}\")\n"
        "            if lost:\n"
        "                (WORK / \"speaker_overrides_lost.txt\").write_text(\"\\n\".join(str(x) for x in lost), encoding=\"utf-8\")\n"), 1)

# ---- этап 7: темп при сборке — глобальный множитель + выравнивание медленных реплик по своей роли ----
if "NOX_ASM_V2" not in s:
    a_start = s.index("def assemble(rows, roles, head_pause=True):")
    a_end = s.index("def atempo_chain(speed: float) -> str:")
    new_asm = (
        "TEMPO_G = 1.0        # общий множитель темпа при сборке (--tempo)\n"
        "TEMPO_EQ = True      # выравнивание: медленные реплики подтягиваются к медиане темпа своей роли (до x1.25)\n"
        "PAUSE_SCALE = 0.85   # множитель пауз между сегментами при сборке (--pauses)\n"
        "TRIM_PAUSES = True   # сжимать длинные паузы внутри фраз (--no-trim-pauses выключает)\n"
        "\n"
        "\n"
        "def _squeeze_pauses(x, sr, keep=0.35, frac=0.4, cap=0.8):\n"
        "    # паузы внутри фразы длиннее keep сжимаются: new = keep + (dur-keep)*frac, не больше cap;\n"
        "    # вырезается середина паузы (края затухания остаются) — без щелчков, речь не трогается\n"
        "    import numpy as np\n"
        "    w = int(sr * 0.01)\n"
        "    k = len(x) // w\n"
        "    if k < 20:\n"
        "        return x\n"
        "    a = np.abs(x[:k * w]).reshape(k, w).max(axis=1)\n"
        "    thr = max(2e-3, 0.03 * float(np.percentile(a, 95)))\n"
        "    sil = a < thr\n"
        "    out, i = [], 0\n"
        "    while i < k:\n"
        "        j = i\n"
        "        while j < k and sil[j] == sil[i]:\n"
        "            j += 1\n"
        "        if sil[i] and (j - i) * 0.01 > keep:\n"
        "            dur = (j - i) * 0.01\n"
        "            n_keep = int(min(cap, keep + (dur - keep) * frac) / 0.01)\n"
        "            head = n_keep // 2\n"
        "            tail = n_keep - head\n"
        "            out.append(x[i * w:(i + head) * w])\n"
        "            out.append(x[(j - tail) * w:j * w])\n"
        "        else:\n"
        "            out.append(x[i * w:j * w])\n"
        "        i = j\n"
        "    out.append(x[k * w:])\n"
        "    return np.concatenate(out)\n"
        "RE_RU_LETTERS = re.compile(r\"[^А-Яа-яЁё]\")\n"
        "\n"
        "\n"
        "def _stretch(x, sr, factor, key):\n"
        "    # ускорение без изменения тона (ffmpeg atempo); результат кэшируется в work/tempo\n"
        "    import subprocess\n"
        "    import soundfile as sf\n"
        "    if abs(factor - 1.0) < 0.015:\n"
        "        return x\n"
        "    cdir = WORK / \"tempo\"\n"
        "    cdir.mkdir(exist_ok=True)\n"
        "    cp = cdir / f\"{key}_{factor:.3f}.wav\"\n"
        "    if cp.exists():\n"
        "        y, _ = sf.read(str(cp), dtype=\"float32\")\n"
        "        return y\n"
        "    ff = find_ffmpeg()\n"
        "    if not ff:\n"
        "        return x\n"
        "    src = cdir / f\"{key}_src.wav\"\n"
        "    tmp = cdir / f\"{key}_{factor:.3f}.tmp.wav\"\n"
        "    sf.write(str(src), x, sr, format=\"WAV\", subtype=\"PCM_16\")\n"
        "    r = subprocess.run([ff, \"-y\", \"-hide_banner\", \"-loglevel\", \"error\", \"-i\", str(src),\n"
        "                        \"-af\", atempo_chain(factor), str(tmp)])\n"
        "    try:\n"
        "        src.unlink()\n"
        "    except OSError:\n"
        "        pass\n"
        "    if r.returncode != 0 or not tmp.exists():\n"
        "        return x\n"
        "    os.replace(tmp, cp)\n"
        "    y, _ = sf.read(str(cp), dtype=\"float32\")\n"
        "    return y\n"
        "\n"
        "\n"
        "def assemble(rows, roles, head_pause=True):\n"
        "    # NOX_ASM_V2: склейка сегментов с паузами + темп (TEMPO_G x выравнивание по роли) -> (audio, sr, главы, missing)\n"
        "    import numpy as np\n"
        "    import soundfile as sf\n"
        "    loaded, sr, missing = [], None, 0\n"
        "    for i, r in enumerate(rows):\n"
        "        p = wav_for(r, roles)\n"
        "        if not p.exists():\n"
        "            missing += 1\n"
        "            continue\n"
        "        x, s_ = sf.read(str(p), dtype=\"float32\")\n"
        "        if x.ndim > 1:\n"
        "            x = x.mean(axis=1)\n"
        "        sr = sr or s_\n"
        "        if s_ != sr:\n"
        "            sys.exit(f\"разная частота дискретизации: {p} {s_} != {sr}\")\n"
        "        loaded.append((i, r, x))\n"
        "    if not loaded:\n"
        "        return None, None, [], missing\n"
        "    factors = {}\n"
        "    if TEMPO_EQ and TEMPO_G > 0:\n"
        "        by_role = {}\n"
        "        for i, r, x in loaded:\n"
        "            letters = len(RE_RU_LETTERS.sub(\"\", r[\"text\"]))\n"
        "            if letters >= 40 and r[\"kind\"] != \"head\":\n"
        "                by_role.setdefault(resolve_role(r[\"speaker\"], roles), []).append((i, letters / max(len(x) / sr, 0.05)))\n"
        "        for role, items in by_role.items():\n"
        "            if len(items) >= 5:\n"
        "                med = float(np.median([v for _, v in items]))\n"
        "                for i, rate in items:\n"
        "                    factors[i] = min(max(med / rate, 1.0), 1.25)   # только подтягиваем медленных\n"
        "    pdiv = TEMPO_G ** 0.5 if TEMPO_G > 1 else 1.0            # паузы укорачиваем мягче, чем речь\n"
        "    pieces, chapters, t = [], [], 0.0\n"
        "    for i, r, x in loaded:\n"
        "        pause = int(r[\"pause_before_ms\"]) if (i > 0 or head_pause) else 0\n"
        "        pause = int(pause * PAUSE_SCALE / pdiv)\n"
        "        pieces.append(np.zeros(int(sr * pause / 1000), dtype=\"float32\"))\n"
        "        t += pause / 1000\n"
        "        if r[\"kind\"] == \"head\":\n"
        "            if chapters and chapters[-1][1] == r[\"ch_title\"] and t - chapters[-1][0] < 15:\n"
        "                chapters.pop()\n"
        "            chapters.append((t, r[\"ch_title\"]))\n"
        "        if TRIM_PAUSES:\n"
        "            x = _squeeze_pauses(x, sr)\n"
        "        f = TEMPO_G * factors.get(i, 1.0)\n"
        "        x = _stretch(x, sr, f, wav_for(r, roles).stem + (\"t\" if TRIM_PAUSES else \"\"))\n"
        "        pieces.append(x)\n"
        "        t += len(x) / sr\n"
        "    pieces.append(np.zeros(int(sr * PAUSE[\"chapter_end\"] / 1000 * PAUSE_SCALE / pdiv), dtype=\"float32\"))\n"
        "    return np.concatenate(pieces), sr, chapters, missing\n"
        "\n"
        "\n"
    )
    s = s[:a_start] + new_asm + s[a_end:]
    # планирование частей — в минутах ВЫХОДА (с учётом темпа)
    s = s.replace("        if acc >= minutes * 60 and boundary_ok:", "        if acc >= minutes * 60 * max(TEMPO_G, 1.0) and boundary_ok:", 1)
    # CLI: --tempo / --no-equalize у build и watch; установка глобалей после разбора аргументов
    s = s.replace('    w.add_argument("--speed", type=float, default=1.0, help="тот же смысл, что у build --speed")\n',
                  '    w.add_argument("--speed", type=float, default=1.0, help="тот же смысл, что у build --speed")\n'
                  '    w.add_argument("--tempo", type=float, default=1.0, help="темп речи при сборке (1.3 = +30%), паузы укорачиваются мягче")\n'
                  '    w.add_argument("--no-equalize", action="store_true", help="не подтягивать медленные реплики к темпу роли")\n'
                  '    w.add_argument("--pauses", type=float, default=0.85, help="множитель пауз между репликами/абзацами при сборке")\n'
                  '    w.add_argument("--no-trim-pauses", action="store_true", help="не сжимать длинные паузы внутри фраз")\n', 1)
    s = s.replace('    b.add_argument("--strict", action="store_true", help="падать, если нет какого-то wav")\n',
                  '    b.add_argument("--strict", action="store_true", help="падать, если нет какого-то wav")\n'
                  '    b.add_argument("--tempo", type=float, default=1.0, help="темп речи при сборке (1.3 = +30%)")\n'
                  '    b.add_argument("--no-equalize", action="store_true")\n'
                  '    b.add_argument("--pauses", type=float, default=0.85)\n'
                  '    b.add_argument("--no-trim-pauses", action="store_true")\n', 1)
    if s.count("    args = ap.parse_args()\n") != 1:
        sys.exit("этап 7: якорь parse_args не найден")
    s = s.replace("    args = ap.parse_args()\n",
                  "    args = ap.parse_args()\n"
                  "    global TEMPO_G, TEMPO_EQ, PAUSE_SCALE, TRIM_PAUSES\n"
                  "    TEMPO_G = float(getattr(args, \"tempo\", 1.0) or 1.0)\n"
                  "    TEMPO_EQ = not getattr(args, \"no_equalize\", False)\n"
                  "    PAUSE_SCALE = float(getattr(args, \"pauses\", 0.85) or 0.85)\n"
                  "    TRIM_PAUSES = not getattr(args, \"no_trim_pauses\", False)\n", 1)
    # watch: при смене темпа части пересобираются
    anchor_w = "    sent = set()\n"
    if s.count(anchor_w) != 1:
        sys.exit("этап 7: якорь watch/sent не найден")
    s = s.replace(anchor_w, anchor_w +
                  "    tv = WORK / \"tempo.ver\"\n"
                  "    cur_t = f\"{TEMPO_G:.3f}|{int(TEMPO_EQ)}|{PAUSE_SCALE:.2f}|{int(TRIM_PAUSES)}\"\n"
                  "    prev_t = tv.read_text(encoding=\"utf-8\").strip() if tv.exists() else \"\"\n"
                  "    if prev_t != cur_t and PARTS.is_dir() and any(PARTS.glob(\"part_*\")):\n"
                  "        for f in PARTS.glob(\"part_*\"):\n"
                  "            f.unlink()\n"
                  "        for f in (PARTS_INDEX, DUR_CACHE):\n"
                  "            if f.exists():\n"
                  "                f.unlink()\n"
                  "        print(f\"watch: темп сборки изменился ({prev_t or 'нет'} -> {cur_t}) — части пересобираются\")\n"
                  "    tv.write_text(cur_t, encoding=\"utf-8\")\n", 1)
# ---- этап 10: настройки книги из work/settings.json («Настройки озвучки.xlsx»): паузы, сжатие пауз, темп/громкость ролей ----
if "NOX_PT_V10" not in s:
    i0 = s.index("\nPAUSE = dict(")
    i1 = s.index("\n", i0 + 1) + 1
    s = s[:i1] + (
        "# NOX_PT_V10: настройки книги (пишет nastroyki_ozvuchki.py из «Настройки озвучки.xlsx»)\n"
        "try:\n"
        "    SETTINGS = json.loads((WORK / \"settings.json\").read_text(encoding=\"utf-8\"))\n"
        "except Exception:\n"
        "    SETTINGS = {}\n"
        "PAUSE.update({k: int(v) for k, v in (SETTINGS.get(\"pause\") or {}).items() if k in PAUSE})\n"
    ) + s[i1:]
    a10 = "TRIM_PAUSES = True   # сжимать длинные паузы внутри фраз (--no-trim-pauses выключает)\n"
    if s.count(a10) != 1:
        sys.exit("этап 10: якорь TRIM_PAUSES не найден")
    s = s.replace(a10, a10 + (
        "SQZ_KEEP = float(SETTINGS.get(\"sqz_keep\", 0.35))   # паузы внутри фразы короче этого не трогаются, с\n"
        "SQZ_CAP = float(SETTINGS.get(\"sqz_cap\", 0.8))      # и после сжатия не длиннее этого, с\n"), 1)
    a10b = (
        "        if TRIM_PAUSES:\n"
        "            x = _squeeze_pauses(x, sr)\n"
        "        f = TEMPO_G * factors.get(i, 1.0)\n"
        "        x = _stretch(x, sr, f, wav_for(r, roles).stem + (\"t\" if TRIM_PAUSES else \"\"))\n"
    )
    if s.count(a10b) != 1:
        sys.exit("этап 10: якорь сборки (темп) не найден")
    s = s.replace(a10b, (
        "        rcfg = roles[\"roles\"].get(resolve_role(r[\"speaker\"], roles), {})   # NOX_PT_V10: темп/громкость роли\n"
        "        if TRIM_PAUSES:\n"
        "            x = _squeeze_pauses(x, sr, keep=SQZ_KEEP, cap=SQZ_CAP)\n"
        "        f = TEMPO_G * factors.get(i, 1.0) * float(rcfg.get(\"tempo\", 1.0) or 1.0)\n"
        "        sfx = \"\"\n"
        "        if TRIM_PAUSES:\n"
        "            sfx = \"t\" if (SQZ_KEEP, SQZ_CAP) == (0.35, 0.8) else f\"t{int(SQZ_KEEP * 100)}_{int(SQZ_CAP * 100)}\"\n"
        "        x = _stretch(x, sr, f, wav_for(r, roles).stem + sfx)\n"
        "        gain = float(rcfg.get(\"gain\", 1.0) or 1.0)\n"
        "        if abs(gain - 1.0) > 0.01:\n"
        "            x = np.clip(x * gain, -1.0, 1.0)\n"
    ), 1)
    a10c = (
        "    if prev_t != cur_t and PARTS.is_dir() and any(PARTS.glob(\"part_*\")):\n"
        "        for f in PARTS.glob(\"part_*\"):\n"
        "            f.unlink()\n"
        "        for f in (PARTS_INDEX, DUR_CACHE):\n"
        "            if f.exists():\n"
        "                f.unlink()\n"
        "        print(f\"watch: темп сборки изменился ({prev_t or 'нет'} -> {cur_t}) — части пересобираются\")\n"
        "    tv.write_text(cur_t, encoding=\"utf-8\")\n"
    )
    if s.count(a10c) != 1:
        sys.exit("этап 10: якорь watch/tempo.ver не найден")
    s = s.replace(a10c, (
        "    # NOX_PT_V10: полный отпечаток сборки — темп, паузы, сжатие, темп/громкость ролей, части, формат, подписи\n"
        "    roles0 = load_roles()\n"
        "    asm = dict(t=cur_t, pause=PAUSE, sqz=[SQZ_KEEP, SQZ_CAP], parts=args.parts, fmt=args.format, br=args.bitrate,\n"
        "               title=args.title, author=args.author, speed=args.speed,\n"
        "               roles={k: [float(v.get(\"tempo\", 1.0) or 1.0), float(v.get(\"gain\", 1.0) or 1.0)]\n"
        "                      for k, v in sorted(roles0[\"roles\"].items())\n"
        "                      if float(v.get(\"tempo\", 1.0) or 1.0) != 1.0 or float(v.get(\"gain\", 1.0) or 1.0) != 1.0})\n"
        "    asm_s = json.dumps(asm, ensure_ascii=False, sort_keys=True)\n"
        "    av = WORK / \"assembly.json\"\n"
        "    prev_a = av.read_text(encoding=\"utf-8\").strip() if av.exists() else \"\"\n"
        "    asm_changed = (prev_a != asm_s) if prev_a else (prev_t != cur_t)\n"
        "    if asm_changed and PARTS.is_dir() and any(PARTS.glob(\"part_*\")):\n"
        "        for f in list(PARTS.glob(\"part_*\")) + [HERE / \"book.mp3\", HERE / \"book.m4b\"]:   # НЕ book.docx!\n"
        "            if not f.exists():\n"
        "                continue\n"
        "            f.unlink()\n"
        "        for f in (PARTS_INDEX, DUR_CACHE):\n"
        "            if f.exists():\n"
        "                f.unlink()\n"
        "        if dest and dest.is_dir():                       # старые части на Диске — другого темпа/нарезки\n"
        "            for f in list(dest.glob(\"part_*.mp3\")) + list(dest.glob(\"part_*.m4b\")) + [dest / \"book.mp3\", dest / \"book.m4b\"]:\n"
        "                try:\n"
        "                    f.unlink()\n"
        "                except OSError:\n"
        "                    pass\n"
        "        print(\"watch: настройки сборки изменились (темп/паузы/роли/части) — части пересобираются, старые на Диске убраны\")\n"
        "    tv.write_text(cur_t, encoding=\"utf-8\")\n"
        "    av.write_text(asm_s, encoding=\"utf-8\")\n"
    ), 1)

# ---- этап 12: разбор диалога — вставка «— …, —» внутри ремарки не ломает чередование речь/ремарка ----
if "NOX_PT_V12" not in s:
    a12 = "        parts = SPLIT_RE.split(body)\n"
    if s.count(a12) != 1:
        sys.exit("этап 12: якорь SPLIT_RE.split не найден")
    s = s.replace(a12, a12 + (
        "        # NOX_PT_V12: «— Маргит, — согласился он. Потом кивнул — я похолодел, — и сказал: — Мэри говорит…»:\n"
        "        # тире после «похолодел,» закрывает вставку внутри ремарки, а не начинает речь (дальше — строчная буква)\n"
        "        _k = 1\n"
        "        while _k < len(parts) - 1:\n"
        "            if len(re.findall(r\"(?<![,.!?…;:»”\\\"])\\s[—–]\\s\", \" \" + parts[_k] + \" \")) % 2 == 1 and parts[_k + 1][:1].islower():\n"
        "                parts = parts[:_k] + [parts[_k] + \" — \" + parts[_k + 1]] + parts[_k + 2:]\n"
        "                continue\n"
        "            _k += 2\n"
        "        if force_kind not in (None, \"!\") and parts and parts[0].rstrip().endswith(\":\"):\n"
        "            parts = [\"\"] + parts             # абзац начинается словами автора: «Он сказал: — …»\n"
    ), 1)

# ---- этап 11: числа, время, римские номера глав, латиница -> слова (слой текста v6) ----
NUMNORM = r'''
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
'''
if "NOX_PT_V11" not in s:
    a11 = "def prepare_text(text: str, use_num2words: bool, accent: str) -> str:\n"
    if s.count(a11) != 1:
        sys.exit("этап 11: якорь prepare_text не найден")
    s = s.replace(a11, NUMNORM + "\n\n" + a11, 1)
    a11b = r'    t = text.replace("\u00ab", "").replace("\u00bb", "").replace("\u201c", "").replace("\u201d", "")' + "\n"
    if s.count(a11b) != 1:
        sys.exit("этап 11: якорь кавычек в prepare_text не найден")
    s = s.replace(a11b, a11b + "    t = _normalize_numbers(t)                        # NOX_PT_V11: 23:47, (XXIX), №14, KA 7 -> слова\n", 1)
    if 'TEXT_LAYER_VERSION = "k1a-v5"' not in s:
        sys.exit("этап 11: TEXT_LAYER_VERSION v5 не найден")
    s = s.replace('TEXT_LAYER_VERSION = "k1a-v5"', 'TEXT_LAYER_VERSION = "k1a-v6"', 1)
    # словарь: границы слова — не только кириллица, но и цифры/латиница
    s = s.replace('_DICT_RE = re.compile(r"(?<![А-Яа-яЁё])\\+?(" + "|".join(alts) + r")(?![А-Яа-яЁё+])")',
                  '_DICT_RE = re.compile(r"(?<![А-Яа-яЁёA-Za-z0-9])\\+?(" + "|".join(alts) + r")(?![А-Яа-яЁёA-Za-z0-9+])")', 1)

# ---- этап 13: сборка частей — свежий кэш ускорения, паузы после точки и при смене голоса, ровная громкость,
#      части только из проверенных записей и ПЕРЕСБОРКА части, если в ней переозвучена хоть одна реплика ----
if "NOX_PT_V13" not in s:
    def _rep(a, b, what):
        global s
        if s.count(a) != 1:
            sys.exit(f"этап 13: якорь «{what}» найден {s.count(a)} раз(а) — патч не применён")
        s = s.replace(a, b, 1)

    # 1) паузы: между предложениями длинного абзаца и при смене голоса внутри абзаца (раньше обе — 120 мс)
    _rep('PAUSE.update({k: int(v) for k, v in (SETTINGS.get("pause") or {}).items() if k in PAUSE})\n',
         r'''PAUSE.setdefault("sentence", 420)   # NOX_PT_V13: кусок абзаца кончился точкой -> пауза предложения
PAUSE.setdefault("turn", 260)       # NOX_PT_V13: смена голоса внутри абзаца («— Стой! — крикнул он»)
PAUSE.update({k: int(v) for k, v in (SETTINGS.get("pause") or {}).items() if k in PAUSE})
''', "PAUSE.update")
    _rep("    def add(kind, speaker, conf, pause, text, para_idx):\n        nonlocal seg_id, extra_pause\n",
         r'''    def add(kind, speaker, conf, pause, text, para_idx):
        nonlocal seg_id, extra_pause
        if int(pause) == PAUSE["chunk"] and rows and int(rows[-1]["para"]) == para_idx:   # NOX_PT_V13
            _prev = rows[-1]
            if re.search(r"[.!?…][\"»”)]*\s*$", _prev["text"]) and \
                    not (_prev["kind"] == "speech" and kind == "narr" and not text[:1].isupper()):
                pause = PAUSE["sentence"]      # конец предложения (кроме «— Стой! — крикнул он»)
            elif _prev["kind"] != kind:
                pause = PAUSE["turn"]
''', "add()")

    # 2) кэш ускорения: ключ — СОДЕРЖИМОЕ записи. Раньше ключом было только имя реплики, и переозвученная
    #    (исправленная) реплика в части подменялась старой ускоренной копией — обрезанной или прежним голосом.
    _rep("    for i, r, x in loaded:\n        pause = int(r[\"pause_before_ms\"]) if (i > 0 or head_pause) else 0\n",
         r'''    for i, r, x in loaded:
        _h = hashlib.blake2b(np.ascontiguousarray(x).tobytes(), digest_size=6).hexdigest()   # NOX_PT_V13
        pause = int(r["pause_before_ms"]) if (i > 0 or head_pause) else 0
''', "цикл сборки")
    _rep("        x = _stretch(x, sr, f, wav_for(r, roles).stem + sfx)\n",
         r'''        x = _stretch(x, sr, f, wav_for(r, roles).stem + "_" + _h + sfx)
        if NORM_ON:
            x = _level(x, sr, NORM_DB)        # NOX_PT_V13: ровная громкость реплик и голосов
''', "_stretch(...)")
    # ускорение (ffmpeg atempo) на хвосте файла может съесть последние десятки мс — даём запас тишины и отрезаем его
    _rep('    sf.write(str(src), x, sr, format="WAV", subtype="PCM_16")\n',
         '    import numpy as np\n'
         '    sf.write(str(src), np.concatenate([x, np.zeros(int(sr * 0.3), dtype="float32")]), sr, format="WAV",\n'
         '             subtype="PCM_16")   # NOX_PT_V13: запас тишины — конец слова не съедается\n', "src для atempo")
    _rep('''    if r.returncode != 0 or not tmp.exists():
        return x
    os.replace(tmp, cp)
    y, _ = sf.read(str(cp), dtype="float32")
    return y
''', '''    if r.returncode != 0 or not tmp.exists():
        return x
    y, _ = sf.read(str(tmp), dtype="float32")
    y = y[:int(len(x) / factor) + int(sr * 0.03)]          # запас обратно (вся речь внутри)
    sf.write(str(tmp), y, sr, format="WAV", subtype="PCM_16")
    os.replace(tmp, cp)
    return y
''', "конец _stretch")

    # 3) выравнивание медленных реплик: темп по звучащей речи (паузы внутри не в счёт), предел из настроек
    _rep("                by_role.setdefault(resolve_role(r[\"speaker\"], roles), []).append((i, letters / max(len(x) / sr, 0.05)))\n",
         "                by_role.setdefault(resolve_role(r[\"speaker\"], roles), []).append((i, letters / max(_active_sec(x, sr), 0.05)))\n",
         "темп реплики")
    _rep("                    factors[i] = min(max(med / rate, 1.0), 1.25)   # только подтягиваем медленных\n",
         "                    factors[i] = min(max(med / rate, 1.0), EQ_MAX)   # только подтягиваем медленных (NOX_PT_V13)\n",
         "предел выравнивания")

    # 4) порог тишины при сжатии пауз ниже: тихие концы слов и шёпот не считаются паузой
    _rep("    thr = max(2e-3, 0.03 * float(np.percentile(a, 95)))\n",
         "    thr = max(1.5e-3, 0.012 * float(np.percentile(a, 95)))   # NOX_PT_V13: тихие окончания слов — не пауза\n",
         "порог тишины")

    # 5) новые функции и настройки
    _rep("def assemble(rows, roles, head_pause=True):\n", r'''EQ_MAX = float(SETTINGS.get("eq_max", 1.15))      # NOX_PT_V13
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


def _asr_gate():
    # проверка распознаванием включена и проверяльщик жив (его сердцебиение не старше 10 минут)
    if not SETTINGS.get("asr_check"):
        return False
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
        return p.with_suffix(".ok").read_text(encoding="utf-8").split("\t")[0] == f"{st_.st_size}:{st_.st_mtime_ns}"
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


def assemble(rows, roles, head_pause=True):
''', "def assemble")

    # 6) готовность: только проверенные записи (если проверка распознаванием работает)
    _rep("    while ready < len(rows) and wav_for(rows[ready], roles).exists():\n",
         "    gate = _asr_gate()                                  # NOX_PT_V13\n"
         "    while ready < len(rows) and _seg_ok(rows[ready], roles, gate):\n", "ready")

    # 7) кэш длительностей — с размером файла (переозвученная реплика не берёт старую длительность)
    _rep("    k = path.stem\n    if k not in cache:\n",
         "    try:\n        k = f\"{path.stem}:{path.stat().st_size}\"   # NOX_PT_V13\n    except OSError:\n        k = path.stem\n"
         "    if k not in cache:\n", "duration_of")

    # 8) части: пересборка, если содержимое изменилось; часть с репликой на переозвучке ждёт, а не собирается с дырой
    _a = s.find("def build_parts(rows, roles, minutes")
    _b = s.find("\ndef copy_to(", _a)
    if _a < 0 or _b < 0 or "        if out.exists():\n            continue\n" not in s[_a:_b]:
        sys.exit("этап 13: build_parts не найден в ожидаемом виде — патч не применён")
    s = s[:_a] + r'''def build_parts(rows, roles, minutes, title, author, bitrate, fmt="m4b", quiet=False, speed=1.0):
    """Собирает закрытые части, которых нет в parts/ ИЛИ в которых переозвучена реплика (NOX_PT_V13).
    Часть, где запись ещё на переозвучке/проверке, ждёт (прежняя версия остаётся) — дыр в частях не бывает."""
    parts, ready, complete = plan_parts(rows, roles, minutes)
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
                 "sig": old_sig.get(n)}
        frozen.append(entry)
        sig = _part_sig(rows[a:b + 1], roles) if b < ready else None
        if sig is None:
            continue                                   # в части реплика на переозвучке — подождём
        if out.exists() and old_sig.get(n) == sig:
            continue
        audio, sr, chapters, missing = assemble(rows[a:b + 1], roles, head_pause=False)
        if missing or audio is None:
            continue                                   # запись ушла на переозвучку прямо сейчас — соберём позже
        ch_name = rows[a]["ch_title"]
        total = encode_m4b(audio, sr, chapters, out, f"{title} — часть {n:02d} ({ch_name})", author, bitrate,
                           speed=speed)
        entry["sig"] = sig
        new_files.append(out)
        if not quiet:
            print(f"[parts] {out.name}  {total / 60:5.1f} мин  с «{ch_name}»")
        else:
            print(f"[watch] {time.strftime('%H:%M')}  собрана {out.name} ({total / 60:.1f} мин)"
                  + (" — пересобрана: в ней переозвучены реплики" if old_sig.get(n) else ""), flush=True)
    PARTS_INDEX.write_text(json.dumps(frozen, ensure_ascii=False, indent=1), encoding="utf-8")
    if not quiet:
        print(f"готово сегментов подряд с начала: {ready}/{len(rows)}; частей собрано: {len(frozen)}"
              + (" — книга целиком озвучена" if complete else ""))
    complete = complete and all(e.get("sig") for e in frozen)
    return new_files, complete

''' + s[_b:]

    # 9) отправка на Диск: заново, если часть пересобрана (раньше отправлялась только один раз за запуск)
    _rep("    sent = set()\n",
         r'''    sent_f = WORK / "sent.json"            # NOX_PT_V13: какие версии частей уже на Диске
    try:
        sent = json.loads(sent_f.read_text(encoding="utf-8"))
    except Exception:
        sent = {}
''', "sent")
    _rep('''            if dest and f.name not in sent:
                copy_to(dest, f)
                sent.add(f.name)
                print(f"[watch] {time.strftime('%H:%M')}  отправлена {f.name}")
''', r'''            if not dest:
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
''', "отправка")
    _rep('''        ready = sum(1 for r in rows if wav_for(r, roles).exists())
        print(f"[watch] {time.strftime('%H:%M')}  озвучено {ready}/{len(rows)} сегментов, частей готово {n_parts}")
''', r'''        ready = sum(1 for r in rows if wav_for(r, roles).exists())
        _gate = _asr_gate()
        _chk = sum(1 for r in rows if _seg_ok(r, roles, True)) if _gate else None
        print(f"[watch] {time.strftime('%H:%M')}  озвучено {ready}/{len(rows)} сегментов"
              + (f", проверено распознаванием {_chk}" if _chk is not None else "") + f", частей готово {n_parts}")
''', "строка статуса")
    # книга целиком: склейка частей (без перекодирования); пересобирается, если пересобрана какая-то часть
    _rep('''            full = HERE / f"book.{args.format}"
            if not full.exists():
                audio, sr, chapters, _ = assemble(rows, roles)
                encode_m4b(audio, sr, chapters, full, args.title, args.author, args.bitrate, speed=args.speed)
                print(f"[watch] собрана книга целиком: {full}")
''', r'''            full = HERE / f"book.{args.format}"
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
''', "книга целиком")

    # 10) отпечаток сборки: новая схема кэша, громкость, предел выравнивания -> части пересоберутся (и на Диске)
    _rep("    asm = dict(t=cur_t, pause=PAUSE, sqz=[SQZ_KEEP, SQZ_CAP], parts=args.parts, fmt=args.format, br=args.bitrate,\n",
         r'''    _tdir = WORK / "tempo"                     # NOX_PT_V13: старый кэш ускорения (по имени реплики) — разово в мусор
    if _tdir.is_dir() and not (_tdir / ".v2").exists():
        shutil.rmtree(_tdir, ignore_errors=True)
    _tdir.mkdir(exist_ok=True)
    (_tdir / ".v2").touch()
    asm = dict(t=cur_t, pause=PAUSE, sqz=[SQZ_KEEP, SQZ_CAP], parts=args.parts, fmt=args.format, br=args.bitrate,
               cache=2, eq=EQ_MAX, norm=[NORM_ON, NORM_DB],
''', "asm")
    # при пересборке всех частей — забыть, что было отправлено
    _rep("        for f in (PARTS_INDEX, DUR_CACHE):\n            if f.exists():\n                f.unlink()\n        if dest and dest.is_dir():",
         "        for f in (PARTS_INDEX, DUR_CACHE, WORK / \"sent.json\", WORK / \"book.sig\"):\n            if f.exists():\n                f.unlink()\n        sent = {}\n        if dest and dest.is_dir():",
         "сброс частей")

# ---- этап 14: номера глав 30/40/50+ словами-порядковыми; «Содержание» не читается; кто «я» — из настроек книги ----
if "NOX_PT_V14" not in s:
    def _rep14(a, b, what):
        global s
        if s.count(a) != 1:
            sys.exit(f"этап 14: якорь «{what}» найден {s.count(a)} раз(а) — патч не применён")
        s = s.replace(a, b, 1)
    # «Глава XXX» читалось «Глава тридцать», XL — «сорок», L и дальше — цифрами: теперь «тридцатая», «сороковая», «пятидесятая»
    _rep14("def ordinal_f(n: int) -> str:\n    if n < len(ORD_F):\n        return ORD_F[n]\n",
           "def ordinal_f(n: int) -> str:\n    if n < len(ORD_F):\n        return ORD_F[n]\n"
           "    if (n % 10 == 0 or n >= 50) and 1 <= n <= 99 and \"_ord_fem\" in globals():   # NOX_PT_V14\n"
           "        return _ord_fem(n)\n", "ordinal_f")
    # строка «Содержание» перед оглавлением — пауза вместо слова (нумерация абзацев не сдвигается)
    _rep14('        if "toc" in low or "оглавлен" in low or "contents" in low:\n',
           '        if txt.rstrip(".:").strip().lower() in ("содержание", "оглавление"):   # NOX_PT_V14\n'
           '            out.append(("<<<SCENE>>>", False, False))\n'
           '            continue\n'
           '        if "toc" in low or "оглавлен" in low or "contents" in low:\n', "оглавление")
    # рассказчик от первого лица («сказал я»): книга 1–2 — Джек, книга 3 — капитан (Ян); задаётся в таблице настроек
    _rep14('PAUSE.update({k: int(v) for k, v in (SETTINGS.get("pause") or {}).items() if k in PAUSE})\n',
           'PAUSE.update({k: int(v) for k, v in (SETTINGS.get("pause") or {}).items() if k in PAUSE})\n'
           'ME = str(SETTINGS.get("me") or ME)   # NOX_PT_V14: кто говорит «я»\n', "ME")

# ---- этап 15: пауза после точки ВНУТРИ куска, мягкие края реплик, карта времени каждой части (для поиска мест) ----
if "NOX_PT_V15" not in s:
    def _rep15(a, b, what):
        global s
        if s.count(a) != 1:
            sys.exit(f"этап 15: якорь «{what}» найден {s.count(a)} раз(а) — патч не применён")
        s = s.replace(a, b, 1)
    _rep15("def assemble(rows, roles, head_pause=True):\n", r'''SENT_GAP_ON = bool(SETTINGS.get("sent_gap", True))   # NOX_PT_V15
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
        if g0 <= 75 and -60 < float(db[:g0].max()) < -20:
            x[:g0 * w] = 0.0
            fixes.append(f"щелчок в начале ({g0 * 2} мс)")
        break
    e0 = runs[-1][1]
    if _last_letter(text) not in _PLOSIVES:
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
''', "def assemble")
    _rep15("            out.append(x[i * w:(i + head) * w])\n            out.append(x[(j - tail) * w:j * w])\n",
           "            out.append(_fade(x[i * w:(i + head) * w], 0, int(sr * 0.005)))   # NOX_PT_V15: стык без щелчка\n"
           "            out.append(_fade(x[(j - tail) * w:j * w], int(sr * 0.005), 0))\n", "сжатие пауз")
    _rep15("    pieces, chapters, t = [], [], 0.0\n",
           "    pieces, chapters, t = [], [], 0.0\n    _TL.clear()                                            # NOX_PT_V15\n",
           "pieces=")
    _rep15("            x = np.clip(x * gain, -1.0, 1.0)\n        pieces.append(x)\n        t += len(x) / sr\n",
           "            x = np.clip(x * gain, -1.0, 1.0)\n"
           "        x = _edges(x, sr)                                  # NOX_PT_V15\n"
           "        _TL.append((t, t + len(x) / sr, r))\n"
           "        pieces.append(x)\n        t += len(x) / sr\n", "pieces.append(x)")
    # карта времени части: parts/part_NN.times.tsv — где в части какая реплика (для поиска места на слух)
    _rep15('''        total = encode_m4b(audio, sr, chapters, out, f"{title} — часть {n:02d} ({ch_name})", author, bitrate,
                           speed=speed)
''', '''        total = encode_m4b(audio, sr, chapters, out, f"{title} — часть {n:02d} ({ch_name})", author, bitrate,
                           speed=speed)
        try:                                               # NOX_PT_V15
            def _mmss(v):
                v = v / (speed or 1.0)
                return f"{int(v // 60):02d}:{v % 60:04.1f}"
            out.with_name(f"part_{n:02d}.times.tsv").write_text(
                "".join(f"{_mmss(a_)}\\t{_mmss(b_)}\\t{r_['id']}\\t{resolve_role(r_['speaker'], roles)}\\t{wav_for(r_, roles).stem}"
                        f"\\t{r_['text'][:120]}\\n" for a_, b_, r_ in _TL), encoding="utf-8")
        except Exception:
            pass
''', "encode части")
    # до кэша ускорения: щелчки по краям записи и пауза после точки внутри куска (по распознаванию или осторожно по звуку)
    _rep15("        _h = hashlib.blake2b(np.ascontiguousarray(x).tobytes(), digest_size=6).hexdigest()   # NOX_PT_V13\n",
           "        x, _dk = declick(x, sr, r['text'])                 # NOX_PT_V15: щелчки на краях записи\n"
           "        if SENT_GAP_ON and r['kind'] != 'head':\n"
           "            _rc = roles['roles'].get(resolve_role(r['speaker'], roles), {})\n"
           "            _f0 = TEMPO_G * factors.get(i, 1.0) * float(_rc.get('tempo', 1.0) or 1.0)\n"
           "            _tg = PAUSE['sentence'] / 1000 * PAUSE_SCALE / pdiv * 0.75 * max(_f0, 1.0)\n"
           "            _gp = _load_gaps(wav_for(r, roles))\n"
           "            x = _gap_fix(x, sr, _gp, _tg) if _gp is not None else _sent_gaps(x, sr, r['text'], _tg)\n"
           "        _h = hashlib.blake2b(np.ascontiguousarray(x).tobytes(), digest_size=6).hexdigest()   # NOX_PT_V13\n",
           "кэш-ключ")
    # части собираются только из записей, проверенных НОВОЙ проверкой (v2: щелчки, оборванные гласные, границы фраз)
    _rep15(r'''        return p.with_suffix(".ok").read_text(encoding="utf-8").split("\t")[0] == f"{st_.st_size}:{st_.st_mtime_ns}"
''', r'''        _okt = p.with_suffix(".ok").read_text(encoding="utf-8")   # NOX_PT_V15
        return _okt.split("\t")[0] == f"{st_.st_size}:{st_.st_mtime_ns}" and "\tv2" in _okt
''', "_seg_ok")
    _rep15(r'''        h.update(f"{p.name}:{st_.st_size}:{st_.st_mtime_ns}:{r['pause_before_ms']}|".encode("utf-8"))
''', r'''        h.update(f"{p.name}:{st_.st_size}:{st_.st_mtime_ns}:{r['pause_before_ms']}|".encode("utf-8"))
        try:                                                # NOX_PT_V15: границы предложений тоже
            g_ = p.with_suffix(".gaps").stat()
            h.update(f"g{g_.st_size}:{g_.st_mtime_ns}|".encode("utf-8"))
        except OSError:
            pass
''', "_part_sig")
    _rep15("               cache=2, eq=EQ_MAX, norm=[NORM_ON, NORM_DB],\n",
           "               cache=2, eq=EQ_MAX, norm=[NORM_ON, NORM_DB], sgap=[SENT_GAP_ON, 0.75, 2], edges=2, declick=1,\n",
           "asm")

# ---- этап 16: точнее ударения (ruaccent turbo3), никаких вставок тишины внутрь записи, бережное снятие щелчков ----
# (а) омографы: ruaccent «turbo» ошибается в ~9 % (статья авторов, COLING 2025: turbo 0.909, turbo3 0.964) -> turbo3.1/turbo3;
#     если модель не загрузилась — «turbo» с громким предупреждением; если не загрузилось ничего — ОСТАНОВКА (без ударений
#     озвучивать нельзя), а не тихая озвучка без ударений. Слой текста k1a-v7: аудит go переозвучит только изменившиеся реплики.
# (б) пауза после точки внутри готовой записи больше не вставляется: граница слов по звуку/распознаванию угадывается неточно,
#     и тишина попадала внутрь слова («х…рошо»). Паузы — только между кусками и при озвучке по предложениям.
# (в) щелчок в начале снимается, только если это именно он: короткий всплеск (до 80 мс), после него не меньше 150 мс тишины;
#     если реплика начинается со стечения согласных («Спасибо», «Кто», «С тех пор») — тишины нужно 250 мс: смычка внутри
#     слова короче. На записях книги 1 щелчок = всплеск ~40 мс, затем 400–500 мс тишины.
if "NOX_PT_V16" not in s:
    def _rep16(a, b, what):
        global s
        if s.count(a) != 1:
            sys.exit(f"этап 16: якорь «{what}» найден {s.count(a)} раз(а) — патч не применён")
        s = s.replace(a, b, 1)
    _rep16('TEXT_LAYER_VERSION = "k1a-v6"', 'TEXT_LAYER_VERSION = "k1a-v7"   # NOX_PT_V16: ruaccent turbo3', "слой текста v6")
    _rep16('SENT_GAP_ON = bool(SETTINGS.get("sent_gap", True))   # NOX_PT_V15\n',
           'SENT_GAP_ON = False   # NOX_PT_V15 -> V16: вставка тишины в готовую запись отключена (резала слова)\n',
           "SENT_GAP_ON")
    _rep16('''                from ruaccent import RUAccent
                _ACCENTIZER = RUAccent()
                _ACCENTIZER.load(omograph_model_size="turbo", use_dictionary=True)
                _fix_ruaccent_onnx(_ACCENTIZER)
                _extend_homographs(_ACCENTIZER)
                _tune_ruaccent(_ACCENTIZER)
''', '''                _ACCENTIZER = _load_ruaccent()            # NOX_PT_V16: turbo3.1/turbo3, проверка, громкий отказ
''', "загрузка ruaccent")
    _rep16("def _plus_to_acute(t, sparse=False):\n", r'''NOX_STAGE = 16
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
''', "_plus_to_acute")
    # щелчок в начале: строже (всплеск <= 80 мс, тишина после >= 150 мс, без стечения согласных в начале)
    _rep16('''        if g0 <= 75 and -60 < float(db[:g0].max()) < -20:
            x[:g0 * w] = 0.0
''', '''        if (g0 <= 75 and g1 - g0 >= 75 and int((db[:g0] > -60).sum()) <= 40   # NOX_PT_V16
                and -60 < float(db[:g0].max()) < -20 and (g1 - g0 >= 125 or _edge_word_ok(text, head=True))):
            x[:g0 * w] = 0.0
''', "declick: начало")
    _rep16("    if _last_letter(text) not in _PLOSIVES:\n        tail = db[e0:]\n",
           "    if _last_letter(text) not in _PLOSIVES and _edge_word_ok(text, head=False):   # NOX_PT_V16\n"
           "        tail = db[e0:]\n", "declick: конец")
    _rep16("sgap=[SENT_GAP_ON, 0.75, 2], edges=2, declick=1,", "sgap=[SENT_GAP_ON, 0.75, 2], edges=2, declick=2,",
           "подпись сборки")   # другие правила щелчков -> части пересобираются (записи не трогаются)
    _rep16("def declick(x, sr, text=\"\"):\n", r'''_CONS16 = set("бвгджзйклмнпрстфхцчшщ")


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
''', "declick")

# ---- этап 17: ударения ПО МЕСТУ (work/stress_fixes.tsv), слова КАПСОМ читаются как слова, а не по буквам ----
# (а) где ударение зависит от смысла («сложил ру́ки» / «из руки́», «у воро́т» / «во́рот рубахи»), словарь бессилен:
#     work/stress_fixes.tsv задаёт ударение конкретного слова конкретной реплики (ключ = sha1 её текста). Поправки
#     получены сверкой двух моделей (ruaccent и Silero Stress) и решены по смыслу фразы. Изменился текст реплики —
#     её поправки просто не применяются.
# (б) «НЕ ВХОДИТЬ», «НАГРАДА», «Зарядить ТИС»: слово капсом движок может прочесть по буквам. Перед разметкой такие слова
#     пишутся как обычные (первая буква остаётся заглавной). Сокращения без гласных (МВД) и вроде США не трогаются.
if "NOX_PT_V17" not in s:
    def _rep17(a, b, what):
        global s
        if s.count(a) != 1:
            sys.exit(f"этап 17: якорь «{what}» найден {s.count(a)} раз(а) — патч не применён")
        s = s.replace(a, b, 1)
    _rep17('TEXT_LAYER_VERSION = "k1a-v7"', 'TEXT_LAYER_VERSION = "k1a-v8"', "слой текста v7")
    _rep17('''    t = text.replace("\\u00ab", "").replace("\\u00bb", "").replace("\\u201c", "").replace("\\u201d", "")
''', '''    t = text.replace("\\u00ab", "").replace("\\u00bb", "").replace("\\u201c", "").replace("\\u201d", "")
    t = _decaps(t)                                   # NOX_PT_V17: «НЕ ВХОДИТЬ» -> «Не Входить» (не по буквам)
''', "начало prepare_text")
    _rep17('''    if accent in ("acute", "sparse"):
        t = _plus_to_acute(t, accent == "sparse")
''', '''    if accent in ("acute", "sparse"):
        t = _plus_to_acute(t, accent == "sparse")
        t = apply_stress_fixes(text, t)              # NOX_PT_V17: ударения по месту (work/stress_fixes.tsv)
''', "acute в prepare_text")
    _rep17("def prepare_text(text: str, use_num2words: bool, accent: str) -> str:\n", r'''NOX_STAGE = 17
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


def prepare_text(text: str, use_num2words: bool, accent: str) -> str:
''', "def prepare_text")
    _rep17("NOX_STAGE = 16\n", "", "NOX_STAGE 16")          # NOX_PT_V17: номер этапа теперь объявлен выше

# ---- этап 18: скобки -> паузы; запись привязана к ОБРАЗЦУ голоса; старые записи по номеру реплики не переносятся ----
# (а) «смотришь (тогда он притворялся мёртвым), а когда…»: ruaccent склеивал скобку со словом («смо́тришь(тогда́»),
#     движок на непроизносимых скобках сбивался — у рассказчика звучал чужой (женский) голос. В книге 1 таких реплик 109.
#     Скобки теперь становятся паузами-тире ещё ДО расстановки ударений; нижняя кавычка «„» убирается, как « » “ ”.
# (б) имя записи было = роль + текст: пересоздали образец голоса роли — записи прежним голосом оставались в книге.
#     Теперь в имя входит отпечаток образца (wav + его текст): сменился образец — реплики этой роли озвучиваются заново.
#     Уже готовые записи при первом запуске переименовываются под текущий образец (озвученное не теряется).
# (в) файлы 000123.wav (по номеру реплики, самая первая схема) больше не переносятся: номер мог указывать уже на
#     другую реплику другой роли — так в книгу могла попасть чужая запись.
# (г) карта времени части (part_NN.times.tsv): добавлены секунды начала/конца — для дорожек эффектов и музыки.
if "NOX_PT_V18" not in s:
    def _rep18(a, b, what):
        global s
        if s.count(a) != 1:
            sys.exit(f"этап 18: якорь «{what}» найден {s.count(a)} раз(а) — патч не применён")
        s = s.replace(a, b, 1)
    _rep18('TEXT_LAYER_VERSION = "k1a-v8"', 'TEXT_LAYER_VERSION = "k1a-v9"', "слой текста v8")
    _rep18('''    t = _decaps(t)                                   # NOX_PT_V17: «НЕ ВХОДИТЬ» -> «Не Входить» (не по буквам)
''', '''    t = _parens(t)                                   # NOX_PT_V18: скобки -> паузы-тире (до ruaccent: он их склеивал)
    t = _decaps(t)                                   # NOX_PT_V17: «НЕ ВХОДИТЬ» -> «Не Входить» (не по буквам)
''', "_decaps в prepare_text")
    _rep18("def prepare_text(text: str, use_num2words: bool, accent: str) -> str:\n", r'''def _parens(t):
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
''', "def prepare_text")
    _rep18("def wav_for(r: dict, roles: dict) -> Path:\n", r'''_VSIG = {}
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
    """NOX_PT_V18, разово: записи по старой схеме имён (роль + текст) -> новая (роль + образец + текст), считая, что
    сделаны нынешним образцом. Дальше смена образца переозвучивает реплики роли сама."""
    done = WORK / "keys_v18.done"
    if done.exists() or not WAVS.is_dir():
        return 0
    k = 0
    for r in rows:
        role = resolve_role(r["speaker"], roles)
        old = seg_key(role, r["text"])
        new = wav_for(r, roles).stem
        if old == new or not (WAVS / f"{old}.wav").exists() or (WAVS / f"{new}.wav").exists():
            continue
        for f in sorted(WAVS.glob(old + ".*")):
            try:
                os.replace(f, WAVS / (new + f.name[len(old):]))
            except OSError:
                pass
        k += 1
    try:
        done.write_text(time.strftime("%Y-%m-%d %H:%M:%S") + f"\t{k}", encoding="utf-8")
    except OSError:
        pass
    if k:
        print(f"записи привязаны к образцам голосов: {k} (новые имена файлов, озвученное сохранено)", flush=True)
    return k


def wav_for(r: dict, roles: dict) -> Path:
    _role = resolve_role(r["speaker"], roles)                       # NOX_PT_V18: + отпечаток образца голоса
    return WAVS / f"{seg_key(_role + '|' + voice_sig(_role, roles), r['text'])}.wav"


def _wav_for_v17(r: dict, roles: dict) -> Path:
''', "def wav_for")
    _rep18('''    """Старые файлы 000123.wav (по номеру сегмента) -> имена по хешу. Безопасно при нескольких воркерах."""
''', '''    """Старые файлы 000123.wav (по номеру сегмента) -> имена по хешу. Безопасно при нескольких воркерах."""
    return migrate_voice_keys(rows, roles)          # NOX_PT_V18: по номеру больше не переносим — номер мог смениться
''', "migrate_legacy_wavs")
    _rep18('''                "".join(f"{_mmss(a_)}\\t{_mmss(b_)}\\t{r_['id']}\\t{resolve_role(r_['speaker'], roles)}\\t{wav_for(r_, roles).stem}"
                        f"\\t{r_['text'][:120]}\\n" for a_, b_, r_ in _TL), encoding="utf-8")
''', '''                "".join(f"{_mmss(a_)}\\t{_mmss(b_)}\\t{r_['id']}\\t{resolve_role(r_['speaker'], roles)}\\t{wav_for(r_, roles).stem}"
                        f"\\t{r_['text'][:120]}\\t{a_ / (speed or 1.0):.3f}\\t{b_ / (speed or 1.0):.3f}\\n"   # NOX_PT_V18: секунды
                        for a_, b_, r_ in _TL), encoding="utf-8")
''', "карта времени части")
    _rep18("NOX_STAGE = 17\n", "NOX_STAGE = 18\n", "NOX_STAGE 17")

# ---- этап 19: абзац по явной разметке ролей (сценарий: «para<TAB>=<TAB>⟦Роль⟧текст⟦narrator⟧текст…») ----
# Разбор по тире чередует «речь — ремарка — речь». Лишнее тире сбивало чередование: «Маг, который упал, — это не маг,
# а ужин» (тире внутри фразы), «…как всё, что говорил, — как пишут в отчётах» (тире внутри ремарки), «„В тебе что-то
# не то“, — сказала она. — Хорват помолчал.» (цитата внутри реплики) — и речь персонажа читал рассказчик. Для таких
# абзацев сценарий задаёт разметку явно: кто читает какой кусок. Текст разметки сверяется с абзацем книги (буквы и
# цифры); не совпал (книга правилась) — абзац разбирается как раньше, с предупреждением.
if "NOX_PT_V19" not in s:
    def _rep19(a, b, what):
        global s
        if s.count(a) != 1:
            sys.exit(f"этап 19: якорь «{what}» найден {s.count(a)} раз(а) — патч не применён")
        s = s.replace(a, b, 1)
    _rep19('''        lines = [l.strip() for l in txt.split("\\n") if l.strip()]
''', '''        lines = [l.strip() for l in txt.split("\\n") if l.strip()]
        _eq = [rl for pref, rl in SPEAKER_OVERRIDES.get(pi, []) if pref == "="]        # NOX_PT_V19: явная разметка
        if _eq:
            _parts = [(m.group(1).strip(), m.group(2).strip())
                      for m in re.finditer("\\u27e6([^\\u27e7]+)\\u27e7([^\\u27e6]*)", _eq[-1])]
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
''', "lines в segment")
    _rep19('''        if kind == "speech" and para_idx in SPEAKER_OVERRIDES:
''', '''        if kind == "speech" and para_idx in SPEAKER_OVERRIDES and \\
                not any(p_ == "=" for p_, _ in SPEAKER_OVERRIDES[para_idx]):   # NOX_PT_V19: роль уже задана разметкой
''', "add: сценарий")
    _rep19("NOX_STAGE = 18\n", "NOX_STAGE = 19\n", "NOX_STAGE 18")

# ---- этап 20: части — только из записей, проверенных ТЕКУЩЕЙ проверкой; вторая проверка речи в собранной части ----
# Почему части пересобирались и в них не было фраз Хекубах:
#  (а) go запускал окно сборки раньше проверки распознаванием: первый круг сборки шёл «без проверки» и собирал все части
#      из прежних записей (пустые короткие реплики попадали в части); -> проверка этой книги объявлена файлом
#      work/asr_okv.txt (его пишет verify при запуске): пока он есть, сборка ждёт проверку с первой минуты;
#  (б) сборка принимала метку прежней проверки (v2–v4, короткие реплики тогда не слушались); -> нужна метка текущей
#      проверки из work/asr_okv.txt (сейчас v6);
#  (в) перепроверка переписывала файл границ предложений (.gaps) с тем же содержимым, отпечаток части менялся по времени
#      файла — часть пересобиралась без изменений; -> в отпечатке содержимое .gaps, а не время.
# Вторая проверка: после склейки части на месте каждой реплики должна звучать речь. Сборка «съела» речь — часть
# собирается заново бережно (без снятия щелчков и сжатия пауз у этой реплики); пуста сама запись — она уходит на
# переозвучку (если попытки не кончились), часть ждёт. Всё — в work/part_check.txt.
# Прежние части собраны без этих правил — разово убираются и собираются заново (отпечаток сборки gate=20).
if "NOX_PT_V20" not in s:
    def _rep20(a, b, what):
        global s
        if s.count(a) != 1:
            sys.exit(f"этап 20: якорь «{what}» найден {s.count(a)} раз(а) — патч не применён")
        s = s.replace(a, b, 1)
    _rep20('''def _asr_gate():
    # проверка распознаванием включена и проверяльщик жив (его сердцебиение не старше 10 минут)
    if not SETTINGS.get("asr_check"):
        return False
''', '''_OKV_C = [0.0, ""]


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
    if _okv_req():          # NOX_PT_V20: проверка этой книги запущена — сборка ждёт её с первой минуты, даже пока она грузится
        return True
''', "_asr_gate")
    _rep20('''        return _okt.split("\\t")[0] == f"{st_.st_size}:{st_.st_mtime_ns}" and "\\tv2" in _okt
''', '''        _v = _okv_req()                                       # NOX_PT_V20: метка текущей проверки, не прежней
        return _okt.split("\\t")[0] == f"{st_.st_size}:{st_.st_mtime_ns}" and "\\tv2" in _okt \\
            and (not _v or _v in _okt.strip().split("\\t"))
''', "_seg_ok")
    _rep20('''        try:                                                # NOX_PT_V15: границы предложений тоже
            g_ = p.with_suffix(".gaps").stat()
            h.update(f"g{g_.st_size}:{g_.st_mtime_ns}|".encode("utf-8"))
        except OSError:
            pass
''', '''        try:                                                # NOX_PT_V20: границы предложений — по содержимому
            h.update(b"g" + p.with_suffix(".gaps").read_bytes() + b"|")
        except OSError:
            pass
''', "_part_sig")
    # бережная сборка реплики, у которой обработка «съела» речь
    _rep20('''        x, _dk = declick(x, sr, r['text'])                 # NOX_PT_V15: щелчки на краях записи
        if SENT_GAP_ON and r['kind'] != 'head':
''', '''        _raw = r["id"] in _RAW_IDS                          # NOX_PT_V20: бережно — без снятия щелчков и правки пауз
        if not _raw:
            x, _dk = declick(x, sr, r['text'])             # NOX_PT_V15: щелчки на краях записи
        if SENT_GAP_ON and r['kind'] != 'head' and not _raw:
''', "assemble: declick")
    _rep20('''        if TRIM_PAUSES:
            x = _squeeze_pauses(x, sr, keep=SQZ_KEEP, cap=SQZ_CAP)
''', '''        if TRIM_PAUSES and not _raw:
            x = _squeeze_pauses(x, sr, keep=SQZ_KEEP, cap=SQZ_CAP)
''', "assemble: squeeze")
    _rep20('''        x = _stretch(x, sr, f, wav_for(r, roles).stem + "_" + _h + sfx)
''', '''        if _raw:
            sfx = "r"                                      # NOX_PT_V20: бережная — свой кэш темпа (иначе вернётся сжатая)
        x = _stretch(x, sr, f, wav_for(r, roles).stem + "_" + _h + sfx)
''', "assemble: кэш темпа бережной")
    _rep20('''def build_parts(rows, roles, minutes, title, author, bitrate, fmt="m4b", quiet=False, speed=1.0):
''', '''_RAW_IDS = set()                 # NOX_PT_V20: реплики, у которых сборка «съела» речь, — собираются бережно
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
    for t0, t1, r in list(_TL):
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
        lines.append(f"{time.strftime('%Y-%m-%d %H:%M')}\\tчасть {num:02d}\\tid {r['id']}\\t{resolve_role(r['speaker'], roles)}"
                     f"\\tв части {got:.2f} с, в записи {raw:.2f} с, нужно {need:.2f} с\\t{verdict}\\t{r['text'][:90]}\\n")
    if lines:
        try:
            with PART_CHECK.open("a", encoding="utf-8") as f:
                f.writelines(lines)
            (WORK / "part_check_redo.json").write_text(json.dumps(sorted(redo_done)), encoding="utf-8")
        except OSError:
            pass
        print(f"[watch] {time.strftime('%H:%M')}  часть {num:02d}: вторая проверка — без речи {len(lines)} реплик"
              f" (work\\\\part_check.txt)", flush=True)
    return res


def build_parts(rows, roles, minutes, title, author, bitrate, fmt="m4b", quiet=False, speed=1.0):
''', "def build_parts")
    _rep20('''        audio, sr, chapters, missing = assemble(rows[a:b + 1], roles, head_pause=False)
        if missing or audio is None:
            continue                                   # запись ушла на переозвучку прямо сейчас — соберём позже
''', '''        audio, sr, chapters, missing = assemble(rows[a:b + 1], roles, head_pause=False)
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
''', "build_parts: assemble")
    # разовый сброс частей: прежние собраны без этих правил (и сведения звука из них тоже)
    _rep20("sgap=[SENT_GAP_ON, 0.75, 2], edges=2, declick=2,\n",
           "sgap=[SENT_GAP_ON, 0.75, 2], edges=2, declick=2, gate=20,\n", "asm")
    _rep20('''        print("watch: настройки сборки изменились (темп/паузы/роли/части) — части пересобираются, старые на Диске убраны")
''', '''        _zv = HERE / "parts_zvuk"                      # NOX_PT_V20: сведения звука из прежних частей недействительны
        if _zv.is_dir():
            for f in _zv.glob("*/part_*"):
                try:
                    f.unlink()
                except OSError:
                    pass
        print("watch: " + ("части собираются заново — только из записей, проверенных текущей проверкой распознаванием; "
                           "прежние части убраны" if '"gate"' not in prev_a else
                           "настройки сборки изменились (темп/паузы/роли/части) — части пересобираются, старые на Диске убраны"))
''', "сброс частей: сообщение")
    _rep20('''        print(f"[watch] {time.strftime('%H:%M')}  озвучено {ready}/{len(rows)} сегментов"
              + (f", проверено распознаванием {_chk}" if _chk is not None else "") + f", частей готово {n_parts}")
''', '''        print(f"[watch] {time.strftime('%H:%M')}  озвучено {ready}/{len(rows)} сегментов"
              + (f", проверено распознаванием {_chk}" if _chk is not None else "") + f", частей готово {n_parts}")
        if _chk is not None and _chk < ready and _okv_req():      # NOX_PT_V20: проверка умерла — части ждут
            try:
                _hb = time.time() - (WORK / "asr_active.txt").stat().st_mtime
            except OSError:
                _hb = None
            if _hb is None or _hb > 900:
                print(f"[watch] !! проверка распознаванием не отвечает (см. verify.log, verify.err.log) — части ждут "
                      f"проверенных записей; запустите go ещё раз", flush=True)
''', "строка статуса")
    _rep20("NOX_STAGE = 19\n", "NOX_STAGE = 20\n", "NOX_STAGE 19")

# ---------------------------------------------------------------------------------------------------------------------
# ЭТАП 21 (NOX_PT_V21): работа ПО ЧАСТЯМ. Автор закрепил часть в студии — конвейер её больше не трогает: не переозвучивает,
# не перепроверяет, не пересобирает и не удаляет. Работа идёт над одной частью — первой незакреплённой («окно работы»);
# дальше неё ничего не озвучивается и не проверяется. Границы собранных частей теперь хранятся по номерам реплик
# (у одинаковых реплик одной роли запись общая — по имени записи конец части находился неверно).
# Подпись сборки (assembly.json) не меняется: уже собранные части остаются как есть.
if "NOX_PT_V21" not in s:
    def _rep21(a, b, what):
        global s
        if s.count(a) != 1:
            sys.exit(f"этап 21: якорь «{what}» найден {s.count(a)} раз(а) — патч не применён")
        s = s.replace(a, b, 1)
    _rep21('''def plan_parts(rows, roles, minutes: float):
    """-> (parts, complete). parts: список (start_idx, end_idx_incl, closed). Границы уже собранных частей
    заморожены в work/parts.json (по хешам сегментов), чтобы номера частей не «ехали» между запусками."""
    cache = load_durations()
    keys = [wav_for(r, roles).stem for r in rows]
    key_pos = {k: i for i, k in enumerate(keys)}
    frozen = []
    try:
        for p in json.loads(PARTS_INDEX.read_text(encoding="utf-8")):
            a, b = key_pos.get(p["start"]), key_pos.get(p["end"])
            if a is None or b is None or b < a or (frozen and a != frozen[-1][1] + 1) or (not frozen and a != 0):
                break
            frozen.append((a, b))
    except Exception:
        frozen = []
    # сколько сегментов готово подряд с начала книги
    ready = 0
    gate = _asr_gate()                                  # NOX_PT_V13
    while ready < len(rows) and _seg_ok(rows[ready], roles, gate):
        ready += 1
    complete = ready == len(rows)
    parts = [(a, b, True) for a, b in frozen]
    start = frozen[-1][1] + 1 if frozen else 0
    acc = 0.0
    for i in range(start, ready):
        r = rows[i]
        acc += int(r["pause_before_ms"]) / 1000 + duration_of(wav_for(r, roles), cache)
''', '''# NOX_PT_V21: ПО ЧАСТЯМ. Закреплённые автором части — studio/fixed_parts.json (пишет «Нокс Студия»).
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
    """Закреплённые автором части -> [(a, b)] по возрастанию (номера строк, включительно). Нет файла — []."""
    try:
        d = json.loads(FIXED_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    uid, pos, out = None, None, []
    for p in d.get("parts") or []:
        try:
            a, b = int(p["a"]), int(p["b"])
        except Exception:
            continue
        ok = 0 <= a <= b < len(rows)
        if p.get("a_uid") and p.get("b_uid"):
            if uid is None:
                uid = row_uids(rows)
            ok = ok and uid[a] == p["a_uid"] and uid[b] == p["b_uid"]
            if not ok:                                  # книгу переразбили — те же реплики ищем по меткам
                if pos is None:
                    pos = {u: i for i, u in enumerate(uid)}
                a2, b2 = pos.get(p["a_uid"]), pos.get(p["b_uid"])
                if a2 is not None and b2 is not None and a2 <= b2:
                    a, b, ok = a2, b2, True
        if ok:
            out.append((a, b))
    out.sort()
    return out


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
    """Номера строк всех закреплённых реплик: их не озвучивают, не проверяют и не собирают заново."""
    out = set()
    for a, b in (fixed_parts(rows) if fx is None else fx):
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
        ids = [ln.split("\\t")[2] for ln in
               (PARTS / f"part_{int(p['n']):02d}.times.tsv").read_text(encoding="utf-8").splitlines() if ln.count("\\t") >= 4]
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
    idpos = {str(r["id"]): i for i, r in enumerate(rows)}
    try:
        for p in json.loads(PARTS_INDEX.read_text(encoding="utf-8")):
            if int(p.get("n", 0)) != num or not p.get("sig") or p.get("fixed"):
                continue
            pos = _part_pos(p, rows, roles, idpos)
            if pos and pos[0] == i0:
                has = any((PARTS / f"part_{num:02d}.{x}").exists() for x in ("mp3", "m4b"))
                return i0, pos[1], num, bool(has and _part_sig(rows[i0:pos[1] + 1], roles) == p["sig"])
    except Exception:
        pass
    md = run_mode()
    target = float(md.get("part_sec") or float(SETTINGS.get("part_min", 19) or 19) * 60 * max(TEMPO_G, 1.0))
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
    for i in range(i0, len(rows)):                      # озвученное — по настоящей длине, остальное — по оценке
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
    return i0, len(rows) - 1, num, False


def plan_parts(rows, roles, minutes: float, upto=None):
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
''', "plan_parts")
    _rep21('''def build_parts(rows, roles, minutes, title, author, bitrate, fmt="m4b", quiet=False, speed=1.0):
    """Собирает закрытые части, которых нет в parts/ ИЛИ в которых переозвучена реплика (NOX_PT_V13).
    Часть, где запись ещё на переозвучке/проверке, ждёт (прежняя версия остаётся) — дыр в частях не бывает."""
    parts, ready, complete = plan_parts(rows, roles, minutes)
''', '''def build_parts(rows, roles, minutes, title, author, bitrate, fmt="m4b", quiet=False, speed=1.0, only=None, upto=None):
    """Собирает закрытые части, которых нет в parts/ ИЛИ в которых переозвучена реплика (NOX_PT_V13).
    Часть, где запись ещё на переозвучке/проверке, ждёт (прежняя версия остаётся) — дыр в частях не бывает.
    NOX_PT_V21: закреплённые автором части не собираются; only=N — собирать только часть N (режим «по частям»)."""
    parts, ready, complete = plan_parts(rows, roles, minutes, upto)
    _nfx = len(fixed_prefix(rows))
''', "build_parts: начало")
    _rep21('''        entry = {"n": n, "start": wav_for(rows[a], roles).stem, "end": wav_for(rows[b], roles).stem,
                 "sig": old_sig.get(n)}
        frozen.append(entry)
''', '''        entry = {"n": n, "start": wav_for(rows[a], roles).stem, "end": wav_for(rows[b], roles).stem,
                 "sig": old_sig.get(n), "a_id": rows[a]["id"], "b_id": rows[b]["id"]}
        frozen.append(entry)
        if n <= _nfx:                                  # NOX_PT_V21: закреплена автором — конвейер её не трогает
            entry["fixed"] = True
            entry["sig"] = entry["sig"] or "fixed"
            continue
        if only is not None and n != only:
            continue                                   # по частям: остальные части ждут своей очереди
''', "build_parts: закреплённые")
    _rep21('''    if asm_changed and PARTS.is_dir() and any(PARTS.glob("part_*")):
        for f in list(PARTS.glob("part_*")) + [HERE / "book.mp3", HERE / "book.m4b"]:   # НЕ book.docx!
            if not f.exists():
                continue
            f.unlink()
''', '''    if asm_changed and PARTS.is_dir() and any(PARTS.glob("part_*")):
        try:                                            # NOX_PT_V21: файлы закреплённых автором частей остаются
            _keep = {f"part_{k:02d}" for k in range(1, len(fixed_prefix(load_rows())) + 1)}
        except Exception:
            _keep = set()
        for f in list(PARTS.glob("part_*")) + [HERE / "book.mp3", HERE / "book.m4b"]:   # НЕ book.docx!
            if not f.exists() or f.name[:7] in _keep:
                continue
            f.unlink()
''', "watch: смена настроек сборки")
    # сверка настроек сборки вынесена из watch в отдельную функцию: её же вызывает nox_part.py (сборка по частям),
    # поэтому подпись сборки у обоих режимов одна и та же, и переход между режимами ничего не пересобирает
    _a21 = s.find('    tv = WORK / "tempo.ver"\n')
    _e21 = '    av.write_text(asm_s, encoding="utf-8")\n'
    _b21 = s.find(_e21, _a21)
    if _a21 < 0 or _b21 < 0 or s.count('    tv = WORK / "tempo.ver"\n') != 1:
        sys.exit("этап 21: блок сверки настроек сборки в watch не найден — патч не применён")
    _blk21 = s[_a21:_b21 + len(_e21)]
    _d21 = '            for f in list(dest.glob("part_*.mp3")) + list(dest.glob("part_*.m4b")) + [dest / "book.mp3", dest / "book.m4b"]:\n'
    if _blk21.count(_d21) != 1:
        sys.exit("этап 21: удаление старых частей на Диске в watch не найдено — патч не применён")
    _blk21 = _blk21.replace(_d21, _d21 + '                if f.name[:7] in _keep:\n                    continue                    '
                            '# NOX_PT_V21: черновик закреплённой части остаётся\n')
    s = s[:_a21] + "    sent = _asm_setup(args, dest, sent)             # NOX_PT_V21\n" + s[_b21 + len(_e21):]
    _rep21("def watch(args):\n", '''def _asm_setup(args, dest, sent):
    """NOX_PT_V21: сверка настроек сборки (темп, паузы, роли, длина части, формат). Изменились — черновики частей
    убираются и собираются заново; файлы частей, закреплённых автором, остаются. -> sent (что уже на Диске)."""
''' + _blk21 + '''    return sent


def watch(args):
''', "watch")
    # правила «везде дальше по книге», которые автор задал в студии (ударение слова), — поверх словаря автора:
    # следующие части конвейер сразу озвучивает по ним, а не «как раньше, а студия потом переозвучит»
    _rep21("""                if word and fixed:
                    _WORD_DICT[word] = fixed
    return _WORD_DICT
""", """                if word and fixed:
                    _WORD_DICT[word] = fixed
        try:                                            # NOX_PT_V21: правила студии (studio/rules_dict.tsv)
            for line in (HERE / "studio" / "rules_dict.tsv").read_text(encoding="utf-8").splitlines():
                if line.strip() and not line.startswith("#") and "\\t" in line:
                    word, fixed = line.split("\\t", 1)
                    if word.strip() and fixed.strip():
                        _WORD_DICT[word.strip()] = fixed.strip()
        except OSError:
            pass
    return _WORD_DICT
""", "словарь автора")
    # сборка файла: цепочка выравнивания громкости — одной константой (её же берёт студия, когда сводит голос со
    # звуками без повторного сжатия); m4b — 48 кГц (loudnorm работает на 192 кГц, и AAC без этого выходил в 96 кГц)
    _rep21("""def encode_m4b(audio, sr, chapters, out: Path, title: str, author: str, bitrate: str, cover=None,
""", """LOUDNORM_AF = "loudnorm=I=-18:TP=-2:LRA=11"        # NOX_PT_V21


def encode_m4b(audio, sr, chapters, out: Path, title: str, author: str, bitrate: str, cover=None,
""", "encode_m4b")
    _rep21("""    af = "loudnorm=I=-18:TP=-2:LRA=11"
    if speed != 1.0:
        af = atempo_chain(speed) + "," + af
""", """    af = LOUDNORM_AF
    if speed != 1.0:
        af = atempo_chain(speed) + "," + af
""", "loudnorm в encode_m4b")
    _rep21("""        cmd += ["-c:a", "aac", "-movflags", "+faststart", "-f", "ipod"]
    if cover:
""", """        cmd += ["-c:a", "aac", "-ar", "48000", "-movflags", "+faststart", "-f", "ipod"]   # NOX_PT_V21
    if cover:
""", "aac в encode_m4b")
    # индекс частей и кэш длительностей читают сразу несколько процессов (воркеры, проверка, сборщик, студия) —
    # писать их через временный файл, чтобы никто не прочёл половину
    _rep21("""def write_wav_atomic(path: Path, data, sr):
""", """def _atomic_text(path: Path, text: str):
    \"\"\"NOX_PT_V21: запись через временный файл; в Windows подмена повторяется, если файл в этот миг читают.\"\"\"
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
""", "write_wav_atomic")
    _rep21("""    PARTS_INDEX.write_text(json.dumps(frozen, ensure_ascii=False, indent=1), encoding="utf-8")
""", """    _atomic_text(PARTS_INDEX, json.dumps(frozen, ensure_ascii=False, indent=1))
""", "запись parts.json")
    _rep21("""    DUR_CACHE.write_text(json.dumps(cache), encoding="utf-8")
""", """    _atomic_text(DUR_CACHE, json.dumps(cache))
""", "запись кэша длительностей")
    _rep21("NOX_STAGE = 20\n", "NOX_STAGE = 21\n", "NOX_STAGE 20")

bak = p.with_suffix(".py.bak-k2")
bak.write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
p.write_text(s, encoding="utf-8")
py_compile.compile(str(p), doraise=True)
print(f"патч применён (правок: {applied} + ударения), резервная копия: {bak.name}")

'@
$utf8 = New-Object System.Text.UTF8Encoding $false
[IO.File]::WriteAllText("$Root\patch_k1a.py", $patchPy, $utf8)
& "$Root\env\python.exe" "$Root\patch_k1a.py" "$Root\nox_audiobook.new.py"
if ($LASTEXITCODE -ne 0) { Write-Host "патч не применился — пришлите вывод в чат" -ForegroundColor Red; exit 1 }
try {
    if (Test-Path "$Root\nox_audiobook.py") { [IO.File]::Replace("$Root\nox_audiobook.new.py", "$Root\nox_audiobook.py", [NullString]::Value) }
    else { Move-Item "$Root\nox_audiobook.new.py" "$Root\nox_audiobook.py" }
} catch {
    Copy-Item "$Root\nox_audiobook.new.py" "$Root\nox_audiobook.py" -Force
    Remove-Item "$Root\nox_audiobook.new.py" -Force -ErrorAction SilentlyContinue
}

Step "ruaccent в envfast (контекстные ударения: замок/дома/уже...)"
$FPy = "$Root\envfast\Scripts\python.exe"
$env:HF_HOME = "$Root\hf"
& $FPy -c "import ruaccent" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  установка ruaccent (1-2 мин)..."
    & $FPy -m pip install --disable-pip-version-check -q ruaccent
    & $FPy -c "import ruaccent" 2>$null
}
if ($LASTEXITCODE -eq 0) {
    Write-Host "  ruaccent установлен; проверяю слой текста (режим $Accent). Загрузка модели ударений — до 1–2 минут, окно может молчать: не прерывайте..."
    # проверка идёт через prepare_text патченного конвейера: словарь + омографы + НЕЗНАКОМОЕ имя (путь, который падал)
    $chk = "import sys; sys.path.insert(0, r'$Root'); import nox_audiobook as n; " +
           "t = n.prepare_text('Красное небо: замок Яндора стоит дома у болота, кто-то ждал Хорвата и Льюиса…', False, '$Accent'); " +
           "print('  слой текста:', n.TEXT_LAYER_VERSION, '| модель омографов ruaccent:', getattr(n, 'RUACC_MODEL', None), '->', t)"
    & $FPy -c $chk
    if ($LASTEXITCODE -ne 0) {
        # без ударений озвучивать нельзя: воркеры всё равно остановились бы на первой реплике
        Write-Host "  ПРОВЕРКА ruaccent НЕ ПРОШЛА — озвучка не запускается. Пришлите вывод выше в чат." -ForegroundColor Red
        if ("$Accent" -ne "none") { exit 1 }
    }
} else {
    Write-Host "  ruaccent не установился — пришлите вывод в чат (озвучка возможна и без него: словарь имён работает)" -ForegroundColor Yellow
}

Step "Модель озвучки: $Model (разово ~4 ГБ в $Root\hf, потом из кэша)"
$dlPy = "from huggingface_hub import snapshot_download`n" +
        "import sys`n" +
        "ids = [r'$Model', 'siriusfreak/qwen3-tts-12hz-1.7b-ru-stress-cf'] if 'stress' in r'$Model' else [r'$Model']`n" +
        "pins = {'shib79/qwen3-tts-12hz-1.7b-ru-stress-cf': '66338b83519a05e3d4f923e0f9b4cd2ce686f138'}`n" +
        "for rid in ids:`n" +
        "  for rev in ([pins[rid], None] if rid in pins else [None]):`n" +
        "    try:`n" +
        "        p = snapshot_download(rid, revision=rev, allow_patterns=['*.json', '*.txt', 'model.safetensors', 'speech_tokenizer/*'])`n" +
        "        print('  модель на месте:', p, '(закреплённая версия)' if rev else ''); sys.exit(0)`n" +
        "    except Exception as e:`n" +
        "        print('  не скачалась', rid, rev or '', '-', type(e).__name__, str(e)[:120])`n" +
        "sys.exit(1)`n"
& $FPy -c $dlPy
if ($LASTEXITCODE -ne 0) { Write-Host "  МОДЕЛЬ НЕ СКАЧАЛАСЬ — проверьте интернет и запустите снова (или go -Base: базовая модель без ударений)" -ForegroundColor Red; exit 1 }

Step "Рукопись и разбивка по ролям (GPU не нужен)"
if ($Title) {     # защита от чужой рукописи: в начале файла должно быть название книги
    $chk = "import sys, docx`nd = docx.Document(sys.argv[1])`nt = ' '.join(p.text for p in d.paragraphs[:60]).lower()`nsys.exit(0 if sys.argv[2].lower() in t else 3)"
    & "$Root\env\python.exe" -c $chk $Docx $Title
    if ($LASTEXITCODE -eq 3) {
        Write-Host ("Рукопись " + $Docx + " — не «" + $Title + "» (название не найдено в начале файла). Положите нужную рукопись и запустите снова.") -ForegroundColor Red
        exit 1
    }
}
Write-Host ("  рукопись: " + $Docx)
if ((Resolve-Path -LiteralPath $Docx).Path -ne "$Root\book.docx") { Copy-Item -LiteralPath $Docx "$Root\book.docx" -Force }
Set-Location $Root
& "$Root\env\python.exe" "$Root\nox_audiobook.py" segment "$Root\book.docx" --max-chars $MaxChars   # короче 300: модель с ударениями глотает слова на длинных кусках
if ($LASTEXITCODE -ne 0) { Write-Host "разбивка упала — пришлите вывод в чат" -ForegroundColor Red; exit 1 }

Write-Host ""
if ($env:NOX_FROM_GO -eq "1") {
    Write-Host "ПОДГОТОВКА КНИГИ ЗАКОНЧЕНА: рукопись разбита на фразы. Дальше go сам проверит голоса и начнёт озвучку — запускать ничего не нужно." -ForegroundColor Green
} else {
    Write-Host "ГОТОВО. Озвучка сюда НЕ запускалась — запускается отдельно, когда решите:" -ForegroundColor Green
    Write-Host "  1) недостающие голоса (безопасно, скажет если все есть):"
    Write-Host "     & `"D:\kniga1a\env\python.exe`" `"D:\kniga1a\nox_audiobook.py`" cast --attn sdpa"
    Write-Host "  2) озвучка (полная книга ~23 ч аудио, это 10–14 часов работы видеокарты):"
    Write-Host "     powershell -NoProfile -ExecutionPolicy Bypass -File `"`$HOME\Downloads\run_fast.ps1`" -Root D:\kniga1a"
    Write-Host "  3) во втором окне — части на Диск (отдельная подпапка, с первой версией не смешается):"
    Write-Host "     & `"D:\kniga1a\env\python.exe`" `"D:\kniga1a\nox_audiobook.py`" watch --parts 19 --dest `"`$env:USERPROFILE\YandexDisk\Posoh_izdanie2`" --format mp3 --title `"Посох Забвения`" --author `"Д.`""
}
