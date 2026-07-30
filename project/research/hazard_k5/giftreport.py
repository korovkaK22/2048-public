import json, math
from collections import defaultdict, Counter

def wilson(d, n, z=1.96):
    if n == 0: return (0.0, 0.0)
    p = d / n; den = 1 + z*z/n
    c = (p + z*z/(2*n)) / den
    h = z*math.sqrt(p*(1-p)/n + z*z/(4*n*n)) / den
    return (max(0., c-h), min(1., c+h))

rows = [json.loads(l) for l in open("res_gift.jsonl")]

OBS = {  # the measured table from 24 instrumented games (FACTS §5)
    (2,0):.48,(2,1):.26,(2,2):.09,(2,3):.04,(2,4):.02,(2,5):.003,
    (3,0):.57,(3,1):.36,(3,2):.19,(3,3):.10,(3,4):.02,(3,5):.009,
    (4,0):.78,(4,1):.58,(4,2):.51,(4,3):.39,
}

SRCK = {}   # (src_seed, ply) -> k of the real board it came from
for r in rows:
    if r["arm"] == "gift0":
        SRCK[(r["src_seed"], r["ply"])] = r["k"]

def block(pred, title):
    g = defaultdict(list)
    for r in rows:
        if pred(r):
            g[(r["k"], r["e"])].append(r)
    if not g:
        return
    print(f"\n--- {title}")
    print(f"{'k':>2} {'e':>2} {'n':>5} {'P(death)':>9} {'95% CI':>13} "
          f"{'observed':>9}  {'games':>5}  reasons")
    for (k, e) in sorted(g):
        rs = g[(k, e)]
        n = len(rs); d = sum(1 for r in rs if r["died"])
        lo, hi = wilson(d, n)
        obs = OBS.get((k, e))
        ng = len({r["src_seed"] for r in rs})
        c = Counter(r["reason"] for r in rs)
        print(f"{k:>2} {e:>2} {n:>5} {100*d/n:>8.1f}% "
              f"{'['+format(100*lo,'.0f')+'-'+format(100*hi,'.0f')+']':>13} "
              f"{(format(100*obs,'.0f')+'%') if obs is not None else '  —':>9}  "
              f"{ng:>5}  {dict(c)}")

print("P(death <= 100 moves). Rollouts from REAL bot boards, depth 6, farm.")
block(lambda r: r["arm"] == "gift0",
      "CONTROL: real board, UNTOUCHED  (validates the harness vs the measured table)")
block(lambda r: r["arm"] == "gift1" and SRCK.get((r["src_seed"], r["ply"])) == 3,
      "real k=3 board + ONE free 1024  ->  k=4  (validates the GIFTING procedure)")
block(lambda r: r["arm"] == "gift1" and SRCK.get((r["src_seed"], r["ply"])) == 4,
      "real k=4 board + ONE free 1024  ->  k=5   <<< THE MOST REALISTIC k=5")
block(lambda r: r["arm"] == "gift2" and SRCK.get((r["src_seed"], r["ply"])) == 3,
      "real k=3 board + TWO free 1024s ->  k=5   <<< THE ANSWER (large n)")
block(lambda r: r["arm"] == "gift2" and SRCK.get((r["src_seed"], r["ply"])) == 4,
      "real k=4 board + TWO free 1024s ->  k=6  (is k=6 really dead?)")

# paired: same source board+seed, k vs k+1
print("\nPAIRED (same real board, same spawn seed, only the gift differs):")
pair = defaultdict(dict)
for r in rows:
    pair[(r["src_seed"], r["ply"], r["rep"])][r["arm"]] = r
for a, b in (("gift0", "gift1"), ("gift1", "gift2"), ("gift0", "gift2")):
    both = [p for p in pair.values() if a in p and b in p]
    if not both: continue
    da = sum(1 for p in both if p[a]["died"]); db = sum(1 for p in both if p[b]["died"])
    ma = sum(p[a]["moves"] for p in both) / len(both)
    mb = sum(p[b]["moves"] for p in both) / len(both)
    ka = both[0][a]["k"]; kb = both[0][b]["k"]
    print(f"  n={len(both):4d}  {a}(k={ka}): death {100*da/len(both):5.1f}%  "
          f"mean {ma:5.1f} moves   ->   {b}(k={kb}): death {100*db/len(both):5.1f}%  "
          f"mean {mb:5.1f} moves")
