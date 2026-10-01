"""Build a fake 'existing library' that mimics several creators' layouts."""
import io, sys, zipfile, py7zr
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import meshes as M

root = Path(sys.argv[1]); root.mkdir(parents=True, exist_ok=True)
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
with zipfile.ZipFile(root / "Space Rats/Rat Trooper.zip", "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr("Rat_Trooper_Presupported.stl", M.stl_bytes(M.with_supports(M.sphere(9, 50))))
    z.writestr("Rat_Trooper.stl", M.stl_bytes(M.sphere(9, 50)))
    z.writestr("Rat_Trooper.ctx", b"CTX header" + b"\x00" * 64)
with py7zr.SevenZipFile(root / "Necro Lords.7z", "w") as z:
    z.writestr(M.stl_bytes(M.with_supports(M.torus(n=160))), "Necro Lords/Lich King/STL/Supported/lich_king.stl")
    z.writestr(M.stl_bytes(M.torus(n=160)), "Necro Lords/Lich King/STL/Unsupported/lich_king.stl")
    z.writestr(M.ascii_stl(M.sphere(5, 30)), "Necro Lords/Skeleton/skeleton_ascii.stl")
    z.writestr(M.stl_bytes(fig), "Necro Lords/Skeleton/skeleton_shield.stl")
# Loose files at release level, named by part
w("Bits Pack/Orc_Warboss_Body.stl", fig)
w("Bits Pack/Orc_Warboss_Axe.stl", tor)
w("Bits Pack/Orc_Warboss_Head_sup.stl", M.with_supports(M.sphere(5, 40)))
w("Bits Pack/Banner.stl", tor)
print("sample library at", root)
