# On-device vision offload — design & feasibility

> Мета: прибрати передачу повних кадрів 1080×2400 на хост щоходу. Ідея оператора —
> рахувати **дешеву кольорову математику НА ТЕЛЕФОНІ** (16 центрів клітинок →
> класифікація по палітрі) і слати хосту крихітний **board-пакет (22 байти)**, а
> повний кадр тягнути лише раз на ~10 с як контроль (score/timer OCR, popup/auth,
> дрейф палітри). Кінцева ціль пайплайна: **overhead ≤ 50 мс/хід**, щоб пляшковим
> горлом став сам фізичний свайп.
>
> **TL;DR рекомендація (2 фази):**
> 1. **Зараз, майже безкоштовно:** `scrcpy --crop` на область дошки+score →
>    існуючий H.264-шлях кодує/везе ~5–6× менше пікселів, хост декодує й семплить
>    менший кадр. Нуль нового коду на телефоні. ~90 % виграшу.
> 2. **Ендшпіль (ідея оператора):** маленький helper-APK з **MediaProjection**,
>    що семплить 16 клітинок на телефоні й пушить 22-байтні пакети через
>    `adb forward` — без root, без AccessibilityService, без перехоплення трафіку.
>    Overhead на хості ~1 мс; свайп стає пляшковим горлом.
>
> Прототип (перевірено офлайн): `src/bot2048/ondevice/` (packet + reference
> sampler) + `tests/ondevice/`, і reference-APK у `project/ondevice-apk/`.

## 1. Feasibility check that gates everything (verified offline)

The whole idea rests on one claim: **color-only sampling on the phone must
reproduce the host's authoritative digits+color parser.** It does. The Python
reference sampler (`bot2048.ondevice.sampler`, reusing the exact color path of
`bot2048.vision.parser`) was run against every lossless PNG device frame in
`tests/vision/fixtures`:

| frames tested | color-only == digit parser | worst nearest-palette distance |
|---|---|---|
| 10 / 10 settled boards (incl. `board_256.png`, `board_d_labeled.png`) | **exact match on all 16 cells** | **≤ 1.4** (band is ~40 to the nearest wrong tile) |

The one disagreement found anywhere — `logs/board_check.png` cell 15 — is a cell
whose face is pure green `(29,174,93)` = tile 32 at **distance 0.0**, where the
*digit* parser misread "2". Color-only was arguably right; and it pinpoints why
the 10 s digit control-check exists (reconcile the two signals, auto-learn).

Color-only's only real blind spot is a tile whose exponent is **not in the
palette yet** — it snaps confidently to the nearest known color. That is exactly
what the periodic host-side full-frame digit read catches and auto-learns
(`vision.parser.learn_tile_color`). The packet carries a per-cell `uncertain`
mask (nearest distance > 20) so the host knows which cells to re-verify.

**Conclusion: the design is sound.** The rest is choosing the execution host.

## 2. Options (root NOT allowed; APK install now allowed; no accessibility, no traffic intercept)

| # | Option | New device code | Needs consent/app | Capture works? | Verdict |
|---|---|---|---|---|---|
| A | `screencap -p` full frame (today's baseline) | none | no | yes | too slow (~0.65 s/frame) |
| B | scrcpy full-frame H.264 stream (current primary) | none | no | yes (board), black under FLAG_SECURE | works; host decodes full frame |
| **C** | **scrcpy `--crop` board region + host sample** | none (config) | no | yes | **ship first — 90 % win, ~0 effort** |
| **D** | **helper APK, MediaProjection + on-device sample** | Kotlin APK | 1 consent tap | yes (board), black under FLAG_SECURE | **endgame winner — lowest latency, matches operator's idea** |
| E | `app_process` jar sampler (scrcpy-style, on-device sample) | Java/jar | no (launched like scrcpy) | yes | strong alt to D; no app on phone, but fragile non-SDK SurfaceControl reflection |
| F | pushed NDK arm64 binary in `/data/local/tmp` | native | no | **no** | rejected (see below) |
| G | Termux / Termux:API | app + scripts | app install | **no** | rejected (see below) |

### Why F (native binary) is rejected
Executing a pushed static arm64 binary from `/data/local/tmp` as the **shell**
user is itself fine on SDK 33 (the standard `frida-server`/`gdbserver`/`simpleperf`
mechanism; the W^X/exec-from-app-dir restriction hits *untrusted_app*, not the
shell domain). The killer is elsewhere: **a shell-domain binary cannot read
display pixels without root.** `/dev/graphics/fb0` is gone on modern devices and
SurfaceFlinger screenshot needs `READ_FRAME_BUFFER`/`CAPTURE` (a privileged
domain). So the binary would have to shell out to `screencap` for every
frame — and each fresh `adb shell screencap` pays the repo-measured **~200 ms adb
round-trip** (docs/device-ui-notes.md). As a resident daemon it still can't get
pixels without `screencap`. No path to the 50 ms budget. Dead end.

### Why G (Termux) is rejected
Termux runs in the *untrusted_app* domain (app UID), so `screencap` from Termux
is permission-denied — Termux famously **cannot screenshot without root**, for the
same capture-permission reason as F. It also means a second general-purpose app on
the banking phone for zero capture benefit. Rejected.

### Why D (MediaProjection) is the endgame winner over E (app_process jar)
Both capture the display without root and can sample on-device. Trade-off:

- **D (APK):** uses the *public* `MediaProjection` API — stable across Android
  versions, minimal code, minimal permissions. Cost: one consent tap per session
  and a foreground-service notification; a process kill loses the grant.
- **E (jar):** launched exactly like scrcpy-server (`app_process`, shell uid) —
  **no consent dialog, no app installed, no notification**. Cost: it must reflect
  against **non-SDK, per-version `SurfaceControl`/`DisplayManager` internals**
  (this is most of scrcpy-server's complexity), so it is far more brittle to build
  and maintain than D.

With APK install now allowed and the operator doing setup by hand, **D's one-tap
consent is a cheaper, more robust path than E's version-fragile reflection.** E
remains the fallback if the consent lifecycle ever becomes a problem (e.g. fully
unattended long sessions).

## 3. Latency budget vs the 50 ms step overhead

Per-move overhead = everything except the physical swipe. Numbers below:
`screencap`/adb round-trips and scrcpy LAN latency from docs/live-setup.md +
docs/ops-playbook.md; host sample/codec measured on this host (see §5); on-device
sample is a tight Java/C loop over 16 small regions of a raw buffer (no H.264
decode, no full-frame numpy alloc → sub-ms).

| Option | capture → available | classify | packet/transfer to host | host decode+parse | **per-move overhead** | swipe is bottleneck? |
|---|---|---|---|---|---|---|
| A screencap full | ~650 ms (PNG+WiFi) | — | in-frame | ~26 ms | **~680 ms** | no |
| B scrcpy full stream | continuous; swipe reflects in ~35–70 ms | — | compressed frame | decode ~10–20 ms + sample ~26 ms | **~35–45 ms compute** (+stream lag) | borderline |
| **C scrcpy --crop** | continuous; ~35–70 ms | — | smaller compressed frame | decode ~3–6 ms + sample ~5 ms | **~10 ms compute** (+stream lag) | mostly |
| **D MediaProjection APK** | on-device, VSYNC ~16 ms | ~1–3 ms on device | 22 B over persistent socket, <1 ms | decode packet <0.1 ms | **~1 ms host + ~20–30 ms move→packet** | **yes** |
| E app_process jar | same as D | ~1–3 ms | 22 B, <1 ms | <0.1 ms | same as D | yes |

Key points:
- The 22-byte packet on a **persistent** `adb forward` socket removes the
  ~200 ms per-move adb-spawn overhead entirely — the reason F fails and D/E win.
- Host-side work in D/E collapses to "read 22 bytes, CRC, unpack nibbles"
  (~40 µs measured for encode+decode). The strategy/engine then dominate host
  time, and the **physical swipe (~60 ms `input swipe`) becomes the bottleneck**,
  exactly the stated goal.
- C gets most of the transfer/decode win for zero device code by shrinking the
  encoded region; it does **not** remove scrcpy's ~35–70 ms stream latency, so it
  is "good enough / ship now," not the 50 ms endgame.

### Failure modes & the 10 s control check
All fast paths are **equally blind to the auth wall**: under `FLAG_SECURE`
(monobank «Уведіть ПІН-код») scrcpy/MediaProjection stream **all-black** frames,
and `screencap -p` returns **0 bytes**. So across B/C/D/E:

- fast path detects black → sets `SECURE_SUSPECTED` (D/E) or the host sees black
  (B/C) → **the canonical AUTH oracle stays the `screencap` 0-byte probe**
  (`ScreencapSource.is_auth_blocked`) + `uiautomator` anchors, unchanged.
- The **~10 s full-frame control tick** (one `screencap` or one full scrcpy
  frame) does what the packet deliberately omits:
  - `vision.states.detect_state` → popup / CONFIRM_NEW_GAME / NOT_IN_GAME / auth;
  - digit parse `vision.parser.parse_board` → reconcile with the fast board,
    resolve `uncertain` cells, `learn_tile_color` for new tiles;
  - score/timer OCR drift check (the running score is already known from the
    engine per-move, so the packet needs no score — OCR is a *check*, not a
    source);
  - if the palette lives on-device, push learned entries back over the same
    socket (host→device control frame; defined, not yet built).

Between ticks the fast board also self-checks: `BOARD_CHANGED` must flip after a
swipe (else re-swipe/settle), and `is_game_over` is decided host-side on the
parsed board (never on the popup — known app bug).

## 4. Security / ToS sanity

- **No root** in any recommended option (C/D/E).
- **No traffic interception**: nothing reads or proxies monobank's network. The
  socket carries only our own board packets over `adb forward` (localabstract, no
  `INTERNET` permission).
- **No AccessibilityService** for capture — MediaProjection does **not** use one,
  and the 50 ms budget is met without it. ⚠️ Do not reach for one: quite apart
  from the anti-tamper risk, the accessibility route was later built and tested
  for INPUT and **monobank rejects gestures injected that way** (verified on the
  device). Input therefore stays on `adb` — a separate channel — via a persistent
  shell.
- **Minimal APK footprint** (D): `FOREGROUND_SERVICE`,
  `FOREGROUND_SERVICE_MEDIA_PROJECTION`, `POST_NOTIFICATIONS` only. No INTERNET,
  no storage, `exported=false` service.
- ⚠️ **Residual risk to flag:** MediaProjection captures the banking app's own
  screen (the game window is *not* `FLAG_SECURE` on this device — proven: scrcpy
  read the board pixel-identical to `screencap`). Capturing a banking app's
  screen, even one's own game, could in principle be flagged by app-integrity
  checks. Risk assessed **low** (the game window is non-secure and MediaProjection
  is a system-mediated, user-consented capture the app cannot easily attribute),
  but if avoiding *any* app on the banking phone is preferred, use **E
  (app_process jar)** — it installs nothing and behaves like the already-tolerated
  scrcpy path.
- The PIN is never touched by any of this (auto-login stays a separate,
  env-only, human-supervised feature per device-ui-notes.md).

## 5. Prototype (this change) — what is built and verified

New, self-contained package `src/bot2048/ondevice/` (touches no owned modules):

- **`packet.py`** — `BoardPacket`, the fixed **22-byte** wire record: magic `B2`,
  version, flags, `seq` (u16), 16 nibbles board, `uncertain` (u16 mask),
  `device_ts_ms` (u32), CRC-16/CCITT-FALSE. Encode/decode, resync-by-magic,
  CRC-validated. Tens of bytes vs a 442 KB PNG.
- **`sampler.py`** — the **reference on-device sampler**, reusing
  `vision.parser`'s exact color path so device and host cannot diverge:
  crop → median of non-white pixels → nearest palette → `uncertain` mask +
  `BOARD_CHANGED`/`SECURE_SUSPECTED` flags. This is the *spec* the Kotlin port
  implements.

Tests `tests/ondevice/` (**45 tests, green; ruff + mypy --strict clean**):
- accuracy: color-only board == digit parser on all 10 real PNG fixtures;
- confidence: real settled frames raise no `uncertain` flags (max dist ≤ 1.4);
- an off-palette cell trips the `uncertain` bit; black frame → `SECURE_SUSPECTED`;
- packet round-trip, CRC/magic/version/length rejection, nibble order, resync,
  CRC known-vector `0x29B1`.

Measured on this host (upper bounds; on-device is far cheaper):
`sample 16 cells (full 1080×2400 numpy)` ≈ **26 ms**; `encode+decode` ≈ **42 µs**.

### Reference APK — `project/ondevice-apk/` (option D)
Complete Kotlin source + Gradle for the MediaProjection helper: `MainActivity`
(one consent tap), `SamplerService` (VirtualDisplay + ImageReader → sample →
abstract `LocalServerSocket bot2048_sampler`), `BoardSampler` / `BoardPacket`
(ports of the Python reference), `assets/calibration.json` (centers+palette),
manifest with the minimal permissions above, and a README with build/install/run
and **golden packet vectors** for conformance. Marked **untested on-device**
(written while the device was hands-off during run7); the golden vectors +
`ImageReader rowStride`/median gotchas are documented so the port can be
validated before live use.

Host consumer (to be wired by the runner-owning agent — **not touched here**): a
`FrameSource`-sibling that reads 22-byte records from `127.0.0.1:<port>` and
yields `BoardPacket`, with the existing `StreamingCaptureDevice` fallback ladder
to `screencap` for the 10 s control tick and AUTH.

## 6. Recommendation

1. **Ship C now** (`scrcpy --crop` to the board+score region + a crop-aware
   calibration offset): ~90 % of the transfer/decode win, zero new device code,
   reuses the proven scrcpy path. Immediate step under the current stream latency.
2. **Build D next** (MediaProjection helper APK): the operator's on-device
   sampler, the only option that gets host overhead to ~1 ms and makes the swipe
   the bottleneck. Prototype source is in `project/ondevice-apk/`; validate
   against the golden vectors, then wire the host consumer.
3. Keep **E (app_process jar)** as the fallback if unattended sessions make the
   MediaProjection consent lifecycle painful, or if installing any app on the
   banking phone is ruled out.

Never: root, AccessibilityService, traffic interception, or a per-move
`adb shell` spawn.
