# Live setup — phone, ADB, calibration

Step-by-step guide from a bare Windows host to a bot playing 2048 inside
the monobank app (`com.ftband.mono`, reference screen 1080×2400).

Live play ALWAYS runs on the host. Docker is only for batch simulations
(see `project/docker/compose.yaml`).

## 1. Install adb + scrcpy

```bash
cd project
scripts/setup_adb.sh          # idempotent; --force to re-download
```

This puts Android platform-tools and scrcpy into `project/tools/`
(gitignored) and verifies `adb version`.

## 2. Enable USB debugging on the phone

1. Settings → About phone → tap **Build number** 7 times (unlocks
   Developer options).
2. Settings → Developer options → enable **USB debugging**.
   For long sessions also enable **Stay awake while charging** and keep the
   phone on a charger.
3. Connect via USB (mode "File transfer" if asked). Confirm the phone's
   "Allow USB debugging?" prompt — check **Always allow**.
4. Verify:

```bash
tools/platform-tools/adb.exe devices
# -> <serial>   device        (NOT "unauthorized" / "offline")
```

Optional live mirroring while the bot plays: `tools/scrcpy/scrcpy.exe`.

## 3. Install the device extras (host only)

```bash
./.venv/Scripts/pip install -e .[device]   # adbutils, opencv, pillow
```

## 4. Calibration screenshots

Raw screenshots live in `screens/` (already collected for the reference
device — `Screenshot_*_com.ftband.mono.jpg`). If you recalibrate (new
device/DPI), capture with the game open:

```bash
tools/platform-tools/adb.exe exec-out screencap -p > ../screens/board.png
```

States to capture:

- **Normal board** — several shots with different tile values on screen
  (at least 2, 4, 8, 16, 32, 64) so every palette color can be sampled;
- **Empty-ish board** right after "Нова гра" (empty-cell background color);
- **Game over popup** (popup with a button);
- **New-game confirmation popup** — tapping «Нова гра» opens «Почати нову
  гру? Прогрес буде втрачено» with «Так, нова гра» (blue, ≈ (540, 1532)),
  «Продовжити поточну гру» (≈ (540, 1682)) and «×» (≈ (173, 718)) — this is
  the `CONFIRM_NEW_GAME` screen state; restart flow is «Нова гра»
  (≈ (540, 2143)) → «Так, нова гра». Confirm exact coordinates here;
- **Any other popup/ad** if one appears;
- **Lockscreen** (for wake/unlock state detection).

UI landmarks on the reference device: header «Рахунок / Рекорд», timer above
the board, cat mascot overlapping the board's top edge (cell centers are not
covered), «Нова гра» button bottom-center, sound button bottom-right, close
«×» top-left. Details: `docs/device-ui-notes.md`.

## 5. Build the calibration file

```bash
./.venv/Scripts/bot2048 vision calibrate --image ../screens/<board>.jpg
# (skeleton for now — writes data/calibration.json when implemented)
./.venv/Scripts/bot2048 vision parse ../screens/<board>.jpg   # sanity check
```

`data/calibration.json` (gitignored) stores: board bbox, 16 cell centers,
palette `(rgb, exponent)` — schema in `src/bot2048/vision/calibration.py`.
Tile colors are clearly distinct (2=dark gray, 4=slate gray-violet, 8=blue,
16=teal, 32=green, 64=orange); exact RGB is always measured, never assumed.

## 6. Run

```bash
./.venv/Scripts/bot2048 live play --strategy expectimax --games 1
./.venv/Scripts/bot2048 live research --games 20     # spawn-rule measurement
```

Both are wired skeletons today: they validate calibration, construct the
runner and exit with code 2 explaining what is still missing.

## Minicap frame stream — investigated 2026-07-10 (result: ❌ minicap, ✅ scrcpy-server)

Goal was a fast frame source to replace `screencap` (~0.65 s/frame over WiFi-adb).

### Baseline measured on THIS device (192.0.2.10:5555, WiFi-adb, 1080×2400)
- `exec-out screencap -p` (PNG, ~442 KB): **~0.64–0.90 s/frame** (~1.4 fps).
- `exec-out screencap` (raw RGBA, ~10 MB): **~1.4–2.0 s/frame** — WORSE (10 MB
  transfer dominates the WiFi link). Do not use raw over WiFi.

### ❌ minicap does NOT work on this device (Android 13 / SDK 33 / arm64-v8a)
The only real prebuilt source is the npm package **`@devicefarmer/minicap-prebuilt`**
(latest `2.7.3`; the old `DeviceFarmer/minicap-prebuilt` GitHub repo is gone / 404).
Its `prebuilt/arm64-v8a/lib/` tops out at **android-30** — there is **no android-31/32/33**.
The `minicap` PIE binary loads, but every available `.so` fails the SurfaceFlinger
ABI link on SDK 33:
- android-30 → `cannot locate symbol "_ZN7android2ui4Size7INVALIDE"`
- android-29 → `... SurfaceComposerClient::getPhysicalDisplayToken ...`
- android-28 → `... SurfaceComposerClient::getBuiltInDisplay ...`

minicap.so is compiled against the internal (unstable) SurfaceFlinger C++ ABI per
SDK; there is no compatible prebuilt for Android 13, and building one needs the AOSP
tree. **Verdict: abandon minicap on this phone.**

### ✅ Working fast path: scrcpy-server raw H.264 stream (verified end-to-end)
scrcpy's server runs on Android 13, mirrors the display, and in `raw_stream` mode
emits a **pure H.264 elementary stream** on an abstract socket — decode it with any
H.264 decoder (pyav / opencv / ffmpeg). Verified: 120 frames decoded at native
**1080×2400 (H.264 High)**, frame pixel-identical to a simultaneous `screencap`.

The scrcpy **GUI client** (`scrcpy.exe` v4.0) segfaults on this Windows host right
after "Recording started" — ignore it; we drive the **server** directly, which is
what the Python client will do too.

Recipe (server jar comes from `scripts/setup_adb.sh` → `tools/scrcpy/scrcpy-server`,
this repo verified with scrcpy **v4.0**):

```bash
export MSYS_NO_PATHCONV=1                 # git-bash: stop /data/... path mangling
ADB="tools/platform-tools/adb.exe -s 192.0.2.10:5555"

$ADB push tools/scrcpy/scrcpy-server /data/local/tmp/scrcpy-server
$ADB forward tcp:1313 localabstract:scrcpy_01234567   # socket name = scrcpy_<scid>

# start server (video-only, raw H.264, no framing headers). Runs until the
# client disconnects. scid MUST be 8 hex chars and <= 7fffffff (it is parsed as
# a signed int — "ffffffff" throws NumberFormatException).
$ADB shell "CLASSPATH=/data/local/tmp/scrcpy-server app_process / \
  com.genymobile.scrcpy.Server 4.0 scid=01234567 log_level=info \
  audio=false control=false cleanup=false raw_stream=true send_dummy_byte=false \
  max_size=0 video_codec=h264 tunnel_forward=true video_bit_rate=8000000 max_fps=60"

# consumer connects to 127.0.0.1:1313 and feeds bytes to an H.264 decoder.
# quick proof with ffmpeg (host has C:\ffmpeg; bounded by frame count, not -t):
ffmpeg -f h264 -i tcp://127.0.0.1:1313 -frames:v 120 frame_%04d.png
```

Key flags / quirks:
- `raw_stream=true` disables device/codec/frame-meta headers; `send_dummy_byte=false`
  drops the leading `0x00` — so the socket is a clean H.264 stream from byte 0.
- `max_size=0` keeps **native 1080×2400** → existing vision calibration stays valid.
- `control=false audio=false` = capture-only (no input channel, no audio socket).
- The server **only emits frames when the screen changes** (+ periodic keyframes):
  ideal for the bot — a swipe/merge yields a fresh frame in ~1 frame-time; a static
  board sends almost nothing (do not treat "few frames" as a fault). Keep only the
  **latest** decoded frame for freshness.
- Client disconnect ⇒ server exits (must re-launch + re-`forward` per session).
- **Latency:** steady-state is a real-time video stream (scrcpy LAN latency ~35–70 ms
  per upstream docs); the win vs `screencap` is that a recent frame is *continuously*
  available with no ~0.65 s per-grab round-trip. (Our 120-frame capture measured
  ~7 fps, but that was content-static + host PNG-encode bound, NOT the pipeline.)
- **Decoder dep:** venv has `opencv 5.0` (ffmpeg-backed) + Pillow but **no `av`
  (pyav)** — `pip install av` for a clean socket decoder, or shell out to ffmpeg.
- **FLAG_SECURE:** not re-tested live (would force a re-login). Reasoning from how
  the compositor treats secure layers on non-secure captures: the monobank login
  screen should come through as an **all-black frame** (secure layers excluded),
  *not* the 0-byte result `screencap` gives. So under scrcpy, detect `AUTH_REQUIRED`
  by an **all-black frame + uiautomator anchors** («Уведіть ПІН-код»…), not by empty
  bytes. See `device-ui-notes.md` for the auth-detection anchors.

### Fallback also verified: `screenrecord` H.264 (on-device native, no linker risk)
`adb exec-out "screenrecord --output-format=h264 --time-limit=5 --bit-rate=8000000 -"`
streams a valid decodable 1080×2400 H.264 stream (confirmed with ffprobe). Simpler
(no jar to push) but inferior to scrcpy for a bot: inherent start-up buffering, a
hard **180 s** session cap (must restart), and no per-frame freshness control.
Prefer scrcpy-server; keep `screenrecord` only as an emergency capture path.

## Safety rules (do not break these)

- ⚠️ Android notifications slide in at the **top of the screen**; a gesture
  near the top can pull down the shade or open a notification. All swipes
  are generated by the `Humanizer` and clamped into a safe zone — the board
  bbox (≈ 25–75% of screen height) inset by a margin. Never bypass it.
- Taps only at **calibrated** button coordinates («Нова гра», popup button).
  No exploratory taps, ever.
- Game over is decided by the bot (`core.engine.is_game_over` on the parsed
  board), **not** by the popup — known app bug: the game may not stop when
  no moves remain.
