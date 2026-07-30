---
name: games-continue-infra-is-fine
description: "Live games did NOT crash/die to infra — they CONTINUED; vision_mismatch/screen_unexpected statuses are LOGGING/game-splitting artifacts, not game deaths. All results are VALID. Never frame infra as a reliability problem or a lever."
metadata:
  node_type: memory
  type: feedback
  originSessionId: 9994db1d-76a1-45df-a78d-0d55139499dc
---

Andrii, twice now (2026-07-23, emphatically the 2nd time), corrected me on the SAME thing and got frustrated I kept repeating it:

**The live games did NOT fall/crash. They CONTINUED. Therefore ALL game results are VALID — it was NOT infra dying, NOT games cut short, NOT a reliability failure.**

The DB statuses `vision_mismatch`, `screen_unexpected`, `interrupted`, etc. are **logging / game-splitting artifacts** — the recorder split ONE continuous game into multiple segments (or misread a frame), while the actual game kept playing on the phone. Infra works fine; the games run to their real end. The only real issue was ever a STATISTICS/counting one (how two cells merging simultaneously get scored/segmented), NOT the game or the infra stopping.

**Why:** I keep re-deriving "X% of games died to operational issues → fix infra = a volume/reliability lever." That framing is WRONG on both counts: (a) those games did not die (they continued), and (b) there is no infra reliability problem to fix. Presenting it as a lever wastes his time and signals I didn't absorb his correction.

**How to apply:**
- NEVER present vision_mismatch/screen_unexpected/interrupted (or any non-`game_over`/non-`win_2048` status) as game deaths, infra failures, or a reliability lever.
- When a DB batch shows a "low max score," remember segments UNDERSTATE the true continuous game — do not conclude live play is weak or short from segment logs.
- The leaderboard RECORD is tracked correctly (~49.6k); do not lean on game-splitting to explain scores. See [[cashout-and-champion-state]] and the game-splitting notes in [[beat-58k-via-k4-cascade]].
- Related standing rule: [[numbers-must-come-from-data]] — his corrections keep beating my aggregation.
