"""Best-guess tags on newly synced MyMiniFactory items (no server needed).

    python tests/autotag_test.py
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ["DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import autotag, db, mmf  # noqa: E402

GUESSES = [  # name, MyMiniFactory tags, release, genre hint, race hint, expected tags
    ("High Elf Arcane Archer - Eldiara", [], "", "", "", ["fantasy", "elf"]),
    ("Dragonborn Necromancer", [], "", "", "", ["fantasy", "dragonkin"]),
    ("Iensi The Demon Hunter [presupported]", [], "", "fantasy", "", ["fantasy", "human"]),
    ("Savage orc boyz mob (pre-supported)", [], "", "scifi", "", ["scifi", "ork"]),  # the creator's genre wins
    ("Orkula", [], "", "", "", ["ork"]),
    ("Mork Borg - Goblin Boss", [], "", "fantasy", "", ["fantasy", "goblin"]),
    ("MrModulork's Spacesuit Orc", [], "", "fantasy", "", ["scifi", "ork"]),  # but not over "space"
    ("Arrow Starfighter", [], "", "", "", ["scifi", "vehicle"]),
    ("Battle Tank - Blessed Sisters", [], "", "scifi", "", ["scifi", "human", "vehicle"]),
    ("Destroyer Sisters - Blessed Sisters", [], "", "scifi", "", ["scifi", "human"]),
    ("Barbarian King - Jagard - Bust - April 2024", [], "", "", "", ["fantasy", "human", "bust"]),
    ("Medieval Market Terrain - Scatter (Pre-Supported)", [], "", "", "", ["fantasy", "terrain"]),
    ("Sewer Bases and Toppers (Pre-supported)", [], "", "fantasy", "", ["fantasy", "base"]),
    ("STL-Terrain Tribes Topper Pack", [], "", "fantasy", "", ["fantasy", "base"]),
    ("Skulls for Basing - Basing Bits", [], "", "fantasy", "", ["fantasy", "base", "basing bits"]),
    ("Trench Terrain: Iron Wall", [], "", "", "", ["grimdark", "terrain"]),
    ("Cyberpunk - Cyber Santa - FREE model", [], "", "fantasy", "", ["cyberpunk"]),
    ("Brave Sun 1st Edition Rulebook", [], "", "scifi", "", []),  # not a miniature
    ("The Banshee Knife 1:1", [], "", "fantasy", "", []),
    ("GrimGuard Slayer Painting Guide + Model", [], "", "scifi", "dwarf", ["scifi", "dwarf"]),
    ("Smuggod Akimbo", [], "", "scifi", "ork", ["scifi", "ork"]),  # a creator that makes one race
    ("Wotznokk", ["Sci-Fi", "Elf"], "", "", "", ["scifi", "elf"]),  # from MyMiniFactory's own tags
    ("Aether", [], "", "", "", []),  # nothing to go on: no tags rather than a wild guess
]
for name, site, release, genre, race, want in GUESSES:
    got = autotag.guess(name, site, release, genre, race)
    assert got == want, (name, got, want)

assert autotag.genre_hint([["scifi"], ["scifi", "ork"], ["fantasy"]]) == "scifi"
assert autotag.genre_hint([["scifi"], ["fantasy"]]) == "", "a mixed creator gives no hint"
assert autotag.race_hint([["scifi", "ork"]] * 5 + [["scifi"]]) == "ork"
assert autotag.race_hint([["fantasy", "ork"]] * 2 + [["fantasy", "elf"]] * 2) == ""
assert autotag.race_hint([["fantasy", "ork"]] * 2) == "", "too few to tell"

# ---------------------------------------------------------------- in a sync
db.init()
c = db.conn()


def item(i, name, creator="Orc Works", source="purchase", collection="", tags=()):
    return {"id": i, "name": name, "creator": creator, "source": source, "collection": collection, "tags": list(tags)}


# Items already there before this version were tagged by hand: they are left alone.
c.execute("INSERT INTO mmf_items(id, name, creator, updated) VALUES (1, 'Orc Warboss', 'Orc Works', 0)")
c.execute("INSERT INTO mmf_links(item_id, source, collection) VALUES (1, 'purchase', '')")
c.execute("DELETE FROM settings WHERE key='mmf_autotag_v'")
c.commit()
db.init()
mmf.set_tags([1], ["scifi", "ork"], [])
FIRST = [item(1, "Orc Warboss"), item(2, "Orc Warboss Bust", tags=["Warhammer", "Orc"]),
         item(3, "Grot Mob"), item(4, "Ork Shoota Boyz"),
         item(5, "Elf Ranger", creator="Elf Folk", source="tribe", collection="Elf Folk Tribe · 02/2024"),
         item(6, "Forest Ruins", creator="Elf Folk", source="tribe", collection="Elf Folk Tribe · 02/2024")]
res = mmf.import_library({"items": FIRST, "complete": ["purchase", "tribe"]})
own = mmf.own_tags()
assert own[1] == ["ork", "scifi"], "an item from before isn't tagged again"
assert own[2] == ["bust", "ork", "scifi"], own[2]  # genre from the creator's other item, not "Orc" again
assert mmf.item(2)["mmf_tags"] == ["Warhammer", "Orc"], "MyMiniFactory's own tags arrive with the sync"
assert own[3] == ["goblin", "scifi"] and own[4] == ["ork", "scifi"], own
assert own[5] == ["elf", "fantasy"] and own[6] == ["fantasy", "terrain"], own
assert res["autotagged"] == 5, res

# Tags the user removes or adds stay that way on later syncs.
mmf.set_tags([4], ["painted"], ["scifi"])
SECOND = FIRST + [item(7, "Ork Deff Kopta", tags=["Sci-Fi"]),
                  item(8, "Elf Archer", creator="Elf Folk", source="tribe", collection="Elf Folk Tribe · 03/2024")]
res = mmf.import_library({"items": SECOND, "complete": ["purchase", "tribe"]})
own = mmf.own_tags()
assert own[4] == ["ork", "painted"], own[4]
assert own[7] == ["ork", "scifi"] and own[8] == ["elf", "fantasy"], own
assert res["autotagged"] == 2, res

# An item that drops out of a sync and comes back keeps its tags and isn't tagged again.
mmf.set_tags([3], [], ["goblin"])
mmf.import_library({"items": [x for x in SECOND if x["id"] != 3], "complete": ["purchase", "tribe"]})
assert not mmf.item(3)
assert mmf.import_library({"items": SECOND, "complete": ["purchase", "tribe"]})["autotagged"] == 0
assert mmf.own_tags()[3] == ["scifi"]

# Tags are spelled the way the library spells them.
c.execute("INSERT INTO model_tags(model_id, tag) VALUES ('m1', 'Fantasy')")
c.commit()
mmf.import_library({"items": SECOND + [item(9, "Wizard of the Woods", creator="Someone Else")],
                    "complete": ["purchase", "tribe"]})
assert mmf.own_tags()[9] == ["Fantasy", "human"], mmf.own_tags()[9]
print("autotag ok")
