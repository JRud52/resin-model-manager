"""Procedural test meshes written as binary STL."""
import numpy as np, struct

def grid_surface(fn, nu, nv):
    u = np.linspace(0, 2*np.pi, nu+1); v = np.linspace(0, np.pi*2, nv+1)
    U, V = np.meshgrid(u, v, indexing="ij")
    P = fn(U, V)
    a, b, c, d = P[:-1,:-1], P[1:,:-1], P[1:,1:], P[:-1,1:]
    t1 = np.stack([a, b, c], 2); t2 = np.stack([a, c, d], 2)
    return np.concatenate([t1.reshape(-1,3,3), t2.reshape(-1,3,3)])

def torus(R=20, r=7, n=200, z=0):
    return grid_surface(lambda U,V: np.stack([(R+r*np.cos(V))*np.cos(U), (R+r*np.cos(V))*np.sin(U), r*np.sin(V)+z], -1), n, n//2)

def sphere(rad=10, n=120, c=(0,0,0)):
    def f(U,V):
        V = V/2
        return np.stack([c[0]+rad*np.sin(V)*np.cos(U), c[1]+rad*np.sin(V)*np.sin(U), c[2]+rad*np.cos(V)], -1)
    return grid_surface(f, n, n)

def cylinder(rad, h, n=64, c=(0,0,0)):
    def f(U,V):
        Z = V/(2*np.pi)*h
        return np.stack([c[0]+rad*np.cos(U), c[1]+rad*np.sin(U), c[2]+Z], -1)
    return grid_surface(f, n, 8)

def figure():
    return np.concatenate([cylinder(14, 3, 96), cylinder(5, 25, 48, (0,0,3)), sphere(7, 80, (0,0,33)),
                           cylinder(1.5, 18, 24, (8,0,15)), sphere(3, 30, (8,0,34))])

def with_supports(tris, n=30):
    rng = np.random.default_rng(1)
    zmin = tris[...,2].min()
    raft = cylinder(18, 2, 96, (0,0,zmin-8))
    sup = [cylinder(0.4, 6, 8, (x, y, zmin-6)) for x, y in rng.uniform(-10, 10, (n, 2))]
    return np.concatenate([tris, raft] + sup)

def write_stl(path, tris):
    tris = np.asarray(tris, dtype=np.float32)
    with open(path, "wb") as f:
        f.write(b"test".ljust(80, b" ")); f.write(struct.pack("<I", len(tris)))
        rec = np.zeros(len(tris), dtype=[("n","<f4",(3,)),("v","<f4",(3,3)),("a","<u2")])
        rec["v"] = tris; f.write(rec.tobytes())

def ascii_stl(tris):
    out = ["solid t"]
    for t in tris:
        out.append("facet normal 0 0 0\nouter loop")
        out += [f"vertex {x:e} {y:e} {z:e}" for x,y,z in t]
        out.append("endloop\nendfacet")
    out.append("endsolid t"); return "\n".join(out).encode()

def stl_bytes(tris):
    import io, tempfile, os
    fd, p = tempfile.mkstemp(suffix=".stl"); os.close(fd)
    write_stl(p, tris); b = open(p, "rb").read(); os.unlink(p); return b
