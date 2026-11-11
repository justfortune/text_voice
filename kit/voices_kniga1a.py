#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# NOX_KIT 2026-11-12a
"""
voices_kniga1a.py — редакторский кастинг голосов первой книги по описаниям из текста.
Обновляет work/roles.json, удаляет устаревшие образцы голосов (cast пересоздаст) и
удаляет уже озвученные сегменты ТОЛЬКО изменённых ролей (рассказчик/Джек/Хорват не трогаются).
Запуск: из папки проекта (D:\\kniga1a):  env\\Scripts или env python voices_kniga1a.py
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import nox_audiobook as n  # noqa: E402

RU = "Clear Russian diction."
V = {
    # роль: (инструкция VoiceDesign, основание из текста)
    "капитан":   ("Old airship captain around 65, warm hoarse smoky voice, unhurried, kind grandfather tone. " + RU, "Яндор, старый капитан"),
    "Яблочник":  ("Street fruit seller around 55, simple good-natured voice, a bit loud, market cadence. " + RU, "торговец яблоками"),
    "Пятак":     ("Street boy around 12, thin quick voice, sniffs, cocky but scared sometimes. " + RU, "мальчишка-вор, шмыгает носом"),
    "Дориан":    ("Young man of 17, soft restrained voice, polite, hides feelings. " + RU, "юноша 16–18 лет"),
    "Льюис":     ("Teenage apprentice around 16, light lively voice, slightly mischievous. " + RU, "ученик Гильдии"),
    "Альби":     ("Teenage boy around 15, bright voice that cracks a little, blushes easily, enthusiastic. " + RU, "рыжий, краснеет до ушей"),
    "Лэнс":      ("Knight around 35, firm steady baritone, short clipped sentences, calm authority. " + RU, "рыцарь, всегда прав"),
    "Макс":      ("Huge innkeeper around 50, low-pitched deep bass, warm and unhurried, speaks calmly at moderate volume. " + RU, "огромный трактирщик"),
    "Бреннет":   ("Craftsman around 45, warm rough workshop voice, honest and simple. " + RU, "мастер, руки в масле"),
    "Кинкейд":   ("Armor merchant around 50, low-pitched resonant baritone, confident jovial salesman, moderate volume. " + RU, "широкий как дверь, гремит"),
    "Маргит":    ("Old landlady around 70, small cranky grandmother voice, sleepy and strict but kind inside. " + RU, "старушечий голос"),
    "Тилли":     ("Little girl of 8, high clear child voice, serious and proud. " + RU, "девочка 8 лет"),
    "Глинда":    ("Tough working woman around 45, low rough voice, direct, no-nonsense. " + RU, "женщина в копях, резкая"),
    "Тина":      ("Woman around 25, tired, cool, matter-of-fact tone, slight irritation. " + RU, "подруга Джека"),
    "Морган":    ("Gang leader around 45, low cold commanding voice, quiet menace, never hurries. " + RU, "главарь, угрожающе тихий"),
    "Святоша":   ("Bandit around 35, soft insinuating half-whisper, falsely pious tone. " + RU, "тихий вкрадчивый"),
    "Ласка":     ("Knife-thrower around 30, quick light mocking voice. " + RU, "быстрый насмешливый"),
    "Кривой":    ("Gloomy bearded crossbowman around 45, low sullen voice, short grumbled phrases. " + RU, "мрачный бородач"),
    "Хряк":      ("Big dumb thug around 35, thick slow loud voice. " + RU, "громила с дубиной"),
    "Чепчик":    ("Male bandit around 40 with a thin reedy worried voice, like a fussy old lady, comic contrast. " + RU, "тонкий озабоченный голос"),
    "Усатый":    ("Plain soldier-type around 40, ordinary gruff voice. " + RU, "бандит"),
    "Вежливый":  ("Sinister servant around 40, icy flawlessly polite even voice, graveyard calm, slightly sweet. " + RU, "ледяная вежливость, пахнет могилой"),
    "Старшой":   ("Leader of beggars around 40, reasonable tired voice, street wisdom, низкий. " + RU, "рассудительный главарь"),
    "Грамотей":  ("Young bookish beggar around 20, flat monotone reading voice, absent-minded. " + RU, "очкарик с книжкой"),
    "Кочерга":   ("Very old toothless beggar around 75, mumbling shaky voice, chews words. " + RU, "шамкает пустыми дёснами"),
    "Вьюн":      ("Skinny street boy of 16 (Nils Penny): thin fast adolescent voice, slightly breaking, alert, quick talker. " + RU, "мальчишка с тонкой шеей, 16 лет по показаниям"),
    "Малой":     ("Street kid around 11, small high voice. " + RU, "младший из ватаги"),
    "Булка":     ("Chubby boy around 12, slow contented voice, often eating. " + RU, "ест яблоко целиком"),
    "Одноухий":  ("Ancient giant, enormous slow deep voice, old and grey, speaks simply, heavy pauses. " + RU, "старый седой великан"),
    "Брюхо":     ("Giant cook-helper, huge hollow booming voice, simple-minded, few words. " + RU, "великан у вертела"),
    "Малый":     ("Young giant, low-pitched deep but youthful voice, naive and curious, speaks slowly and softly. " + RU, "младший великан"),
    "Король":    ("Giant king, colossal slow rumbling bass, speaks human words short and costly. " + RU, "король великанов, говорит коротко"),
    "Повар":     ("Giant cook, deep businesslike rumble, discusses cooking calmly. " + RU, "повар великанов"),
    "Лопрок":    ("Very old candle-keeper around 70, quiet dry whispery voice, speaks like telling an old tale. " + RU, "старик-свечник"),
    "Мордвин":   ("Old swamp hermit around 65, slow muffled bass, long pauses, talks past questions. " + RU, "отшельник на болоте"),
    "Кейн":      ("Local guide around 35, relaxed low baritone, wry dry humor, calm even in danger. " + RU, "весёлый проводник без двух пальцев"),
    "Эйдан":     ("Guild cartographer around 30, nervous intelligent voice, quick frightened speech. " + RU, "испуганный картограф"),
    "Делвин":    ("Rat-king of the undercity around 45, sly creaky voice from the dark, amused and dangerous. " + RU, "крысиный король"),
    "Бизанти":   ("Money-changer around 50, dry precise clipped voice, counts while talking. " + RU, "меняла, быстро считает"),
    "Корнелиус": ("Old caged scholar around 70, soft tired cultured voice, gentle irony. " + RU, "старик в клетке"),
    "Ужасающий": ("Warlord of Dun Mir around 50, mighty harsh commanding bass, battlefield voice. " + RU, "лорд Ужасающий"),
    "Брам":      ("Old boatman around 55, taciturn weathered voice, few words. " + RU, "лодочник"),
    "Монах":     ("Young monk around 30, quiet detached voice, as if half-absent. " + RU, "монах, забывший имя"),
    "Хекубах":   ("Powerful sorceress around 35, velvet smooth cold voice, seductive and merciless, unhurried. " + RU, "гладкий ласковый голос, враг"),
    "Посох":     ("A talking wooden staff: dry creaky wooden timbre, slow, slightly otherworldly, deadpan. " + RU, "говорящий посох Тис"),
    "голос":     ("Neutral unseen male voice, slightly muffled, mysterious. " + RU, "безымянный голос"),
    "Ковальски": ("Auto shop owner around 50, plain earthly working voice. " + RU, "шиномонтаж на Земле"),
    "Старый":    ("Old man around 70, worn quiet voice. " + RU, "старик"),
    # --- v2: роли, у которых был дежурный голос «мужчина лет сорока» (отсюда «левые голоса») ---
    "Алдвин":    ("Very old monk around 75, brother Aldvin: slow, deep, serene voice, long pauses, looks through you. " + RU, "брат Алдвин, старый друг Хорвата"),
    "Ансельм":   ("Caged scholar around 45 who counts everything: high, very precise, slightly nasal voice, even tempo. " + RU, "голос высокий и очень точный"),
    "Архивариус": ("Old archivist around 75: small dry patient voice, white whiskers, slow and bookish, gentle irony. " + RU, "старичок в чёрной шапочке с бакенбардами"),
    "Смотритель": ("Stern old city warden around 65 with an hourglass: dry, authoritative, bureaucratic, unhurried. " + RU, "старик-смотритель с песочными часами"),
    "Едок":      ("Giant obsessed with food: enormous slow greasy bass, few words, chewing cadence. " + RU, "толстый великан у котелка"),
    "Пёс":       ("Bandit nicknamed the Dog around 40: flat, dry, dangerous voice, never raises it, crossbowman. " + RU, "Пёс с арбалетом"),
    "Большой":   ("Big young thug around 25, low-pitched rough voice, simple and slow-witted, moderate volume. " + RU, "Большой Мешок"),
    "Учитель":   ("Guild teacher of syllables around 50: pedantic, theatrical, articulates every sound, exasperated patience. " + RU, "Учитель слогов"),
    "гробовщик": ("City undertaker around 50: low mournful voice with a hungry glint, falsely sorrowful. " + RU, "городской гробовщик"),
    "хозяин":    ("Shop or inn owner around 50: polite, salesman cadence, a little oily. " + RU, "хозяин лавки/трактира"),
    "мастер":    ("Craftsman around 50: calm practical voice, speaks about work with respect. " + RU, "мастер"),
    "старик":    ("Old man around 70: cracked, worn, slow voice, breathy, a bit deaf. " + RU, "безымянный старик"),
    "старичок":  ("Little old man around 75: thin high shaky voice, fussy and kind. " + RU, "старичок"),
    "старуха":   ("Old woman around 70: thin, creaky, grumbling voice. " + RU, "старуха"),
    "женщина":   ("Woman around 40: plain, tired, matter-of-fact voice. " + RU, "безымянная женщина"),
    "девушка":   ("Young woman around 18: light, quick, clear voice. " + RU, "девушка"),
    "девочка":   ("Little girl around 7: high, thin child voice, sincere. " + RU, "девочка"),
    "мальчик":   ("Boy around 11: high boyish voice, quick and curious. " + RU, "мальчик"),
    "парень":    ("Young man around 20: light rough voice, cocky, street tone. " + RU, "парень"),
    "солдат":    ("Soldier around 30: hoarse, blunt, barracks voice. " + RU, "солдат"),
    "стражник":  ("City guard around 35: bored, heavy, official voice. " + RU, "стражник"),
    "сержант":   ("Sergeant around 45: barking, gravelly, commanding voice. " + RU, "сержант"),
    "рыцарь":    ("Knight around 35: firm, measured, chivalrous baritone. " + RU, "рыцарь"),
    "король":    ("King: deep, slow, ceremonial voice, weary authority. " + RU, "король"),
    "монах":     ("Monk around 40: quiet, even, detached voice, almost whisper. " + RU, "монах"),
    "учитель":   ("Teacher around 50: clear, pedantic, instructive voice. " + RU, "учитель"),
    "волшебник": ("Old wizard around 70: dry, tired, precise voice with authority. " + RU, "волшебник"),
    "гость":     ("Guest around 45: pleasant, polite, slightly formal voice. " + RU, "гость"),
    "Голос":     ("Unseen voice in the dark: low, calm, slightly echoing, unhurried, mysterious. " + RU, "Голос с большой буквы"),
    # --- роли, найденные при редакторском разборе диалогов (сценарий по ролям) ---
    "Наставник": ("Boy of 12 who teaches the Reversal spell: small clear high child voice, but dry, even, unhurried lecturer tone, no emotion, precise. " + RU, "мальчик-наставник с голосом профессора"),
    "Грунбар":   ("Former Guild apprentice around 50, heavy drinker: thick slurred bilious voice, resentful, sneering. " + RU, "бывший ученик Хорвата, пьющий, желчный"),
    "Джорган":   ("Good-natured young tavern barker around 22 in a leather vest: loud cheerful inviting voice, market sing-song. " + RU, "зазывала у таверны Максимилиана"),
    "Эм-Эр":     ("Master of the Registry, Guild gatekeeper around 60 with a pince-nez: thin pedantic nasal voice, bureaucratic, precise. " + RU, "привратник Гильдии в пенсне"),
    "Ганем":     ("Rich haughty senior apprentice around 17: smooth sneering young voice, drawling, condescending. " + RU, "богатый насмешник, соперник Льюиса"),
    "Священник": ("Parish priest around 55: soft unctuous pastoral voice, slow and measured, a little weary. " + RU, "священник церкви Св. Аллистора"),
    "Бочонок":   ("Fat gravedigger from a gang around 40: thick wheezing voice, slow, simple-minded. " + RU, "толстяк-землекоп из шайки Моргана"),
    "Сыч":       ("Long-legged hunched bandit lookout around 30: high hurried stammering voice, cowardly. " + RU, "бандит-дозорный, трус"),
    "Главарь":   ("Gang leader around 50 with a gold tooth: velvet mocking baritone, unhurried, amused menace. " + RU, "главарь банды у дома Страваса"),
    "Высокий":   ("Tall grey-cloaked servant of the enemy around 50: hoarse rasping voice with a cold, chewing cloves, politely tired, ageing fast. " + RU, "высокий в сером, слуга Хекубах"),
    "Круглый":   ("Short round grey-cloaked bookkeeper around 45: warm approving tenor, solemn, pleased with his ledger. " + RU, "круглый в сером, счетовод"),
    "Молодой":   ("Young grey-cloaked recruit around 19: nervous breaking frightened voice, hurried. " + RU, "молодой в сером, новичок"),
    "Джерад":    ("Herbalist around 40, thin and long-armed: gentle thoughtful voice, calm, slightly dreamy. " + RU, "травник, хозяин лягушки Льюиса"),
    "Тавиус":    ("Elderly grey peasant mage around 65: slow earthy rural voice, kind, weathered. " + RU, "крестьянин-маг из Брина"),
    "Марта":     ("Big peasant woman around 55 who was once a she-wolf: low quiet rough voice, warm, few words. " + RU, "жена Тавиуса"),
    "Матильда":  ("Red-haired freckled young woman around 18, boatman's daughter: bright quick voice, bold. " + RU, "дочь лодочника Брама"),
    "Молчун":    ("Thin hunched silent giant guard: hollow deep slow voice, very few words, clipped. " + RU, "великан-охранник копей"),
    "Берта":     ("Small captive woman around 35 with a black braid, blacksmith's wife: firm, worn, determined voice. " + RU, "пленница из Брина"),
    "Сверчок":   ("Slave boy around 12 with a coal-black face: thin hoarse quick voice, wary. " + RU, "мальчишка из невольников"),
    "Надсмотрщик": ("Enormous giant overseer with an iron collar and a weight on a chain: brutal thunderous roar, slow, cruel. " + RU, "великан-надсмотрщик"),
    "Борг":      ("Guard decurion of Dun Mir around 40: rough jovial military voice, barking, hearty. " + RU, "десятник стражи Дан Мира"),
    "Лисбет":    ("Kitchen girl of 15, flushed from the stove: light timid quick voice. " + RU, "кухонная девушка Дан Мира"),
    "Тео":       ("Young silent mage prisoner around 25: quiet hoarse voice, speaks rarely, guarded. " + RU, "молодой маг-узник из клетки"),
    "Млург":     ("Owner of the Wizard's Shop around 50: oily wheedling shopkeeper voice, slightly croaky, long-nosed. " + RU, "хозяин Лавки Волшебника в Галаве"),
    "Малый Мешок": ("Younger of the two Sack brothers around 20, thug: thin quick cocky voice, snub-nosed. " + RU, "младший брат-громила, гл. 17–18"),
    "Демон":     ("Chief demon of the underworld, ancient: enormous cavernous rumbling voice, slow, mocking, echoing. " + RU, "главный демон Подземного мира"),
    "Генрик":    ("Keeper of the bewitched wolves in Ix around 45, fox-fur hat and whistle: sharp foxy voice, quick, cunning. " + RU, "хозяин загона волков"),
    "Мистик":    ("Soothsayer at the well in Ix around 50, dark gaunt face: hollow slow prophetic voice, hypnotic. " + RU, "прорицатель у колодца"),
    "Дед":       ("Jack's grandfather on Earth, old farmer around 75: warm slow dry voice, smells of hay and tobacco, plain speech. " + RU, "дед Джека на Земле"),
    "Главный Мертвец": ("Ancient half-dead necromancer scholar around 90: thin dry rustling voice, impeccably polite, curious, patient and merciless. " + RU, "старик-некромант, учитель Хекубах, гл. 44"),
    "Неизвестный ученик": ("Ghost of a Guild apprentice of 17: faint hollow whisper, barely audible, flat, long pauses. " + RU, "призрак ученика, гл. 43"),
    "Свидетель": ("Stone statue guardian: flat dry colourless official voice, slow, slightly resonant like stone, no emotion. " + RU, "каменные стражи у лестницы, гл. 42"),
}
ALIASES = {"Тиса": "Посох", "Тисом": "Посох", "Максимилиан": "Макс", "Яндор": "капитан"}
# «Голос как у роли» по решению автора (05.11): все голоса из подземелий звучат как «голос (гл.22)». Применяется
# в таблицу «Роли» один раз; дальше главнее таблица (очистите ячейку — роль снова своим голосом).
SAME_AS = {"Голос": "голос (гл.22)", "голос (гл.24)": "голос (гл.22)", "голос (гл.29)": "голос (гл.22)",
           "голос (гл.40)": "голос (гл.22)", "голос (гл.41)": "голос (гл.22)"}

# Пол/возраст для явного указания в инструкции и для проверки образцов по высоте тона (voicecheck_k1a.py).
# Всё, что не перечислено, — взрослый мужчина.
FEMALE = {"Маргит", "Глинда", "Тина", "Хекубах", "старуха", "женщина", "девушка", "Марта", "Матильда", "Берта", "Лисбет"}
GIRL = {"Тилли", "девочка"}
BOY = {"Пятак", "Малой", "Булка", "мальчик", "Наставник", "Сверчок"}
TEEN = {"Альби", "Льюис", "Дориан", "парень", "Вьюн", "Ганем", "Молодой", "Малый Мешок", "Джорган", "Тео", "Неизвестный ученик"}   # подростки/юноши: тон переходный, по F0 не проверяем
CHILD = GIRL | BOY
DEFAULT_INSTRUCT_PREFIXES = ("A man in his forties", "A woman in her thirties")
# 05.11: у Макса, Кинкейда, Малого, Кейна, Большого генератор голоса на словах «громкий/гремящий/весёлый» раз за разом давал
# высокий голос (200–300 Гц, как крик) — описания переписаны: «low-pitched», умеренная громкость, характер — словами.
# роли, чей образец нужно пересоздать разово из-за уточнённого возраста/пола (срабатывает только пока инструкция отличается)
FORCE_RECAST = {"Вьюн"}


def with_gender(role: str, instr: str) -> str:
    """Явный пол в начале инструкции: VoiceDesign без него выбирает пол случайно."""
    if instr.startswith(("Male voice", "Female voice", "Boy's voice", "Little girl's voice", "Young male voice")):
        return instr
    m = GENERIC_SPLIT.match(role)          # «женщина (гл.24)» -> пол базовой роли «женщина»
    if m:
        role = m.group(1)
    if role in FEMALE:
        return "Female voice. " + instr
    if role in GIRL:
        return "Little girl's voice (child). " + instr
    if role in BOY:
        return "Boy's voice (child, not an adult). " + instr
    if role in TEEN:
        return "Young male voice. " + instr
    return "Male voice. " + instr


GENERIC_SPLIT = re.compile(r"^(.*) \(гл\.(\d+)\)$")   # «старик (гл.12)» — другой безымянный старик в другой сцене
GENDER_PREFIXES = ("Male voice", "Female voice", "Boy's voice", "Little girl's voice", "Young male voice")


def gender_prefix(instr: str) -> str:
    for g in GENDER_PREFIXES:
        if instr.startswith(g):
            return g
    return ""


def main():
    roles = n.load_roles()
    existed = set(roles["roles"])
    changed = []
    # роли вида «старик (гл.12)» (из сценария): описание базовой роли + свой вариант голоса
    for role in list(roles["roles"].keys()):
        m = GENERIC_SPLIT.match(role)
        if m and m.group(1) in V:
            base_instr = V[m.group(1)][0]
            V[role] = (base_instr + f" Voice variant {m.group(2)}: a different person of the same kind.", f"{m.group(1)}, сцена главы {m.group(2)}")
    # роли, которые проверка образцов (voicecheck_k1a.py) велела пересоздать
    recast_path = n.WORK / "recast.txt"
    recast = set()
    if recast_path.exists():
        recast = {x.strip() for x in recast_path.read_text(encoding="utf-8").splitlines() if x.strip()}
    for role, (instr, why) in V.items():
        instr = with_gender(role, instr)
        cfg = roles["roles"].get(role)
        if cfg is not None and cfg.get("user_cfg"):      # роль правили в «Настройки озвучки.xlsx» — таблица главнее
            continue
        if cfg is None:
            roles["roles"][role] = {"instruct": instr, "same_as": None, "count": 0}
            changed.append(role)
            continue
        prev = cfg.get("instruct") or ""
        if prev != instr:
            cfg["instruct"] = instr
            cfg["same_as"] = None
        # образец пересоздаём только если его ещё не было, он был «дежурным» или его забраковала проверка;
        # просто уточнённый текст инструкции уже созданный голос не трогает (чтобы не менять удачные голоса)
        if (prev.startswith(DEFAULT_INSTRUCT_PREFIXES) or role in recast or role in FORCE_RECAST
                or (gender_prefix(prev) and gender_prefix(prev) != gender_prefix(instr))   # сменился пол/возраст — старый образец не годится
                                                                       # (образец без префикса — не смена пола, его проверит voicecheck)
                or not n.voice_files(role, n.voice_stems(roles))[0].exists()):
            if prev != instr or role in recast:
                changed.append(role)
    # роли вне V (их описал сам конвейер): тоже явный пол, без пересоздания
    for role, cfg in roles["roles"].items():
        if role in V or role in ("narrator", "Джек", "Хорват") or cfg.get("user_cfg"):
            continue
        instr = with_gender(role, cfg.get("instruct") or "")
        if instr != cfg.get("instruct"):
            cfg["instruct"] = instr
    roles.setdefault("aliases", {})
    for a, b in ALIASES.items():
        if roles["aliases"].get(a) != b:
            roles["aliases"][a] = b
            changed.append(a)
    # роли, у которых в текущей разбивке нет ни одной реплики (старые догадки «Тиса», базовые «старик» и т.п.), —
    # убираем из roles.json, чтобы cast не тратил время на ненужные голоса. Роль остаётся, если её удаление
    # поменяло бы латинские имена файлов других ролей (voice_stems нумерует совпадения по порядку).
    pruned = []
    if n.CSV_PATH.exists():
        al = roles.get("aliases", {})
        rows_ = n.load_rows()
        used = {al.get(r["speaker"], r["speaker"]) for r in rows_} | {n.resolve_role(r["speaker"], roles) for r in rows_}
        keep = used | {"narrator", "Джек", "Хорват"} | set(roles["aliases"].values()) \
            | {cfg.get("same_as") for cfg in roles["roles"].values() if cfg.get("same_as")}
        for role in [r for r in roles["roles"] if r not in keep]:
            before = n.voice_stems(roles)
            trial = dict(roles, roles={k: v for k, v in roles["roles"].items() if k != role})
            after = n.voice_stems(trial)
            if all(after[k] == before[k] for k in trial["roles"]):
                roles["roles"] = trial["roles"]
                pruned.append(role)
        for role in pruned:
            while role in changed:
                changed.remove(role)
    n.ROLES_PATH.write_text(json.dumps(roles, ensure_ascii=False, indent=2), encoding="utf-8")

    # устаревшие образцы голосов изменённых ролей -> удалить (cast пересоздаст по новым описаниям)
    stems = n.voice_stems(roles)
    removed_samples = 0
    for role in set(changed) & set(roles["roles"].keys()):
        wav_p, txt_p = n.voice_files(role, stems)
        for p in (wav_p, txt_p):
            if p.exists():
                p.unlink()
                removed_samples += 1

    # уже озвученные сегменты изменённых ролей -> удалить (их немного, переозвучатся новым голосом)
    removed_wavs = 0
    if n.CSV_PATH.exists() and n.WAVS.is_dir():
        changed_set = set(changed)
        for r in n.load_rows():
            if n.resolve_role(r["speaker"], roles) in changed_set:
                p = n.wav_for(r, roles)
                if p.exists():
                    p.unlink()
                    removed_wavs += 1
    if recast_path.exists():
        recast_path.write_text("", encoding="utf-8")
    pruned_old = [r for r in pruned if r in existed]
    print(f"ролей обновлено: {len(set(changed))}; образцов голосов на пересоздание: {removed_samples}; "
          f"озвученных сегментов сброшено: {removed_wavs}"
          + (f"; ролей без реплик убрано: {len(pruned_old)} ({', '.join(pruned_old[:12])}{'…' if len(pruned_old) > 12 else ''})"
             if pruned_old else ""))
    print("Основания кастинга: " + "; ".join(f"{k} — {v[1]}" for k, v in list(V.items())[:8]) + "; ...")


if __name__ == "__main__":
    main()
