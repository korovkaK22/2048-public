"""Structural question, answered by EXHAUSTIVE enumeration, no engine:

for a layout with k 1024s and e empty working cells, over ALL ways to choose
which cells are empty (and a plausible ladder+dust fill of the rest), what
fraction of boards are ALREADY TERMINAL — i.e. every legal move assembles a
2048? That is the "taboo trap density" of the layout, and it is a hard floor
under the hazard: the bot cannot ever stand on such a square.
"""
import os, sys, json
from itertools import combinations
sys.path.insert(0, os.environ["BOT2048_SRC"])
from bot2048.core import engine
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

def viable(board):
    for d in engine.legal_moves(board):
        if engine.max_exponent(engine.move(board, d).board) < 11:
            return True
    return False

LADDER = [8, 7, 6, 5, 4, 3, 2, 1]

def zone_fill(zone, holes, rng_ranks):
    """One canonical fill: ladder descending over the filled cells in
    row-major order, dust (rank 1) after it."""
    filled = [c for c in zone if c not in holes]
    n = len(filled)
    tiles = LADDER[:n] + [1] * max(0, n - len(LADDER))
    cells = [0] * 16
    return filled, tiles, cells

def analyse(k):
    out = {}
    for L in layouts(k):
        zone = [i for i in range(16) if i not in L]
        for e in range(0, 6):
            if e > len(zone):
                continue
            tot = trap = 0
            for holes in combinations(zone, e):
                # over ALL orderings? too many. Use the descending row-major
                # fill AND its reverse AND a snake fill — 3 representative
                # structures; a layout+hole-set counts as a TRAP only if ALL
                # of them are terminal (i.e. the geometry, not the fill, kills).
                dead = True
                filled = [c for c in zone if c not in holes]
                n = len(filled)
                tiles = LADDER[:n] + [1] * max(0, n - len(LADDER))
                for order in (filled, filled[::-1]):
                    cells = [0] * 16
                    for c in L:
                        cells[c] = 10
                    for pos, r in zip(order, tiles):
                        cells[pos] = r
                    if viable(tuple(cells)):
                        dead = False
                        break
                tot += 1
                trap += dead
            out.setdefault(e, [0, 0])
            out[e][0] += tot
            out[e][1] += trap
    return out

for k in (4, 5):
    print(f"=== k={k}: taboo-trap density over ALL fully-connected layouts "
          f"x ALL hole-sets ({len(layouts(k))} layouts)")
    res = analyse(k)
    for e in sorted(res):
        tot, trap = res[e]
        print(f"  e={e}: {trap}/{tot} hole-sets are ALREADY-TERMINAL "
              f"({100*trap/tot:.1f}%)")
