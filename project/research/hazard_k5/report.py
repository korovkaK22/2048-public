import json, sys, math
from collections import defaultdict, Counter

def wilson(d, n, z=1.96):
    if n == 0: return (0.0, 0.0)
    p = d / n
    den = 1 + z*z/n
    c = (p + z*z/(2*n)) / den
    h = z*math.sqrt(p*(1-p)/n + z*z/(4*n*n)) / den
    return (max(0.0, c-h), min(1.0, c+h))

def load(path):
    return [json.loads(l) for l in open(path) if l.strip()]

def table(rows, keyf, label):
    g = defaultdict(list)
    for r in rows:
        if r.get("reason") == "no_viable_plant":
            continue
        g[keyf(r)].append(r)
    print(f"\n=== {label}")
    print(f"{'cell':<14} {'n':>5} {'P(death<=100)':>14} {'95% CI':>16} "
          f"{'median moves':>13}  reasons")
    for k in sorted(g):
        rs = g[k]
        n = len(rs); d = sum(1 for r in rs if r["died"])
        lo, hi = wilson(d, n)
        mv = sorted(r["moves"] for r in rs)
        med = mv[len(mv)//2]
        c = Counter(r["reason"] for r in rs)
        print(f"{str(k):<14} {n:>5} {100*d/n:>13.1f}% "
              f"{'['+format(100*lo,'.0f')+'-'+format(100*hi,'.0f')+'%]':>16} "
              f"{med:>13}  {dict(c)}")

for path, label, kf in [
    ("res_ctl.jsonl", "CONTROLS: same generator at k=2,3,4 (calibration)",
     lambda r: f"k={r['k']} e={r['e']}"),
    ("res_main.jsonl", "MAIN: k=5, layout (0,3,5,12,15), clean ladder",
     lambda r: f"k=5 e={r['e']}"),
    ("res_messy.jsonl", "MESSY control (ranks scattered at random)",
     lambda r: f"k={r['k']} e={r['e']}"),
]:
    try:
        table(load(path), kf, label)
    except FileNotFoundError:
        print(f"\n=== {label}: not finished")

# layout sweep
try:
    rows = load("res_sweep.jsonl")
    g = defaultdict(list)
    for r in rows:
        if r.get("reason") == "no_viable_plant": continue
        g[tuple(r["layout"])].append(r)
    print(f"\n=== SWEEP: all 36 k=5 layouts at e=2, ranked by survival")
    out = []
    for L, rs in g.items():
        n = len(rs); d = sum(1 for r in rs if r["died"])
        lo, hi = wilson(d, n)
        mv = sum(r["moves"] for r in rs)/n
        out.append((d/n, lo, hi, n, mv, L))
    out.sort()
    for p, lo, hi, n, mv, L in out:
        grid = "".join(("X" if r*4+c in L else ".") + ("|" if c==3 else "")
                       for r in range(4) for c in range(4))
        print(f"  P={100*p:5.1f}% [{100*lo:4.0f}-{100*hi:4.0f}%] n={n:3d} "
              f"mean_moves={mv:5.1f}  {grid}  {L}")
except FileNotFoundError:
    print("\n=== SWEEP: not finished")
