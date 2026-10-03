"""Matching MyMiniFactory titles to local releases and models (no server needed).

    python tests/mmf_match_test.py
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ["DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, mmf  # noqa: E402

db.init()
LOCAL = [  # creator, release, model
    ("Bestiarum Miniatures", "Bestiarum - September 2023", "Swamp Troll"),
    ("Bestiarum Miniatures", "Bestiarum - September 2023", "Bog Witch"),
    ("Bestiarum Miniatures", "Bestiarum - October 2023", "Frost Giant"),
    ("Archvillain Games", "2023-09 Ironclad Dwarves", "Dwarf Captain"),
    ("Mini Forge", "Dragon Lords", "Red Dragon"),
    ("Mini Forge", "Dragon Lords", "Knight"),
    ("Titan Forge", "Witch - With Free Dragon Warhammer Mini", "Witch"),
    ("DM Stash", "Elf Wizard", "Elf Wizard"),
    ("Bite The Bullet", "BTB 39 23-09 Explorers Fellowship", "Ranger"),
    ("Lord of the Print", "Welcome Pack", "Goblins"),
    ("Lord of the Print", "Welcome Pack", "602ab09167439 angel-fighter"),
    ("Titan Forge", "Rogues", "Halfling Male Rogue"),
    ("Titan Forge", "Armoury", "Weapon"),
    ("", "Lich_King_Presupported", "Lich King"),
]
c = db.conn()
for i, (creator, release, model) in enumerate(LOCAL):
    c.execute("INSERT INTO files(rel_path, logical_path, name, ext, size, mtime, creator, release, model, model_id) "
              "VALUES (?,?,?,?,?,?,?,?,?,?)", (f"f{i}.stl", f"f{i}.stl", f"f{i}.stl", ".stl", 1, 0, creator, release,
                                               model, f"m{i}"))
c.commit()
index = mmf._local_index()

CASES = [  # MyMiniFactory title, creator, expected release (None = not in the library)
    ("Bestiarum Miniatures September 2023 Release", "Bestiarum Miniatures", "Bestiarum - September 2023"),
    ("202310 Bestiarum Bundle", "Bestiarum Miniatures", "Bestiarum - October 2023"),
    ("Swamp Troll - 32mm Presupported", "Bestiarum Miniatures", "Bestiarum - September 2023"),
    ("Ironclad Dwarves", "Archvillain Games", "2023-09 Ironclad Dwarves"),
    ("Red Dragon", "Someone Else", "Dragon Lords"),
    ("The Lich King (Supported)", "", "Lich_King_Presupported"),
    ("September 2023 Release", "Another Creator", None),  # only a date in common
    ("Black Knight", "Another Creator", None),            # one shared everyday word
    ("Frost Giant Jarl", "Bestiarum Miniatures", "Bestiarum - October 2023"),
    ("Zelina the Witch Empress - Female Sorceress", "TitanForge", None),  # one word of a long title
    ("Wood Elf Queen Sillavana (Elf Wizard Druid)", "TwinGoddessMini", None),  # another creator's model
    ("Explorers Fellowship", "Bite the Bullet", "BTB 39 23-09 Explorers Fellowship"),
    ("Welcome Pack", "Cast n Play", None),                  # every tribe has one
    ("Goblins (Pre-Supported)", "Cast n Play", None),       # one word, another creator
    ("Weapon Pack", "WargamesCrew", None),
    ("25mm Base for Miniatures", "Fireball Figurines", None),
    ("Halfling Rogue", "Nerikson", None),                   # close, but another creator
    ("Welcome Pack", "Lord of the Print", "Welcome Pack"),  # the creator's own
]
bad = 0
for title, creator, want in CASES:
    got = index.match(title, creator)
    got_release = got and got["release"]
    flag = "ok " if got_release == want else "BAD"
    bad += got_release != want
    print(f"{flag} {title!r} by {creator or '-'} -> {got_release!r}")
assert not bad, f"{bad} wrong matches"

# A creator renamed in the app is one creator: Lord of the Print now goes by Rescale Miniatures.
assert not index.match("Angel Fighter", "Rescale Miniatures")
mmf.rename_creator("Lord of the Print", "Rescale Miniatures")
got = mmf._local_index().match("Angel Fighter", "Rescale Miniatures")
assert got and got["release"] == "Welcome Pack", got
print("MATCH OK")
