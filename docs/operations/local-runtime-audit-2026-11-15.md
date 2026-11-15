# Локальная среда Nox и состояние MyBook — аудит 2026-11-15

**Тип документа:** снимок диагностики конкретной рабочей станции, не универсальная спецификация для всех ПК.  
**Дата:** 15 ноября 2026, примерно 14:38–14:49 (+03:00).  
**Репозиторий/ветка:** justfortune/text_voice, audit/nox-regression-fixes-2026-11-12.  
**Проверенный HEAD до этого документирующего коммита:** edbb42cc900b6d8781ccd5d431525ea1ec780172.

> Сырые TXT/CSV/JSON отчёты не коммитятся: они содержат имя Windows-пользователя, имя компьютера, абсолютные локальные пути и названия личных каталогов. Здесь оставлена нормализованная техническая сводка.

## 1. Executive summary

1. Найден 21 кандидат python.exe; 19 успешно опрошены. Один WindowsApps execution alias намеренно не запускался; один старый venv не смог запустить Python из-за устаревшей ссылки в конфигурации.
2. Получено 1004 строки Python/Conda package inventory. Это записи по всем окружениям, а не 1004 уникальных пакета.
3. Два Nox Runtime-окружения используют Python 3.12.16 и оба видят NVIDIA RTX 3090 через PyTorch CUDA 12.8. Оба проходят pip check.
4. Runtime-окружения совпали по всем пакетным именам и версиям со своими legacy-копиями: legacy base Nox env — 95/95, legacy Nox envfast — 105/105, legacy Kniga1a envfast — 105/105. Версия Python legacy — 3.12.15, Runtime — 3.12.16.
5. env и envfast не являются дубликатами и не должны сливаться: у них разные TTS engines и разные целевые версии Transformers.
6. Главный незавершённый пункт для MyBook — закреплённая модель ru-stress отсутствует в новом Runtime HF cache. Перед повторной загрузкой следует проверить legacy HF cache: исторический manifest уже фиксировал эту модель в старом Nox Runtime.
7. MyBook не прошёл сегментацию. Исходник движка согласован; generated engine, roles и segments ещё не опубликованы.
8. Временные launcher v3/v4 останавливались на ошибках сгенерированной Python-строки диагностики. Это не подтверждение неисправности CUDA или пакетов и не изменение исходников репозитория.

## 2. Проверенная топология Nox

Локальные корни на дату аудита:

- Repository: E:\NoxWorkspace\NoxAudiobook
- Shared Runtime: E:\NoxWorkspace\NoxAudiobook_Runtime
- Book workspace: E:\NoxWorkspace\NoxAudiobook_Books\MyBook

MyBook содержит directory junctions:

| MyBook path | Target |
|---|---|
| env | Runtime env |
| envfast | Runtime envfast |
| hf | Runtime hf |
| tools | Runtime tools |
| pip_cache | Runtime pip_cache |

Это ссылки на общие ресурсы, а не пять дополнительных Python environments. Runtime остаётся единым владельцем окружений, model cache и tools. Не копировать пакеты или модели внутрь MyBook поверх junctions.

### 2.1 Runtime env — базовый движок и сегментация

| Свойство | Проверенное значение |
|---|---|
| Interpreter | Runtime env, Python 3.12.16 |
| Environment type | Conda prefix |
| Python distributions | 95 |
| Conda records | 20 |
| PyTorch | 2.11.0+cu128 |
| Torch CUDA build | 12.8 |
| CUDA | available = true |
| GPU | NVIDIA GeForce RTX 3090 |
| Transformers | 4.57.3 |
| qwen-tts | 0.1.1 |
| pip check | No broken requirements found |

Отсутствие faster_qwen3_tts, ruaccent, openpyxl и faster_whisper в этом env не является автоматически неисправностью: это не основной fast TTS environment.

### 2.2 Runtime envfast — ускоренный TTS и проверки

| Свойство | Проверенное значение |
|---|---|
| Interpreter | Runtime envfast, Python 3.12.16 |
| Environment type | venv |
| Python distributions | 105 |
| PyTorch | 2.11.0+cu128 |
| Torch CUDA build | 12.8 |
| CUDA | available = true |
| GPU | NVIDIA GeForce RTX 3090 |
| faster-qwen3-tts | 0.5.4 |
| Transformers | 5.15.1 |
| qwen-tts-hf | 0.1.1.post1 |
| ruaccent | 1.5.8.3 |
| faster-whisper | 1.2.1 |
| openpyxl | 3.1.5 |
| pip check | No broken requirements found |

Класс FasterQwen3TTS успешно импортировался. CUDA device count = 1; capability = 8.6. На момент deep check было около 23.3 GiB свободной VRAM из примерно 24 GiB. Это idle-state observation, а не доказательство, что три TTS worker безопасно помещаются одновременно.

### 2.3 Почему env и envfast нельзя объединять

Среди 95 distributions в env и 105 distributions в envfast найдено 92 общих имени:
- 70 общих пакетов имеют одинаковые версии;
- 22 общих пакета имеют разные версии;
- только в env: qwen-tts, six, wheel;
- только в envfast: 13 пакетов, включая faster-qwen3-tts, faster-whisper, ruaccent, openpyxl, qwen-tts-hf, silero-stress, ctranslate2, sentencepiece.

Ключевые version differences:
- transformers: env 4.57.3, envfast 5.15.1;
- huggingface_hub: 0.36.2 vs 1.33.0;
- также различаются filelock, gradio, fastapi, fsspec, networkx, numba, onnxruntime, pydantic и другие.

В kit/run_fast.ps1 ветки установка envfast явно закрепляет transformers==5.15.1; комментарий в скрипте объясняет выбор для Qwen Mimi encoder. Базовый pipeline/сегментация использует Runtime env; fast worker запускается через envfast. Не объединять эти наборы и не менять Transformers без воспроизводимого regression/smoke test.

## 3. Legacy comparison

Старые каталоги не удалять, не перемещать, не переустанавливать и не использовать как временную папку.

| Legacy interpreter | Runtime counterpart | Package identity/version match | Python |
|---|---|---:|---|
| D:\nox_audiobook\env\python.exe | Runtime env | 95/95 | 3.12.15 → 3.12.16 |
| D:\nox_audiobook\envfast\Scripts\python.exe | Runtime envfast | 105/105 | 3.12.15 → 3.12.16 |
| D:\kniga1a\envfast\Scripts\python.exe | Runtime envfast | 105/105 | 3.12.15 → 3.12.16 |

Дополнительное наблюдение: legacy D:\kniga1a\envfast имеет base_prefix, указывающий на legacy Nox base env. Это отдельный fast venv со своим пакетом, но с Python base из старой общей среды. Не переносить и не удалять старые каталоги.

## 4. Сводка остальных найденных Python environments

Имена пользовательских профилей и абсолютные домашние пути намеренно опущены.

| Environment / project label | Python | Python distributions | Conda records | Torch / CUDA | Observation |
|---|---:|---:|---:|---|---|
| Conda base | 3.13.11 | 69 | 109 | Torch отсутствует | pip check pass; не использовать для Nox |
| Standalone Python 3.12 | 3.12.10 | 0/не возвращены | 0 | Torch отсутствует | Базовая установка |
| Standalone Python 3.10 | 3.10.0 | 95 | 0 | Torch 2.12.0+cpu; CUDA false | Kivy, Whisper, Selenium, grpc/kafka и другие legacy packages |
| Conda env legato | 3.12.12 | 31 | 21 | Torch 2.5.1+cu121; CUDA true | Отдельное ML/audio env |
| PyCharm project mozaic | 3.10.0 | 0 | 0 | — | pip check fail; incomplete/unresolved |
| PyECL project venv | 3.10.0 | 6 | 0 | — | Project-specific |
| PyKPRAparallel venv | 3.10.0 | 29 | 0 | — | Project-specific test stack |
| PySMRtests venv | 3.10.0 | 17 | 0 | — | Project-specific test stack |
| PytestForTatneft venv | 3.10.0 | 22 | 0 | — | pytest/grpc/kafka stack |
| PytestForTatneft local diarization | 3.10.0 | 9 | 0 | — | Separate diarization env |
| PytestForTatneft whisper GPU label | 3.13.11 | 42 | 0 | Torch 2.12.0+cpu; CUDA false | Despite folder label, installed Torch is CPU build; pip check fail due setuptools constraint |
| pythonProject venv | 3.10.0 | 0 | 0 | — | pip check fail; incomplete/unresolved |
| PythonProject1 venv | 3.10.0 | 6 | 0 | — | Project-specific |
| staff venv | 3.10.0 | 2 | 0 | — | Project-specific |
| Desktop PytestForTransneft venv | unknown | 0 | 0 | — | Python probe failed; venv appears to retain stale base interpreter reference |
| WindowsApps python alias | not probed | — | — | — | Execution alias, intentionally not launched |

Инвентаризация нашла 21 executable candidate, 19 successfully probed и 1004 package rows. Она не доказывает, что на всех дисках нет других Python: сканировались ограниченные корни и глубина. Это снимок, а не указание удалять или чистить сторонние окружения.

### 4.1 Non-Nox issues: keep separate

- Whisper-GPU-labelled project env использует CPU Torch 2.12.0 и не проходит pip check: установлен setuptools 82.0.1, а Torch требует setuptools<82. Не исправлять в рамках Nox.
- mozaic и pythonProject venv вернули 0 distributions и pip check failures. Роль этих проектов нужно выяснить до ремонта.
- Старый Desktop Transneft venv не стартовал; ссылка на base Python устарела. Файлы проекта сохранить.
- Conda base Python 3.13.11 — активный python в наблюдаемом терминале с (base). Для Nox использовать явные пути к Runtime interpreters.

Python 3.14 не обнаружен в проверенных scan roots, PATH, Python Launcher и сохранённых PyCharm SDK. Это не доказывает его отсутствие во всех непроверенных папках. Сохранённый SDK проекта в .idea/misc.xml указывает на Runtime envfast, Python 3.12.

## 5. MyBook state

### Подтверждено

- MyBook root существует в Nox Books workspace.
- book.docx существует, 856,464 bytes.
- SHA-256: b95a364421e02b4a35961a5f1e8e04f336593338d9be715388314907cbd82021.
- Настройки озвучки.xlsx существует.
- work/settings.json и work/accent_dict.csv существуют.
- work/voices существует, рекурсивное число файлов — 0.
- nox_audiobook.new.py, nox_fast.py и patch_k1a.py существуют.
- SHA-256 nox_audiobook.new.py одинаков в repository base engine, Runtime engine и staged MyBook file: 00d3c7de08ddf79f4d7e974e977e6b79483e303a4205f0174193cd3481499ac7.

### Ещё не созданы

- MyBook nox_audiobook.py;
- MyBook nox_part.py;
- work/segments.csv;
- work/roles.json;
- work/prep.sig;
- work/parts.json, durations.json, assembly.json, fast_args.json.

Это согласуется с тем, что preparation остановилась до segmentation. Исходный engine присутствует и совпадает с repository. Текущая segment() функция создаёт новый roles template, если roles.json отсутствует. Не переносить роли из другой книги в новую рукопись.

### Настройки, требующие проверки

Диагностическая сводка вернула:
- workers = 3;
- part_min = 19;
- accent = acute;
- max_chars = 200;
- share_from = пусто;
- docx = пусто;
- dest = MyBook;
- title = MyBook;
- author = пусто.

Workbook существует, но title=MyBook нужно сопоставить с фактическим названием книги «Посох Забвения» до segmentation/export. Не принимать имя папки за название аудиокниги автоматически. Пустой share_from означает, что cross-book role seeding не настроен; оставить пустым без явного авторского решения.

Копия manuscript в _incoming была занята другим процессом во время позднего аудита; повторный хеш этой копии не получен. Более ранняя проверка уже подтвердила совпадение SHA-256 staged MyBook DOCX с ожидаемым загруженным документом. Не принудительно закрывать Word и не менять ни одну копию ради повторной проверки.

## 6. HF model cache and audio tools

### 6.1 Runtime HF cache

Deep check обнаружил HF hub directory, но не обнаружил pinned model repository/snapshot в Runtime:
- model: shib79/qwen3-tts-12hz-1.7b-ru-stress-cf;
- pinned revision: 66338b83519a05e3d4f923e0f9b4cd2ce686f138;
- ожидаемые файлы: config.json, model.safetensors, speech_tokenizer/.

Ранее закоммиченный data/sreda_manifest.json (2026-11-12) записывает эту модель в legacy environment с размером около 9,088 MB. Это историческое свидетельство; фактическое наличие и читаемость legacy cache в аудите 2026-11-15 не проверялись.

**Следующее действие:** сначала read-only проверить legacy cache. Если файлы на месте, спланировать verified copy в Runtime HF cache с проверкой хешей и rollback. Скачивать из Hugging Face только если legacy файлы отсутствуют или повреждены. Не копировать symlink stubs без проверки целевых blob-файлов и размеров.

### 6.2 FFmpeg, FFprobe, SoX

- FFmpeg и FFprobe присутствуют в Runtime tools.
- Их нет в process PATH.
- Текущий base engine ищет ffmpeg в соседнем tools directory, поэтому изменение global PATH пока не требуется.
- sox.exe не найден в Runtime tools и PATH. Python distribution sox установлен. Нужность standalone SoX binary для текущего pipeline этой проверкой не доказана; не устанавливать только из-за этого сообщения.

## 7. Recovery launcher attempts — interpretation

Два временных recovery launcher были запущены в StageOnly:
- v3 завершился при формировании Python probe: TorchVersion использовался с unary plus;
- v4 завершился с Python SyntaxError в сгенерированной строке print(torch=...).

Последующий прямой аудит доказал импорт Torch, доступность CUDA, наличие RTX 3090 и импорт FasterQwen3TTS. Ошибки launcher не следует трактовать как отсутствие CUDA или повреждение пакетов.

StageOnly не достиг segmentation или synthesis. Исходники repository не изменялись этими временными ошибками. Не использовать старые v3/v4 downloads как canonical setup scripts.

## 8. Required safe sequence

1. Сохранить текущее состояние: ничего не удалять, не переименовывать, не обновлять и не объединять. Старые D: проекты оставить нетронутыми.
2. Read-only проверить legacy model cache и pinned snapshot.
3. Сверить title/author в workbook и settings с «Посох Забвения»; оставить share_from пустым.
4. Завершить изолированный launch kit из точной версии ветки; не использовать Downloads как изменяемый источник кода.
5. Подготовить engine транзакционно, с резервной копией MyBook. Оставить nox_audiobook.new.py до post-check.
6. Проверить completeness kit: nox_part.py, nox_fast.py, verify_k1a.py, voicecheck_k1a.py, settings script, Studio files и required sidecars; использовать manifest/hash validation.
7. Копировать или скачивать model только после cache check. HF_HOME должен быть явно направлен на Runtime hf; pinned revision и все нужные файлы проверить до load.
8. Запустить CPU-only segmentation на восстановимой копии и проверить counts, roles, title, encoding и work outputs до GPU.
9. Выполнить single-worker GPU smoke-test на коротком opt-in sample, проверить audio/logs и реальный peak VRAM.
10. Только затем тестировать 2 и 3 workers. Три worker — настроенная цель, не подтверждённая безопасная нагрузка.
11. Полный regression/verification run — только после успешного sample и part workflow.

Будущий initializer должен быть fail-closed, совместим с PowerShell 5.1, где возможно идемпотентен, явно перечислять каждую запись, создавать backup до изменения book state, не убивать посторонние Python процессы и не менять legacy D: каталоги. Установка пакетов должна быть отдельным явным режимом с pinned versions и последующим pip check.

## 9. Git / branch context

- Repository: justfortune/text_voice.
- Working branch: audit/nox-regression-fixes-2026-11-12.
- Remote HEAD до документирующего коммита: edbb42cc900b6d8781ccd5d431525ea1ec780172.
- Локальная Windows working copy сообщала о незакоммиченном изменении tests/test_nox_audiobook_regressions.py (один insert и один delete в последнем diff stat). Содержимое этого локального изменения не извлекалось и в remote documentation commit не включено.
- Этот commit добавляет документацию и read-only inventory utility. Он не меняет TTS engine, Studio, segmentation logic, tests или package constraints.
- После pull локально проверить, что прежняя модификация тестового файла сохранилась.

## 10. Re-run inventory

Из PowerShell 5.1:

    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$PSScriptRoot\tools\nox_python_environment_inventory.ps1"

Скрипт сохраняет отчёты в Downloads. Он выполняет bounded discovery, однократно опрашивает каждый уникальный Python executable, собирает pip/Conda package metadata и запускает pip check. Он не устанавливает, не обновляет и не удаляет пакеты, не меняет проекты и не мутирует Git.

Сгенерированные TXT/CSV/JSON содержат локальные identifiers, пути и package details. Это локальные diagnostic artifacts, не source files. Перед передачей наружу их нужно проверить и очистить; не коммитить в публичный repository.

## 11. Deliberately not included

- Не включены manuscript, DOCX, XLSX, role maps, voice samples, WAV/MP3, audio exports, Studio projects, caches, model weights и raw package reports.
- Не опубликованы user-profile name, computer name и полные inventory CSV/JSON.
- Этот документационный commit не изменял локальные Windows environments, рабочие каталоги или старые D: проекты.
- Не заявляется, что полный audiobook synthesis или трёхворкерная производительность уже проверены.
