"""Play REAL baseline games and snapshot every decision state with k in {3,4}.

These are boards the bot actually stood on — no synthetic-realism assumptions.
They are the substrate for the paired gifting experiment (gift.py).
"""
import json, os, random, sys, time
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed
sys.path.insert(0, os.environ["BOT2048_SRC"])

RANK_1024 = 10

def play(seed):
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
    board, _ = engine.new_game(model, rng)
    snaps = []
    ply = 0
    while ply < 6000:
        if engine.is_game_over(board):
            break
        k = sum(1 for x in board if x == RANK_1024)
        if k in (3, 4):
            e = sum(1 for x in board if x == 0)
            if e <= 5:
                snaps.append({"seed": seed, "ply": ply, "k": k, "e": e,
                              "board": list(board)})
        try:
            dec = strat.choose(board)
        except NoLegalMoveError:
            break
        res = engine.move(board, dec.direction)
        if not res.moved:
            break
        ply += 1
        if engine.max_exponent(res.board) >= 11:
            break
        board, _ = engine.spawn_random(res.board, model, rng)
    return {"seed": seed, "moves": ply, "snaps": snaps,
            "maxk": max((sum(1 for x in board if x == RANK_1024),), default=0)}

if __name__ == "__main__":
    n = int(sys.argv[1]); base = int(sys.argv[2]); out = sys.argv[3]
    t0 = time.time()
    got = 0
    with open(out, "w") as fh, ProcessPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(play, base + i) for i in range(n)]
        for j, f in enumerate(as_completed(futs)):
            r = f.result()
            for s in r["snaps"]:
                fh.write(json.dumps(s) + "\n")
            got += len(r["snaps"])
            fh.flush()
            print(f"{j+1}/{n} games  {r['moves']} moves  {len(r['snaps'])} snaps  "
                  f"total={got}  {time.time()-t0:.0f}s", file=sys.stderr, flush=True)
