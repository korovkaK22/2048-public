"""Animation-timeline analyzer for early-spawn research.

Extracts frames from an on-device screen recording of a single 2048 swipe,
samples the 16 calibrated cell centers per frame, and reports:
  * motion_start  : first frame whose parsed board differs from frame 0
  * settle        : last frame at which the board changed (== animation end)
  * anim_ms       : settle - motion_start (full slide+merge+spawn duration)
  * per-cell motion contamination: how many DISTINCT non-empty classes each
    still-empty-at-start cell shows during motion (tiles sliding through the
    center -> false single-frame reads).

Usage: python analyze_anim.py <clip.mp4> [label]
Run from project/. Requires the bundled ffmpeg/ffprobe.
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import sys

from PIL import Image

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
FF = os.path.join(
    ROOT, "project/tools/ffmpeg/ffmpeg-master-latest-win64-gpl/bin/ffmpeg.exe"
)
FP = os.path.join(
    ROOT, "project/tools/ffmpeg/ffmpeg-master-latest-win64-gpl/bin/ffprobe.exe"
)
CAL = json.load(open(os.path.join(ROOT, "project/data/calibration.json")))
CENTERS = CAL["cell_centers"]
PAL = CAL["palette"]
EMPTY = (21, 21, 36)


def dist(a, b):
    return sum((p - q) ** 2 for p, q in zip(a, b)) ** 0.5


def patch(im, x, y, r=6):
    px = im.load()
    n = 0
    s = [0, 0, 0]
    for dx in range(-r, r + 1):
        for dy in range(-r, r + 1):
            c = px[x + dx, y + dy]
            s[0] += c[0]
            s[1] += c[1]
            s[2] += c[2]
            n += 1
    return (s[0] / n, s[1] / n, s[2] / n)


def classify(rgb):
    best = None
    bd = 1e9
    for p in PAL:
        d = dist(rgb, p["rgb"])
        if d < bd:
            bd = d
            best = (p["exponent"], d)
    return best


def analyze(mp4, label):
    fdir = os.path.join(ROOT, "tmp", "es_an_" + label)
    os.makedirs(fdir, exist_ok=True)
    for f in glob.glob(os.path.join(fdir, "*.png")):
        os.remove(f)
    subprocess.run(
        [FF, "-v", "error", "-i", mp4, "-fps_mode", "passthrough", "-q:v", "2",
         os.path.join(fdir, "f_%04d.png")],
        check=True,
    )
    pts = [
        float(x)
        for x in subprocess.check_output(
            [FP, "-v", "error", "-select_streams", "v:0", "-show_entries",
             "frame=pts_time", "-of", "csv=p=0", mp4]
        ).decode().split()
    ]
    frames = sorted(glob.glob(os.path.join(fdir, "f_*.png")))
    n = min(len(frames), len(pts))
    frames, pts = frames[:n], pts[:n]

    im0 = Image.open(frames[0]).convert("RGB")
    base = [classify(patch(im0, x, y))[0] for x, y in CENTERS]
    boards = []
    cellclass = {i: [] for i in range(16)}
    for fp, t in zip(frames, pts):
        im = Image.open(fp).convert("RGB")
        row = []
        for i, (x, y) in enumerate(CENTERS):
            e = classify(patch(im, x, y))[0]
            row.append(e)
            cellclass[i].append(e)
        boards.append((t, row))

    motion = next((t for t, r in boards if r != base), None)
    settle = None
    prev = boards[0][1]
    for t, r in boards[1:]:
        if r != prev:
            settle = t
        prev = r
    final = boards[-1][1]
    anim_ms = (settle - motion) * 1000 if (motion and settle) else None

    # motion contamination: cells empty at base AND empty in final (never a real
    # tile) but showing non-empty classes mid-motion == slide-through false reads
    contam = {}
    for i in range(16):
        if base[i] == 0 and final[i] == 0:
            seen = {c for t, c in zip(pts, cellclass[i]) if motion and settle
                    and motion <= t <= settle and c != 0}
            if seen:
                contam[i] = sorted(seen)
    return {
        "label": label, "n_frames": n, "base": base, "final": final,
        "motion_s": motion, "settle_s": settle, "anim_ms": anim_ms,
        "contam_cells": contam,
    }


if __name__ == "__main__":
    mp4 = sys.argv[1]
    label = sys.argv[2] if len(sys.argv) > 2 else os.path.basename(mp4).split(".")[0]
    r = analyze(mp4, label)
    print(json.dumps(r, indent=2))
