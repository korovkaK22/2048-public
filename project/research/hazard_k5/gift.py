"""PAIRED GIFTING on REAL boards — the decisive experiment.

Take a board the bot ACTUALLY stood on with k reservoirs and e empties.
Gift it one more 1024, placed on the SAFEST empty cell (the placement that
leaves the most safe directions). The gift is maximally generous:
    +1024 of mass, FREE, for the price of exactly ONE empty cell.
That is precisely the trade the mass model claims is a bargain.

Then play 100 moves from BOTH boards with the SAME spawn seed:
    control   = the real board            -> (k,   e)
    treatment = the real board + a 1024    -> (k+1, e-1)

Calibration: doing this to real k=3 boards yields k=4 boards, whose hazard is
ALREADY KNOWN from the 24 instrumented games (78/58/51/39). If the gifted
k=4 hazard reproduces that table, the procedure is validated — and the same
procedure applied to real k=4 boards gives a trustworthy k=5 number.
"""
import json, os, random, sys, time
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed
sys.path.insert(0, os.environ["BOT2048_SRC"])

RANK_1024 = 10

def safe_dirs(board):
    from bot2048.core import engine
    return [d for d in engine.legal_moves(board)
            if engine.max_exponent(engine.move(board, d).board) < 11]

def gift(board):
    """Put a 1024 on the empty cell that leaves the MOST safe directions
    (ties -> the cell with the fewest 1024 neighbours). Returns None if no
    placement leaves a viable board."""
    from bot2048.core import engine
    best, bestkey = None, None
    for c in range(16):
        if board[c] != 0:
            continue
        cells = list(board); cells[c] = RANK_1024
        b = tuple(cells)
        s = len(safe_dirs(b))
        if s == 0:
            continue
        r, cc = divmod(c, 4)
        adj1024 = sum(1 for dr, dc in ((-1,0),(1,0),(0,-1),(0,1))
                      if 0 <= r+dr < 4 and 0 <= cc+dc < 4
                      and board[(r+dr)*4 + (cc+dc)] == RANK_1024)
        key = (s, -adj1024)
        if bestkey is None or key > bestkey:
            bestkey, best = key, b
    return best

def rollout(board, seed, horizon=100):
    from bot2048.ai.go_engine import GoExpectimaxStrategy
    from bot2048.ai.strategies import NoLegalMoveError
    from bot2048.core import engine
    from bot2048.core.types import SpawnModel
    global _S
    try:
        strat = _S
    except NameError:
        strat = _S = GoExpectimaxStrategy(
            depth=6, p4=0.1, adaptive=False,
            binary=Path(os.environ["BOT2048_ENGINE2048_BIN"]), farm=True)
    rng = random.Random(seed)
    model = SpawnModel()
    moves, reason = 0, "survived"
    squeezed = plies = 0
    while moves < horizon:
        if engine.is_game_over(board):
            reason = "game_over"; break
        plies += 1
        if len(safe_dirs(board)) <= 1:
            squeezed += 1
        try:
            dec = strat.choose(board)
        except NoLegalMoveError:
            reason = "game_over"; break
        res = engine.move(board, dec.direction)
        if not res.moved:
            reason = "illegal"; break
        moves += 1
        if engine.max_exponent(res.board) >= 11:
            reason = "forced_2048"; break
        board, _ = engine.spawn_random(res.board, model, rng)
    return moves, reason, plies, squeezed

def job(args):
    """One real board -> a CHAIN of arms sharing the SAME spawn seed:
        gift0 = the real board, untouched          (k,   e)
        gift1 = + one free 1024 on an empty cell   (k+1, e-1)
        gift2 = + two free 1024s                   (k+2, e-2)
    Paired: the only difference between arms is the reservoir count."""
    snap, rep = args
    board = tuple(snap["board"])
    seed = hash((snap["seed"], snap["ply"], rep)) & 0x7FFFFFFF
    out = []
    b = board
    for ng in range(3):
        if ng > 0:
            b = gift(b) if b is not None else None
        if b is None:
            out.append({"arm": f"gift{ng}", "k": snap["k"] + ng,
                        "e": snap["e"] - ng, "src_seed": snap["seed"],
                        "ply": snap["ply"], "rep": rep, "moves": 0,
                        "reason": "no_safe_placement", "died": True,
                        "plies": 0, "squeezed": 0})
            continue
        m, r, p, sq = rollout(b, seed)
        out.append({"arm": f"gift{ng}", "k": snap["k"] + ng, "e": snap["e"] - ng,
                    "src_seed": snap["seed"], "ply": snap["ply"], "rep": rep,
                    "moves": m, "reason": r,
                    "died": r in ("game_over", "forced_2048"),
                    "plies": p, "squeezed": sq})
    return out

if __name__ == "__main__":
    snaps_path, out_path = sys.argv[1], sys.argv[2]
    reps = int(sys.argv[3])
    per_cell = int(sys.argv[4])   # max distinct snapshots per (k,e) cell
    snaps = [json.loads(l) for l in open(snaps_path)]
    # subsample: at most per_cell snapshots per (k,e), spread across seeds/plies
    from collections import defaultdict
    by = defaultdict(list)
    for s in snaps:
        by[(s["k"], s["e"])].append(s)
    rng = random.Random(7)
    chosen = []
    for key, lst in sorted(by.items()):
        rng.shuffle(lst)
        chosen += lst[:per_cell]
        print(f"(k={key[0]},e={key[1]}): {len(lst)} snaps -> using "
              f"{min(len(lst), per_cell)}", file=sys.stderr)
    jobs = [(s, r) for s in chosen for r in range(reps)]
    print(f"{len(jobs)} rollout pairs", file=sys.stderr)
    t0 = time.time(); done = 0
    with open(out_path, "w") as fh, ProcessPoolExecutor(max_workers=6) as ex:
        for res in as_completed([ex.submit(job, j) for j in jobs]):
            for r in res.result():
                fh.write(json.dumps(r) + "\n")
            fh.flush(); done += 1
            if done % 100 == 0:
                el = time.time() - t0
                print(f"{done}/{len(jobs)} {el:.0f}s eta {el/done*(len(jobs)-done):.0f}s",
                      file=sys.stderr, flush=True)
