# Latency scout — device/UI pipeline (READ-ONLY findings)

Scope: the ~330 ms/move live breakdown (perceive ~168, swipe ~109, settle ~53,
THINK ~0). Engine is out of the picture; every candidate below is a device/UI
seam. **Nothing here was implemented** — this is a ranked survey of the
remaining latency headroom. Doctrine constraint applied to every item: BANKING
APP, zero-blind-tap, reliability-first — no cut that raises misread/misfire risk
is proposed as a win.

## How the per-move wall clock actually flows (from code)

1. **Swipe** — `PersistentSwipeShell` writes
   `input swipe x1 y1 x2 y2 dur; echo <marker>` into a long-lived
   `adb -s <serial> shell` (`device/swipe_shell.py:147`, `:209`). The adb *fork*
   is already amortized; the residual ~64 ms is dominated by the on-device
   `/system/bin/input` invocation (a fresh `app_process` JVM spin-up per
   gesture) that runs **before** the 45 ms gesture even starts.
2. **Settle** — one blind sleep of `settle_delay_s + humanizer.next_delay()`
   (`runner/swipe_controller.py:183`). Live uses the APK value
   `SETTLE_DELAY_APK_S = 0.05` (`runner/live_config.py:39`, selected in
   `runner/cli.py:788`); FAST pacing profile ⇒ `next_delay() == 0`, matching the
   measured ~53 ms.
3. **Perceive** — `ObservationGate.observe_after_swipe`
   (`runner/observation.py:111`): APK packet gate first
   (`_observe_via_apk`, `:220`), frame path only on deferral. The APK `poll()`
   never touches the socket on the critical path — a background drain thread
   publishes every decoded `BoardPacket` into a 1-slot newest-wins mailbox
   stamped with host-monotonic receive time (`device/apk_capture.py:26-31`,
   `:238-249`). On a lull the gate sleeps `_APK_LULL_POLL_S = 0.02`
   (`runner/live_config.py:61`, slept at `runner/observation.py:263`); between
   non-matching packets it does **not** sleep (`apk_poll_s = 0.0`,
   `observation.py:306`).

Key structural fact: on the APK happy path the board arrives **already
recognized on-device** — host color recognition / vision parse is only paid on
frame-path fallback moves. So the ~168 ms "perceive" is mostly *waiting for the
app's own slide+spawn animation to settle and the on-device sampler to publish
the settled packet*, not host CPU. The floor is
`(animation tail after swipe) + (device sampler detection/publish latency) +
(mailbox dwell ≤ lull-poll quantum)`. That reframes the shave targets: the
biggest real wins move the *start* of the chain earlier (swipe overhead) or cut
the *publish/poll* latency, not the waiting itself.

## Ranked findings (largest safe win first)

| # | Optimization | file:line / mechanism | est. ms saved/move | Risk | RELIABILITY tradeoff | Effort |
|---|---|---|---|---:|---|---|
| 1 | **Swipe via `CMD_SWIPE` accessibility gesture instead of shell `input swipe`.** The sampler socket already carries a fire-and-forget swipe opcode (`ondevice/host.py:207` `SamplerLink.swipe`, `ondevice/packet.py` `encode_swipe`), and `device/apk_capture.py:471-480` already sketches the one-shot `input swipe` fallback "so a move is never dropped". Removes the on-device `input` JVM spin-up that burns most of the ~64 ms *before* the gesture starts — the whole downstream chain (settle + gate wait) shifts earlier. | `host.py:207`, `apk_capture.py:471`, `swipe_shell.py:147` | **~40–60** | Low–Med | Neutral-to-positive *if* gated: no shell echo ack, but the swipe is already verified by the packet/predict gate (predicted board + settled 2/4 spawn); an unregistered gesture surfaces as "unchanged" and goes through the existing retry path (`swipe_attempts` counter). MUST keep the capability probe + automatic fallback to the shell path; a silent gesture-drop *rate* increase would show up as swipe_retry_total — watch it in A/B. | Med (wiring + probe + A/B) |
| 2 | **Cut `_APK_LULL_POLL_S` 0.02 → 0.005–0.01.** On a lull the gate sleeps 20 ms between mailbox reads; the settled-spawn packet therefore sits in the mailbox up to ~20 ms (avg ~10) before being consumed. `poll()` is a lock+read, no socket I/O, so a finer quantum is nearly free. Confirm with the existing `apk_age_ms` telemetry (`observation.py:286`, `move_telemetry.py` `_MoveCounters.apk_age_ms`) — if accepted-packet age is already ~0, skip this. | `live_config.py:61`, `observation.py:263` | ~5–15 | ~Zero | None — the gate is accept-only (exact predicted+2/4 match); polling faster cannot misclassify. Slightly more host CPU wakeups. | Trivial (1 constant + telemetry check) |
| 3 | **Lower `SETTLE_DELAY_APK_S` 0.05 → 0.02–0.03.** (Question (a).) On the APK path the blind sleep mostly *overlaps* animation the gate would wait out anyway, so part of a cut just migrates SETTLE→PERCEIVE (net 0). The real win is the fraction of moves (no-merge, short animation) where the settled packet is published *before* the 50 ms elapses — today those moves over-wait. Safe because both gates are accept-only; an early poll can never accept a mid-slide board (full-board equality + settled 2/4 spawn, `observation.py:166-181`, `:223-229`). | `live_config.py:39`, slept at `swipe_controller.py:183` | ~5–20 (move-mix dependent) | Low | ~None on APK path (accept-only gates). Two second-order checks: frame-path fallback moves pay one extra early capture (predict-gate skips it — still safe); and `ponder_window_ms` shrinks (`live_config.py:448`) — with THINK ~0 ms this is a non-issue, but confirm `prefetch_hit` doesn't drop in A/B. | Trivial (constant + A/B) |
| 4 | **Device-side sampler publish cadence.** (Questions (c)/(e).) Host-side redundancy is already engineered out (push drain thread, 1-slot mailbox, board decoded on-device, no per-capture reconnect — reconnect only on drop, `apk_capture.py:190-204`). The remaining perceive floor is the APK's own detection→publish latency after the board settles. Measure: `apk_age_ms` ≈ 0 *and* perceive still ~168 ⇒ the wait is on-device (or genuine animation). If the sampler's internal detection tick is coarse, tightening it (or a `SET_RATE`-style knob, `host.py:203`) cuts perceive directly. | APK sampler + `host.py:203` | 0–30 (unknown until measured) | Low | None on host doctrine — still accept-only gating; on-device cost is battery/CPU, watch for frame-drop side effects. | Med–High (APK change + reinstall + A/B) |
| 5 | **Gate fallback rate audit (p95, not mean).** Every deferral from the packet gate pays the frame path: full-frame screencap chain + vision parse, ~an order of magnitude dearer (a screencap frame is ~0.65 s over WiFi per `move_telemetry.py:156-171`). If `gate_fallback` / lull-timeout deferrals are more than a few %, the p95 win from fixing *causes* (WiFi stalls, `apk_gate_max_wait_s = 0.8` budget, `live_config.py:246`) beats any constant-tuning. Read `bot2048 db latency` + `gate_hit`/`gate_fallback`/`capture_kind` **after** the live window closes. | `observation.py:246-263`, `live_config.py:246` | p95/throughput, not mean | Zero (read-only audit) | None. | Low |
| 6 | **Swipe gesture duration 45 ms.** `LIVE_SWIPE_DURATION_MS = 45` is already the FAST profile floor; going lower risks the app not registering the fling (misfire = retry = net loss, and a registered-but-misinterpreted gesture is a doctrine violation risk). | `live_config.py` / pacing profile | ~0–10 | **High** | Direct misfire risk on a banking app. **Not recommended.** | — |
| 7 | **Pipelining perceive into the *next* move / firing swipe N+1 before move N verifies.** (Question (d).) Perceive already overlaps everything it safely can: the drain thread receives packets concurrently, and the engine ponders during swipe+settle (`ponder_window_ms`, `live_config.py:422-448` — hence THINK ~0). Going further means acting on an unverified board = blind tap. | — | (would be ~50+) | **Unacceptable** | Violates zero-blind-tap doctrine outright. **Rejected.** | — |

## Direct answers to (a)–(e)

- **(a) `SETTLE_DELAY_APK_S` below 50 ms?** Yes, safely (item 3) — the gates are
  accept-only so early polls can't misclassify — but expect only partial net
  gain; much of the cut just moves wait into PERCEIVE.
- **(b) Remove the ~64 ms swipe overhead?** The adb fork is already removed
  (persistent shell); the residual is the on-device `input` binary. The
  designed-but-unwired `CMD_SWIPE` accessibility path removes it (item 1) —
  biggest single safe win.
- **(c) Redundant per-perceive work in APK capture?** No — no per-capture
  reconnect, no extra frames, no sync decode on the happy path; board decode is
  on-device, host poll is a mailbox read. Residual waste is only the 20 ms lull
  quantum (item 2).
- **(d) Overlap/pipeline perceive with the previous move?** Already maximally
  overlapped within doctrine (drain thread + ponder). Further pipelining =
  blind-tap; rejected (item 7).
- **(e) Is color recognition a chunk of the 168 ms?** Not on the APK happy
  path (recognition is on-device). It only bites on fallback moves — hence the
  fallback-rate audit (item 5) matters more than parse micro-optimization.

## Recommended first 3 (safe, high ms/risk ratio)

1. **Item 2** — lull poll 20 ms → 5–10 ms, after confirming `apk_age_ms` > 0 on
   accepted packets. One constant, zero doctrine risk.
2. **Item 3** — `SETTLE_DELAY_APK_S` 50 → 30 ms as a live A/B (the CLI already
   exposes the settle knob, `cli.py:608`).
3. **Item 1** — `CMD_SWIPE` accessibility swipe with capability probe + shell
   fallback, A/B'd against the persistent shell; watch `swipe_attempts` /
   `swipe_retry_total` for any gesture-drop regression.

Combined realistic shave: **~60–90 ms/move** (~20–25% throughput) with no
reliability regression, versus ~30 ms/move target.

## A/B notes

- Items **1, 3, 4** need a live A/B to confirm (item 2 confirmable from
  `apk_age_ms` telemetry alone). Compare `eval_details["timing"]` phase means +
  p95 and the reliability counters (`gate_hit`, `gate_fallback`,
  `swipe_attempts`, `capture_kind`) across equal-length windows.
- Benching/A-B must **NOT** run concurrently with a contaminating load (no
  parallel builds, no scrcpy viewers, no other adb consumers on the channel),
  and not against a live runner process that is still going, or its log/db —
  wait for the window to close.
