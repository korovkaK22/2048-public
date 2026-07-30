import json, sys
from itertools import combinations
from collections import deque

def rc(i): return divmod(i, 4)
def idx(r, c): return r * 4 + c
def _nb(i):
    r, c = rc(i)
    return [idx(r+dr, c+dc) for dr, dc in ((-1,0),(1,0),(0,-1),(0,1))
            if 0 <= r+dr < 4 and 0 <= c+dc < 4]
NB = {i: _nb(i) for i in range(16)}
def non_adj(s):
    ss = set(s); return all(n not in ss for i in s for n in NB[i])
def conn(zone):
    zs = set(zone); seen = {zone[0]}; q = deque([zone[0]])
    while q:
        x = q.popleft()
        for n in NB[x]:
            if n in zs and n not in seen:
                seen.add(n); q.append(n)
    return len(seen) == len(zs)

def layouts(k):
    return [s for s in combinations(range(16), k)
            if non_adj(s) and conn([i for i in range(16) if i not in s])]

L5 = layouts(5)          # 36
L4 = layouts(4)          # fully-connected 12-cell zones
FOUR_CORNERS = (0, 3, 12, 15)
CANON5 = (0, 3, 5, 12, 15)   # the layout quoted in the task

what = sys.argv[1]
H, D = 100, 6
jobs = []

if what == "sweep36":          # rank all 36 k=5 placements
    n = int(sys.argv[2])       # runs per layout
    for li, L in enumerate(L5):
        for s in range(n):
            jobs.append([list(L), 2, 900000 + li * 1000 + s, H, D, "clean"])

elif what == "main":           # the headline table
    n = int(sys.argv[2])
    L = tuple(json.loads(sys.argv[3])) if len(sys.argv) > 3 else CANON5
    for e in range(5):
        for s in range(n):
            jobs.append([list(L), e, 100000 + e * 1000 + s, H, D, "clean"])

elif what == "ctl":
    # SAME generator at k=2,3,4 -> CALIBRATES the method against the measured
    # table (4% / 10% / 39% at e=3). Corner family, one 1024 added each step,
    # ending exactly at the k=5 layout under test.
    n = int(sys.argv[2])
    FAM = {2: (0, 3), 3: (0, 3, 12), 4: (0, 3, 12, 15), 5: CANON5}
    for k in (2, 3, 4):
        for e in range(5):
            for s in range(n):
                jobs.append([list(FAM[k]), e, 200000 + k * 100000 + e * 1000 + s,
                             H, D, "clean"])

elif what == "messy":          # structural pessimism control, k=5 and k=4
    n = int(sys.argv[2])
    for e in range(5):
        for s in range(n):
            jobs.append([list(CANON5), e, 300000 + e * 1000 + s, H, D, "messy"])
            jobs.append([list(FOUR_CORNERS), e, 400000 + e * 1000 + s, H, D, "messy"])

elif what == "k6":             # is k=6 really dead? best (9-cell) zone
    n = int(sys.argv[2])
    best = []
    for s in combinations(range(16), 6):
        if not non_adj(s):
            continue
        zone = [i for i in range(16) if i not in s]
        # largest component
        zs = set(zone); seen = set(); comps = []
        for st in zone:
            if st in seen: continue
            q = deque([st]); c = {st}; seen.add(st)
            while q:
                x = q.popleft()
                for nn in NB[x]:
                    if nn in zs and nn not in seen:
                        seen.add(nn); c.add(nn); q.append(nn)
            comps.append(c)
        if max(len(c) for c in comps) == 9:
            best.append(s)
    for li, L in enumerate(best):
        for e in range(5):
            for s in range(n):
                jobs.append([list(L), e, 500000 + li * 10000 + e * 1000 + s, H, D, "clean"])

json.dump(jobs, sys.stdout)
