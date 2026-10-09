"""Best-guess tags for newly synced MyMiniFactory items.

The same kinds of tags the library was given by hand: a genre (fantasy, scifi,
cyberpunk, grimdark), a race (human, elf, ork, undead, ...) and terrain, base,
basing bits, vehicle and bust. They are guessed from the item's name, its
MyMiniFactory tags and release, and, for the genre, from the tags already on
the creator's other items and the other items of the same release.

Rulebooks, PDFs, discount codes, counters and life-size props get no tags.
"""
from __future__ import annotations

import re

GENRES = ("fantasy", "scifi", "cyberpunk", "grimdark")


def _words(*parts: str) -> str:
    """'Sci-Fi Tank (Pre-Supported)' -> ' sci fi tank pre supported '."""
    return " " + " ".join(re.sub(r"[^a-z0-9]+", " ", (p or "").lower()) for p in parts).strip() + " "


def _rx(*words: str) -> re.Pattern:
    """Whole words; a trailing * allows any ending (elf* = elf, elfs; dwar* = dwarf, dwarven),
    and "re:..." is a regular expression for one word."""
    alts = [w[3:] if w.startswith("re:") else re.escape(w[:-1]) + r"[a-z]*" if w.endswith("*") else re.escape(w)
            for w in words]
    return re.compile(r" (?:" + "|".join(alts).replace(r"\ ", " ") + r") ")


def _has(rx: re.Pattern, text: str) -> bool:
    return bool(rx.search(text))


# Not miniatures: these get no tags at all.
_NOT_A_MINI = _rx("rulebook*", "rules", "pdf", "painting guides", "paint guide", "campaign guide", "discount*",
                  "statblocks", "stats", "compendium", "dossier", "game aids", "counter", "counters", "token",
                  "tokens", "making of", "1 1", "1 1 scale", "baubles", "dice vault", "measure chain",
                  "command deck", "lore items", "intro", "books", "potion flask", "health potion", "vault key",
                  "knife", "dagger")
_NOT_A_MINI_UNLESS = _rx("model", "miniature*", "mini", "bust", "terrain")  # "Painting Guide + Model" is a mini

# Genre words that settle it, before what the creator's other items are.
_CYBERPUNK = _rx("cyberpunk", "cyber", "technopunk", "gridrunner", "netrunner", "hacked")
_GRIMDARK = _rx("grimdark", "trench*")
_SAYS_SCIFI = _rx("sci fi", "scifi", "science fiction", "spacesuit*", "space")
_SAYS_FANTASY = _rx("fantasy", "dnd", "d d", "5e")
# Words that lean one way, when the creator's other items don't say.
_SCIFI = _rx("sci fi", "scifi", "science fiction", "space", "spaceship*", "starship*", "starfighter*", "voidship*",
             "fleet", "fleets", "frigate*", "dreadnought*", "cruiser*", "spacewear", "colonist*", "astronaut*",
             "laser*", "plasma", "blaster*", "android*", "cyborg*", "droid*", "mech", "mechs", "exo suit*",
             "combatsuit*", "power armou*", "clone trooper*", "troopers", "xeno*", "alien*", "galactic",
             "shuttle*", "dropship*", "star wars", "40k", "grimdark future", "auto rifle")
_FANTASY = _rx("fantasy", "dnd", "d d", "5e", "dungeons", "pathfinder", "wizard*", "sorcer*", "warlock*",
               "necromancer*", "paladin*", "cleric*", "bard", "bards", "druid*", "ranger*", "barbarian*",
               "knight*", "dragon", "dragons", "dragonborn", "wyrm*", "wyvern*", "elf", "elves", "elven",
               "dwar*", "halfling*", "gnome*", "goblin*", "hobgoblin*", "orc", "orcs", "lich", "vampire*",
               "skeleton*", "treant*", "beholder*", "kobold*", "witch", "witches", "mage", "mages", "giant",
               "minotaur*", "centaur*", "satyr*", "werewol*", "medieval", "viking*", "valkyrie*", "pegasus")

# Races. Earlier entries win (a "Dragonborn Necromancer" is dragonkin, not human).
_RACES = (
    ("human", _rx("demon hunter*", "witch hunter*", "dragon knight*", "dragon cult*", "dragon warlock*",
                  "dragon priest*", "nathan drake")),
    ("dragonkin", _rx("dragonborn", "drekon*", "kobold*", "draconian*", "drakonid*", "dragonkin")),
    ("ogre", _rx("ogre*", "ogryn*")),
    ("ratfolk", _rx("ratmen", "ratman", "ratfolk", "rat folk", "skaven", "raticus", "rat warriors", "rat brawlers",
                    "war drum rat", "rat plague")),
    ("lizardfolk", _rx("lizardm*", "lizardfolk", "lizard folk", "lizards", "saurian*", "saurus", "axolian*")),
    ("halfling", _rx("halfling*", "hobbit*")),
    ("gnome", _rx("gnome*")),
    ("goblin", _rx("goblin*", "hobgoblin*", "gob", "gobs", "gretchin*", "grot", "grots", "snotling*", "runt", "runts", "runtz",
                    "git", "gits")),
    ("ork", _rx("orc", "orcs", "orcish", "ork", "orks", "orkaz", "orruk*", "boyz", "clorc",
                 "re:(?!(?:w|y|f|p|c|st|n|m)ork )[a-z]+ork", "re:ork[a-z]+")),
    ("dwarf", _rx("dwarf", "dwarfs", "dwarves", "dwarven", "duergar", "dvergr*")),
    ("elf", _rx("elf", "elfs", "elves", "elven", "elvish", "drow", "aelf*", "eldar")),
    ("giant", _rx("hill giant*", "frost giant*", "stone giant*", "fire giant*", "cloud giant*", "storm giant*",
                  "goliath*", "giant roroa")),
    ("construct", _rx("golem*", "construct*", "robot*", "warforged", "automaton*", "mechanod*", "drone*",
                      "clockwork", "mek")),
    ("undead", _rx("undead", "undying", "skeleton*", "zombie*", "lich", "liches", "vampire*", "ghoul*", "wraith",
                   "wraiths", "spectral", "spectre*", "revenant*", "mummy", "mummies", "mummified", "ghost*",
                   "banshee*", "draugr", "death knight*", "pharaoh", "bones", "skeletal", "necron*", "witch king*",
                   "cadaver*", "wailer*", "gravelord", "crawling hand*", "strahd von*", "pale one")),
    ("demon", _rx("demon", "demons", "daemon*", "devil*", "imp", "imps", "hellhound*", "dretch*", "succub*",
                  "infernal", "quasit*", "soul reaper*")),
    ("dragon", _rx("dragon", "dragons", "wyrm*", "wyvern*", "drake", "drakes", "dracolich*", "dracotera")),
    ("alien", _rx("alien*", "xeno*", "xenarid*", "hive", "hives", "hivemind", "void spawn*", "voidspawn*",
                  "tyranid*", "encephalid*")),
    ("beastkin", _rx("werewol*", "lycan*", "minotaur*", "tauros", "kitsune*", "catfolk", "tabaxi",
                     "simiax", "satyr*", "centaur*", "harp*", "gnoll*", "beastm*", "tortle*", "frog folk",
                     "froglok*", "shark captain", "shark man", "sharkzerker*", "fishm*", "selachian*", "swine*",
                     "bullywug*", "ratkin", "wolfkin", "aarakocra", "frogrider*", "wereshark*")),
    ("beast", _rx("wolf", "wolves", "spider*", "crocodile*", "owl", "owls", "raven", "ravens", "dog", "dogs", "hound",
                  "hounds", "bear", "bears", "boar", "boars", "monkey*", "ape", "apes", "rat swarm*",
                  "greatwolf", "ray", "jellies")),
    ("monster", _rx("monster*", "monstrous", "beholder*", "treant*", "yeti*", "xueren", "kaiju*", "blight*",
                    "troll*", "hydra*", "kraken*", "sharktopus", "owlbear*", "mimic*", "chimera*", "basilisk*",
                    "gorgon*", "medusa*", "manticore*", "abomination*", "horror*", "ettin*", "cultists of the dredge",
                    "ankou", "behemoth*", "leviathan*", "hag", "hags", "dryad*",
                    "tarrasque*", "kelpie*", "doppelganger*", "nothic*", "red cap*", "banderhobb*", "bulette*",
                    "aberration*", "dredge", "shambling mound*", "mimic", "jackalope*", "aswang")),
    ("human", _rx("human*", "barbarian*", "knight*", "paladin*", "cleric*", "bard", "bards", "druid*", "wizard*",
                  "witch", "witches", "warlock*", "rogue", "ranger*", "monk", "monks", "samurai", "valkyrie*",
                  "shieldmaiden*", "shield maiden*", "cultist*", "sister", "sisters", "trooper*", "soldier*",
                  "infantrymen", "marksmen", "legionaries", "plague doctor*", "pirate*", "mercenar*",
                  "gunslinger*", "blacksmith*", "princess*", "necromancer*", "assassin*", "bloodhunter*",
                  "viking*", "templar*", "teutonic", "cossack*", "serfs", "commissar*", "highwayman", "kunoichi",
                  "ninja", "sorcerer*", "sorceress*", "scientist*",
                  "supplicants", "fanatics", "crusader*", "nun", "nuns")),
)

_TERRAIN = _rx("terrain", "scenery", "scenic", "scatter", "building*", "house", "houses", "ruin", "ruins", "barricade*",
               "fence*", "wall", "walls", "tile*", "exteriors", "interiors", "cottage*", "tavern*", "temple*", "gatehouse*",
               "monument*", "pillar*", "column*", "hut", "huts", "jail*", "fountain*", "camp props", "ladders",
               "scaffold*", "crate*", "obstacle*", "warlayer", "openlock", "dungeon blocks", "cave", "caves",
               "mansion*", "graveyard*", "foundry", "market", "hangar*", "crystals",
               "toy rock", "rocks")
_BASE = _rx("base", "bases", "topper*", "basing")
_BASING_BITS = _rx("basing bits", "for basing", "basing pack*")
_NOT_BASE = _rx("hive base", "base camp", "with base", "base game", "rod base", "and base", "base included")
_VEHICLE = _rx("tank", "tanks", "transport", "bike", "bikes", "biker*", "jetbike*", "jet", "jets", "walker",
               "walkers", "wagon*", "kart*", "exo suit*", "combatsuit*", "starship*", "spaceship*", "starfighter*",
               "fleet", "fleets", "frigate*", "cruiser*", "dreadnought*", "destroyer", "airship*", "gunship*",
               "shuttle*", "truck*", "buggy", "buggies", "dropship*", "speeder*", "speedsters", "skycoach*",
               "convoy", "unicycle*", "mekas", "kan", "kans", "roller", "space taxi", "heavy vehicle",
               "support vehicle", "drive section", "platform", "stormrider", "skyfire", "voidship*")
_NOT_VEHICLE = _rx("destroyer sister*", "walker pose", "pharaoh walker", "sunwalker", "tank trap", "hangar")
_BUST = _rx("bust", "busts")


def guess(name: str, mmf_tags: list[str] = (), release: str = "", genre_hint: str = "",
          race_hint: str = "") -> list[str]:
    """Tags for one item. The hints are the genre and race nearly all the creator's (or the
    release's) tagged items have, used when the item's own words don't say."""
    own = _words(name, " ".join(mmf_tags))
    if (_has(_NOT_A_MINI, _words(name)) or re.search(r"\d+ off ", _words(name))) and not _has(_NOT_A_MINI_UNLESS, _words(name)):
        return []
    text = _words(name, " ".join(mmf_tags), release)
    out = []
    if _has(_CYBERPUNK, text):
        out.append("cyberpunk")
    elif _has(_GRIMDARK, text):
        out.append("grimdark")
    elif _has(_SAYS_SCIFI, own):
        out.append("scifi")
    elif _has(_SAYS_FANTASY, own):
        out.append("fantasy")
    elif genre_hint in GENRES:
        out.append(genre_hint)
    elif _has(_SCIFI, text):
        out.append("scifi")
    elif _has(_FANTASY, text):
        out.append("fantasy")
    is_place = _has(_TERRAIN, own)
    is_base = _has(_BASE, own) and not _has(_NOT_BASE, own)
    if not is_place and not is_base:
        race = next((r for r, rx in _RACES if _has(rx, own)), None) or race_hint
        if race:
            out.append(race)
    if is_base:
        out.append("base")
        if _has(_BASING_BITS, own):
            out.append("basing bits")
    elif is_place:
        out.append("terrain")
    if _has(_VEHICLE, own) and not _has(_NOT_VEHICLE, own):
        out.append("vehicle")
    if _has(_BUST, own):
        out.append("bust")
    return out


RACES = tuple(dict.fromkeys(r for r, _ in _RACES))


def majority(tag_lists: list[list[str]], kinds: tuple[str, ...], share: float, least: int = 1) -> str:
    """The tag of `kinds` most of these tag lists have, when at least `share` of the lists with
    one of them (and at least `least` lists) agree."""
    counts: dict[str, int] = {}
    for tags in tag_lists:
        low = {t.casefold() for t in tags}
        for k in kinds:
            if k in low:
                counts[k] = counts.get(k, 0) + 1
                break
    if not counts:
        return ""
    best = max(counts, key=counts.get)
    return best if counts[best] >= max(least, share * sum(counts.values())) else ""


def genre_hint(tag_lists: list[list[str]]) -> str:
    return majority(tag_lists, GENRES, 0.6)


def race_hint(tag_lists: list[list[str]]) -> str:
    """Only for creators who make one race (an ork range): most of their figures must have it.
    Figures are the tag lists with a genre that aren't terrain or bases."""
    tag_lists = [tags for tags in ({t.casefold() for t in tl} for tl in tag_lists)
                 if tags & set(GENRES) and not tags & {"terrain", "base"}]
    with_race = sum(1 for tags in tag_lists if tags & set(RACES))
    if with_race < 3 or with_race < 0.5 * len(tag_lists):
        return ""
    return majority(tag_lists, RACES, 0.85, 3)
