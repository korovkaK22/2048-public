# Experiment log — what we tried, result, why

A running ledger so we never re-run a dead end and can see the whole history at a
glance. **Append one short entry per tested variant** (heuristic, weight profile,
depth, strategy). Newest first. Keep it terse: config → result → takeaway.

Format:
```
## <date> <short name>
- **Config:** <what changed / key weights / depth / seeds>
- **Result:** <numbers — mean, max, %3x/4x 1024, forced-2048, or pass/fail>
- **Why / takeaway:** <what it means, keep or drop, next step>
```

---

## 2026-10-25 — 🔎 vision: аудит 91 інциденту i7 → «settled»-gate для колір-only читань + стан SYNC_OVERLAY

- **Config:** усі VISION_MISMATCH (26) / SCREEN_UNEXPECTED (36) / GAME_OVER_UNEXPECTED (7) бандли + 55 реальних debug-кадрів прогнано через поточний `vision/` (`tmp/replay_incidents.py`), кожен mismatch пояснено перебором гіпотез (дрейф tracked-дошки на 1 клітинку / інший напрямок / misread кадру / два свайпи) і звірено з `last_moves.json` + `moves/spawns` у БД. Таблиця причин — `docs/device-ui-notes.md` §«Розбір інцидентів i7».
- **Result:** (1) **9/9 дрейфів** = спавн попереднього ходу, записаний як «2», реальна плитка «4» (APK-режим); настояні кадри читаються без жодної колір/цифри-розбіжності (max 3.7 RGB на 1 700 клітинках) → винен не парсер, а gate, що приймає перехідний пакет/кадр fade-in (шлях «4» проходить за 11.7–12.9 RGB від «2»). Фікс у host-vision: `COLOR_SETTLED_MAX_DIST=8` — колір-only читання >8 від палітри = `unsettled` (exp 0, lenient) / `BoardParseError` (strict); регресії `test_fading_tile_never_reads_as_a_different_tile` (sweep alpha 0…1 для 2/4/8/16/32), `test_fading_4_dips_near_the_2_entry_but_stays_unsettled`, `test_unsettled_color_strict_raises`, `test_settled_gate_has_headroom_over_real_faces`. Повторний replay: усі 149 ігрових кадрів читаються байт-у-байт як раніше (змінились лише попапи/притемнені екрани — там колір-снепи стали `unsettled`). (2) **7 SU** — оверлей «Синхронізація… / Вийти» → новий `ScreenState.SYNC_OVERLAY` (4 анкери в колонкових проміжках, 0/457 фолсів), фікстури `sync_overlay.png` / `sync_overlay_tall.png` (900×900 панель), тести `test_sync_overlay_*`. (3) 8 SU win/game-over, 2 SU «128», 4 SU «↻-кнопка», 2 VM палітри — вже закриті поточним кодом (підтверджено replay). (4) 3 VM — дзеркальний горизонтальний свайп (не vision), 11 SU/GOU — апка вийшла з гри (детект коректний), решта — пристрій/мережа.
- **Why / takeaway:** misread'и живого телефона — це **анімація + gate «перший кадр, що збігся»**, не палітра і не лапа. Host-кадровий шлях закрито; APK-шлях (`ondevice` + Kotlin `uncertainMaxDist=20`, gate ігнорує `uncertain`) — та сама дірка, потрібен той самий поріг 8 + повага до маски на спавн-клітинці (пропозиція, live-тест). Супервізору — політика «чекати» для `SYNC_OVERLAY` замість FATAL.

## 2026-07-25 — 🎚️ ENDGAME CALIBRATION: champion move-ranking vs EXACT max-P DP → RED (survival wall is geometry, not champion mis-calibration)

- **Config:** the last untried training-architecture pre-check — is the survival wall (champion never reaches 5×512) an artifact of the CHAMPION value-function MIS-RANKING endgame moves vs the exact DP-optimum (⇒ a phase/survival net could fix it, GREEN), or geometry (RED)? New `ntuple tbcalib` (scratchpad/tbcalib-build, built on tbmaxp @126c6f0 — reused bit-exact, not rewritten). Builds the exact retrograde max-P DP `V=max_π P(reach live 3×1024+≥5×512)` over the mobile domain from goal-2 navigable seeds (poised for 5×512: 3 non-adj 1024 + 4×512 + 2 mergeable 256 + tail), then on every closure state with a REAL decision (≥2 in-domain moves, DP per-move V differ) compares the DP move-ranking to the CHAMPION's production expectimax ranking (l0.003, n.leafW, d3). Plus the DECISIVE test: paired honest-RNG rollouts forcing champion-move vs DP-support-preserving-move, measuring survival. Two seed sets: 220@cap3040 (5433 decisions) and 200@cap2860 (1630 decisions).
- **Result:** **RED.** Argmax-agreement champ-vs-DP = **64–66%**, Spearman **0.63**, critical mis-rank (champion prefers an in-domain path-KILLER dp-V=0 over the support-preserving move) = **10.5–10.7%**, global-move leak 35–37%. But the DECISIVE rollout kills the GREEN reading: on 130–150 critical mis-rank boards × 80–100 honest games/arm, forcing the DP-optimal support-preserving move buys **ZERO** survival — mean moves survived CHAMP=6.7 vs DP=6.4 (cap3040) and 5.3 vs 5.3 (cap2860); boards-where-longer is a coin-flip (78/150, 61/130). The board dies in ~5–7 moves EITHER WAY. DP-move only raises the forced-2048 cash-out rate (0.55 vs 0.47) — it ends the game SOONER via the taboo tile, not later. Separately: on states the champion ACTUALLY reaches (mass ≤2280, tbmaxp), DP V=0 everywhere ⇒ no ranking gradient to mis-calibrate at all.
- **Why / takeaway:** the champion/DP disagreement is an OBJECTIVE difference (champion maximizes score/survival; DP maximizes P-of-5×512-cascade — proven EV-negative), NOT a survival calibration hole. Any board POISED for 5×512 must already carry mass ≥2560 (4×512+2×256) ⇒ near-full ⇒ ~6-move survival — that IS the fill-pressure wall restated exactly. A phase/survival net has (a) no reachable states to act on (arrival wall) and (b) nothing to gain even if handed the poised boards (the max-P move survives no longer). Closes the multi-stage survival-net architecture. Do NOT build it. Tool + seeds kept in scratchpad/tbcalib-build.

## 2026-07-25 FINAL closure — banker-ON tail + aggregate-hypothesis kill

- **Config:** Scaled the champion (exp-utility net λ=0.003, depth-4, maxcash exact banker; same knobs as champdist_400: T=49601, EMPTIES=8, NODES=6M, PCONF=0.6, EARLYFIRE=300, maxcashDepth=8) from n=400 to n=8220, banker-ON, fresh seed0=600000, full per-game score tail logged. Fixes the Ph0 banker-OFF error (which had killed the cash-out tail).
- **Result:** n=8220 → max=**51,128** (frozen; did not move a single point from n=2880 onward), p99.9=51,092, p50=38,928, **≥52k = 0, ≥53k = 0**. Top-6 games all in a 32-point band at ~51,100. Reproduces champdist_400 (max 51,108) bit-for-bit at 20× the sample. Wall confirmed at ~1.2e-4 rarity — a 2nd independent metric matching the 52,212-achievability analysis.
- **Why / takeaway:** Single-game ceiling ~51-52k is now doubly confirmed. Separately, the aggregate-leaderboard hypothesis (that 58k might be a cumulative not single-game score) was killed logically: our own leaderboard row is ~49.6k over hundreds of played games — a cumulative would be in the millions, so the board is best-single-game. CLOSED: no autonomous sim lever remains (жодної ортогональної ідеї з апріорі >5% поза списком закритих); PoC-B human-imitation dead (user won't play). Only path to 58k is throughput (more live attempts to fish the rare tail board), an ops decision not research. Run stopped by user at n=8220; cron faa128c2 deleted; caffeinate reverted. See memory beat-58k-final-closure.

## 2026-07-24 — 🚧 ONLINE CMDP / constrained support-search on CHAMPION → NO-GO (8th proof; squeeze even under a HARD survival budget)

- **Config:** new `ntuple cmdpsearch` (cmdpsearch.go, branch exp/cmdp-search from exp/qd-mapelites). Per-move CONSTRAINED program instead of a fixed variance bonus: choose the legal non-terminal move that MAXIMISES immediate non-1024 support-mass SUBJECT TO hazard = P(death within h moves) ≤ ε; if infeasible, fall back to the champion-safest move (survival preserved). Hazard FROM CHAMPION VALUE: the champion depth-limited search value of each afterstate is a certainty-equivalent in move units (U = cBase + ln W/λ under the exp objective, λ=0.003 cBase=1800), memoryless death → hazard = 1−(1−1/U)^h. Champion l0.003, banker-OFF, **depth 2** (depth-4 = 123 ms/move → 150-250 s/game, infeasible at n=2000; depth-2 champion sup-max 1932-2338 vs depth-4 2280 — same ballpark, 2560 far above both), h=8, **n=2000 fresh per ε**, ε∈{0.02,0.05,0.10,0.20}, movecap 20000, 4 workers.
- **Result:** **RED — no ε crosses 2560; the constrained optimum is stuck below the floor.** Champion depth-2 baseline (n=2000): reach-k3 1660/2000 = **83%**, sup med 1080 / p90 1556 / **max 2338**, reach-5×512 0/2000, board-mass med 34860. **All four ε produced BYTE-IDENTICAL results:** reach-k3 1389/2000 = **69.5%**, sup med 1036 / p90 1512 / **max 2084**, reach-5×512 0/2000, board-mass med 33052. Table (ε | sup-max | p90 | med | reach-k3(survival) | reach-5×512): 0.02|2084|1512|1036|69.5%|0 · 0.05|2084|1512|1036|69.5%|0 · 0.10|2084|1512|1036|69.5%|0 · 0.20|2084|1512|1036|69.5%|0. (Confirms an earlier n=160 spot ε=0.02: 68.1%/990/2084.)
- **Why / takeaway:** the ε-sweep is **degenerate at h=8**: hazard is BIMODAL — for U>~397 every move has hazard<0.02 (all ε accept → pure support-greedy) and for U<~36 every move has hazard>0.20 (all ε reject → champion-safest fallback), and the discriminating band U∈[36,397] never flips the argmax-support decision across 2000 games. **That bimodality IS the geometry:** the k=3 endgame where support≥2560 would have to be built lives at U<36 (few moves left) — a region NO ε∈{0.02..0.20} can certify as survivable, so any hard survival budget forces safe play exactly there and caps support at champion levels. And the single effective operating point is a textbook SQUEEZE: it is STRICTLY WORSE than the champion on BOTH axes — support 2084<2338 AND survival 69.5%<83% — support does not detach from survival, they fall together. At the ≥90%-survival frontier the only policy is the champion itself (depth-4 reach-k3 93%, max 2280 < 2560). A HARD/Lagrangean constraint (not a κ bonus, not an expectile fixed-point shift) reaches the same wall as the 7 prior proofs. **STOP — this is the GEOMETRY, not the policy class.** 53k practical ceiling stands. Code + sweep output on branch exp/cmdp-search.

## 2026-07-24 — 🔴 Ph3 tail-banker: SUPPORT-CONDITIONAL cash target (RAISE) — RED

- **Config:** гілка `exp/tail-banker`. Replay EXACT banker (`playCashoutFrom`, канонічні
  ручки: T=49601, EMPTIES=8, NODES=6M, PCONF=0.6, EARLYFIRE=300, EARLY_EMPTIES=1, d4,
  champion-l0.003) з КОЖНОЇ хвостової дошки (volume-backbone харвест: 92 дошки, усі
  3×1024+3×512, Φ 41.7–43.6k), rollouts=12 → 1104 ігор, seed0=700000. Порівняння
  (A) фіксований 49901-флор vs (B) state-conditional: підняти cash target до 53/54/55k
  поки жива дошка = deep support. Нова ручка `BOT2048_SUPPORT_TARGET` (+ env-тюнингові
  пороги `SUPPORT_N1024/N512/N256`), state-conditional у `playCashoutFrom`.
- **Result:** **(A)=(B) — важіль або НЕ спрацьовує, або строго шкодить.** Baseline A:
  max 51036 (~50.0k real після −4·N₄/−2%), median 43772, P(≥50k)=**5.53%** (61/1104,
  12 різних дощок), P(≥53k)=0%, top-3 engine 51036/50604/50568 (~50.0/49.6/49.6k real —
  збігається з live-рекордами 49.6k). **Літеральний тригер (3×1024∧≥4×512∧≥1×256):
  raised=0/1104 — регіон forward-НЕДОСЯЖНИЙ** (4-й 512 з'їдає 256-ladder → n256=0 саме
  коли n512≥4; 51/1104 ігор торкались 4×512 але ЗАВЖДИ з 0×256). **Reachable тригер
  (кинув n256): raised=51, up=4/down=18/same=29, net ΔScore=−64080; P(≥50k) 5.53%→4.26%
  (ГІРШЕ)**; типові втрати −4096…−6120 (тримання губить весь каскад при forced-cash).
  Target 53/54/55k — ІДЕНТИЧНІ (усі вище досяжного). **Guardrail OK: не-raised ігри
  БАЙТ-ІДЕНТИЧНІ до A (diff=0).**
- **Why / takeaway:** **DROP. 10-й доказ стелі + симетрія до banker-arm-timing memory:**
  як пониження target руйнує top-3, так і ПІДНЯТТЯ його руйнує — champion не може
  forward-reach 4×512-rung (53.2k), тож тримання support-дошки замість банку ~50k
  просто дає їй деградувати до forced-cash нижче. Фіксований 49901-банкер уже оптимальний.
  Realistic top-3 з пулу = **~50k engine (~49.5k real), делівериться СТАНДАРТНИМ банкером**;
  важіль — не target, а ОБ'ЄМ (P(≥50k)=5.5% → більше хвостових дощок = більше ≥50k хендів).
  Пул малий (92, росте з харвестом); числа зростуть, але вердикт структурний (memory:
  beat-58k «4×512 — hard forward ceiling»), не n-обмежений.

## 2026-07-23 — 🧭 PoC-A: ONLINE GOAL-DIRECTED SURVIVAL-PRUNED BEAM planner → RED (8th proof, squeeze)

- **Config:** the one buildable planner none of the 7 priors tried — not a retrain, not a per-node veto, not a κ-tilt, but an EXPLICIT forward planner. New `ntuple goalbeam` (goalbeam.go, branch exp/goal-beam). Every real move rolls a width-W beam of honestly-spawned trajectories `depth` plies deep, ranks by composite = wGoal·goalProgress + wSurv·surv (surv = frozen champion l0.003 value as hazard proxy), prunes branches with surv < floorFrac·max-surv, accepts the move from the best LIVING trajectory, falls back to pure-survival root when the beam dies (never suicides). goalProgress steers toward "k=3 carrying non-1024 support-mass ≥2560" (live 3×1024+5×512). Honest RNG, independent planner rng. W=300 d=4 seed=8, movecap 3000. Definitive: config A (wg1/ws5/floor0.6, best-survival) n=2000; goal-push C (wg4/ws1/floor0.35) n=800; plus n=24+400 sweep.
- **Result:** **RED.** reach-5×512 = **0/2000** at config A (and 0 across all 3224 games, every config). Config A: reach-k3 361/2000 (18%), PEAK support median 356 / p90 732 / **max 1130** (« 2560 floor, and « champion's own 2280), peak #512 dist {0:298,1:59,2:4} (5 never), **median moves 1215** (champion ~1520; below the −10% gate floor ~1370 → survival collapsed). Goal-push C n=800: reach-k3 12.6%, support max 1052, median moves 1166 — pushing the goal harder LOWERS support AND survival AND reach-k3 together. Table (cfg | sup-max | reach-k3 | med-moves | reach-5×512): A(ws5)|1130|18%|1215|0 · B(bal)|634|17%|1134|0 · C(wg4)|1052|13%|1166|0.
- **Why / takeaway:** support NEVER separates from survival — identical verdict to the 7th (κ) proof but via a completely different mechanism. The beam's only steering lever is "hoard support material," which is exactly the fill-pressure the project has proven fatal: chasing it collapses reach-k3 (93%→13-18%) and median moves before support can accumulate; playing safe merely converges toward the champion ceiling (2280<2560, 5×512=0). No composite balance crosses the gate because the strongest possible survival substrate (the champion itself) already tops at 2280. An explicit planner adds no new reachability. 8th independent proof; STOP the ceiling-attack line — pivot to throughput/reliability. Code committed (28fda37); goalbeam subcommand kept (inert unless invoked).

## 2026-07-23 — 🧬 QD / MAP-ELITES over farm-evaluator levers → NO-GO (7th orthogonal proof; support squeeze)

- **Config:** new substrate = FARM evaluator (not net), 9-gene lever space {kappa, empty, convex, stair, snake, cornerRank, hoard-trigger, lateEmpty, lateGrad}. Wired env-hook `BOT2048_FARM_KAPPA`→search-time mean-variance certainty-equiv E[X]+κ·sd(X) at chance nodes; new `ntuple farmeval` per-genome kernel (banker-off, depth-3, common 24-seed block, emits peak non-1024 support-mass @k≥3 + fill/n512/moves). Python MAP-Elites driver (archive 4×4×4, iso+line-DD, uniform sel). Fitness = P90 peak-support. PoC gate: 241 evals × 24 games ≈ 5800 games in **761 s** (farm depth-3 ~8 games/s aggregate — 20× the spec throughput estimate). Branch exp/qd-mapelites (e424b2d).
- **Result:** **NO-GO.** Global max peak-support over all 241 lever-combos = **~1126** (< the 2560 5×512 floor by 2.3×). Best median peak-support ~575 (« 2400 gate). Paired farm-default baseline (n=100): reach-k3 21%, sup-med 466, max 654, 0/100 ≥2560. Archive collapsed to 1 cell (most farm games never reach k3 → median descriptors always weak → degenerated to a 241-eval hill-climb on support). **κ-sweep is the clincher:** κ 0→0.2 baseline (reach-k3 12-13/60, max 560-732); κ=0.4 = risk-tilt working as designed (sup-max ↑1072 BUT reach-k3 ↓6/60); κ=0.7 reach-k3 2/60; **κ≥1.0 reach-k3 = 0/60** (total death). hoard-on / aggressive-crowd corners also collapse reach-k3→0.
- **Why / takeaway:** support rises **ONLY** as reach-k3 collapses — the identical squeeze that killed highvar-β (β=8/25) and expectile (R5). A continuous, tail-objective, collapse-IMMUNE static-policy sweep still cannot cross 2560; the risk-seeking direction eats the fill-pressure death-tax exactly like the value-net class did. This is the cleanest disproof yet: it's the **GEOMETRY** (fill-pressure hazard), not the policy class. Confirms ~53k practical ceiling. STOP — do not run the full 6-h QD prosin. kappa hook + farmeval kept (clean, tested, byte-identical at κ=0).

## 2026-07-23 — 🤝 COOPERATIVE-SPAWN upper-bound probe → HUMAN-VOLUME (buildable, but P*≈4e-179)

- **Config:** new `ntuple coopprobe` (coopprobe.go). Forward rollout from EMPTY; every spawn chosen by a 1-ply (or 2-ply) cooperative optimizer over honest {2,4}×cell — the BEST-luck environment. Player policies: frozen champion (support-cooperative spawns), champveto (4th-1024 vetoed), support-greedy depth-4 planner, and **champself** (spawn maximizes the champion's OWN net value = its best-luck survival). Measures peak non-1024 support-mass @k=3, #512, live 3×1024+5×512, and P* = honest joint-prob of the relied-on spawns (single-choice + compatible-fraction). Champion l0.003, depth 4, respdepth 2, banking OFF.
- **Result:** support-TARGETED cooperation FAILS all mandated policies (<2560): champion coop max 1730 (see-saws to a 4th 1024 18/24 regardless of spawns), champveto max 1730 (fill-pressure FORCES the 4th 16/24), greedy planner max 954 (depth-4 horizon-limited: rushes the scaffold, 0 support at k=3). A/B proves cooperation DOES help a self-consistent player (greedy honest 0/60 reach-k=3 → coop 19/60). BUT **champself CROSSES**: peak support **2584 > 2560**, and **2/24 build a LIVE 3×1024+5×512** (banks up to Φ50452). P* over the ~2242-spawn line: single-choice 1e-2115, compat-frac(tol=1) 1e-1561, **compat-frac(tol=50, generous) = 3.9e-179** — geometric-mean 20–83% compatible mass PER spawn.
- **Why / takeaway:** the target is **NOT geometrically walled** — it builds under ideal luck when you cooperate with the champion's ACTUAL objective (survival), not an imposed support objective (which disrupts it below honest RNG). But P* ≪ 1e-8 by 170+ orders: reaching it needs ~2242 player-favorable spawns in a row, i.e. the honest RNG must decline to apply fill-pressure the whole game. That is exactly R4's EV-truth (carrying support is EV-losing under honest fill-pressure) — so training can't exploit it either. **Verdict HUMAN-VOLUME → honest STOP; 53k stands as the practical ceiling.** Do NOT build a cooperative-spawn/curriculum stack on this. Code on branch exp/coop-spawn-probe.

## 2026-07-23 — 🔴 planner probe: goal-augmented expectimax (option C) — NO GLIMMER

- **Config:** `ntuple plannerprobe` — frozen champion (champion-l0.003), banker-off,
  matched depth 3. Non-greedy planner = multiply every expectimax leaf by
  `exp(beta*g)`, support tilt `g = 0.5*#512 + non1024mass/2048` (EV-sacrifice knob;
  beta=0 == champion). Measures mass-rich-k=3 reach rate (peak non-1024 mass ≥2400).
  n=150 paired seeds; beta scan {0.3,0.7,1.5,3.0} n=32.
- **Result:** champion mass-rich=0/150 (baseline reproduces DP harvest: median 1160,
  max 2254, n512=4 once/150=0.7%). Planner beta=0.3: mass-rich **0/150 (1.0×, no
  lift)**, max support **2254 identical**, reach-5×512 0 both, n512=4 1→3 (NS,
  mass-short), reach-k=3 DROPS 141→123. Larger beta → monotone policy collapse
  (beta3.0: reach-k=3 6/32, support median 538).
- **Why / takeaway:** RED. Tilting toward support = holding tiles = board fills =
  death BEFORE mass-rich k=3. The rich TAIL (max support) is byte-unmoved by an
  EV-sacrificing planner. Third independent confirmation of R4 EV-truth + DP V=0
  (which upper-bounds beam/MCTS too). Wall is DYNAMICS not policy-greed. Do NOT
  build the full planner. 53k stands. Report: scratchpad/planner-probe.md.

## 2026-07-23 — 🔴 expectile-TD risk-seeking retrain (останній важіль стелі)

- **Config:** новий важіль `ntuple train -expectile τ` — асиметрична вага TD-кроку:
  ×τ на позитивній помилці (target>V, апсайд), ×(1−τ) на негативній → V тягнеться до
  високого (оптимістичного) експектиля = risk-seeking, «прийми EV-збиток заради більшого
  support-mass» (гіпотеза DP-таблбейзу: розрив = 2560 support для 5×512, чемпіон стеля 2280).
  Не merge-invariant (на відміну від mass/survival reward) → фікс-точка ≠ чемпіон. Warm-start
  champion-l0.003 (exp λ=0.003 збережено). Вимір `ntuple masshunt` (banker-off, пік non-1024
  mass та #512 @k=3). τ=0.60/0.70/0.90, α 0.02–0.03, короткі прогони.
- **Result:** чемпіон (expectile off): reach-k=3 93/100, peak support median 1222 / **max 2002**,
  reach-5×512 0/100. **Усі τ: reach-k=3 = 0/100, peak support 0, peak board-mass max 5832** (≈ один
  1024). Не «менше support» — НУЛЬ: мережа не будує навіть три 1024. Колапс миттєвий (τ0.6/α0.02
  за 60 с). Wiring перевірено: `-expectile 0` warm-start відтворює greedy-медіану 1525→1586.
- **Why / takeaway:** exp-utility мережа зберігає V≈залишкові ходи (~1500–2000); апсайд-вага +
  optimistic vInit роздуває всі ваги рівномірно до насичення → leaf ≈const → policy-free → смерть
  до k=3. Фікс-точка вироджена; менша α лише сповільнює до тієї ж атракторної смерті. Разом з R4
  (EV-truth: палити support чесно оптимально) і DP (чемпіон ніколи не доставляє mass-rich n512=4)
  → **~53k — чесна стеля для класу value-net (mean, exp-utility І expectile однаково не hoard-ять
  support).** Людський градієнт 53→58k потребує іншого класу політики (не value-greedy). Мертвий
  важіль, НЕ переробляти. Звіт: `scratchpad/expectile-smoke.md`.

## 2026-07-23 — 🔬 ENDGAME max-P TABLEBASE: the 53k→58k gap is UPSTREAM mass-management (exact, not EV)

- **Config:** exact retrograde max-P DP (`tbmaxp.go`, verified bit-exact vs top-down memo, maxErr 0.0) over the mobile 3×1024-domain (tiles ≤512, mass-stratified DAG). V(s)=max_π P(ever reach a live 3×1024+≥5×512 board | s). Then looked up V on ~150 REAL champion k=3 entry states (banker-off self-play).
- **Result:** V over real champion entries = **median 0, max 0, %V≥1% = 0** → P(record via a perfect endgame navigator) ≈ 0.833×0 ≈ **0** (unchanged). The navigable region is real but NARROW: from a mass-rich n512=4 board (~1 merge from 5×512) V mean 0.23 / max 1.0, but a tight-headroom one-merge board is only **V=1.4%** (R4's fill-pressure hazard, now exact). The champion never ARRIVES: peak support n512 modally 2, =4 once/150, =5 never; peak non-1024 mass median 1146 / **max 2280 < the 2560 floor to even hold 5×512**.
- **Why / takeaway:** the record is NOT an endgame problem (the frozen champion finalizes optimally, solver 57,349) and NOT reachable by more volume (rung ceiling 53,255). The ENTIRE 53k→58k gap = one upstream quantity: **arrive at k=3 with a mass-rich n512=4 support layer (peak non-1024 mass 2280→2560)**. The champion converts too much support into 1024s (see-saw) and arrives ~280 mass short. Humans do it (populated leaderboard GRADIENT 53→58k, per Andrii — NOT an outlier, so 58k is real & repeatable). It's EV-negative (R4) but human-doable → the only untested lever is a RISK-SEEKING training objective (expectile-TD, τ→1; NOT the merge-invariant mass/survival reward whose fixed point = champion). Endgame navigator NOT worth building (nothing to navigate from). Do NOT re-run mean/mass retrains.

## 2026-07-23 — 🎯 κ RISK-TILT on the CHAMPION NET (closes the farm-substrate hole) → NO-GO (7th proof, airtight)

- **Config:** clean re-run of the mean-variance certainty-equiv E[X]+κ·sd(X) at chance nodes, but on the STRONG substrate: the actual champion net (l0.003), NOT the farm evaluator the prior QD PoC used (farm reach-k3 21% / sup-max 654 was never a fair test of whether risk-tilt lifts a strong policy). New env-hook `BOT2048_KAPPA` wired into the champion net searcher in `ntuple-curriculum masshunt` (reuses search.go's existing κ branch — byte-identical to champion at κ=0). Sweep κ∈{0,0.15,0.3,0.5,0.75,1.0}, each masshunt n=200 fresh, banker-off, depth-3, movecap 3500. Branch exp/qd-mapelites (6380b89).
- **Result:** **NO-GO.** κ=0 baseline == champion: reach-k3 **186/200 (93%)**, sup-max 2254 (median 1100) — matches the ~2280/~93% reference (2254 vs 2280 = depth-3 vs depth-4; reach-k3 exact). Every κ>0 is STRICTLY WORSE on BOTH axes: sup-max 2254→2084→1894→1590→1172→1020 and reach-k3 186→172→166→121→73→43 fall together, monotonically. reach-5×512 = 0/200 at every κ. **No κ crosses 2350 — the max any κ>0 reaches (2084) is BELOW baseline.** Table (κ | sup-max | p90 | med | reach-k3 | reach-5×512): 0|2254|1796|1100|186|0 · 0.15|2084|1596|1094|172|0 · 0.3|1894|1452|1046|166|0 · 0.5|1590|1090|664|121|0 · 0.75|1172|706|322|73|0 · 1.0|1020|538|190|43|0.
- **Why / takeaway:** on the farm the squeeze at least TRADED reach for support (κ=0.4 sup-max ↑1072 as reach-k3 ↓); on the champion the risk-tilt is **strictly dominated** — it lowers support AND reach simultaneously, no trade, no ceiling movement. Support does NOT move independently of reach-k3; it never separates. This is the cleanest form of the fill-pressure death-tax: tilting a policy that already plays near-optimally into higher-variance branches just kills it faster. The 7th proof is now airtight on the class the previous test left untouched. It is the GEOMETRY, not the substrate. STOP — do not retrain risk-seeking, do not run full QD. κ hook kept (tested, inert at κ=0).

## 2026-07-23 — ❌ CURRICULUM-RETRAIN (Stacker): mass-reward is merge-INVARIANT → converges to champion

- **Config:** n-tuple retrain, `-target mass` (terminal += 0.05·sSumBoard(final)) + support-bearing curriculum (restart from d0-d3 seeds toward 3×1024+5×512, gate ≥2×256+≥3 empties). Two inits: warm-start champion, and FROM-SCRATCH. λ=0.003, α=0.10-0.15, seedp 0.45-0.60, ~15-43M episodes each arm.
- **Result:** BOTH arms converge to the champion. reach-5×512 = **0** throughout (target metric never moved). Warm-start: transient 3×512 bump (15%) faded to 2% + bank regressed 39.9k→36.8k. From-scratch: median-moves peaked 1697@4M then monotonic decline over 11M eps → champion's 1520; caps at 3×512 in 8% (WORSE than champion's 22% per R2), bank ≈ champion 38.9k. (Earlier survival-reward arms degraded outright.)
- **Why (decisive, Fable + code-verified):** `sSumBoard` = sum of tile VALUES, which is **preserved by every merge** (512+512→1024 unchanged) and grows only from spawns ⟹ terminal mass ≈ 2.2·moves ⟹ `0.05·sSumBoard` is a **disguised survival reward** (~0.11/move), blind to density. So the reward's fixed point IS the best survivor = the champion. Two independent inits → the same point = the fixed point is real, not undertraining. (Score-mass Φ variant is worse: it rewards the see-saw 4th-1024 upgrade, pointing away from the 3×1024+5×512 target.) Curriculum can't fix it — start-distribution doesn't move the TD fixed point; the ≥40% fresh episodes overwrite it back (R3-exec failure, 3rd time). And R4 stands: holding support is EV-negative on average.
- **Why / takeaway:** **retraining under an honest/mass (expectation) reward cannot break 53k — the target board is EV-negative on average, and mass is invariant to the target config.** Every run of this class converges to champion. "~10 humans hold 53k+" is an ORDER STATISTIC over huge human volume, NOT proof a mean-53k policy exists. **The bot must attack the TAIL**, not the mean: either an explicit P(bank≥53k) / risk-seeking objective, or **opportunistic cascade-finalize** — the frozen champion already banks mean 55.8k / max 63.6k when a support-bearing dense board happens to occur (R3-exec clincher); harvest that existing tail + volume. Real gap = cash-out TIMING, not survival (confirms cashout-and-champion-state memory). Do NOT re-run mass/survival-reward retrains.

## 2026-07-23 — fill-adaptive (empties-gated) support-hoarding → RED (4th proof)

- **Config:** the LAST untested degree of freedom. Leaf mult = exp(βeff·g), βeff =
  βhi·clamp((empties−3)/3,0,1) (full @ e≥6, HARD ZERO @ e≤3), phase-off @ k≥3;
  g = #512 + 0.5·(mergeable 256-pairs). Frozen champion-l0.003, banker-off, d3,
  paired seeds, n=150/arm, βhi ∈ {0,0.2,0.5,1.0,3.0}. Tools `birthempties.go`
  (Step-0), `gatedprobe.go` (Step-1). Motivation: convex death hazard → hoarding
  is ~free at high empties, so an empties-gated tilt (untested by R4/DP/const-planner).
- **Step 0 (window check):** empties at each 1024 birth — 1st: med 8 (100% e≥5);
  2nd: med 7 (91% e≥5, mode 6); 3rd: med 6 (87% e≥5, mode 6). → cheap deferral
  window EXISTS (window YES), so the lever got its fair shot.
- **Result:** peak-support MAX @k=3: champ **2254** → gated 2052/2046/2034/1768 —
  REGRESSES at every βhi. mass-rich(≥2400) 0/150 all arms; reach-5×512 0/150 all;
  n512=4 @k=3: champ 1 → **0 in every gated arm**. Guardrail: βhi=1.0 collapses
  reach-k=3 141→86; at βhi=0.2 reach-k=3 recovers to 131 (≈floor) but tail STILL
  regresses (2052<2254). No operating point is both survivable AND helpful.
- **Why / takeaway:** the hoarding premise is INVERTED — consolidated support (the
  record's need) is built by MERGING mass; the tilt rewards HOLDING unmerged
  512s/256-pairs, so it declines the consolidating merges and arrives at k=3 more
  FRAGMENTED/support-poorer. Window is real but paying into it fragments, not
  consolidates. Clean 4th proof (survival confound removed at low β) alongside R4 /
  DP / const-planner. **~53k is the honest ceiling for the value-net class — no
  asterisks.** Report: `scratchpad/filladaptive-test.md`.

## 2026-07-22 — 🧱 CEILING INVESTIGATION: ~53k is the MEASURED true ceiling (competitor 58k unreproducible)

- **Context:** competitor top-1 = 58k. Re-derived theory + ran an autonomous Fable(think)↔Opus(test) loop to break past our ~53k. Full ledger: `scratchpad/ceiling-attack-ledger.md`.
- **Theory (corrected):** score = Σ(r−1)·2^r − 4·N₄; 51k & 100k earlier ceilings were BOTH wrong. Legality max 106,496; achievability ~81k. **58k = physical capacity of a dense k=4 board** (not a cascade). k=5 dead (92% death even under strong net @d6). Spawns honest uniform (P4=0.10, no anti-player bias). Live "stat problem" = game-splitting, not merge-scoring (per-move scoring exact 0/318k).
- **Levers tested (all RED):** (1) banker target/arm tuning → destroys top-3. (2) node-cap 6M→40M → no-op. (3) leaf-shaping frozen net → 0/46 broke Φ48k. (4) endgame value-shaping RETRAIN (champion-wall, 10.7M ep) → 0/19 Φ>48k, never 2×512 under 4×1024. (R1) search-guard on 1024-adjacency → no-op (1024s already spread ~89%). (R2) soft-guard delaying 4th-1024 → net won't accumulate 512s, stalls. (R3-exec) backward curriculum from goal-2/3 seeds → seeds were jammed dead-ends, no shift after 4.7M ep.
- **Key positives found:** R3 probe — frozen champion handed a 3×1024+5×512 board banks **median 57,348, 0% death** (record IS bankable from a dense board). R3-exec clincher — from a support-BEARING board (3×1024+4×512+2×256+empties) frozen champion banks **mean 55,846 / max 63,576 / 49% ≥55k**. So finalize + survival of the record board both WORK when support exists.
- **The decider (R4 counterfactual-EV gate, frozen champion, no training):** is reaching a support-bearing dense board a policy gap or EV-truth? Support-burn decision points reachable ~37% (not a geometry wall), but **paired ΔEV(preserve−burn) = −1173 (CI ±721), replicated −1553; death-rate 0.94→0.99 (preserving RAISES death); P(≥55k)=0/180 both branches.** Burning support is HONESTLY EV-optimal — carrying an extra 256-cell into a fill-pressured endgame is fatal (the k=5 fill-pressure mechanic). The 55–63k support board is off the champion's honest forward distribution — survivor-biased, not forward-reachable without a net EV loss.
- **Why / takeaway:** **~53k is the true ceiling for our engine+banker.** The record dense board exists and is bankable if handed to us, but is NOT forward-reachable by good play — the hazard tax is geometric (fill-pressure), so almost certainly a true ceiling for any policy on 4×4, not just the champion. Competitor's 58k is unexplained but unreproducible by any of 4 rigorous mechanism-grounded levers. **STOP chasing the ceiling; the real gains are throughput/reliability (live game-splitting fix, P4=0.10 model fix, more attempts), not a higher single-game ceiling.** Autonomous loop closed (Fable flagged R4 as the last mechanism-grounded lever; RED → stop).
- **CAPSTONE — optimal endgame solver (retrograde/exact-to-terminal, `endgame.go`):** user asked to change the engine; we tested the STRONGEST possible engine — PROVABLY-OPTIMAL endgame play. Exact optimal bank by support rung: 3×1024+5×512 → 57,364 (solver validated: optimal 57,349 == champion's measured 57,348, so the champion already plays it optimally); 3×1024+**4×512** (the reachable rung) → **max 53,255, median ~52k**; 3×1024+3×512 → ~46k. The champion reaches 4×512, **never 5×512** (0/219). So optimal play from the REACHABLE board is itself ~53k. **53k is the geometric OPTIMUM of the reachable state space, not engine suboptimality — changing the engine recovers ~0.** The only >53k class (5×512) is unreachable (needs 256+256; reaching it is EV-fatal fill-pressure). ~~Definitive geometric stop.~~ **⚠️ OVERCLAIM RETRACTED 2026-07-22:** "geometric" was extrapolated — the solver went INTRACTABLE exactly on the path-to-5×512 boards (live 256/128 ladder, >2×10⁸ states), so it proved optimality only from the ALREADY-REACHED 4×512 rung, not that 5×512 is unreachable. The whole "53k ceiling" was measured as MEAN EV at small N; the record is a TAIL. R4's ΔEV=−1173 is the signature of a variance-increasing record-seeking move (we rejected it for being record-worthy); P(≥55k)=0/180 is compatible with a true ~0.5% rate. Competitor's 58k (< our own achievability ~81k) is most plausibly a reachable TAIL our EV-greedy champion prunes. Honest framing: **53k = our EV-median policy's ceiling at small N, NOT a geometric ceiling.** Decisive tail-test running (R4 forced-preserve chained, N~2000, metric P(≥55k)/max, banking off). Do NOT re-cite this as a proven ceiling until that resolves.

## 2026-07-22 — RED: risk-seeking RETRAIN (higher λ) не досягає dense record-board

- **Config:** warm-start із `champion-l0.003.bin`, чесна нагорода `-target survival` (БЕЗ
  density-shaping). Ладдер λ = 0.006/0.01/0.02 (base 1800, α=0.1); плюс gentle α=0.02
  (λ=0.006/0.01) і aggressive rebased λ=0.015 base=1400. Eval: структурна arena depth-3,
  paired seeds (seed0=2000), n=120. `reach-5×512` = on-board `final_mass ≥ 5632` (3·1024+5·512).
- **Result:** **reach-5×512 = 0/120 для ВСІХ нетів, включно з чемпіоном** (max on-board mass:
  champ 5134, gentle-0.006 5112 << 5632). λ≥0.01 (будь-яка α/base) КОЛАПСУЄ мережу
  (self-play median 1540→~300, arena median →185–192, S-sum →~2100). Єдиний СТАБІЛЬНИЙ
  risk-seeker (λ=0.006, α=0.02) гне НЕ ТУДИ: k1024=4 rate 42%→16%, median mass 4374→3870,
  forced-2048 25%→41% (рветься до 2048 на тонкій дошці), S-sum 36675→31482, σ(moves) 262→349.
- **Why / takeaway:** DROP. Чемпіон (λ=0.003) вже сидить у density-максимумі; вищий λ або
  чисельно колапсує (exp-tail blow-up на рідких довгих іграх), або, коли стабільний, міняє
  щільність на швидкий високо-варіативний ривок до 2048 — протилежне тому, що треба рекорду.
  Record-board off ANY short-fine-tuned n-tuple policy's reach у цьому net-класі; конкурентські
  58k — інший режим/артефакт. AMBER-виноска: не тестовано multi-hour from-scratch train і
  redesigned tail/record objective зі СТАБІЛЬНОЮ (clipped/CVaR) утилітою — саме форма objective,
  а не λ, — той важіль, що лишився. Деталі: `scratchpad/risk-retrain.md`.

## 2026-07-21 — 🧩 soft leaf-shaping: 1024-clustering + snake + 512-taboo (ПРОВАЛ ceiling, підтвердив see-saw)

- **Config:** гілка `exp/heuristic-levers`, білд `tmp/shape-build/ntuple/`. Чемпіон `w-l0.003.bin`,
  depth-4, чисте survival (без cashout). Додано АДИТИВНІ soft-члени в leaf У move-units ДО
  exp-трансформу: `shapedLeaf = obj.leafW(evalNonNeg(b) + shapeTerm(b))` (файл `shape.go`,
  env `BOT2048_SHAPE=1`). Три члени: (a) 1024-clustering — нагорода за близькі НЕсуміжні 1024,
  ШТРАФ за ортогонально-суміжні рівні 1024 (merge-trap); (b) snake — нагорода за монотонні
  рядки/стовпці; (c) 512-taboo — штраф за прямо-зливний 512-512 при k1024≥4, бонус за 3-й/4-й 512.
  Ваги (untuned): cluster=1.5 adj=4.0 snake=2.0 taboo=6.0 hold=4.0 spread=4.0. Інструментував
  `playSearchPeak` → `peak_phi` (=max sSumBoard дошки board_after) + `peak_phi_board`. Seeds: 16
  reacher (200025…201699) + 30 найщільніших k1024=4 champdist (2xxx), n=46/умову, 1 гра.
- **Result:** **БІНАРНО НІ — жодна гра (shaped чи baseline) не пробила Φ 48k.** max peak-Φ
  baseline **45 448** → shaped **46 664** (+1216); median 42 646 → **38 708 (−3938)** — shaping
  ПОГІРШив типову гру. Геометрія РУХАЛАСЬ як задумано: 1024 mean pairwise Manhattan 2.699→2.576,
  #512-гістограма зсунулась вправо (baseline 2:2,3:1 → shaped 2:8,3:4,4:1), АЛЕ merge-trap
  суміжності зросли 3→12 пар (adj-штраф закволий). **0 з 92 дошок будь-коли тримали k1024≥4 І
  ≥2×512 одночасно** — а 4×1024+3×512=49 152 це мінімум для >48k.
- **Why / takeaway:** SEE-SAW підтверджено й у baseline: mean#512 при k1024=3 → 1.50, при k1024=4
  → 0.81 (shaped 1.59 → 0.53). Кожен зайвий 1024 ВИТІСНЯЄ ~0.5–1.0 з шару 512. Shaping лише
  зсуває розподіл у кут low-k1024/high-512 (робив 4×512 ціною 4-го 1024) або в 5×1024 ціною всіх
  512 — але тримати обидва шари 16 клітин + taboo-independent-set НЕ дозволяють. Це гілка
  «геометрія змінилась, Φ НЕ росте → фрагментація ІНТРИНЗИЧНА»: 58k / пробій 48k **майже
  недосяжні** для цієї політики через leaf-shaping. Наступний важіль — не leaf, а АРХІТЕКТУРА
  (перетрен нету на ціль, що co-holds обидва шари); навіть тоді 10-клітинна вимога 58k-дошки
  натякає на жорстку стелю ~49–50k для однієї 16-клітинної survival-дошки. Код закоммічено
  (force-add під `tmp/shape-build/`, tmp gitignored); повний звіт — `scratchpad/shaping-poc.md`.

## 2026-07-21 — 🔴 k=5 ПІД СИЛЬНОЮ ПОЛІТИКОЮ: ВСЕ ОДНО МЕРТВИЙ (гіпотезу вбито)

- **Config:** перевірка, чи «k=5 мертвий» — артефакт СЛАБКОЇ farm-політики. Ті самі ПОСАДЖЕНІ
  дошки, що й у baseline (`plant()` з `research/hazard_k5/hazard.py`, layout CANON5 `(0,3,5,12,15)`,
  e=0..4, n=300/комірку = 1500 дощок, згенеровані `gen_boards.py`, k_start=5 звірено на всіх 1500),
  але політика роллауту замінена на СИЛЬНУ: **чемпіон-мережа `champion-l0.003.bin`** (exp-utility
  λ=0.003) + expectimax. Харнес — новий Go-сабкоманд `rollk5` у `research/hazard_k5_strong/`
  (копія ntuple-движка + `rollk5.go`; смерть = `bestMove==-1`, тобто затор АБО forced-2048 —
  байт-в-байт те саме означення, що baseline). Горизонт 100, p4=0.1, workers=5.
- **Result:** смерть ≤100 ходів **НЕ падає** з силою політики:

  | e | baseline farm (лог) | STRONG champ d4 | STRONG champ d6 | WEAK farm d6 (мій харнес) |
  |---|---|---|---|---|
  | 0 | 97.0% | 93.7% | 87.0% | 97.7% |
  | 1 | 93.3% | 95.7% | 85.0% | 93.3% |
  | 2 | 96.7% | 98.0% | 93.3% | 97.3% |
  | 3 | 99.3% | 99.7% | 96.3% | 100.0% |
  | 4 | 100% | 99.7% | 98.7% | 100.0% |
  | **усе** | **93–100%** | **97.3%** (1460/1500) | **92.1%** (1381/1500) | **97.7%** (1465/1500) |

  Медіана виживання: WEAK farm d6 = **24** ходів, champ d4 = **24**, champ d6 = **31** (треба ~200).
  Додатково: спеціалізована анти-k5 мережа `champion-wall.bin` (шейпінг «виживи k5 + щільний k4»)
  при d6 = **91.9%** смерті, медіана 26 — теж не рятує. Мій WEAK-farm контроль (97.7%) відтворює
  baseline-таблицю ⟹ харнес валідний.
- **Why / takeaway:** гіпотезу «k=5 death — артефакт слабкої гри» **ВБИТО**. Найсильніша конфігурація
  (чемпіон, глиб.6) знижує смерть лише 97.7%→92.1% і залишається 85–99% по комірках — і близько не
  до порогу 60–70%, що відкрив би k=5. Медіана виживання росте максимум 24→31 ходів проти ~200
  потрібних. **k=5 — геометрично мертвий незалежно від політики.** Стеля лишається ~58–62k ⟹
  курс — **мульти-каскад на k=4 + тайминг кеш-ауту**, а НЕ «як жити на k=5». 5-й резервуар —
  уникати, не будувати (підтверджує §k=5 від 2026-07-11). Харнес: `project/research/hazard_k5_strong/`.

## 2026-07-12 — 🔴 doom-oracle тайминг кеш-ауту A/B n=50 — **МЕРТВИЙ**

- **Config:** `-doomcash` + `-maxcashout` на чемпіоні; adversarial AND-OR
  survival-оракул (M=16 плаїв, empties-gate ≤6, k≥3) ґейтить ТАЙМИНГ кеш-ауту.
  Парний A/B n=50 (продовження smoke-запису нижче).
- **Result:** **МЕРТВИЙ. Запалює ЗАРАНО** — cashed **96%** проти baseline **26%**;
  медіана **40088→34824 (−5264)**; max **47812→41192**.
- **Why / takeaway:** adversarial spawn-модель **неправильної форми** (worst-case
  spawn занадто песимістичний) → оракул кричить «doom» задовго до реальної
  загрози, банкує рано й ріже хвіст. Відкинуто (спростовує smoke-оптимізм нижче:
  smoke n=2 брехав, як завжди під навантаженням хвоста).

## 2026-07-12 — 🔄 MASS-ретрейн у роботі (all-in гемблер, `-target mass`)

- **Config:** ретрейн objective `-target mass -massw 0.05` — «all-in гемблер»:
  термінальна цінність = `mass*S − sum` (винагороджує накопичену масу дошки, не
  довжину). k=5 як ціль ВІДКИНУТО заздалегідь (виміряний hazard 93–100% смерті
  на 100 ходів — див. запис k=5 нижче).
- **Result (interim):** ~5–7M епізодів, метрика РОСТЕ — медіана 1267→~1990 і не
  плато. Навчання триває; не заважати процесу.
- **Why / takeaway:** перевіряємо, чи «жадібна до маси» ціль дає товщий правий
  хвіст, ніж чемпіон (λ=0.003). Вранці — парний A/B **mass-vs-champ** (n≥400).
  Поки що лише проміжна крива, не рішення.

## 2026-07-12 — 🎯 target-conditioned солвер ЗІБРАНО (`-exact`, замінює EV-правило)

- **Config:** новий `-exact` — expectimax, чия ТЕРМІНАЛЬНА цінність = індикатор
  успіху `1[final≥T]`, тож `V(board)=P(final≥T)`. Guard: вмикатися лише коли
  `maxFinal(b)≥T` (лінія ще може дійти до T); region-gate: лише коли
  `countEmpty(b)≤BOT2048_EXACT_EMPTIES(=5)`. У регіоні солвер ВОЛОДІЄ вибором
  ходу (P-argmax, тай-брейк по E[banked]) і сам запалює 2048, коли це оптимально;
  тонкомаржинальний незворотний fire ґейтиться на `P*≥BOT2048_TARGET_PCONF(=0.95)`.
  Замінює EV-правило (`-evfire`); ev/doom пригнічені під `-exact`.
- **Result:** зібрано, **`go build` OK**, закомічено на гілці
  **`feat/ntuple-exact-solver`** (off `feat/ntuple-champion`). Телеметрія:
  `cashed_out / final_mass / endgame_banked / peak_potential / pstar_at_fire /
  reached_target`. A/B — після ретрейну.
- **Why / takeaway:** рекорд — це ПОРОГОВА подія, тож правильна ціль —
  ЙМОВІРНІСТЬ P(final≥T), а не E[banked] (яке ріже хвіст, див. EV-fire нижче).
  ⚠️ **Застереження для A/B:** при `BOT2048_TARGET=45000` seed 1000 солвер ударив
  3M node cap (`BOT2048_EXACT_NODES`) і впав у fallback (P=0 → чемпіон-мережа) —
  для чесного A/B **підняти `BOT2048_EXACT_NODES` АБО знизити `BOT2048_EXACT_EMPTIES`**,
  щоб solve завершувався в регіоні.

## 2026-07-12 — 🎯 BANKING LEAK FIXED: earlyfire 0/16 → 13/16 (record banked, no retrain)

- **Config:** wall net `champion-wall-snap-0316.bin`, 16 reacher seeds, `ntupleTexact -cashout -maxcashout -exact`,
  depth 4. Baseline `EARLYFIRE=0` vs fix `EXACT_EARLYFIRE=300 EXACT_EARLY_EMPTIES=1 EXACT_EMPTIES=8 NODES=6M PCONF=0.6`.
- **Result:** baseline **0/16** banked ≥49601 (max 48980; exact solver hits its node cap even @20M, returns P=0,
  defers to the net → fires only at jam = single forced +2048). Fix → **13/16** banked, max **53000**. Safety: 30
  NORMAL seeds off vs on = **bit-identical** (median 38952, 0/30 changed) — earlyfire is inert on non-record games.
- **Why / takeaway:** BANKING (not reach) was the leak — reach≈0.9% but realized≈0.06% ⇒ ~1/16 banked. Fire the
  score-maximiser HEURISTICALLY at `pot≥target+300` with `countEmpty≥1` (window `[EARLY_EMPTIES, EXACT_EMPTIES]`),
  NOT waiting for the intractable exact P-confirm. KEY: potential/density are coupled (record pot only at <5 empties)
  ⇒ `EARLY_EMPTIES` must be LOW (1, not 5) or the window never sees the peak. Realized rate ~0.06%→~0.73% (~12×),
  net unchanged, no retrain. Deploy = port earlyfire+exact+cashout into `serve.go` (the live bridge).

## 2026-07-12 — ❌ опустити банкір-поріг під рекорд-бар 48500 (мертва смуга) — DEAD END

- **Config:** Andrii-бар: рекорд=48500, топ-3=50к. Ідея — страхувати дошки, що пікають у
  «мертвій смузі» 48500–49900 (телефон f9ab4476: peak 49552 → банк 45612, злив 3940), бо
  поточний банкір озброюється лише на cashValue≥49901. Дві спроби на 10 reacher-сідах
  (wall-нет, depth4, EMPTIES=8 NODES=6e6 EARLY_EMPTIES=2): (A) TARGET 49601→48500;
  (B) хірургічно лише арм — TARGET=49601, EARLYFIRE=-800 (арм≈48801).
- **Result:** (A) 2/10 обвали: 230210 51024→44404, 230250 50536→44984 (солвер сатисфайсить
  на нижчій цілі). (B) **9/10 регресій -1352…-5116** (230279 -5116, 230035 -4900,
  230293 -4752); top-3 8→(значно менше), лише 230148 незмінний. Причина: раннє озброєння
  капає дошку біля точки арму (~47-48к) — wall-нет не встиг добудувати до 51к.
- **Why / takeaway:** DROP назавжди. Пізній арм (49901) — майже оптимум: дає reach-нету
  добудувати до піку, тоді банкір бере overshoot 50-51к (топ-3). Обрізати заради нижньої
  планки = жертвувати верхньою рукою (доведено 5×, узгоджено з soft-not-hard-overrides).
  Єдиний теор. безпечний фікс мертвої смуги — справжній peak-детектор (банк на *спаді*
  cashValue), але ризик>виграш при reach-вузькому місці. Банкір НЕ чіпати: T=49601 arm=300 EE=2.

## 2026-07-12 — ✅ peak-potential пре-чек: РЕКОРД ДОСЯЖНИЙ (ендгейм НЕ мертвий)

- **Config:** пре-чек `peak_potential` = max по траєкторії `cashValue(board)` =
  найкращий очікуваний ФІНАЛЬНИЙ score (у sSum-одиницях, apples-to-apples з T) на
  партіях чемпіона. Порівняння smoke n=5 vs A/B n=50.
- **Result:** n=5 **ОМАНЛИВИЙ** — 0 перетинів T, capped ~45.7k (виглядало як
  «стеля»). n=50 показав РЕАЛЬНИЙ хвіст: **2% партій чемпіона мають
  peak_potential≥49601** (max **50253**, p99 **49209**).
- **Why / takeaway:** **рекорд ДОСЯЖНИЙ, ендгейм НЕ мертвий** — потенціал є, ловити
  його має таргет-солвер. Ще один urok verify-numbers: **n=5 — це не дані** (мало
  не поховали живий напрям через smoke-«стелю»).

## 2026-07-12 — ⚡ perf-стек движка: leaf-cache + PGO + GOAMD64=v3 + workers=8

- **Config:** швидкісний трек на чемпіоні (w-l0.003, movesцель), гілка `perf/engine-speedup`.
  Чотири зміни, кожна bit-exact: (1) **leaf-value cache** — lock-free direct-mapped
  `board→float64`, Hyatt-XOR, 2^22×16B=64MB, повний float64 (не float32!), пробиться на
  cprob/depth-cutoff через `s.leaf`; НЕ інвалідується (чиста функція дошки), переживає rungs
  і ходи; default ON, kill-switch `BOT2048_LEAF_CACHE=0`. (2) **PGO** — `default.pgo` з
  ендгейм-профілю, `go build -pgo=auto`. (3) **GOAMD64=v3** (CPU підтримує, без SIGILL; на
  amd64 Go не фьюзить FMA → результати не міняються). (4) **workers 6→8** (env-only,
  `BOT2048_SEARCH_WORKERS=8`). Бенч: 150 ендгейм-дошок (empty≤3) з games.db, fixed
  `(depth=5,cprob=1e-4)`, серійний шлях детермінований → чексум рішень як bit-identity-гейт.
- **Result:** серійний (bit-identity) шлях baseline 38089ms → v3+PGO 32185ms (1.18×) →
  +leaf-cache 17292ms = **2.20×**, nodes/s 1.97e5→4.33e5. Чексум `1c04de39f1130696`
  ІДЕНТИЧНИЙ на всіх кроках (baseline / v3+PGO / +cache) — bit-exact підтверджено.
  Leaf-cache hit-rate **60.9%** (вища за оцінку скаута — ендгейм має багато
  spawn-order транспозицій). Workers sweep (v3+PGO, cache ON, fixed-work): w6=3940ms,
  w7≈3585ms, **w8≈3410ms (найкраще + найстабільніше, 5.07× vs serial)** — leaf DRAM-latency-
  bound, гіпертреди ДОПОМАГАЮТЬ, резерв 2 ядер (NumCPU−2) втрачає throughput. Live-шлях
  (parallel) OLD(v1,off,w6) 7837ms → NEW(v3+PGO,on,w8) 3501ms = **2.24×**. Тести
  `TestParallelRungMatchesSerial` (99.2%, той самий float32-drift), `TestEval*` — PASS.
- **Why / takeaway:** усі 4 IN, ЖОДНОЇ зміни ходів чемпіона (серійний чексум незмінний).
  ~2.2× compute на ендгеймі ≈ **+1 rung cprob-горизонту** за той самий бюджет (budget-mode
  800ms: mean-deepest-rung 9.72→9.80). Білд: `GOAMD64=v3 go build -pgo=auto`; лаунч
  `BOT2048_SEARCH_WORKERS=8`. Дефолт-формула workers НЕ змінена (портабельність). Ваги
  недоторкані. Усі виміряні числа — у цьому ж записі (вище, «Result»).

## 2026-07-12 — движкова латенсі-оптимізація (330мс→<300мс): 1 виграш, 4 дохлих

- **Мета:** зняти ~40мс з fast-move (330мс) щоб влізти під 300мс UI-підлогу на Windows i7-9700K
  (більше ігор за годину → більше рекорд-спроб). Fable(напрям) + 2 recon-агенти + виміри.
- **✅ ЄДИНИЙ виграш — `--prefetch` ON:** движок уже має спекулятивний prefetch (serve.go) —
  рахує наступний хід на всіх ядрах ПІД ЧАС 300мс свайпу+скріншоту, віддає з кешу ~8мс.
  Live крутився з `--no-prefetch` (бо колись «live не time-rushed»). Bit-identical
  (abort-on-stale, serve.go:28-30). Ховає весь DRAM-bound пошук → каденс <300мс.
- **❌ Build-флаги:** `GOAMD64=v3 -pgo=auto` вже в BUILD.md і вже застосовані; PGO-delta
  на fast-шляху в межах шуму (профіль ендгеймний). Виграшу нема, лише перевірити `go version -m`.
- **❌ Leaf-cache 64→128/256MB** (`leafCacheLogSize` 22→23/24): hit-rate 68.4%→68.5%→68.6%.
  31% промахів — **compulsory (first-touch), не capacity**. Мертвий важіль. Не мержено.
- **❌ Workers для fast-move:** серійний шлях (`bestMove`, budget_ms=0) — SEARCH_WORKERS
  ігнорується; на budget-шляху 8 воркерів *повільніше* за 1 (пік ~4). Non-lever.
- **❌ Node-cap `EXACT_NODES` 6M→30M:** 10/10 reacher-сідів **delta 0** (банкає cap-незалежний
  earlyfire-maximiser depth-8). +пам'ять 1.5GB/хід за нуль виграшу. Не деплоїмо.
- **Профіль fast-move:** 75% = обробка листа (net-eval 61%, з них idx-gather у 57MB таблицю
  = 43% — DRAM-latency bound; i7 у 9x повільніший за Mac саме тут); дерево лише 14%.
- **Висновок:** движок вичавлений. `--prefetch` — єдиний важіль. Рекорд вирішує к-сть ігор.
- **На пост-сесію (Fable):** verify-before-fire — консультувати exact-солвер (великий кеп)
  ПЕРЕД незворотним 2048-fire; зараз earlyfire оминає солвер і грає E[score]-жадібно. Правильне
  застосування безлімітного часу на хід у ендгеймі, але це код+ревалідація, не під дедлайн.

## 2026-07-12 — perf: MLP-eval gather-split — НЕГАТИВНО на M4

- **Config:** векторизація leaf-eval через gather-split MLP-шлях (спроба
  прискорити оцінку листа).
- **Result:** **НЕГАТИВНО на M4** — leaf уже ~270 нс, split не окупається (накладні
  gather > виграш).
- **Why / takeaway:** запхано за прапорець `BOT2048_EVAL_MLP` (default OFF) — може
  виграти на x86, але на M4 не вмикати. Не блокує основну лінію.

## 2026-07-12 — depth 5 vs 4 (ntuple net, record pipeline) — depth 5 CATASTROPHIC

- **Config:** same 16 depth-4-reacher seeds + earlyfire fix; `-depth 5` vs `-depth 4`.
- **Result:** depth4 **13/16** banked (median 51076, max 53000) → depth5 **0/16** (median 38056, max 43896). EVERY
  seed dropped, −5k to −32k. Cost also spiked (load 33 vs depth-4).
- **Why / takeaway:** DROP depth 5. Deeper reach-phase search plays entirely different games that miss the record
  structure (anti-tail, dramatic — not the −2.3k of farm depth-7 but a −13k median cliff). Caveat: seeds are
  depth-4-selected (biased against d5). But prod fast-phase=med-4, ALL our records are at depth 4, and the farm
  depth≥7 anti-tail prior all agree → keep **depth 4**. Compute belongs in the endgame SOLVER, not deeper net search.

## 2026-07-12 — EV-fire (E-based аналітичне правило) A/B n=50 — SUPERSEDED

- **Config:** `-evfire` (аналітичне EV-правило запалу кеш-ауту, E-максимізація) +
  `-maxcashout`, парний A/B n=50 проти чемпіона.
- **Result:** медіана **41088 (+1000)** АЛЕ max **45276 (−2536)**, cashed **84%**.
- **Why / takeaway:** E-максимізація підіймає медіану, але **вбиває правий хвіст**
  (max падає) → **неправильна ціль для порогового рекорду**. Superseded
  таргет-умовним `-exact` солвером (P(final≥T) замість E[banked]).

## 2026-07-11 — 🟢 LIVE: n-tuple CHAMPION на телефоні (mode 0, serve.go) — перші катки

- **Config:** `feat/ntuple-serve` ntuple + `w-l0.003.bin`, **mode 0 = net-leaf (champion)**, depth 3 фікс,
  `--no-prefetch`, `--capture apk`, `--auto-login`. Engine через `BOT2048_ENGINE2048_BIN`→ntuple.exe.
  Це «сира» мережа БЕЗ endgame-системи (§7 брифа ще не змерджена).
- **Result (живі катки):** 31 868 (1709 ходів, natural deadlock, 3×1024) · 22 940 (1236 ходів,
  **vision_mismatch** — перцепційний абор, не справжній game-over) · 15 040 (748 ходів,
  **forced win_2048** — мережа злила 1024+1024, гра завершилась). Латентність (1731 хід): **344/375 мс/хід**,
  packet-age drain **0.2мс**, IPC 0.1мс, apk-gate 92%, свайп 141 (=80 жест + ~60 Android `input`-spawn),
  settle 156. Движок 25мс на критичному шляху (prefetch OFF).
- **Why / takeaway:** (1) сира мережа без табу/safety РОБИТЬ ранній forced-2048 (748 ход, 15k) — це той
  **~22% лівий хвіст**, який ловить safety-search із §7; **mode 0 без §7 ненадійний** для рекорду.
  (2) наступний крок — budget_ms=200 + median-ескалація (гілки в роботі), НЕ prefetch (live не time-rushed).
  **Важливо для наступних живих прогонів:** ⚠️ `--capture scrcpy` дає **хибний FATAL** (H.264 board-match 81% + завжди-
  присутня «Нова гра» → SCREEN_UNEXPECTED) — live ТІЛЬКИ `--capture apk`. ⚠️ prefetch у serve.go я зробив
  **реальним** (concurrent fan-out, commit 8e75259) — бриф казав no-op; але для місії не критичний.

## 2026-07-11 — 🔴 k=5 РЕЗЕРВУАРІВ: ФУНКЦІЯ РИЗИКУ ВИМІРЯНА. **k=5 МЕРТВИЙ.**

- **Config:** бот сам до k=5 не доходить ⟹ стан ПОДАРОВАНО. Три незалежні конструкції, farm depth 6,
  adaptive off, p4=0.1, baseline-ваги. Горизонт 100 ходів; смерть = затор АБО forced-2048.
  (1) синтетична «чиста» дошка: 5×1024 + драбина 256…2 (маса 510 — рівно те, що дає модель для k=5)
  + пил, рівно `e` порожніх, драбина кладеться вздовж snake-градієнта самого рушія; n=300/комірку.
  (2) та сама, але ранги розкидані випадково («messy»); n=150.
  (3) **РЕАЛЬНІ дошки бота** (60 партій, знімки кожного плая з k=3/k=4) + ПОДАРОВАНІ 1024
  у найбезпечнішу порожню клітинку (безкоштовно +1024 маси за 1 клітинку — рівно та угода,
  яку модель називала вигідною). Парно: та сама дошка, той самий сід спавнів, різниця — лише резервуар.
  Харнес: `project/research/hazard_k5/`.
- **Result:** `P(смерть ≤100 | k=5, e)` — **93–100% ПРИ БУДЬ-ЯКОМУ `e`**:

  | e | синт. «чиста» (n=300) | синт. messy (n=150) | реальна дошка + 2 подарунки (n=80) |
  |---|---|---|---|
  | 0 | 97.0% [94-98] | 99.3% | 100% [95-100] |
  | 1 | 93.3% [90-96] | 100% | 100% [95-100] |
  | 2 | 96.7% [94-98] | 100% | 100% [95-100] |
  | 3 | **99.3%** [98-100] | 100% | **100%** [95-100] |
  | 4 | 100% [99-100] | 100% | — |

  Медіана виживання при k=5: **36/43/29/17/12 ходів** для e=0..4. Треба ~200.
  **Парний зріз (та сама дошка, той самий сід, n=664):** k=3 → смерть 49.2%, μ=69.9 ходів ⟹
  +1 резервуар → **92.9%, μ=28.0** ⟹ +2 → **100.0%, μ=6.0 ходів.**
  **Калібрування:** контрольне плече (реальна дошка, БЕЗ подарунка) відтворює виміряну таблицю
  k=3 (66/41/46/36/10/5% проти 57/36/19/10/2/1%) ⟹ харнес валідний, якщо і зміщений, то ВГОРУ
  по виживанню… ні, у бік БІЛЬШОГО ризику — тобто абсолютні числа можуть бути завищені в 1.5–3×
  у зоні малого ризику. Але при 93–100% це нічого не змінює.
  **k=6:** 100% смерть, і на 18–76 із 80 реальних дощок 6-й 1024 **нікуди поставити** без миттєвого табу.
- **Why / takeaway:** **Екстраполяція (75–90%) була ЗАНАДТО ОПТИМІСТИЧНОЮ. Шлях через k=5 не існує.**
  Механізм — не пакування, а **розпад робочої зони**: перебір каже «11 зв'язних клітинок, драбині треба
  10, влазить із запасом», але **1024 ЇЗДЯТЬ по дошці при кожному свайпі**. Виміряно в реальних роллаутах:
  найбільша ЗВ'ЯЗНА робоча зона падає **нижче 10 клітинок у 44% плаїв**. Статичний перебір розстановок
  описує стан, якого гра не тримає. Табу теж кусається: 5.0–7.3% плаїв при k=5 (проти 0.027% при k≤4),
  ≤1 безпечний напрямок у 1.8–3.3% плаїв.
  **Жодна з 36 розстановок не рятує:** усі 97–100% при e=2. Найкраща — `(0,3,5,12,15)` («4 кути + 1»,
  саме та, що в ТЗ): μ=31.7 ходів проти 3.8 у найгіршої `(0,6,9,12,15)` — 8× за часом життя, але
  все одно 97% смерті. Топ-6 — усі «кутові». Це терм евристики, але не порятунок.
  **Наслідок для δ:** дешевого шляху (δ≈1.38 через k=5) НЕМА. Лишається **δ≈2.47 через k=4** —
  тобто тримати ~1556 робочої маси на 12 клітинках. ⚠️ **І з цього випливає ГОЛОВНЕ: 5-й резервуар треба
  НЕ БУДУВАТИ, А УНИКАТИ.** За розкладом маси k=5 народжується на ~ході 2360, а ціль — 2569 ⟹ наївний
  біг до рекорду ГАРАНТОВАНО проходить через k=5 і там гине. Поточна евристика 5-й резервуар
  **винагороджує** (`sSum` росте з кожним 1024) ⟹ потрібен терм, що забороняє злиття 512+512 при k=4.
- **Виправлення до §7 FACTS (перебір перероблено з нуля):** для k=5 максимальна ЗВ'ЯЗНА зона = **11**,
  а не 10 (36 сирих розстановок = 6 класів за симетрією); для k=6 = **9**, а не 8. Для k=4 повністю
  зв'язних розстановок **193**, а не лише «4 кути».

## 2026-07-11 — 🎯 doom-oracle: тайминг кеш-ауту (smoke, n=2 gated; n=50 A/B у роботі)

- **Config:** прапорець `-doomcash` на чемпіоні (λ=0.003, depth-4) + `-maxcashout`
  ендгейм-максимайзер. Глибокий adversarial AND-OR survival-оракул (M=16 плаїв,
  empties-gate ≤6, k≥3) ґейтить ЛИШЕ ТАЙМИНГ кеш-ауту — НЕ вибір ходу (мережа грає
  вільно, оракул тільки вирішує, КОЛИ передати керування максимайзеру). Якщо жодної
  лінії виживання немає → максимайзер банкує злиття й запалює термінальний 2048.
- **Result (smoke, depth 2, seeds 1000/1002):** baseline застряг на rank-10 (злив 2048,
  cashed=0); doomcash дійшов до rank-11 (2048 ЗАБАНКОВАНО) із вільними клітинками —
  seed1000 +1772 score (34 764→36 536), seed1002 +2684 (33 192→35 876), запалив
  ~на 32 ходи раніше з місцем під банк. OFF-шлях байт-у-байт = чемпіон.
- **Why / takeaway:** механізм працює — перетворює «затор-на-1024» на «забанкований 2048
  з місцем». Це прямий міст-спроба для 47 812→49 600 (ТАЙМИНГ кеш-ауту, НЕ виживання).
  Парний A/B n=50 (depth 4) крутиться, щоб підтвердити, що приріст score тримається.

## 2026-07-11 — принцип soft-vs-hard override (підтвердження, крос-референс)

- **Config:** n/a — фіксація правила за трьома незалежними вимірами.
- **Result:** тепер ВЖЕ 3× підтверджене правило: SOFT leaf-нудж, який value-мережа може
  перекрити, ДОПОМАГАЄ (adj-1024: медіана 2067→2108); HARD move-вето/risk-backup, що
  ПРИГНІЧУЮТЬ мережу, ШКОДЯТЬ (safety-бандл: медіана 2067→1982, best score 47 812→42 292).
- **Why / takeaway:** правило надалі — покращувати через SOFT перекривні-мережею leaf-фічі
  або ретрейн objective, а НЕ через hard root-вето. (Див. також MEMORY: soft-not-hard.)

## 2026-07-11 — soft-leaf тулкіт чемпіона (зібрано, тільки smoke)

- **Config:** три нові SOFT, перекривні мережею leaf-БОНУСИ на чемпіоні, кожен env-ґейтед,
  default 0 (OFF = чемпіон незмінний): `BOT2048_EMPTIES_W` (бонус за порожню клітинку,
  анти-затор), `BOT2048_EDGE1024_W` (бонус за 1024-плитки на зовнішньому кільці),
  `BOT2048_MONO_W` (монотонність дошки). Слідує доведеному патерну adj-1024 (move-units,
  clamp leaf≥0 — щоб зберегти теорему табу/кеш-ауту).
- **Result (smoke, 1 гра, seed 1000):** OFF == чемпіон байт-у-байт; `EMPTIES_W=50` →
  ходи 1724→1912, score 31 624→35 324, без forced_2048 (очікуваний анти-затор напрямок).
  1 гра — це не дані.
- **Why / takeaway:** шляхи виконуються коректно, напрямок перспективний; у черзі на
  per-feature A/B n=50 за doom-тестом.

## 2026-07-11 — depth 7 (farm)

- **Config:** depth 7 vs 6, same weights, 6 seeds.
- **Result:** mean DROPPED 35.1k→32.8k; forced-2048 jumped 5%→33%.
- **Takeaway:** DROP. Depth ≥7 hurts farm; working depth ceiling = **6**.

## 2026-07-11 — Score metric confirmed (rules Q5)

- **Config:** n/a — audit of the reported score vs the game.
- **Result:** `summary.final_score` = classic cumulative 2048 score = the on-screen
  «Рахунок» / leaderboard score. Proven from the live 25264 board (P(4)=9.71% vs
  independently measured 9.77%). farm S-sum is only an internal search proxy.
- **Takeaway:** `P(score≥48k)` targets the REAL number. No change needed.

## 2026-07-11 — STATE: HEUR2 convex already opened the tail — 50k is now a density/frequency problem

- **Finding:** the current baseline (HEUR1 snake + HEUR2 convex) reaches 3×1024 in **10/12** seeds
  and 4×1024 in **1/12** (40460). The old "32k comfort-lock" (path_to_50k, measured pre-HEUR2) is
  GONE — HEUR2 convexity removed that ceiling.
- **Takeaway:** the gap to 48k is now **density** (rich 512/256 support so 4×1024 is worth ~48k not
  ~40k) + **tail frequency**, not "the engine can't build 3-4× 1024". Active levers: ★2 multi-reservoir
  gradient (HEUR4), density rewards, jackpot ensemble. Hoard-retry idea: late-empty ~200-260 + an
  empty gate (`countEmpty>=5`, only push when there is room; current gate ignores empty → early jams).

## 2026-07-11 — Lever 2 (`cprob` = the real depth knob): env-tunable, and 1e-5 is TOO EXPENSIVE TO USE

- **Config:** new env knob `BOT2048_CPROB_THRESH` (search.go `cprobThreshBase`, was a compile-time
  const; default UNCHANGED at 1e-4, so nothing moves unless a config pins it). Added to
  `FARM_ENV_VARS` and to the engine's `version` reply, so it is now part of a run's provenance.
  Probe: `probe-d-cprob-1e5.json` — baseline weights, `CPROB=1e-5`, depth 6, seeds 1000..1012
  (13 games, 13 workers, m4pro, engine `6127de09`).
- **Result:** **422.6 ms/move** — vs the ~27 ms/move the freshly-SWAR'd engine does at 1e-4, i.e.
  **~15.6× slower**, not the ~2× the plan assumed. 13 games took **796 s wall on 13 cores**; a
  100-seed arm would be **~100 minutes**. Game quality on those 13 seeds: median **1762 moves** /
  32 456 score, mean 1767 / 32 676, **σ(moves) = 132** (very tight), max 2097 / **41 164**,
  score = **18.49 × moves** (re-confirms the audit's linear law). 13/13 reached 3×1024; exactly
  **one** reached 4×1024 (seed 1005) — and that one **died by forced-2048** (`tile_cap`, 1/13 = 7.7%).
- **Why / takeaway:** **DROP 1e-5 as a production setting** — on cost, not on quality. It is ~15×
  the compute for a lever we cannot even afford to measure at n=100, and the σ(moves)=132 says it
  makes games *more* uniform, which is the wrong direction for a TAIL target (σ matters ~5× more
  than μ at +3σ — see strategy-2026-07-11 §6). The knob itself is worth keeping: it is now the
  honest name for the depth we actually search (mean leaf depth 4.6, not 6), and the cheap
  direction — **raising** it to buy ~15× more games per hour — is untested and now trivial to test.
  Also worth noting: the only game that built the 4th reservoir was killed by the taboo, so
  forced-2048 is the failure mode of exactly the games we care about, even though it is ~0% overall.
- **NOT MEASURED:** lever 1 (the corner double-count: `CORNER_W=0` + `CORNER_RANK_W` 320/640) and
  the `CONVEX_W=0` arm. Configs are committed and ready (`lever-{a,b,c,d,f}-*.json`, 100 paired
  seeds each, ~8 min/arm at 1e-4); the machine was reclaimed before they ran. **These are the
  cheap arms and they are the ones to run next.**

## 2026-07-11 — Key target refinement

- **Finding:** 4×1024 ALONE ≈ 40k (thin board). 48k+ needs 4×1024 **plus dense
  high-tile support** (512/256 between the reservoirs).
- **Takeaway:** tune for "4 reservoirs + density", not just "reach 4×1024".

## 2026-07-11 — HEUR3 ★1 stage-switching "hoard" (default late weights) — DROPPED

- **Config:** at ≥2× 1024 switch to late weights (empty 340→120, grad ×2.0,
  corner_rank 320); env `LATE_*` / `HOARD_TRIGGER=2`. A/B vs HEUR1+HEUR2 baseline,
  seeds 2000+.
- **Result (full A/B):** net **−27k**, high-variance but net NEGATIVE. Destroyed
  the tail — seed 2005's baseline **4×1024 (40460) → 3×1024 (30792)**; provoked the
  **first forced-2048s** (baseline had 0); hoard max **36872 < baseline 40460**. A
  few seeds improved with richer 512-support (2001/2002/2006), but the losses (early
  jams from too-low empty weight) dominate.
- **Takeaway:** **DROP** with these weights. Default disabled (`HOARD_TRIGGER=99`);
  env knobs kept for future GENTLER experiments (higher trigger, smaller empty drop).
  Lesson: aggressively cutting empty weight late → premature jams that cost the 3rd
  reservoir AND force 2048. Density must come without starving mobility.

## 2026-07-11 — HEUR2 endgame empty-convexity

- **Config:** added convexity reward for endgame empty layout; env `CONVEX_W`.
  Baseline arm CONVEX_W=0 = HEUR1-equivalent.
- **Result (baseline HEUR1+HEUR2, 12 seeds):** mean **~32.8k**, max **40460**,
  10/12 reach 3×1024, one 4×1024, zero forced-2048.
- **Takeaway:** KEEP. Baseline already reaches 3×1024 regularly and occasionally
  4×1024 — well past the old ~30k. Weight A/B pending on Mac.

## 2026-07-11 — HEUR1 structural (snake + rank-corner + stair)

- **Config:** boustrophedon snake gradient (top-left), rank-scaled corner
  (1024-in-corner ≫ 256), stair enabled. vs flat-corner baseline.
- **Result:** mean +2.6k (30.0k→32.6k), median +1.2k, floor +8.9k (killed
  catastrophic jams), forced-2048 0. But upper tail did NOT widen (single snake
  optimizes ONE corner monolith, structurally chokes 3rd/4th reservoir).
- **Takeaway:** KEEP as a base, but insufficient alone. Real lever = 4 SEPARATE
  reservoirs + density (not one snake).

## 2026-07-11 — 2048 stop-rule CONFIRMED LIVE (foundation of the whole route)

- **Config:** 3× live classic games (`--strategy go --depth 6`, NO `--farm`, APK capture),
  deliberately driven to 2048; user filmed the win with an external camera.
- **Result:** every game merged two 1024s → 2048 → app immediately left GAME state
  (`live.apk_not_in_game` next frame, `live.win_popup_2048`, `live.session_done`); the
  merge added **+2048 to «Рахунок»** before the stop (user-observed on camera). Win screen
  is **FLAG_SECURE** → host `adb screencap` returns 0 bytes during the win transition.
- **Why / takeaway:** the n=1 assumption is now n=3 + direct observation — **max score =
  accumulated board score + terminal +2048 cash-out merge; game never self-ends on the
  (cosmetic) timer, only on deadlock or the deliberate 2048 merge.** Green-lights the
  4×1024→cash-out plan. Also surfaced: APK-mode end-of-game PNG is stale (startup frame) →
  fix in flight; `--stop-before-win` flag in flight for controlled brink tests.
