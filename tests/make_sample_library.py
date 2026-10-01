"""Build a fake 'existing library' that mimics several creators' layouts."""
import io, sys, zipfile, py7zr
from PIL import Image
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import meshes as M

root = Path(sys.argv[1]); root.mkdir(parents=True, exist_ok=True)
def pic(color, fmt="JPEG"):
    buf = io.BytesIO(); Image.new("RGB", (640, 480), color).save(buf, fmt); return buf.getvalue()
def wpic(rel, color, fmt="JPEG"):
    p = root / rel; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(pic(color, fmt))
def w(rel, tris):
    p = root / rel; p.parent.mkdir(parents=True, exist_ok=True); M.write_stl(p, tris)
fig, tor = M.figure(), M.torus(n=120)

# Creator A: Release / Supported|Unsupported / Model / parts
for sup, fn in (("Supported", M.with_supports), ("Unsupported", lambda t: t)):
    w(f"Dragon Lords/{sup}/Red Dragon/Red_Dragon_Body.stl", fn(fig))
    w(f"Dragon Lords/{sup}/Red Dragon/Red_Dragon_Wing_L.stl", fn(tor))
    w(f"Dragon Lords/{sup}/Knight/Knight.stl", fn(M.sphere(10, 60)))
(root / "Dragon Lords/Lychee").mkdir(parents=True, exist_ok=True)
(root / "Dragon Lords/Lychee/Knight.lys").write_bytes(b"\x00LYS fake scene")

# Creator B: Release / Model / Model Presupported / Heads options + zip of release
w("Goblin Warband/Goblin Boss/Goblin Boss Presupported/Heads/Head_A_sup.stl", M.with_supports(M.sphere(6, 40)))
w("Goblin Warband/Goblin Boss/Goblin Boss Presupported/Heads/Head_B_sup.stl", M.with_supports(M.sphere(8, 40)))
w("Goblin Warband/Goblin Boss/Goblin Boss Presupported/Body_sup.stl", M.with_supports(fig))
w("Goblin Warband/Goblin Boss/Goblin Boss Unsupported/Body.stl", fig)

# Creator C: zipped models inside a release folder, plus a fully 7zipped release
(root / "Space Rats").mkdir(exist_ok=True)
with zipfile.ZipFile(root / "Space Rats/Rat Captain.zip", "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr("Rat Captain/Supported/rat_captain_sup.stl", M.stl_bytes(M.with_supports(fig)))
    z.writestr("Rat Captain/Unsupported/rat_captain.stl", M.stl_bytes(fig))
    z.writestr("Rat Captain/Unsupported/rat_captain_gun_option1.stl", M.stl_bytes(tor))
    z.writestr("__MACOSX/Rat Captain/._rat_captain.stl", b"junk")
    z.writestr("Rat Captain/rat_captain_preview.jpg", pic("orange"))
    z.writestr("__MACOSX/Rat Captain/._rat_captain_preview.jpg", b"junk")
with zipfile.ZipFile(root / "Space Rats/Rat Trooper.zip", "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr("Rat_Trooper_Presupported.stl", M.stl_bytes(M.with_supports(M.sphere(9, 50))))
    z.writestr("Rat_Trooper.stl", M.stl_bytes(M.sphere(9, 50)))
    z.writestr("Rat_Trooper.ctx", b"CTX header" + b"\x00" * 64)
with py7zr.SevenZipFile(root / "Necro Lords.7z", "w") as z:
    z.writestr(M.stl_bytes(M.with_supports(M.torus(n=160))), "Necro Lords/Lich King/STL/Supported/lich_king.stl")
    z.writestr(M.stl_bytes(M.torus(n=160)), "Necro Lords/Lich King/STL/Unsupported/lich_king.stl")
    z.writestr(M.ascii_stl(M.sphere(5, 30)), "Necro Lords/Skeleton/skeleton_ascii.stl")
    z.writestr(M.stl_bytes(fig), "Necro Lords/Skeleton/skeleton_shield.stl")
    z.writestr(pic("purple", "WEBP"), "Necro Lords/Lich King/lich_king.webp")
    z.writestr(pic("black", "PNG"), "Necro Lords/Necro Lords Promo.png")
# Loose files at release level, named by part
# Creator E: the creator's folder repeated inside the release
w("Explorers Fellowship/Bite The Bullet/Dwarf Ranger/Dwarf_Ranger.stl", fig)
w("Explorers Fellowship/Bite The Bullet/Elf Scout/Elf_Scout.stl", fig)
# A Freebies folder holding a creator's release and a release with no creator
w("Freebies/Bite The Bullet/Free Goblin/Goblin/Goblin.stl", fig)
w("Freebies/Holiday Elf/Elf/Elf.stl", fig)
w("Bits Pack/Orc_Warboss_Body.stl", fig)
w("Bits Pack/Orc_Warboss_Axe.stl", tor)
w("Bits Pack/Orc_Warboss_Head_sup.stl", M.with_supports(M.sphere(5, 40)))
w("Bits Pack/Banner.stl", tor)

# Preview pictures that ship with releases and models.
wpic("Dragon Lords/Dragon Lords Cover.jpg", "red")                        # release picture
wpic("Dragon Lords/Supported/Red Dragon/Red_Dragon_render.png", "green", "PNG")  # inside the model folder
wpic("Dragon Lords/Renders/Knight_front.jpg", "silver")                   # named after a model
wpic("Bits Pack/Orc_Warboss.jpg", "olive")                                # next to loose files
wpic("Goblin Warband/goblin_banner.png", "brown", "PNG")                  # single-model release
(root / "Bits Pack/broken.png").write_bytes(b"not a picture")
# Model pictures kept at release level, named in different ways.
wpic("Dragon Lords/Images/DL_RedDragon_Promo.jpg", "maroon")              # prefix + CamelCase
wpic("Dragon Lords/Images/Knight/01.jpg", "gray")                         # folder named after the model
wpic("Dragon Lords/Dragon Lords - Knight.jpg", "white")                   # release name + model
wpic("Space Rats/previews/02_rat_trooper_painted.png", "yellow", "PNG")   # numbering + picture words
wpic("Bits Pack/orc.jpg", "lime")                                         # start of one model's name
print("sample library at", root)
