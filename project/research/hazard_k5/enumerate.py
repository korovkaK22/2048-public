"""Enumerate 1024-reservoir placements: non-adjacent, working zone connectivity."""
from itertools import combinations
from collections import deque

CELLS = list(range(16))
def rc(i): return divmod(i, 4)
def idx(r, c): return r * 4 + c

def neighbours(i):
    r, c = rc(i)
    out = []
    for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        nr, nc = r + dr, c + dc
        if 0 <= nr < 4 and 0 <= nc < 4:
            out.append(idx(nr, nc))
    return out

NB = {i: neighbours(i) for i in CELLS}

def non_adjacent(sub):
    s = set(sub)
    return all(n not in s for i in sub for n in NB[i])

def components(zone):
    zs = set(zone)
    seen = set()
    comps = []
    for start in zone:
        if start in seen:
            continue
        q = deque([start]); comp = set([start]); seen.add(start)
        while q:
            x = q.popleft()
            for n in NB[x]:
                if n in zs and n not in seen:
                    seen.add(n); comp.add(n); q.append(n)
        comps.append(comp)
    return comps

# symmetry group of the square (8 elements) acting on cell indices
def transforms():
    def t_id(r, c): return (r, c)
    def t_r90(r, c): return (c, 3 - r)
    def t_r180(r, c): return (3 - r, 3 - c)
    def t_r270(r, c): return (3 - c, r)
    def t_fh(r, c): return (r, 3 - c)
    def t_fv(r, c): return (3 - r, c)
    def t_d1(r, c): return (c, r)
    def t_d2(r, c): return (3 - c, 3 - r)
    return [t_id, t_r90, t_r180, t_r270, t_fh, t_fv, t_d1, t_d2]

TR = transforms()

def canon(sub):
    best = None
    for t in TR:
        img = tuple(sorted(idx(*t(*rc(i))) for i in sub))
        if best is None or img < best:
            best = img
    return best

def show(sub):
    s = set(sub)
    return "\n".join("".join("X" if idx(r, c) in s else "." for c in range(4)) for r in range(4))

for k in (4, 5, 6, 7):
    all_na = [s for s in combinations(CELLS, k) if non_adjacent(s)]
    canon_all = {canon(s) for s in all_na}
    # working zone
    full = []   # zone fully connected
    best_zone = 0
    for s in canon_all:
        zone = [i for i in CELLS if i not in s]
        comps = components(zone)
        biggest = max(len(c) for c in comps)
        best_zone = max(best_zone, biggest)
        if len(comps) == 1:
            full.append(s)
    print(f"k={k}: non-adjacent placements={len(all_na)}, up to symmetry={len(canon_all)}, "
          f"working cells={16-k}, max connected zone={best_zone}, "
          f"FULLY-connected layouts (up to symmetry)={len(full)}")
    if k == 5:
        K5 = sorted(full)

print("\n=== all k=5 fully-connected (11-cell zone) layouts, up to symmetry ===")
for n, s in enumerate(K5):
    print(f"--- L{n:02d}  cells={s}")
    print(show(s))
