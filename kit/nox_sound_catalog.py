"""Russian sound names recovered from late 18d, and bounded filesystem catalog."""
import re
import os
from pathlib import Path
import time

# Понятные русские названия для каталога звуков: существа и слова из технических имён игры (WolfAttackInit -> «Волк: начало атаки»)
ENT_RU = {"ogrebrute": "огр-громила", "ogrewarlord": "огр-полководец", "necromancer": "некромант", "urchin": "беспризорник",
          "urchinshaman": "шаман беспризорников", "hecubah": "Хекубах", "grunt": "громила", "lich": "лич", "emberdemon": "демон углей",
          "shade": "тень", "zombie": "зомби", "vilezombie": "гнилой зомби", "troll": "тролль", "swordsman": "мечник",
          "largespider": "большой паук", "smallspider": "паучок", "dryad": "дриада", "wizard": "маг", "archer": "лучник", "wolf": "волк",
          "fireknight1": "огненный рыцарь", "horrendous": "Ужасающий", "demon": "демон", "maiden": "дева", "beholder": "созерцатель",
          "bat": "летучая мышь", "willowisp": "блуждающий огонёк", "skeletonlord": "лорд скелетов", "skeleton": "скелет",
          "plant": "хищное растение", "npc": "горожанин", "golem": "голем", "bear": "медведь", "guard1": "стражник", "ghost": "призрак",
          "frog": "лягушка", "rat": "крыса", "weirdling": "чудик", "syllables": "слоги заклинаний"}
ACT_RU = {"idle": "ожидание", "attack": "атака", "init": "начало", "recognize": "заметил героя", "alert": "тревога", "hurt": "ранен",
          "hit": "удар", "die": "гибель", "death": "гибель", "dying": "гибель", "walk": "шаги", "run": "бег", "step": "шаг", "steps": "шаги",
          "footstep": "шаг", "talk": "речь", "laugh": "смех", "scream": "крик", "growl": "рычание", "roar": "рёв", "howl": "вой",
          "swing": "взмах", "open": "открывается", "close": "закрывается", "break": "ломается", "pickup": "подбирают", "drop": "падает",
          "fire": "огонь", "fireball": "огненный шар", "explosion": "взрыв", "teleport": "телепорт", "heal": "лечение", "poison": "яд",
          "shock": "разряд", "lightning": "молния", "ice": "лёд", "frost": "мороз", "slow": "замедление", "haste": "ускорение",
          "cast": "создание заклинания", "charge": "накопление силы", "impact": "попадание", "loop": "петля", "spawn": "появление",
          "summon": "призыв", "magic": "магия", "wind": "ветер", "water": "вода", "bell": "колокол", "torch": "факел", "lava": "лава",
          "cave": "пещера", "forest": "лес", "night": "ночь", "bird": "птицы", "birds": "птицы", "town": "город", "rain": "дождь",
          "thunder": "гром", "crowd": "толпа", "door": "дверь", "chest": "сундук", "gold": "золото", "coin": "монеты", "potion": "зелье",
          "staff": "посох", "sword": "меч", "bow": "лук", "arrow": "стрела", "axe": "топор", "hammer": "молот", "shield": "щит",
          "armor": "доспех", "spell": "заклинание", "gate": "ворота", "lever": "рычаг", "switch": "переключатель", "trap": "ловушка",
          "key": "ключ", "book": "книга", "scroll": "свиток", "crystal": "кристалл", "bubble": "пузыри", "steam": "пар", "smoke": "дым",
          "wood": "дерево", "stone": "камень", "metal": "металл", "glass": "стекло", "flesh": "плоть", "bone": "кости", "blood": "кровь",
          "sleep": "сон", "dead": "мёртв", "stun": "оглушение", "fly": "полёт", "flap": "взмахи крыльев", "eat": "ест", "bite": "укус",
          "claw": "когти", "spit": "плевок", "cry": "плач", "moan": "стон", "whisper": "шёпот", "chant": "заклинание вслух",
          "ambient": "окружение", "ui": "интерфейс", "level": "новый уровень", "quest": "задание", "complete": "выполнено"}


def sfx_describe(nm: str):
    """Техническое имя игры -> (существо, действие по-русски). «WolfAttackInit» -> («волк», «атака, начало»)."""
    toks = [t for t in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", str(nm)) if not t.isdigit()]
    low = str(nm).lower()
    ent = next((ENT_RU[k] for k in sorted(ENT_RU, key=len, reverse=True) if low.startswith(k)), "")
    words = []
    for t in toks:
        r = ACT_RU.get(t.lower())
        if r and r not in words:
            words.append(r)
    return ent, ", ".join(words)


def bounded_scan(roots, extensions, *, file_cap=30000, visit_cap=90000, seconds=4.0):
    """Bound *enumeration*, not just output; never follow directory symlinks.

    The limit and errors are returned to the UI. A fresh scan is explicit and
    caches also expire, so replacing files in deep directories becomes visible.
    """
    started = time.monotonic()
    items, visited, errors, limited = [], 0, [], False
    for root in roots:
        base = Path(root["path"])
        if not base.is_dir():
            errors.append({"root": root["id"], "error": "папка недоступна"})
            continue
        base = base.resolve()
        section = str(root.get("name") or base.name or "папка")[:60]
        stack = [base]
        while stack and not limited:
            directory = stack.pop()
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        visited += 1
                        if visited > visit_cap or len(items) >= file_cap or time.monotonic() - started > seconds:
                            limited = True
                            break
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        f = Path(entry.path)
                        if f.suffix.lower() not in extensions:
                            continue
                        rel = f.relative_to(base)
                        group = " / ".join(ENT_RU.get(x.lower(), x) for x in rel.parts[:-1][:2])
                        entity, action = sfx_describe(f.stem)
                        title = (entity.capitalize() + (": " + action if action else "")) if entity else action or f.stem.replace("_", " ")
                        hay = re.sub(r"[^0-9a-zа-я]+", " ", " ".join((title, f.stem, rel.as_posix(), entity, action)).lower().replace("ё", "е")).strip()
                        items.append(dict(file=f"ext:{root['id']}/{rel.as_posix()}", name=f.name, title=title[:120],
                                          sub=f"{section}: {group}" if group else section, kind="", dur=None,
                                          lib="ext", rank=2, hay=hay, words=frozenset(hay.split()), sec=section, grp=group))
            except OSError as exc:
                errors.append({"root": root["id"], "path": str(directory), "error": str(exc)})
        if limited:
            break
    items.sort(key=lambda item: (item["sec"].casefold(), item["grp"].casefold(), item["file"].casefold()))
    return items, dict(visited=visited, files=len(items), cap=file_cap, visit_cap=visit_cap,
                       limited=limited, errors=errors[:20], at=time.time())

