# bot2048 on-device board sampler (helper APK)

Reference implementation of the on-device vision offload (see
`docs/ondevice-vision-design.md`). Captures the screen with **MediaProjection**
(no root, no AccessibilityService, no traffic interception), samples the 16 tile
centers on the phone, and streams **22-byte board packets** to the host over an
abstract `LocalServerSocket` reached with `adb forward`.

> Status: **validated on-device and used for live play.** `--capture apk` is the
> capture backend the live runs use (one campaign ran 8342 moves; the packet path
> costs ~20-30 ms move→packet versus ~650 ms for `screencap` over WiFi — see
> `docs/experiment-log.md`). Build verified with Gradle 8.14.3 + JBR 21, SDK 34:
> `app/build/outputs/apk/debug/app-debug.apk`. The packing/classification mirrors
> the Python reference (`bot2048.ondevice`) and shares its golden vectors (below).
>
> The **swipe-injection half** (`SwipeAccessibilityService`, host-side
> `device/apk_swipe.py`) is implemented and unit-tested, and it is the fastest
> input channel available — but **monobank rejects accessibility-injected
> gestures**, verified on the device. So it is a measured negative result, not a
> precaution: swipes ship on the second-fastest path instead, a persistent
> `adb shell` (~109 ms versus ~248 ms for a per-swipe `adb` process spawn). The
> code stays in the tree as the record of what was tried.

## Layout
```
ondevice-apk/
├── settings.gradle.kts
└── app/
    ├── build.gradle.kts
    └── src/main/
        ├── AndroidManifest.xml
        ├── assets/calibration.json        # copied from data/calibration.json (centers+palette)
        └── java/ai/bot2048/sampler/
            ├── MainActivity.kt            # one-time MediaProjection consent, then finishes
            ├── SamplerService.kt          # FGS: VirtualDisplay+ImageReader -> sample -> socket
            ├── BoardSampler.kt            # the color math (port of bot2048.ondevice.sampler)
            └── BoardPacket.kt             # 22-byte packet + CRC (port of bot2048.ondevice.packet)
```

## Build
Needs a JDK 17+ and the Android SDK (compileSdk 34). This folder is a
self-contained Gradle project **with a committed wrapper** (Gradle 8.14.3).

`local.properties` is machine-local and therefore **not** in git — create it
with the path to your own SDK:

```bash
cd project/ondevice-apk
echo "sdk.dir=/path/to/Android/Sdk" > local.properties   # e.g. C\:/Users/<you>/AppData/Local/Android/Sdk

# JDK: any 17+; verified with Android Studio's JBR 21:
#   JAVA_HOME="C:/Program Files/Android/Android Studio/jbr"
./gradlew :app:assembleDebug        # gradlew.bat on Windows cmd
# -> app/build/outputs/apk/debug/app-debug.apk
```

If you recalibrate the device, refresh the asset:
```bash
python - <<'PY'
import json
d=json.load(open("../data/calibration.json"))
json.dump({k:d[k] for k in ("screen_size","cell_centers","palette")},
          open("app/src/main/assets/calibration.json","w"), indent=2)
PY
```

## Install & run (operator, manual)
```bash
adb install -r app/build/outputs/apk/debug/app-debug.apk
adb shell am start -n ai.bot2048.sampler/.MainActivity   # tap "Start now" on the consent dialog
adb forward tcp:27184 localabstract:bot2048_sampler
# host reads fixed 22-byte records from 127.0.0.1:27184
```
The consent tap is the only manual step; keep the phone charged so the
foreground service is not killed (a kill loses the projection grant).

## On-device swipe injection (optional, opt-in) — kills the per-move `input` spawn

`SwipeAccessibilityService` replays the host's pre-computed swipe with
`dispatchGesture()`, so the per-move `adb shell input swipe` — which forks a
fresh Dalvik `input` process on the device (~60 ms) plus an adb-shell hop —
disappears. The host sends an 11-byte `CMD_SWIPE` command (`'W'` + five
big-endian u16: `x1 y1 x2 y2 duration_ms`) over the SAME `adb forward` socket
the board sampler already drains; no second connection, no new port.

Wire contract: `bot2048.ondevice.packet.encode_swipe` (host) and
`FramePacket.CMD_SWIPE` / `SamplerService.handleSwipe` (device) — keep in
lockstep, same as the board packet.

> ⚠️ **This does not work against monobank.** The mechanism itself is sound and
> the win below is real in principle, but the app **rejects gestures injected
> through accessibility** — verified on the device. Live play therefore stays on
> the persistent-shell path. Everything below is kept as the record of a measured
> negative result, and because the same code would work against an app that does
> not reject it.

**One-time enable (operator):** the service is OFF by default and does nothing
until enabled. On the phone: **Settings → Accessibility → bot2048 sampler →
turn On** (deep link: `adb shell am start -a android.settings.ACCESSIBILITY_SETTINGS`).
It reads no screen content (`onAccessibilityEvent` is a no-op); it only emits
gestures. Note also that banking apps can scan for enabled accessibility
services at all, which is a second reason not to rely on this path.

**Host side:** `bot2048.device.apk_swipe.ApkSwipeInjector` sends the command via
`ApkBoardSource.send_swipe` and blocks for `duration_ms` (matching the old
`input swipe` timing). It runs a one-time capability probe
(`accessibility_enabled`, reads `settings get secure
enabled_accessibility_services`); if the service is not enabled, or a socket
write fails, it falls back to `input swipe` so a move is never dropped. Wiring
point (a separate, device-safe change): in `AdbDevice.swipe`, try
`ApkSwipeInjector.swipe(params)` before the persistent-shell path, passing the
shell/one-shot swipe as its `fallback`.

**Verify the win (per-move `swipe_ms` should drop from ~141 to ~80):** with the
service enabled and a run in progress, compare the `adb.swipe` /
`apk_swipe.injected` transport timings in the move telemetry (DB `moves` table /
structlog). The gesture itself is the fast profile's 80 ms; removing the ~60 ms
`input` spawn is the delta.

## Golden conformance vectors
Frames from `tests/vision/fixtures`, sampled with `seq=1, device_ts_ms=0`,
using the reference `data/calibration.json`. The Kotlin `BoardPacket.encode`
output for the same board MUST equal these bytes:

| frame            | board (row-major exponents)                    | packet hex |
|------------------|------------------------------------------------|------------|
| board_a.png      | 0 1 2 2 0 3 1 2 0 1 3 1 0 1 3 1                 | `42320100000101220312013101310000000000007803` |
| board_256.png    | 1 0 0 0 0 0 0 0 1 0 0 0 8 4 0 1                 | `42320100000110000000100084010000000000009a6a` |
| board_fresh.png  | 0 0 0 0 1 1 0 0 0 0 0 0 0 0 0 0                 | `4232010000010000110000000000000000000000b6a3` |

`BoardPacket.crc16("123456789") == 0x29B1` (CRC-16/CCITT-FALSE check value).

The full-frame → board classification is verified against the host's
digits+color parser on every real fixture by
`tests/ondevice/test_ondevice_sampler.py` (Python side). The device port is
correct when it reproduces both the board and these packet bytes.

## Gotchas baked into the code
- **`ImageReader` rowStride**: RGBA_8888 planes are padded — always index with
  `y*rowStride + x*pixelStride`, never `y*width*4`. Getting this wrong yields a
  sheared, garbage sample. Handled in `BoardSampler.px`.
- **FLAG_SECURE**: the monobank auth wall streams BLACK, not pixels; the sampler
  emits a `SECURE_SUSPECTED` packet (zeroed board). The host must still treat
  the `screencap` 0-byte probe as the authoritative AUTH sensor.
- **Median semantics**: even-count medians average the two middle values
  (numpy-compatible) — required for byte-identical packets.
