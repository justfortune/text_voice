#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# NOX_KIT 2026-11-12a
"""
nox_voices.py — БАНК ГОЛОСОВ и подбор голосов ролям. Свои голоса записывать не нужно.

Банк — общий для всех книг: D:\\nox_audiobook\\voicebank (bank.json + <id>.wav/.txt). В нём два вида голосов:
  • заготовки — 66 описанных типажей (рассказчики, юноши, мужчины, старики, девушки, женщины, старухи, дети, существа);
    образец каждой создаёт та же модель, что и голоса книги (Qwen3-TTS VoiceDesign), по готовому описанию;
  • голоса из книг — образцы ролей уже озвученных книг (их можно дать роли другой книги).

Подбор. После разбивки рукописи роли распознаны «вчерне»; каждой роли без образца подбирается голос: пол и возраст — из
таблицы настроек, а если их там нет — по имени роли («старик», «девочка», «Марта»); характер — по имени («капитан» —
твёрдый, «трактирщик» — добродушный) и по словам автора вокруг реплик («прохрипел», «шепнула», «рявкнул»). Заметным ролям
голоса не повторяются. Роль, которой автор сам описал голос, получает голос по своему описанию, как и раньше.

Команды (первая — то, что вызывает go):
  prepare --root КНИГА [--all]     дать голоса ролям текущей части (с --all — всей книги), у которых нет образца
  suggest --root КНИГА [--all] [--json]   только показать подбор, ничего не меняя
  apply   --root КНИГА --role РОЛЬ --voice ID   поставить роли голос из банка (прежний образец — в work\\voices_old)
  revert  --root КНИГА --role РОЛЬ --first      сбросить голос роли: вернуть самый первый её образец
  revert  --root КНИГА --role РОЛЬ [--file ИМЯ]   вернуть роли прежний образец (тот, что был до последней смены, или любой
                                   из work\\voices_old — его имя показывает history)
  history --root КНИГА --role РОЛЬ [--json]   прежние образцы роли (work\\voices_old), от новых к старым
  tidy    --root КНИГА [--apply] [--json]   порядок в голосах: образцы без роли в книге -> work\\voices_old\\bez_roli, записи
                                   банка без файла образца убираются из списка, пустые ссылки ролей снимаются. Без --apply —
                                   только отчёт; ничего не удаляется
  custom  --root КНИГА --name "Название" --desc "описание по-английски" --g m|f [--age adult]
                                   свой голос по описанию — в банк (видеокарта, ~10 секунд после загрузки модели)
  harvest --root КНИГА             образцы голосов книги — в банк (копии; видеокарта не нужна)
  design  --root КНИГА [--only id,id] [--force]   создать образцы заготовок (видеокарта; все 66 — около 10 минут, разово)
  list    [--root КНИГА] [--json]  что есть в банке
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

BANK = Path(os.environ.get("NOX_VOICEBANK") or r"D:\nox_audiobook\voicebank")


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
AGES = ["child", "teen", "young", "adult", "mature", "old"]
AGE_RU = {"child": "ребёнок", "teen": "подросток", "young": "молодой", "adult": "взрослый", "mature": "зрелый", "old": "старый"}
TAG_RU = {"narrator": "рассказчик", "low": "низкий", "high": "высокий", "calm": "спокойный", "lively": "живой",
          "firm": "твёрдый", "soft": "мягкий", "rough": "хриплый", "sly": "хитрый", "cold": "холодный", "warm": "тёплый",
          "cheer": "весёлый", "gloomy": "мрачный", "noble": "благородный", "tired": "усталый", "creaky": "скрипучий",
          "whisper": "шёпот", "loud": "громкий", "wise": "мудрый", "gruff": "ворчливый", "shy": "робкий", "bold": "дерзкий",
          "fast": "быстрый", "slow": "неспешный", "creature": "существо"}
CONFLICT = [("low", "high"), ("calm", "lively"), ("soft", "rough"), ("warm", "cold"), ("cheer", "gloomy"), ("fast", "slow"),
            ("shy", "bold"), ("loud", "whisper"), ("firm", "shy"), ("soft", "loud")]
DICTION = " Very clear Russian diction."
SAMPLE_TEXT = ("Я расскажу вам эту историю так, как помню её сам: без прикрас, но и без умолчаний. "
               "Слушайте внимательно — второй раз я повторять не стану.")
DEFAULT_INSTRUCTS = (
    "A man in his forties with a natural, grounded conversational voice. Clear Russian diction, medium pace.",
    "A woman in her thirties with a natural, warm conversational voice. Clear Russian diction, medium pace.",
)

# (id, название, пол, возраст, приметы, описание для модели)
CATALOG = [
    ("m_narr_warm", "Рассказчик: тёплый, спокойный", "m", "adult", "narrator warm calm",
     "Male narrator around 35, calm and warm storyteller voice with a light smile, measured audiobook pace, intimate microphone tone."),
    ("m_narr_deep", "Рассказчик: низкий, неторопливый", "m", "mature", "narrator low calm slow",
     "Male narrator around 50, deep velvety baritone, unhurried and confident, classic audiobook delivery."),
    ("m_narr_dry", "Рассказчик: суховатый, с иронией", "m", "adult", "narrator cold sly",
     "Male narrator around 40, dry and slightly ironic voice, precise articulation, steady pace."),
    ("m_narr_young", "Рассказчик: молодой, живой", "m", "young", "narrator lively warm",
     "Young male narrator, 25, bright and lively storyteller voice, friendly and natural."),
    ("m_young_lively", "Юноша: живой, быстрый", "m", "young", "lively fast cheer",
     "Young man, 20, quick and lively, cheerful energetic speech, light tenor."),
    ("m_young_shy", "Юноша: тихий, застенчивый", "m", "young", "shy soft calm",
     "Young man, 19, quiet and shy, soft hesitant voice, gentle tone."),
    ("m_young_bold", "Юноша: дерзкий, уверенный", "m", "young", "bold firm loud",
     "Young man, 23, bold and cocky, confident ringing voice, speaks firmly."),
    ("m_young_dreamy", "Юноша: мечтательный, мягкий", "m", "young", "soft warm slow",
     "Young man, 22, dreamy and gentle, soft warm voice, unhurried."),
    ("m_young_sly", "Парень: насмешливый, хитрый", "m", "young", "sly lively",
     "Young man, 24, mocking and sly, playful ironic intonation, quick wit in the voice."),
    ("m_adult_calm", "Мужчина: спокойный, ровный", "m", "adult", "calm warm",
     "Man, 38, calm and even voice, friendly and steady, natural medium pitch."),
    ("m_adult_firm", "Мужчина: твёрдый, командный", "m", "adult", "firm loud low",
     "Man, 40, firm commanding voice of an officer, strong low baritone, short decisive phrases."),
    ("m_adult_rough", "Мужчина: грубый, хрипловатый", "m", "adult", "rough gruff low",
     "Man, 42, rough and raspy low voice, coarse and blunt, a little hoarse."),
    ("m_adult_sly", "Мужчина: вкрадчивый, хитрый", "m", "adult", "sly soft",
     "Man, 37, insinuating sly voice, soft and smooth, with a hidden smirk."),
    ("m_adult_noble", "Мужчина: благородный, учтивый", "m", "adult", "noble calm",
     "Man, 36, noble and courteous, refined dignified voice, calm and well-mannered."),
    ("m_adult_cheer", "Мужчина: весёлый, добродушный", "m", "adult", "cheer warm loud",
     "Man, 40, jolly good-natured voice, hearty and loud, laughing warmth."),
    ("m_adult_gloomy", "Мужчина: мрачный, глухой", "m", "adult", "gloomy low slow",
     "Man, 41, gloomy and sullen, dull low voice, slow heavy speech."),
    ("m_adult_soft", "Мужчина: мягкий, интеллигентный", "m", "adult", "soft calm high",
     "Man, 35, soft-spoken educated voice, gentle and polite, light higher pitch."),
    ("m_adult_dry", "Мужчина: сухой, канцелярский", "m", "adult", "cold high fast",
     "Man, 44, dry clerk voice, thin and slightly nasal, brisk businesslike speech."),
    ("m_adult_fast", "Мужчина: бойкий торговец", "m", "adult", "fast lively cheer",
     "Man, 39, brisk talkative merchant, fast lively persuasive speech, cheerful."),
    ("m_adult_tense", "Мужчина: нервный, напряжённый", "m", "adult", "fast high shy",
     "Man, 34, nervous tense voice, hurried and anxious, slightly high."),
    ("m_mature_boss", "Зрелый: властный, тяжёлый", "m", "mature", "firm low cold",
     "Man, 55, heavy authoritative voice of a ruler, low and cold, speaks slowly and weightily."),
    ("m_mature_kind", "Зрелый: добродушный, тёплый", "m", "mature", "warm calm cheer",
     "Man, 55, kind good-natured voice, warm and calm, fatherly."),
    ("m_mature_tired", "Зрелый: усталый, негромкий", "m", "mature", "tired slow soft",
     "Man, 57, tired weary voice, quiet and slow, with a sigh in it."),
    ("m_mature_cold", "Зрелый: холодный, опасный", "m", "mature", "cold sly calm",
     "Man, 52, cold dangerous voice, calm and icy, quietly threatening."),
    ("m_mature_gruff", "Зрелый: ворчливый, хриплый", "m", "mature", "gruff rough",
     "Man, 58, grumpy gruff voice, hoarse and grumbling."),
    ("m_mature_hearty", "Зрелый: громкий, раскатистый", "m", "mature", "loud cheer low",
     "Man, 54, loud booming voice, rolling low laugh, hearty and big."),
    ("m_mature_wise", "Зрелый: рассудительный наставник", "m", "mature", "wise calm warm",
     "Man, 56, thoughtful mentor, calm wise voice, warm and attentive."),
    ("m_old_wise", "Старик: мудрый, неспешный", "m", "old", "wise calm slow",
     "Old man, 72, wise and unhurried, calm elderly voice with gentle warmth."),
    ("m_old_creaky", "Старик: скрипучий, дребезжащий", "m", "old", "creaky high",
     "Very old man, 80, creaky trembling voice, thin and cracked."),
    ("m_old_hearty", "Старик: бодрый, с хрипотцой", "m", "old", "cheer rough lively",
     "Old man, 68, spry and cheerful, slightly hoarse lively voice."),
    ("m_old_stern", "Старик: суровый, властный", "m", "old", "firm cold low",
     "Old man, 70, stern commanding elder, low dry voice, strict."),
    ("m_old_frail", "Старик: слабый, тихий", "m", "old", "tired soft whisper",
     "Very old man, 85, frail weak voice, quiet, almost whispering."),
    ("m_giant", "Великан: очень низкий бас", "m", "adult", "low loud slow creature",
     "Huge man, extremely deep rumbling bass, slow and heavy, booming."),
    ("m_villain", "Злодей: зловещий полушёпот", "m", "adult", "whisper cold sly creature",
     "Sinister man, 45, menacing half-whisper, cold and sly, chilling."),
    ("m_herald", "Глашатай: звонкий, торжественный", "m", "adult", "loud noble high",
     "Herald, 35, ringing solemn voice, loud and ceremonial, bright tenor."),
    ("m_monk", "Монах: тихий, смиренный", "m", "mature", "soft calm slow wise",
     "Monk, 50, quiet humble voice, soft and slow, peaceful."),
    ("f_narr_warm", "Рассказчица: тёплая, спокойная", "f", "adult", "narrator warm calm",
     "Female narrator around 35, calm warm storyteller voice, measured audiobook pace, intimate tone."),
    ("f_narr_clear", "Рассказчица: ясная, ровная", "f", "adult", "narrator calm cold",
     "Female narrator around 40, clear even voice, composed and precise, steady audiobook pace."),
    ("f_young_bright", "Девушка: звонкая, живая", "f", "young", "lively high cheer",
     "Young woman, 20, bright ringing voice, lively and cheerful."),
    ("f_young_gentle", "Девушка: нежная, тихая", "f", "young", "soft shy warm",
     "Young woman, 19, gentle tender voice, quiet and a little shy."),
    ("f_young_bold", "Девушка: дерзкая, решительная", "f", "young", "bold firm fast",
     "Young woman, 23, bold and determined, firm quick speech, confident."),
    ("f_young_sly", "Девушка: лукавая, насмешливая", "f", "young", "sly lively",
     "Young woman, 22, sly teasing voice, playful and mocking."),
    ("f_young_dreamy", "Девушка: задумчивая, мягкая", "f", "young", "soft slow calm",
     "Young woman, 21, thoughtful soft voice, slow and calm."),
    ("f_adult_calm", "Женщина: спокойная, ровная", "f", "adult", "calm warm",
     "Woman, 35, calm even voice, friendly and composed, natural medium pitch."),
    ("f_adult_firm", "Женщина: строгая, собранная", "f", "adult", "firm cold",
     "Woman, 40, strict collected voice, firm and businesslike."),
    ("f_adult_warm", "Женщина: заботливая, тёплая", "f", "adult", "warm soft",
     "Woman, 36, caring warm voice, soft and motherly."),
    ("f_adult_sharp", "Женщина: резкая, быстрая", "f", "adult", "fast loud bold",
     "Woman, 38, sharp quick-tongued voice, loud and brisk."),
    ("f_adult_noble", "Женщина: благородная, сдержанная", "f", "adult", "noble calm cold",
     "Woman, 37, noble restrained voice of a lady, refined and cool."),
    ("f_adult_cheer", "Женщина: весёлая, смешливая", "f", "adult", "cheer lively loud",
     "Woman, 34, merry laughing voice, lively and loud."),
    ("f_adult_low", "Женщина: низкий грудной голос", "f", "adult", "low calm sly",
     "Woman, 40, low chesty contralto, calm and a little sly, velvety."),
    ("f_adult_tired", "Женщина: усталая, негромкая", "f", "adult", "tired soft slow",
     "Woman, 42, tired quiet voice, soft and slow."),
    ("f_mature_boss", "Хозяйка: властная, громкая", "f", "mature", "firm loud",
     "Woman, 52, bossy mistress of the house, loud firm voice."),
    ("f_mature_kind", "Зрелая: добрая, уютная", "f", "mature", "warm calm cheer",
     "Woman, 55, kind cozy voice, warm and calm."),
    ("f_mature_gossip", "Зрелая: бойкая, говорливая", "f", "mature", "fast lively high",
     "Woman, 50, talkative brisk voice, fast and lively, a bit high."),
    ("f_mature_cold", "Зрелая: холодная, надменная", "f", "mature", "cold noble sly",
     "Woman, 54, cold haughty voice, arrogant and quiet."),
    ("f_old_kind", "Старушка: добрая, мягкая", "f", "old", "warm soft slow",
     "Old woman, 72, kind grandmotherly voice, soft and slow."),
    ("f_old_creaky", "Старуха: скрипучая, колдовская", "f", "old", "creaky sly creature",
     "Very old woman, 80, creaky witch-like voice, cracked and sly."),
    ("f_old_stern", "Старуха: суровая, сухая", "f", "old", "firm cold",
     "Old woman, 70, stern dry voice, strict and sharp."),
    ("c_boy_lively", "Мальчик: звонкий, бойкий", "m", "child", "lively high cheer",
     "Boy, 9 years old, child's ringing voice, lively and cheerful, not an adult."),
    ("c_boy_shy", "Мальчик: тихий, робкий", "m", "child", "shy soft high",
     "Boy, 8 years old, child's quiet timid voice, not an adult."),
    ("c_girl_bright", "Девочка: звонкая, весёлая", "f", "child", "lively high cheer",
     "Little girl, 8 years old, child's bright cheerful voice."),
    ("c_girl_quiet", "Девочка: тихая, серьёзная", "f", "child", "shy soft calm",
     "Little girl, 9 years old, child's quiet serious voice."),
    ("t_boy", "Подросток: ломкий голос, упрямый", "m", "teen", "bold high fast",
     "Teenage boy, 14, breaking adolescent voice, stubborn and quick."),
    ("t_girl", "Девочка-подросток: живая, прямая", "f", "teen", "bold lively high",
     "Teenage girl, 14, lively direct voice, clear and high."),
    ("x_spirit", "Дух: бесплотный шёпот", "m", "adult", "whisper creature slow cold",
     "Otherworldly spirit, breathy hollow whisper, slow and cold, inhuman calm."),
    ("x_beast", "Чудовище: рычащий низкий голос", "m", "adult", "rough low loud creature",
     "Monster, growling very low rough voice, loud and threatening."),
]

# имя роли -> пол/возраст
NAME_WHO = [
    (("мальчик", "мальчишка", "сынок", "паренёк", "малец", "пацан", "мальчуган"), "m", "child"),
    (("девочка", "девчонка", "дочка", "малышка", "девчушка"), "f", "child"),
    (("подросток", "юнец", "отрок"), "m", "teen"),
    (("юноша", "парень", "ученик", "подмастерье", "студент", "оруженосец", "паж", "юнга", "молодой"), "m", "young"),
    (("девушка", "девица", "ученица", "служанка", "горничная", "молодая"), "f", "young"),
    (("старик", "старец", "дед", "старейшина", "старый", "старичок"), "m", "old"),
    (("старуха", "старушка", "бабушка", "бабка", "старая", "карга"), "f", "old"),
    (("женщина", "мать", "мама", "тётя", "тётка", "тетя", "жена", "вдова", "сестра", "хозяйка", "торговка", "королева",
      "княгиня", "графиня", "баронесса", "леди", "госпожа", "дама", "ведьма", "колдунья", "знахарка", "монахиня",
      "кухарка", "прачка", "трактирщица", "фея", "принцесса", "няня", "незнакомка", "крестьянка", "гадалка"), "f", None),
]
MALE_A = {"никита", "илья", "лука", "фома", "кузьма", "савва", "папа", "дядя", "дедушка", "юноша", "старейшина", "воевода",
          "судья", "слуга", "мужчина", "старшина", "вельможа", "громила", "батюшка", "владыка", "паша", "коллега",
          "детина", "верзила", "бродяга", "пьяница", "староста", "глава", "миша", "саша", "женя", "ваня", "петя", "коля",
          "гоша", "лёша", "дима", "сева", "костя", "федя", "вася", "гриша", "митя", "толя", "боря", "стёпа", "данила"}
NAME_TAGS = [
    (("капитан", "командир", "сержант", "генерал", "офицер", "воевода", "стражник", "страж", "солдат", "десятник",
      "сотник", "комендант", "полковник", "лейтенант", "начальник", "надзиратель"), "firm loud"),
    (("король", "королева", "принц", "принцесса", "лорд", "леди", "граф", "барон", "герцог", "князь", "княгиня",
      "канцлер", "посол", "рыцарь", "император", "советник"), "noble"),
    (("маг", "волшебник", "чародей", "мудрец", "архивариус", "библиотекарь", "учитель", "наставник", "магистр",
      "мастер", "профессор", "лекарь", "целитель", "жрец", "аббат", "учёный", "алхимик"), "wise calm"),
    (("трактирщик", "трактирщица", "торговец", "торговка", "купец", "лавочник", "повар", "кухарка", "хозяин",
      "хозяйка", "корчмарь", "мельник", "пекарь"), "cheer warm"),
    (("разбойник", "бандит", "наёмник", "вор", "пират", "палач", "головорез", "громила", "тюремщик", "кузнец",
      "грузчик", "мясник", "вышибала"), "rough low"),
    (("ведьма", "колдун", "колдунья", "чернокнижник", "некромант", "шаман", "гадалка"), "sly creaky"),
    (("дух", "призрак", "тень", "голос", "демон", "существо", "тварь", "дракон", "чудовище", "зверь", "нечто"), "creature"),
    (("монах", "монахиня", "послушник", "священник", "отшельник"), "soft calm"),
    (("шут", "скоморох", "бард", "менестрель", "зазывала", "глашатай"), "lively loud"),
    (("писарь", "чиновник", "секретарь", "судья", "сборщик", "казначей", "управляющий", "счетовод"), "cold fast"),
    (("слуга", "служанка", "лакей", "дворецкий", "конюх", "нищий", "крестьянин", "пастух", "рыбак"), "soft"),
]
# слова автора рядом с репликой -> примета голоса
VERB_TAGS = [
    (("хрип", "сипл", "осипш"), "rough"),
    (("шепт", "шепн", "шёпот", "шепот", "вполголоса", "еле слышно", "одними губами"), "whisper"),
    (("рявк", "гаркн", "заорал", "взревел", "прорычал", "рыкн", "грянул", "громыхн"), "loud"),
    (("пискн", "взвизг", "пропищ", "тоненьк", "звонко", "звонким"), "high"),
    (("пробас", "басом", "прогуд", "низким голосом", "гулко"), "low"),
    (("проворч", "буркн", "пробурч", "ворчлив", "сварлив"), "gruff"),
    (("хихик", "рассмеял", "засмеял", "усмехн", "весело", "хохотн", "расхохот", "ухмыльн"), "cheer"),
    (("вздохн", "устало", "утомлённо", "обречённо"), "tired"),
    (("холодно", "ледян", "процедил", "сухо", "отрезал", "бесстрастн"), "cold"),
    (("ласково", "мягко", "нежно", "тепло", "участливо", "заботливо"), "warm"),
    (("проскрип", "скрипуч", "прошамк", "дребезж"), "creaky"),
    (("протянул", "лениво", "неторопливо", "медленно", "нараспев"), "slow"),
    (("затаратор", "выпалил", "скороговорк", "торопливо"), "fast"),
    (("робко", "смущённо", "застенчив", "пролепет", "запинаясь", "неуверенно"), "shy"),
    (("твёрдо", "жёстко", "приказал", "скомандовал", "велел", "властно", "отчеканил"), "firm"),
    (("вкрадчив", "лукаво", "хитро", "елейн", "ехидн", "язвительн", "насмешлив"), "sly"),
    (("мрачно", "угрюмо", "хмуро", "глухо"), "gloomy"),
    (("спокойно", "ровно", "невозмутимо", "рассудительно"), "calm"),
]
# английское описание голоса -> приметы
EN_TAGS = [
    (("deep", "low ", "bass", "baritone", "contralto"), "low"), (("high", "thin", "squeaky", "shrill", "tenor"), "high"),
    (("calm", "measured", "steady", "even "), "calm"), (("lively", "energetic", "animated", "brisk"), "lively"),
    (("firm", "commanding", "authoritative", "stern", "strict"), "firm"), (("soft", "gentle", "quiet", "tender", "mild"), "soft"),
    (("rough", "raspy", "hoarse", "gravel", "husky", "coarse"), "rough"),
    (("sly", "cunning", "ironic", "mocking", "insinuating", "sarcastic", "smirk"), "sly"),
    (("cold", "icy", "dry", "detached", "haughty", "arrogant"), "cold"), (("warm", "kind", "friendly", "caring", "cozy"), "warm"),
    (("cheerful", "jolly", "merry", "laugh", "good-natured", "jovial"), "cheer"),
    (("gloomy", "grim", "sullen", "somber"), "gloomy"), (("noble", "aristocrat", "refined", "courtly", "dignified", "regal"), "noble"),
    (("tired", "weary", "exhausted", "frail", "weak"), "tired"), (("creaky", "cracked", "trembl", "quaver", "wheez"), "creaky"),
    (("whisper", "hushed", "breathy"), "whisper"), (("loud", "booming", "thunder", "shout"), "loud"),
    (("wise", "sage", "thoughtful", "scholar", "mentor"), "wise"), (("grumpy", "gruff", "grumbl", "cranky"), "gruff"),
    (("shy", "timid", "hesitant", "nervous"), "shy"), (("bold", "daring", "defiant", "cocky", "confident"), "bold"),
    (("fast", "rapid", "hurried", "quick"), "fast"), (("slow", "unhurried", "drawl", "leisurely"), "slow"),
    (("narrator", "storyteller", "audiobook"), "narrator"),
    (("monster", "creature", "demon", "ghost", "spirit", "otherworldly", "inhuman"), "creature"),
]
DECADES = {"twenties": 25, "thirties": 35, "forties": 45, "fifties": 55, "sixties": 65, "seventies": 75, "eighties": 85}


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def age_group(years) -> str:
    y = float(years)
    return "child" if y < 12 else "teen" if y < 18 else "young" if y < 30 else "adult" if y < 48 else "mature" if y < 62 else "old"


# ------------------------------------------------------------------------------------------------------------ банк

def bank_load() -> dict:
    try:
        b = json.loads((BANK / "bank.json").read_text(encoding="utf-8"))
        if isinstance(b.get("voices"), dict):
            return b
    except Exception:
        pass
    return {"ver": 1, "voices": {}}


def bank_save(b: dict):
    BANK.mkdir(parents=True, exist_ok=True)
    tmp = BANK / "bank.json.tmp"
    tmp.write_text(json.dumps(b, ensure_ascii=False, indent=1), encoding="utf-8")
    replace_retry(tmp, BANK / "bank.json")


def all_voices(b: dict) -> dict:
    """Заготовки (и ещё не созданные) + голоса из книг. -> {id: voice}; have = образец лежит в банке."""
    out = {}
    for vid, name, g, age, tags, instr in CATALOG:
        rec = b["voices"].get(vid, {})
        out[vid] = dict(id=vid, name=name, g=g, age=age, tags=tags.split(), instruct=instr + DICTION, kind="design",
                        f0=rec.get("f0"), have=(BANK / f"{vid}.wav").exists() and (BANK / f"{vid}.txt").exists())
    for vid, rec in b["voices"].items():
        if vid in out or rec.get("kind") not in ("book", "custom"):
            continue
        out[vid] = dict(rec, id=vid, have=(BANK / f"{vid}.wav").exists() and (BANK / f"{vid}.txt").exists())
    return out


def f0_of(path) -> float | None:
    """Медианная высота тона образца, Гц (автокорреляция по кадрам 40 мс; None — тон не определить)."""
    import numpy as np
    import soundfile as sf
    try:
        x, sr = sf.read(str(path), dtype="float32")
    except Exception:
        return None
    if x.ndim > 1:
        x = x.mean(axis=1)
    k = max(1, int(sr // 12000))
    if k > 1:
        m = len(x) // k * k
        x = x[:m].reshape(-1, k).mean(axis=1)
        sr = sr / k
    w, h = int(sr * 0.04), int(sr * 0.02)
    lo, hi = max(2, int(sr / 420)), int(sr / 60)
    if len(x) < w * 4 or hi >= w:
        return None
    rms_all = float(np.sqrt(np.mean(x * x))) or 1e-9
    out = []
    for i in range(0, len(x) - w, h):
        fr = x[i:i + w]
        if float(np.sqrt(np.mean(fr * fr))) < 0.35 * rms_all:
            continue
        fr = fr - fr.mean()
        ac = np.correlate(fr, fr, "full")[w - 1:]
        if ac[0] <= 0:
            continue
        ac = ac / ac[0]
        j = int(np.argmax(ac[lo:hi]))
        pk, lag = float(ac[lo + j]), lo + j
        if pk < 0.5:
            continue
        for d in (2, 3):                                 # пик на кратном периоде — берём основной
            l2 = int(round(lag / d))
            if l2 - 1 >= lo and float(ac[l2 - 1:l2 + 2].max()) >= 0.85 * pk:
                lag = l2 - 1 + int(np.argmax(ac[l2 - 1:l2 + 2]))
                break
        out.append(sr / lag)
    return float(np.median(out)) if len(out) >= 8 else None


def pitch_ok(g: str, age: str, f0) -> str:
    """ok / ? / no — годится ли высота тона для пола и возраста (границы — как у проверки голосов книги, с запасом)."""
    if f0 is None:
        return "?"
    if age == "child":
        return "ok" if f0 >= 200 else "?" if f0 >= 180 else "no"
    if g == "m":
        return "ok" if f0 <= 160 else "?" if f0 <= 195 else "no"
    return "ok" if f0 >= 170 else "?" if f0 >= 150 else "no"


# ---------------------------------------------------------------------------------------------------------- книга

def load_n(root):
    root = Path(root).resolve()
    if not (root / "nox_audiobook.py").exists():
        sys.exit(f"в папке {root} нет nox_audiobook.py — книга не подготовлена (запустите go)")
    os.chdir(root)
    sys.path.insert(0, str(root))
    import nox_audiobook as n
    return n


def is_described(cfg: dict) -> bool:
    """Автор сам описал голос роли (в таблице настроек или заготовках книги) — тогда голос создаётся по описанию."""
    ins = str(cfg.get("instruct") or "").strip()
    if not ins or ins in DEFAULT_INSTRUCTS:
        return False
    ui = cfg.get("ui") or {}
    desc = str(ui.get("desc") or "").strip()
    core = re.sub(r"^(Young male voice|Female voice|Male voice|Boy's voice \(child, not an adult\)|Little girl's voice \(child\))\.\s*", "", ins)
    core = re.sub(r"^(About|Around)?\s*\d+\s*years old\.?\s*", "", core, flags=re.I).strip()
    for d in DEFAULT_INSTRUCTS:
        if core == d or desc == d:
            return False
    return bool(core) and (not ui or bool(desc))


def tags_from_text(text: str, table) -> list:
    t = " " + text.lower() + " "
    return [tag for keys, tags in table if any(k in t for k in keys) for tag in tags.split()]


def verb_tags(rows, resolve) -> dict:
    """Слова автора рядом с репликами роли («прохрипел», «шепнула») -> {роль: {примета: сколько раз}}."""
    out = {}
    for i, r in enumerate(rows):
        if r.get("kind") != "speech":
            continue
        role = resolve(r["speaker"])
        for j in (i - 1, i + 1):
            if 0 <= j < len(rows) and rows[j].get("kind") != "speech" and rows[j].get("para") == r.get("para"):
                t = rows[j]["text"].lower()
                for keys, tag in VERB_TAGS:
                    if any(k in t for k in keys):
                        d = out.setdefault(role, {})
                        d[tag] = d.get(tag, 0) + 1
    return out


def role_profile(role: str, cfg: dict, verbs: dict, narrator: str) -> dict:
    """Что известно о роли: пол, возраст, приметы голоса и откуда это взято."""
    name = role.lower().replace("ё", "е")
    words = re.findall(r"[а-яa-z]+", name)
    g = age = None
    why = []
    gv = str(cfg.get("gender") or "")
    if gv in ("муж", "жен", "юноша", "мальчик", "девочка"):
        g = "f" if gv in ("жен", "девочка") else "m"
        age = "child" if gv in ("мальчик", "девочка") else "young" if gv == "юноша" else None
        why.append("пол из таблицы")
    ins = str(cfg.get("instruct") or "")
    low = ins.lower()
    if g is None and ins and ins not in DEFAULT_INSTRUCTS:
        if re.search(r"\b(female|woman|girl|lady|she|her)\b", low):
            g = "f"
        elif re.search(r"\b(male|man|boy|he|his)\b", low):
            g = "m"
    if g is None and ins == DEFAULT_INSTRUCTS[1]:
        g = "f"
    for keys, kg, ka in NAME_WHO:
        if any(k.replace("ё", "е") in words for k in keys):
            if g is None:
                g = kg
                why.append("пол по имени роли")
            if age is None and ka:
                age = ka
                why.append("возраст по имени роли")
            break
    if g is None:
        head = words[0] if words else ""               # «Дух колодца», «Капитан Рейн» — главное слово первое
        g = "f" if head.endswith(("а", "я")) and head not in MALE_A else "m"
        why.append("пол по окончанию имени")
    yrs = cfg.get("age")
    if age is None and isinstance(yrs, (int, float)) and 3 <= float(yrs) <= 99 and (cfg.get("ui") or gv):
        age = age_group(yrs)
        why.append("возраст из таблицы")
    if age is None and ins and ins not in DEFAULT_INSTRUCTS:
        m = re.search(r"\b(\d{1,2})\b", low)
        if m and 4 <= int(m.group(1)) <= 95:
            age = age_group(int(m.group(1)))
        else:
            for wd, y in DECADES.items():
                if wd in low:
                    age = age_group(y)
                    break
            else:
                if re.search(r"\b(elderly|old man|old woman|very old)\b", low):
                    age = "old"
                elif re.search(r"\b(child|boy|little girl)\b", low):
                    age = "child"
                elif "teen" in low:
                    age = "teen"
                elif "young" in low:
                    age = "young"
                elif "middle-aged" in low:
                    age = "mature"
    if age is None:
        age = "adult"
    tags = []
    for keys, tg in NAME_TAGS:
        if any(k.replace("ё", "е") in words for k in keys):
            tags += tg.split()
    if ins and ins not in DEFAULT_INSTRUCTS:
        tags += tags_from_text(ins, EN_TAGS)
    vt = verbs.get(role) or {}
    top = sorted(vt.items(), key=lambda x: -x[1])[:3]
    vsel = [t for t, c in top if c >= 2]
    if vsel:
        tags += vsel
        why.append("слова автора: " + ", ".join(f"{TAG_RU.get(t, t)} ×{vt[t]}" for t in vsel))
    is_narr = role == narrator
    if is_narr:
        tags.append("narrator")
    seen, uniq = set(), []
    for t in tags:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return dict(role=role, g=g, age=age, tags=uniq, narrator=is_narr, why=why, count=int(cfg.get("count") or 0))


def score(prof: dict, v: dict, used: dict, leaf: str):
    """Насколько голос банка подходит роли; None — не подходит совсем."""
    if (prof["age"] == "child") != (v["age"] == "child"):
        return None
    s = 0.0
    if v["g"] != prof["g"]:
        if prof["age"] != "child":
            return None
        s -= 6
    s -= 3 * abs(AGES.index(v["age"]) - AGES.index(prof["age"]))
    vt, pt = set(v.get("tags") or []), set(prof["tags"])
    if prof["narrator"]:
        s += 8 if "narrator" in vt else -3
    else:
        if "narrator" in vt:
            s -= 5
        if "creature" in vt and "creature" not in pt:
            s -= 6
    s += 3 * len((vt & pt) - {"narrator"})
    for a, b in CONFLICT:
        if (a in pt and b in vt) or (b in pt and a in vt):
            s -= 2
    if v.get("kind") == "custom":
        s -= 1                                           # свой голос автор создавал для своей цели: сам роли не даётся первым
    if v.get("kind") == "book":
        src = v.get("src") or {}
        same = str(src.get("role") or "").lower() == prof["role"].lower() and src.get("book") != leaf
        if same:
            s += 80                                      # тот же персонаж в другой книге серии — тот же голос
        elif prof["narrator"] and src.get("narrator"):
            s += 50                                      # рассказчик, уже утверждённый автором в другой книге
        elif int(src.get("count") or 0) >= 150 or src.get("narrator"):
            return None                                  # голос главного героя другой книги чужой роли сам не даётся
        elif src.get("book") == leaf:
            return None                                  # голос роли этой же книги — только вручную («голос как у»)
        else:
            s -= 1
    if pitch_ok(v["g"], v["age"], v.get("f0")) == "no":
        s -= 8
    k = used.get(v["id"], 0)
    if k:
        s -= 100 if prof["count"] >= 5 or prof["narrator"] else 4 * k
    return s + int(hashlib.sha1(f"{prof['role']}|{v['id']}".encode("utf-8")).hexdigest()[:6], 16) / 0xffffff


def book_view(n, all_roles: bool):
    """-> (rows, roles, narrator, нужные роли по порядку важности, {роль: профиль})."""
    rows, roles = n.load_rows(), n.load_roles()
    narrator = getattr(n, "NARRATOR", "narrator")
    aliased = set(roles.get("aliases", {}))
    if all_roles or not hasattr(n, "work_window") or not n.by_parts():
        span = rows
    else:
        try:
            st = getattr(n, "SETTINGS", None) or {}
            n.TEMPO_G = float(st.get("tempo") or 1.12)
            w = n.work_window(rows, roles)
        except Exception:
            w = (0, len(rows) - 1, 1, False)
        if w[0] >= len(rows):
            span = []
        else:
            # с запасом: окно части уточняется по ходу озвучки, голос нужен и ролям чуть дальше его нынешнего края
            target = 1.4 * float((n.run_mode() or {}).get("part_sec") or 19 * 60 * 1.12)
            acc, j = 0.0, w[0]
            while j < len(rows) and (j <= w[1] or acc < target):
                acc += len(rows[j]["text"]) / 15.0 + int(rows[j]["pause_before_ms"]) / 1000
                j += 1
            span = rows[w[0]:j]
    need = []
    for r in span:
        role = n.resolve_role(r["speaker"], roles)
        if role not in need:
            need.append(role)
    verbs = verb_tags(rows, lambda s: n.resolve_role(s, roles))
    profs = {}
    for role, cfg in roles["roles"].items():
        if cfg.get("same_as") or role in aliased:
            continue
        profs[role] = role_profile(role, cfg, verbs, narrator)
    need = [r for r in need if r in profs]
    need.sort(key=lambda r: (not profs[r]["narrator"], -profs[r]["count"]))
    return rows, roles, narrator, need, profs


def used_voices(roles: dict, n) -> dict:
    """Какие голоса банка уже звучат в этой книге -> {id: у скольких ролей}."""
    stems = n.voice_stems(roles)
    used = {}
    for role, cfg in roles["roles"].items():
        vid = (cfg.get("voice") or {}).get("bank")
        if vid and n.voice_files(role, stems)[0].exists():
            used[vid] = used.get(vid, 0) + 1
    return used


def suggest(n, roles, need, profs, voices, only_have=False, force=False) -> dict:
    """-> {роль: [(оценка, голос)…] по убыванию}; заметным ролям голоса не повторяются.
    force — подбирать и ролям, у которых образец уже есть (автор хочет сменить голос)."""
    stems = n.voice_stems(roles)
    leaf = Path(n.HERE).name.lower()
    used = used_voices(roles, n)
    out = {}
    for role in need:
        if n.voice_files(role, stems)[0].exists() and not force:
            continue
        prof = profs[role]
        if force:                                        # свой нынешний голос роли в «занятых» не считается
            cur = (roles["roles"][role].get("voice") or {}).get("bank")
            if cur and used.get(cur):
                used[cur] -= 1
        rej = set((roles["roles"][role].get("voice") or {}).get("rejected") or [])
        cand = []
        for v in voices.values():
            if v["id"] in rej or (only_have and not v["have"]):
                continue
            sc = score(prof, v, used, leaf)
            if sc is not None:
                cand.append((sc, v))
        cand.sort(key=lambda x: -x[0])
        out[role] = cand[:6]
        if cand:
            used[cand[0][1]["id"]] = used.get(cand[0][1]["id"], 0) + 1
    return out


def sample_sig(wav_p: Path, txt_p: Path) -> str:
    """Отпечаток образца голоса (wav + текст образца), 12 знаков; файла нет -> ''. Тот же, что пишет harvest в банк."""
    try:
        return hashlib.sha1(wav_p.read_bytes() + (txt_p.read_bytes() if txt_p.exists() else b"")).hexdigest()[:12]
    except OSError:
        return ""


def put_sample(n, wav_p: Path, txt_p: Path, src_w: Path, src_t: Path):
    """Новый образец роли на место прежнего: сначала обе копии рядом под временными именами, потом две подмены подряд —
    роль ни на миг не остаётся без образца, а недописанный файл никто не прочтёт."""
    n.VOICES.mkdir(parents=True, exist_ok=True)
    tw, tt = wav_p.with_name(wav_p.name + ".new"), txt_p.with_name(txt_p.name + ".new")
    shutil.copyfile(src_w, tw)
    shutil.copyfile(src_t, tt)
    replace_retry(tt, txt_p)
    replace_retry(tw, wav_p)


def keep_old(n, wav_p: Path, txt_p: Path) -> str:
    """Копия нынешнего образца роли в work\\voices_old (имя с датой) -> это имя без расширения ('' — образца не было)."""
    if not wav_p.exists():
        return ""
    old = n.WORK / "voices_old"
    old.mkdir(exist_ok=True)
    name = f"{wav_p.stem}__{time.strftime('%Y%m%d-%H%M%S')}"
    k = 1
    while (old / (name + ".wav")).exists():
        k += 1
        name = f"{wav_p.stem}__{time.strftime('%Y%m%d-%H%M%S')}-{k}"
    shutil.copyfile(wav_p, old / (name + ".wav"))
    if txt_p.exists():
        shutil.copyfile(txt_p, old / (name + ".txt"))
    return name


def set_voice(n, roles: dict, role: str, v: dict, by: str) -> Path:
    """Образец голоса банка -> образец роли. Прежний образец не удаляется: копия в work\\voices_old с датой; прежняя
    привязка голоса запоминается, чтобы смену можно было отменить (revert)."""
    stems = n.voice_stems(roles)
    wav_p, txt_p = n.voice_files(role, stems)
    kept = keep_old(n, wav_p, txt_p)
    put_sample(n, wav_p, txt_p, BANK / f"{v['id']}.wav", BANK / f"{v['id']}.txt")
    cfg = roles["roles"][role]
    prev = cfg.get("voice") or {}
    cfg["voice_prev"] = dict(file=kept, voice=cfg.get("voice"))
    cfg["voice"] = dict(bank=v["id"], name=v["name"], by=by, at=now(), rejected=prev.get("rejected") or [])
    return wav_p


def save_roles(n, roles: dict):
    tmp = n.ROLES_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(roles, ensure_ascii=False, indent=2), encoding="utf-8")
    replace_retry(tmp, n.ROLES_PATH)


def describe(v: dict) -> str:
    return (("муж" if v["g"] == "m" else "жен") + ", " + AGE_RU[v["age"]]
            + (", " + ", ".join(TAG_RU.get(t, t) for t in v.get("tags") or [] if t != "narrator") if v.get("tags") else "")
            + (f", тон {v['f0']:.0f} Гц" if v.get("f0") else ""))


# -------------------------------------------------------------------------------------------------------- команды

def design_voices(n, model, ids, b: dict, force=False, lang="Russian") -> int:
    """Создать образцы заготовок по описаниям. Тон не подходит полу/возрасту — до трёх попыток, остаётся лучшая."""
    import soundfile as sf
    import torch
    cat = {c[0]: c for c in CATALOG}
    BANK.mkdir(parents=True, exist_ok=True)
    made = 0
    for k, vid in enumerate(ids, 1):
        if vid not in cat:
            print(f"  [банк] {vid}: такой заготовки нет", flush=True)
            continue
        _, name, g, age, tags, instr = cat[vid]
        wav_p, txt_p = BANK / f"{vid}.wav", BANK / f"{vid}.txt"
        if wav_p.exists() and txt_p.exists() and not force:
            continue
        best = None
        for attempt in range(3):
            torch.manual_seed(1000 + 77 * attempt + int(hashlib.sha1(vid.encode()).hexdigest()[:4], 16))
            extra = "" if attempt == 0 else (" Unmistakably a child's voice." if age == "child" else
                                             " Unmistakably a male voice, low pitch." if g == "m" else
                                             " Unmistakably a female voice.")
            wavs, sr = model.generate_voice_design(text=SAMPLE_TEXT, language=lang, instruct=instr + DICTION + extra)
            tmp = BANK / f"{vid}.try.wav"
            sf.write(str(tmp), wavs[0], sr)
            f0 = f0_of(tmp)
            ok = pitch_ok(g, age, f0)
            rank = {"ok": 0, "?": 1, "no": 2}[ok]
            if best is None or rank < best[0]:
                if best is not None:
                    best[2].unlink(missing_ok=True)
                keep = BANK / f"{vid}.best.wav"
                replace_retry(tmp, keep)
                best = (rank, f0, keep, ok)
            else:
                tmp.unlink(missing_ok=True)
            if ok == "ok":
                break
        replace_retry(best[2], wav_p)
        txt_p.write_text(SAMPLE_TEXT, encoding="utf-8")
        b["voices"][vid] = dict(kind="design", name=name, g=g, age=age, tags=tags.split(), f0=best[1], pitch=best[3], added=now())
        bank_save(b)
        made += 1
        print(f"  [банк] {k}/{len(ids)}  {name}: тон " + (f"{best[1]:.0f} Гц" if best[1] else "не определён")
              + ("" if best[3] == "ok" else "  (тон на границе — послушайте)" if best[3] == "?" else "  (!! тон не того пола — послушайте)"),
              flush=True)
    return made


def cmd_design(a):
    n = load_n(a.root)
    b = bank_load()
    ids = [x.strip() for x in a.only.split(",") if x.strip()] if a.only else [c[0] for c in CATALOG]
    todo = [i for i in ids if a.force or not ((BANK / f"{i}.wav").exists() and (BANK / f"{i}.txt").exists())]
    if not todo:
        print(f"  банк голосов: все заготовки уже созданы ({len(ids)}) — {BANK}")
        return 0
    print(f"  банк голосов: создаю {len(todo)} заготовок (~10 секунд на голос) -> {BANK}", flush=True)
    model = n.load_qwen(a.model or "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign", a.attn)
    made = design_voices(n, model, todo, b, a.force)
    print(f"  банк голосов: создано {made}; всего заготовок с образцом: "
          f"{sum(1 for c in CATALOG if (BANK / (c[0] + '.wav')).exists())} из {len(CATALOG)}")
    return 0


def cmd_harvest(a):
    n = load_n(a.root)
    roles = n.load_roles()
    rows = n.load_rows() if a.verbs else []
    narrator = getattr(n, "NARRATOR", "narrator")
    stems = n.voice_stems(roles)
    aliased = set(roles.get("aliases", {}))
    leaf = Path(n.HERE).name.lower()
    title = str((getattr(n, "SETTINGS", None) or {}).get("title") or leaf)
    verbs = verb_tags(rows, lambda s: n.resolve_role(s, roles)) if rows else {}
    b = bank_load()
    BANK.mkdir(parents=True, exist_ok=True)
    new = upd = 0
    for role, cfg in roles["roles"].items():
        if cfg.get("same_as") or role in aliased:
            continue
        if (cfg.get("voice") or {}).get("bank"):
            continue                                     # этот голос и так из банка
        wav_p, txt_p = n.voice_files(role, stems)
        if not (wav_p.exists() and txt_p.exists()):
            continue
        vid = "b_" + leaf + "_" + re.sub(r"[^a-z0-9_]+", "_", wav_p.stem.lower() if wav_p.stem.isascii() else n.safe_name(role))
        sig = hashlib.sha1(wav_p.read_bytes() + txt_p.read_bytes()).hexdigest()[:12]
        rec = b["voices"].get(vid)
        if rec and rec.get("sig") == sig and (BANK / f"{vid}.wav").exists():
            continue
        prof = role_profile(role, cfg, verbs, narrator)
        for src, dst in ((txt_p, BANK / f"{vid}.txt"), (wav_p, BANK / f"{vid}.wav")):
            tmpf = dst.with_name(dst.name + ".new")
            shutil.copyfile(src, tmpf)
            replace_retry(tmpf, dst)
        b["voices"][vid] = dict(kind="book", name=f"{role} — «{title}»", g=prof["g"], age=prof["age"],
                                tags=[t for t in prof["tags"]], f0=f0_of(wav_p), sig=sig, added=now(),
                                instruct=str(cfg.get("instruct") or ""),
                                src=dict(book=leaf, role=role, count=int(cfg.get("count") or 0), narrator=role == narrator))
        upd += bool(rec)
        new += not rec
    bank_save(b)
    total = sum(1 for v in b["voices"].values() if v.get("kind") == "book")
    if not a.quiet or new or upd:
        print(f"  банк голосов: из книги «{title}» добавлено {new}, обновлено {upd}; всего голосов из книг: {total} — {BANK}")
    return 0


def cmd_list(a):
    if getattr(a, "harvest", False) and a.root:          # студия: образцы голосов книги — в банк тем же запуском, что и список
        try:
            cmd_harvest(argparse.Namespace(root=a.root, quiet=True, verbs=False, json=False))
        except (Exception, SystemExit) as e:             # список нужен в любом случае: банк читается и без свежих образцов
            print(f"  банк голосов не обновлён образцами книги: {e}", file=sys.stderr)
    b = bank_load()
    vs = all_voices(b)
    used = {}
    if a.root:
        n = load_n(a.root)
        used = used_voices(n.load_roles(), n)
    if a.json:
        def fsig(v):
            w = BANK / f"{v['id']}.wav"
            try:
                st_ = w.stat()
                return f"{st_.st_size:x}-{st_.st_mtime_ns:x}"
            except OSError:
                return ""
        print(json.dumps(dict(bank=str(BANK), voices=[dict(v, used=used.get(v["id"], 0), fsig=fsig(v)) for v in vs.values()]), ensure_ascii=False))
        return 0
    have = [v for v in vs.values() if v["have"]]
    print(f"банк голосов {BANK}: с образцом {len(have)} (заготовок {sum(1 for v in have if v['kind'] == 'design')} из {len(CATALOG)}, "
          f"из книг {sum(1 for v in have if v['kind'] == 'book')}, своих {sum(1 for v in have if v['kind'] == 'custom')})")
    for v in vs.values():
        print(f"  {'есть' if v['have'] else ' —  '}  {v['id']:22s} {v['name']}  [{describe(v)}]")
    return 0


def cmd_suggest(a):
    n = load_n(a.root)
    rows, roles, narrator, need, profs = book_view(n, a.all or bool(a.role))
    vs = all_voices(bank_load())
    if a.role:
        if a.role not in profs:
            sys.exit(f"роли «{a.role}» нет (или у неё общий голос с другой ролью)")
        need = [a.role]
    sug = suggest(n, roles, need, profs, vs, force=bool(a.role))
    if a.json:
        print(json.dumps({role: dict(profile=profs[role], described=is_described(roles["roles"][role]),
                                     cand=[dict(id=v["id"], name=v["name"], score=round(sc, 1), have=v["have"], kind=v.get("kind"),
                                                about=describe(v)) for sc, v in c])
                          for role, c in sug.items()}, ensure_ascii=False))
        return 0
    if not sug:
        print("  голоса: у всех нужных ролей есть образцы")
    for role, c in sug.items():
        p = profs[role]
        print(f"  {role}  [{'муж' if p['g'] == 'm' else 'жен'}, {AGE_RU[p['age']]}"
              + (", " + ", ".join(TAG_RU.get(t, t) for t in p["tags"]) if p["tags"] else "") + f"; реплик {p['count']}]"
              + ("  — голос описан автором: создаётся по описанию" if is_described(roles["roles"][role]) else ""))
        for sc, v in c[:3]:
            print(f"      {sc:6.1f}  {v['name']}" + ("" if v["have"] else "  (образец ещё не создан)"))
    return 0


def cmd_apply(a):
    n = load_n(a.root)
    roles = n.load_roles()
    if a.role not in roles["roles"]:
        sys.exit(f"роли «{a.role}» нет в work\\roles.json")
    vs = all_voices(bank_load())
    v = vs.get(a.voice)
    if not v:
        sys.exit(f"голоса «{a.voice}» нет в банке")
    if not v["have"]:
        sys.exit(f"у заготовки «{v['name']}» ещё нет образца — создайте: nox_voices.py design --root {a.root} --only {v['id']}")
    p = set_voice(n, roles, a.role, v, "author")
    save_roles(n, roles)
    print(f"  роль «{a.role}» -> голос «{v['name']}» ({p.name}); прежний образец — в work\\voices_old")
    return 0


def cmd_revert(a):
    n = load_n(a.root)
    roles = n.load_roles()
    if a.role not in roles["roles"]:
        sys.exit(f"роли «{a.role}» нет в work\\roles.json")
    cfg = roles["roles"][a.role]
    pv = cfg.get("voice_prev") or {}
    old = n.WORK / "voices_old"
    stems = n.voice_stems(roles)
    wav_p, txt_p = n.voice_files(a.role, stems)
    pick = str(getattr(a, "file", "") or "")
    if getattr(a, "first", False):                       # «сбросить голос роли»: самый первый её образец
        pat = re.compile(re.escape(wav_p.stem) + r"__[0-9]{8}-[0-9]{6}(-[0-9]+)?$")
        olds = sorted((f.stem for f in old.glob(wav_p.stem + "__*.wav") if pat.match(f.stem))) if old.is_dir() else []
        if not olds:
            sys.exit(f"голос роли «{a.role}» ещё не меняли — сбрасывать нечего: у неё стоит первоначальный образец")
        pick = olds[0]                                   # имена — с датой и временем: первый по алфавиту = самый ранний
    if pick:                                             # любой прежний образец этой роли — по имени из history
        if not re.fullmatch(re.escape(wav_p.stem) + r"__[0-9]{8}-[0-9]{6}(-[0-9]+)?", pick):
            sys.exit(f"«{pick}» — не прежний образец роли «{a.role}» (их имена показывает history)")
        src_w = old / (pick + ".wav")
        if not src_w.exists():
            sys.exit(f"образца {src_w.name} в work\\voices_old уже нет")
    else:
        src_w = old / (str(pv.get("file") or "") + ".wav")
        if not pv.get("file") or not src_w.exists():
            sys.exit(f"у роли «{a.role}» нет сохранённого прежнего образца (смен голоса не было или папку voices_old очистили)")
    src_t = src_w.with_suffix(".txt")
    if not src_t.exists():
        src_t = txt_p                                    # текст образца у всех голосов книги один
    if sample_sig(src_w, src_t) == sample_sig(wav_p, txt_p):
        print(f"  роль «{a.role}»: этот образец уже стоит ({src_w.name}) — ничего не менялось")
        return 0
    kept = keep_old(n, wav_p, txt_p)                     # и нынешний не теряется
    cur_voice = cfg.get("voice")
    put_sample(n, wav_p, txt_p, src_w, src_t)
    if pick and pick != str(pv.get("file") or ""):
        # привязка к голосу банка — если этот образец там есть (по отпечатку); иначе это «свой образец» роли
        sig = sample_sig(wav_p, txt_p)
        b = bank_load()
        hit = next((dict(bank=vid, name=rec.get("name") or vid) for vid, rec in b["voices"].items()
                    if rec.get("sig") and rec.get("sig") == sig and rec.get("kind") in ("custom",)), None)
        if hit:
            cfg["voice"] = dict(hit, by="author", at=now(), rejected=(cur_voice or {}).get("rejected") or [])
        else:
            cfg.pop("voice", None)
        cfg["voice_prev"] = dict(file=kept, voice=cur_voice)
    else:
        if pv.get("voice"):
            cfg["voice"] = pv["voice"]
        else:
            cfg.pop("voice", None)
        cfg.pop("voice_prev", None)        # возврат «прежнего» расходует отметку: кнопка уходит, случайный второй щелчок голос не сменит;
                                           # заменённый образец остался в voices_old — он виден в «Прежних образцах» (history)
    save_roles(n, roles)
    print(f"  роль «{a.role}»: возвращён прежний образец голоса ({src_w.name}); заменённый — тоже в work\\voices_old")
    return 0


def cmd_history(a):
    """Прежние образцы голоса роли из work\\voices_old — от новых к старым; одинаковые по звуку показываются один раз."""
    n = load_n(a.root)
    roles = n.load_roles()
    if a.role not in roles["roles"]:
        sys.exit(f"роли «{a.role}» нет в work\\roles.json")
    stems = n.voice_stems(roles)
    wav_p, txt_p = n.voice_files(a.role, stems)
    cur = sample_sig(wav_p, txt_p)
    old = n.WORK / "voices_old"
    items, seen = [], {cur} if cur else set()
    pat = re.compile(re.escape(wav_p.stem) + r"__([0-9]{4})([0-9]{2})([0-9]{2})-([0-9]{2})([0-9]{2})([0-9]{2})(-[0-9]+)?$")
    for f in sorted(old.glob(wav_p.stem + "__*.wav"), key=lambda p: p.stem, reverse=True) if old.is_dir() else []:
        m = pat.match(f.stem)
        if not m:
            continue
        sig = sample_sig(f, f.with_suffix(".txt") if f.with_suffix(".txt").exists() else txt_p)
        if not sig or sig in seen:
            continue
        seen.add(sig)
        items.append(dict(file=f.stem, at=f"{m.group(3)}.{m.group(2)}.{m.group(1)} {m.group(4)}:{m.group(5)}",
                          kb=round(f.stat().st_size / 1024), f0=f0_of(f) if len(items) < 12 else None))
    if a.json:
        print(json.dumps(dict(role=a.role, stem=wav_p.stem, have=bool(cur), items=items), ensure_ascii=False))
        return 0
    print(f"  роль «{a.role}» ({wav_p.name}): прежних образцов — {len(items)}")
    for it in items:
        print(f"      {it['at']}  {it['file']}  {it['kb']} КБ" + (f", тон {it['f0']:.0f} Гц" if it.get("f0") else ""))
    return 0


def cmd_tidy(a):
    """Порядок в голосах книги и банка. Ничего не удаляется: лишнее переносится в подпапки. Без --apply — только отчёт."""
    n = load_n(a.root)
    roles = n.load_roles()
    stems = n.voice_stems(roles)
    keep = set()
    for role in roles["roles"]:
        w, tx = n.voice_files(role, stems)
        keep.add(w.name.lower())
        keep.add(tx.name.lower())
    vdir = n.VOICES
    orphans = sorted(p for p in vdir.iterdir() if p.is_file() and p.suffix.lower() in (".wav", ".txt")
                     and p.name.lower() not in keep and not p.name.endswith(".new")) if vdir.is_dir() else []
    b = bank_load()
    cat = {c[0] for c in CATALOG}
    dead = [(vid, rec) for vid, rec in b["voices"].items()
            if rec.get("kind") in ("book", "custom") and not (BANK / f"{vid}.wav").exists()]
    known = cat | set(b["voices"])
    stray = sorted(p for p in BANK.glob("*.wav") if p.stem not in known) if BANK.is_dir() else []
    dead_ids = {vid for vid, _ in dead}
    unbound = [role for role, cfg in roles["roles"].items() if (cfg.get("voice") or {}).get("bank") in dead_ids]
    old = n.WORK / "voices_old"
    prev_gone = [role for role, cfg in roles["roles"].items()
                 if (cfg.get("voice_prev") or {}).get("file") and not (old / (str(cfg["voice_prev"]["file"]) + ".wav")).exists()]
    rep_ = dict(applied=bool(a.apply),
                book=dict(orphans=[p.name for p in orphans], kb=round(sum(p.stat().st_size for p in orphans) / 1024),
                          to=str(old / "bez_roli")),
                bank=dict(dead=[dict(id=vid, name=rec.get("name") or vid) for vid, rec in dead],
                          stray=[p.name for p in stray], to=str(BANK / "bez_zapisi")),
                roles=dict(unbound=unbound, prev_gone=prev_gone))
    if a.apply:
        if orphans:
            dst = old / "bez_roli"
            dst.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            for p in orphans:
                q = dst / p.name
                if q.exists():
                    q = dst / f"{p.stem}__{stamp}{p.suffix}"
                shutil.move(str(p), str(q))
        if stray:
            dst = BANK / "bez_zapisi"
            dst.mkdir(parents=True, exist_ok=True)
            for p in stray:
                for f in (p, p.with_suffix(".txt")):
                    if f.exists():
                        q = dst / f.name
                        if q.exists():
                            q = dst / f"{f.stem}__{time.strftime('%Y%m%d-%H%M%S')}{f.suffix}"
                        shutil.move(str(f), str(q))
        if dead:
            for vid, _ in dead:
                b["voices"].pop(vid, None)
            bank_save(b)
        if unbound or prev_gone:
            for role in unbound:
                v = roles["roles"][role].get("voice") or {}
                v.pop("bank", None)                      # образец роли на месте; запись банка пропала — это теперь «свой образец»
                roles["roles"][role]["voice"] = v
                if not v.get("name"):
                    roles["roles"][role].pop("voice", None)
            for role in prev_gone:
                roles["roles"][role].pop("voice_prev", None)
            save_roles(n, roles)
    if a.json:
        print(json.dumps(rep_, ensure_ascii=False))
        return 0
    word = "убрано" if a.apply else "можно убрать"
    print(f"  порядок в голосах ({'сделано' if a.apply else 'только отчёт; с ключом --apply — выполнить'}):")
    print(f"    образцов без роли в книге: {len([x for x in rep_['book']['orphans'] if x.lower().endswith('.wav')])} ({word} в {rep_['book']['to']})")
    print(f"    записей банка без файла образца: {len(dead)}; файлов банка без записи: {len(stray)}")
    print(f"    ролей со ссылкой на пропавший голос банка: {len(unbound)}; с пропавшим «прежним образцом»: {len(prev_gone)}")
    return 0


def cmd_custom(a):
    """Свой голос по описанию — в банк (kind=custom). Тон сверяется с полом/возрастом, до трёх попыток, как у заготовок."""
    import soundfile as sf
    import torch
    n = load_n(a.root)
    name, desc = a.name.strip(), a.desc.strip()
    if len(name) < 2 or len(desc) < 8:
        sys.exit("нужны название голоса и описание (хотя бы несколько слов)")
    if a.g not in ("m", "f") or a.age not in AGES:
        sys.exit("пол: m или f; возраст: " + ", ".join(AGES))
    if re.search("[а-яё]", desc.lower()):
        print("  заметка: модель создания голосов лучше понимает описание по-английски (как в таблице настроек)", flush=True)
    b = bank_load()
    vid = "u_" + hashlib.sha1((name + "|" + desc).encode("utf-8")).hexdigest()[:10]
    BANK.mkdir(parents=True, exist_ok=True)
    who = {"m": "Male voice.", "f": "Female voice."}[a.g] if a.age != "child" else \
        {"m": "Boy's voice (child, not an adult).", "f": "Little girl's voice (child)."}[a.g]
    instr = desc if re.search(r"\b(male|female|man|woman|boy|girl)\b", desc.lower()) else who + " " + desc
    if "diction" not in instr.lower():
        instr += DICTION
    model = n.load_qwen(a.model or "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign", a.attn)
    best = None
    for attempt in range(3):
        torch.manual_seed(2000 + 91 * attempt + int(hashlib.sha1(vid.encode()).hexdigest()[:4], 16))
        extra = "" if attempt == 0 else (" Unmistakably a child's voice." if a.age == "child" else
                                         " Unmistakably a male voice, low pitch." if a.g == "m" else " Unmistakably a female voice.")
        wavs, sr = model.generate_voice_design(text=SAMPLE_TEXT, language="Russian", instruct=instr + extra)
        tmp = BANK / f"{vid}.try.wav"
        sf.write(str(tmp), wavs[0], sr)
        f0 = f0_of(tmp)
        ok = pitch_ok(a.g, a.age, f0)
        rank = {"ok": 0, "?": 1, "no": 2}[ok]
        if best is None or rank < best[0]:
            if best is not None:
                best[2].unlink(missing_ok=True)
            keep = BANK / f"{vid}.best.wav"
            replace_retry(tmp, keep)
            best = (rank, f0, keep, ok)
        else:
            tmp.unlink(missing_ok=True)
        if ok == "ok":
            break
    (BANK / f"{vid}.txt").write_text(SAMPLE_TEXT, encoding="utf-8")
    replace_retry(best[2], BANK / f"{vid}.wav")
    tags = list(dict.fromkeys(tags_from_text(desc, EN_TAGS)))
    b["voices"][vid] = dict(kind="custom", name=name, g=a.g, age=a.age, tags=tags, f0=best[1], pitch=best[3], added=now(),
                            instruct=instr)
    bank_save(b)
    print(f"  [банк] свой голос «{name}» создан ({vid}): тон " + (f"{best[1]:.0f} Гц" if best[1] else "не определён")
          + ("" if best[3] == "ok" else "  (тон на границе — послушайте)" if best[3] == "?" else "  (!! тон не того пола — послушайте)"),
          flush=True)
    return 0


def cmd_prepare(a):
    n = load_n(a.root)
    rows, roles, narrator, need, profs = book_view(n, a.all)
    stems = n.voice_stems(roles)
    lack = [r for r in need if not n.voice_files(r, stems)[0].exists()]
    scope = "книги" if a.all or not (hasattr(n, "by_parts") and n.by_parts()) else "текущей части"
    if not lack:
        print(f"  голоса: у всех ролей {scope} есть образцы ({len(need)})")
        return 0
    for role in lack:                                   # образец роли убрала проверка тона — этот голос банка ей не давать
        vo = roles["roles"][role].get("voice") or {}
        if vo.get("bank") and vo["bank"] not in (vo.get("rejected") or []):
            vo["rejected"] = (vo.get("rejected") or []) + [vo["bank"]]
            vo["bank"] = None
            roles["roles"][role]["voice"] = vo
    b = bank_load()
    vs = all_voices(b)
    described = [r for r in lack if is_described(roles["roles"][r])]
    plain = [r for r in lack if r not in described]
    sug = suggest(n, roles, plain, profs, vs, only_have=a.no_design)
    picks = {r: (c[0][1] if c else None) for r, c in sug.items()}
    to_make = [v["id"] for v in picks.values() if v and not v["have"]]
    by_desc = described + [r for r, v in picks.items() if v is None]
    print(f"  голоса: ролям {scope} без образца — {len(lack)}: из банка {len(plain) - len([r for r in plain if picks.get(r) is None])}"
          + (f" (создаю заготовок: {len(to_make)})" if to_make else "")
          + (f", по описанию автора {len(described)}" if described else "")
          + (f", без подходящего в банке (по общему описанию) {len(by_desc) - len(described)}" if len(by_desc) > len(described) else ""), flush=True)
    if (to_make or by_desc) and not a.no_design:
        import soundfile as sf
        model = n.load_qwen(a.model or "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign", a.attn)
        if to_make:
            design_voices(n, model, list(dict.fromkeys(to_make)), b)
            vs = all_voices(b)
        sample, lang = roles.get("sample_text") or SAMPLE_TEXT, roles.get("language", "Russian")
        for role in by_desc:                             # как команда cast конвейера
            cfg = roles["roles"][role]
            text = cfg.get("sample_text") or sample
            wavs, sr = model.generate_voice_design(text=text, language=lang, instruct=cfg["instruct"])
            wav_p, txt_p = n.voice_files(role, stems)
            n.VOICES.mkdir(parents=True, exist_ok=True)
            tw = wav_p.with_name(wav_p.stem + ".new.wav")
            sf.write(str(tw), wavs[0], sr)
            txt_p.write_text(text, encoding="utf-8")
            replace_retry(tw, wav_p)                        # образец появляется сразу целым
            print(f"  [голос] {role}: создан по описанию ({len(wavs[0]) / sr:.1f} с)", flush=True)
    done = 0
    for role, v in picks.items():
        if not v:
            continue
        v = vs.get(v["id"], v)
        if not v["have"]:
            print(f"  [голос] {role}: подошла бы заготовка «{v['name']}», но её образец ещё не создан (нужна видеокарта)", flush=True)
            continue
        set_voice(n, roles, role, v, "auto")
        done += 1
        p = profs[role]
        print(f"  [голос] {role} -> «{v['name']}»" + (f"  ({'; '.join(p['why'])})" if p["why"] else ""), flush=True)
    save_roles(n, roles)
    if done:
        print("  подобранные голоса можно сменить в студии: кнопка «Голоса» — список, прослушивание, проба на реплике роли")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("prepare", "suggest", "apply", "revert", "custom", "harvest", "design", "list", "history", "tidy"):
        s = sub.add_parser(name)
        s.add_argument("--root", default=None if name == "list" else str(Path(__file__).resolve().parent))
        s.add_argument("--json", action="store_true")
        if name in ("prepare", "suggest"):
            s.add_argument("--all", action="store_true")
        if name == "suggest":
            s.add_argument("--role", default="", help="подобрать голоса одной роли, даже если образец у неё есть")
        if name == "revert":
            s.add_argument("--role", required=True)
            s.add_argument("--file", default="", help="имя прежнего образца из work\\voices_old (без расширения), см. history")
            s.add_argument("--first", action="store_true", help="самый первый образец роли — тот, с которым она начинала книгу")
        if name == "history":
            s.add_argument("--role", required=True)
        if name == "tidy":
            s.add_argument("--apply", action="store_true", help="выполнить (без ключа — только отчёт)")
        if name == "custom":
            s.add_argument("--name", required=True)
            s.add_argument("--desc", required=True)
            s.add_argument("--g", default="m")
            s.add_argument("--age", default="adult")
        if name in ("prepare", "design", "custom"):
            s.add_argument("--model", default=None)
            s.add_argument("--attn", default="sdpa")
        if name == "prepare":
            s.add_argument("--no-design", action="store_true", help="без видеокарты: только уже созданные голоса банка")
        if name == "design":
            s.add_argument("--only", default="")
            s.add_argument("--force", action="store_true")
        if name == "apply":
            s.add_argument("--role", required=True)
            s.add_argument("--voice", required=True)
        if name == "list":
            s.add_argument("--harvest", action="store_true", help="сначала занести в банк образцы голосов книги (как harvest --quiet)")
        if name == "harvest":
            s.add_argument("--quiet", action="store_true")
            s.add_argument("--verbs", action="store_true", help="учесть слова автора вокруг реплик (дольше)")
    a = ap.parse_args()
    return {"prepare": cmd_prepare, "suggest": cmd_suggest, "apply": cmd_apply, "revert": cmd_revert, "custom": cmd_custom,
            "harvest": cmd_harvest, "design": cmd_design, "list": cmd_list, "history": cmd_history, "tidy": cmd_tidy}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main() or 0)
