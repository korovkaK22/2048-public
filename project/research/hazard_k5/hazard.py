"""Measure P(death <= H moves | k reservoirs, e empty) by PLANTING boards.

Plants a board with k 1024-tiles in a given layout, a descending ladder +
dust in the working zone, exactly `e` empty cells, and a plausible working
mass (~510-530, which the mass model predicts for k=5), then hands it to the
production farm engine (depth 6, adaptive off, p4=0.1) and plays until
game over / forced-2048 / H moves.

Death = game_over OR tile_cap (forced 2048). Both end the game.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from collections import deque
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

SRC = os.environ["BOT2048_SRC"]
sys.path.insert(0, SRC)

RANK_1024 = 10

# ---------------------------------------------------------------- geometry
def rc(i): return divmod(i, 4)
def idx(r, c): return r * 4 + c

def _nb(i):
    r, c = rc(i)
    return [idx(r + dr, c + dc) for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1))
            if 0 <= r + dr < 4 and 0 <= c + dc < 4]

NB = {i: _nb(i) for i in range(16)}

# snake weight the engine itself uses: boustrophedon from top-left, 0.85^step
def _snake_order():
    order = []
    for r in range(4):
        cols = range(4) if r % 2 == 0 else range(3, -1, -1)
        for c in cols:
            order.append(idx(r, c))
    return order

SNAKE_POS = {cell: 0.85 ** step for step, cell in enumerate(_snake_order())}


def connected(zone):
    zs = set(zone)
    seen = {zone[0]}
    q = deque([zone[0]])
    while q:
        x = q.popleft()
        for n in NB[x]:
            if n in zs and n not in seen:
                seen.add(n); q.append(n)
    return len(seen) == len(zs)


def walk(zone, rng):
    """Greedy descending walk through the working zone, anchored at the
    zone cell the engine's snake gradient likes most. Ties broken randomly.
    The ladder is laid along this path (big -> small), so the planted board
    is a board the heuristic itself considers well-formed."""
    zs = set(zone)
    start = max(zone, key=lambda c: (SNAKE_POS[c], rng.random()))
    path = [start]
    seen = {start}
    while len(path) < len(zone):
        cand = [n for n in NB[path[-1]] if n in zs and n not in seen]
        if not cand:
            # dead end: jump to the best unvisited cell adjacent to anything visited
            cand = [c for c in zone if c not in seen
                    and any(n in seen for n in NB[c])]
            if not cand:
                cand = [c for c in zone if c not in seen]
        nxt = max(cand, key=lambda c: (SNAKE_POS[c], rng.random()))
        path.append(nxt); seen.add(nxt)
    return path


# ---------------------------------------------------------------- planting
LADDER = [8, 7, 6, 5, 4, 3, 2, 1]  # 256,128,64,32,16,8,4,2  -> mass 510


def _tiles_for(nfill, rng):
    """The multiset of working tiles: a descending ladder (256..2 = mass 510,
    what the mass model predicts for k=5) plus spawn-like dust to fill up."""
    ladder = LADDER[:nfill] if nfill <= len(LADDER) else list(LADDER)
    dust = []
    while len(ladder) + len(dust) < nfill:
        dust.append(rng.choice([1, 1, 1, 2]))   # 2s and 4s, 9:1 -> ~3:1 surviving
    return ladder, dust


def viable(board):
    """A state the bot could actually be sitting in: at least one legal move
    that does NOT assemble a 2048. A board whose every legal move creates a
    2048 is ALREADY terminal under the monobank rule (taboo trap) — it is a
    death, not a state with a hazard."""
    from bot2048.core import engine
    for d in engine.legal_moves(board):
        if engine.max_exponent(engine.move(board, d).board) < 11:
            return True
    return False


def plant(layout, e, rng, mode="clean", tries=200):
    """k = len(layout) 1024s; working zone gets a descending ladder + dust,
    leaving exactly `e` empties. Boards that are ALREADY dead (no legal move)
    are rejected and resampled — a real board with e empties is, by
    definition, one the bot was alive on.

    mode="clean": ladder laid along the engine's own snake gradient from the
                  best-liked zone cell; dust+empties scattered over the tail.
    mode="messy": every working tile placed at random in the zone (same
                  multiset, same mass) — a pessimistic structural control.
    """
    zone = [i for i in range(16) if i not in layout]
    nfill = len(zone) - e
    rejects = 0
    if nfill < 0:
        return None, 0
    for _ in range(tries):
        ladder, dust = _tiles_for(nfill, rng)
        cells = [0] * 16
        for c in layout:
            cells[c] = RANK_1024
        # WHICH cells stay empty is sampled, not fixed: the far end of the
        # snake is likelier to be empty (that is where a real board's holes
        # are), but every subset has support, so the generator is not pinned
        # to one degenerate empty-set.
        order = walk(zone, rng)                       # anchor -> far end
        if mode == "messy":
            holes = set(rng.sample(zone, e))          # uniform
        else:
            w = {c: (i + 1.0) ** 2 for i, c in enumerate(order)}  # tail-biased
            holes, pool = set(), list(zone)
            for _ in range(e):
                tot = sum(w[c] for c in pool)
                x, acc = rng.random() * tot, 0.0
                for c in pool:
                    acc += w[c]
                    if acc >= x:
                        holes.add(c); pool.remove(c); break
        filled = [c for c in order if c not in holes]
        if mode == "messy":
            rng.shuffle(filled)                       # ranks scattered at random
        for pos, rank in zip(filled, ladder + dust):  # ladder big -> small
            cells[pos] = rank
        board = tuple(cells)
        rejects += 1
        if viable(board):                             # alive on arrival
            return board, rejects - 1
    return None, rejects


def board_mass(board):
    return sum(1 << r for r in board if r)


# ---------------------------------------------------------------- one run
def run_cell(job):
    layout, e, seed, horizon, depth, mode = job
    from bot2048.ai.go_engine import GoExpectimaxStrategy
    from bot2048.ai.strategies import NoLegalMoveError
    from bot2048.core import engine
    from bot2048.core.types import SpawnModel

    global _STRAT
    try:
        strat = _STRAT
    except NameError:
        strat = _STRAT = GoExpectimaxStrategy(
            depth=depth, p4=0.1, adaptive=False,
            binary=Path(os.environ["BOT2048_ENGINE2048_BIN"]), farm=True,
        )

    rng = random.Random(seed)
    model = SpawnModel()
    board, rejects = plant(layout, e, rng, mode=mode)
    if board is None:
        return {"layout": list(layout), "k": len(layout), "e": e, "seed": seed,
                "mode": mode, "reason": "no_viable_plant", "died": None,
                "rejects": rejects, "moves": 0}
    start_mass = board_mass(board)
    work_mass = start_mass - RANK_1024_VAL * len(layout)

    moves = 0
    reason = "survived"
    plies = 0            # decision states seen
    taboo_hit = 0        # states where the taboo removed >=1 legal direction
    squeezed = 0         # states with <=1 SAFE direction
    safe_sum = 0
    while moves < horizon:
        if engine.is_game_over(board):
            reason = "game_over"
            break
        legal = engine.legal_moves(board)
        safe = [d for d in legal
                if engine.max_exponent(engine.move(board, d).board) < 11]
        plies += 1
        safe_sum += len(safe)
        if len(safe) < len(legal):
            taboo_hit += 1
        if len(safe) <= 1:
            squeezed += 1
        try:
            dec = strat.choose(board)
        except NoLegalMoveError:
            reason = "game_over"
            break
        res = engine.move(board, dec.direction)
        if not res.moved:
            reason = "illegal"
            break
        moves += 1
        if engine.max_exponent(res.board) >= 11:
            board = res.board
            reason = "forced_2048"
            break
        board, _ = engine.spawn_random(res.board, model, rng)

    died = reason in ("game_over", "forced_2048")
    return {
        "layout": list(layout), "k": len(layout), "e": e, "seed": seed,
        "mode": mode, "moves": moves, "reason": reason, "died": died,
        "work_mass": work_mass, "rejects": rejects,
        "k_final": sum(1 for r in board if r == RANK_1024),
        "plies": plies, "taboo_hit": taboo_hit, "squeezed": squeezed,
        "safe_sum": safe_sum,
    }


RANK_1024_VAL = 1024


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", required=True, help="json file with job list")
    ap.add_argument("--out", required=True)
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    jobs = [tuple(j) if not isinstance(j, list) else (tuple(j[0]), *j[1:])
            for j in json.loads(Path(args.jobs).read_text())]
    t0 = time.time()
    done = 0
    with open(args.out, "w") as fh, ProcessPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(run_cell, j) for j in jobs]
        for f in as_completed(futs):
            r = f.result()
            if r is not None:
                fh.write(json.dumps(r) + "\n"); fh.flush()
            done += 1
            if done % 50 == 0:
                el = time.time() - t0
                print(f"{done}/{len(jobs)}  {el:.0f}s  eta {el/done*(len(jobs)-done):.0f}s",
                      file=sys.stderr, flush=True)
    print(f"done {done} in {time.time()-t0:.0f}s", file=sys.stderr)


if __name__ == "__main__":
    main()
