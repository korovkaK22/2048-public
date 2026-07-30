package main

// ---------------------------------------------------------------------------
// LIVE RECORD ENGINE — the banking stack ported from the offline arena.
//
// This file carries the machinery that turns a near-record endgame board into a
// deliberately BANKED >= T final score, instead of the blind deep net-value
// search that the golden live engine otherwise runs. It is the integration of
// four arena pieces into the live serve/arena binary:
//
//   - exactSolver          : the target-conditioned expectimax whose terminal
//                            value is the success INDICATOR 1[final >= T], so
//                            V(board) = P(final >= T | optimal play). Drives
//                            MOVE SELECTION once engaged.
//   - scoreSearcher        : the endgame SCORE-maximiser (E[final banked score]),
//                            which banks every beneficial merge and does the
//                            game-ending 1024+1024->2048 merge LAST.
//   - the EARLY-FIRE hook  : once the maximiser already expects to bank >= T
//                            (pot >= T + earlyFire) and free cells remain, hand
//                            the position to the maximiser NOW rather than drift
//                            to a jam and bank the merges too late.
//   - cashout / playCashout: the full-game driver used for the arena repro and
//                            the focused test (self-play with a simulated rng).
//
// EVERYTHING here is dormant unless the master flag BOT2048_RECORD_ENGINE=1 is
// set (recordConfig.on). With the flag off, none of this code is reached from
// serve.go / main.go, so the default engine is byte-for-byte the golden engine.
//
// SCORE MODEL (codebase-consistent, see exactSolver docs below): the monobank
// score of a board is sSumBoard(board) = sum over tiles of S(rank) =
// (rank-1)*2^rank. A merge of two rank-r tiles adds moveScore = 2^(r+1). The
// running game score therefore equals sSumBoard(current board), so
// need = T - sSumBoard(board) is a pure function of the board.
//
// DETERMINISM. The banking decision is a FIXED-NODE solve (nodeCap) plus a
// FIXED-DEPTH score search — no wall-clock branch anywhere — so a given board
// always yields the same move regardless of how much think time is available.
// The serve path calls this in place of the time-bounded budget ladder in the
// endgame, satisfying "the budget feeds the certified banker, not the net".
// ---------------------------------------------------------------------------

import (
	"fmt"
	"math"
	"os"
	"strconv"
)

// exactThinMargin is the "knife-edge" width (in score points) below which an
// irreversible 2048-fire is gated on P* >= pconf: when the bankable score barely
// covers `need`, whether we clear T rides on the spawn lottery, so we refuse a
// low-confidence commit. One big merge (2048) of slack.
const exactThinMargin = 2048.0

// ---------------------------------------------------------------------------
// env helpers ported from the arena (evfire.go). evFloatFromEnv does NOT clamp
// (unlike farmWeightFromEnv), so a legitimately-negative lever survives.
// ---------------------------------------------------------------------------

func evFloatFromEnv(name string, def float64) float64 {
	s, ok := os.LookupEnv(name)
	if !ok || s == "" {
		return def
	}
	v, err := strconv.ParseFloat(s, 64)
	if err != nil {
		return def
	}
	return v
}

// massBoard returns the sum of FACE values (2^rank) of every tile on the board —
// the un-cashed "mass" left on a terminal board. Rank 11 (a 2048 that just ended
// the game) contributes 2048.
func massBoard(board uint64) float64 {
	sum := 0.0
	for i := 0; i < 16; i++ {
		r := (board >> (4 * uint(i))) & 0xF
		if r != 0 {
			sum += float64(uint64(1) << r)
		}
	}
	return sum
}

// cashValue is bankable_now(b): the endgame maximiser's expected final banked
// score from b (which already credits the terminal +2048 when reachable). If no
// maximiser is wired, the cheap single-swipe lower bound scoreLeaf(b) is used.
func cashValue(b uint64, maxCash *scoreSearcher) float64 {
	if maxCash != nil {
		_, v := maxCash.bestScoreMove(b)
		return v
	}
	return scoreLeaf(b)
}

// ---------------------------------------------------------------------------
// Endgame score-maximiser (-maxcashout), ported verbatim from arena search.go.
//
// Once the cash-out trigger fires, the objective flips from SURVIVAL (expected
// remaining moves) to SCORE: maximise the EXPECTED final board score Phi over the
// endgame, where Phi(board) = sSumBoard. Every merge STRICTLY increases Phi, so
// banking merges before the terminal 2048 can only raise the final score. The
// game-ending 1024+1024->2048 merge must be LAST because it ends the game.
//
// Same expectimax machinery as the survival search (same TT geometry, same
// cprob cutoff, same 2*numOpen spawn fan-out) with a SCORE objective. It uses
// the PACKAGE cprobThresh (not the per-searcher field) — the arena parity path.
// ---------------------------------------------------------------------------

// scoreSearcher is a single-threaded endgame expectimax over the SCORE objective.
// It carries its own transposition table (values are board scores, tens of
// thousands, not survival-W, so the two must never share a table). It is created
// once per worker and reused across games: bestScoreMove bumps the epoch, so the
// TT is self-invalidating.
type scoreSearcher struct {
	tt         *ttTable
	depthLimit int32
	p4         float64
	leaves     uint64
}

func newScoreSearcher(depth int, p4 float64) *scoreSearcher {
	return &scoreSearcher{tt: newTTTable(), depthLimit: int32(depth), p4: p4}
}

// scoreLeaf is the depth-limit / cprob-cutoff estimate: the value of the best
// single-swipe cash-out available from board b (sSum + the 2048 swipe's score),
// or sSum(b) if no 2048 (or no move) is available. Both are LOWER BOUNDS on the
// true expected final score from b (Phi never decreases as play continues), so
// the leaf never over-values a continuation over the real cash-out at the root.
func scoreLeaf(b uint64) float64 {
	all := executeAllMoves(b)
	best := sSumBoard(b) // jam / no-2048 fallback: cash the board as-is
	for dir := 0; dir < 4; dir++ {
		nb := all[dir]
		if nb == b || !hasRank11(nb) {
			continue
		}
		if fs := sSumBoard(b) + moveScore(b, dir); fs > best {
			best = fs
		}
	}
	return best
}

// scoreChance is the expectation over the 90%/10% spawn of a 2/4 into each empty
// cell — the same fold as searcher.chanceNode, but backing up SCORE.
func (s *scoreSearcher) scoreChance(board uint64, cprob float64, curdepth int32) float64 {
	if cprob < cprobThresh || curdepth >= s.depthLimit {
		s.leaves++
		return scoreLeaf(board)
	}
	needed := s.depthLimit - curdepth
	useTT := curdepth < cacheDepthLimit
	if useTT {
		if v, ok := s.tt.get(board, needed); ok {
			return v
		}
	}
	numOpen := countEmpty(board)
	if numOpen == 0 {
		// Full post-move board: no spawn, recurse straight into the max node.
		return s.scoreMove(board, cprob, curdepth)
	}
	cprob /= float64(numOpen)
	p4 := s.p4
	p2 := 1.0 - p4
	res := 0.0
	tmp := board
	tile2 := uint64(1)
	for tile2 != 0 {
		if tmp&0xF == 0 {
			v2 := s.scoreMove(board|tile2, cprob*p2, curdepth)
			v4 := s.scoreMove(board|(tile2<<1), cprob*p4, curdepth)
			res += v2*p2 + v4*p4
		}
		tmp >>= 4
		tile2 <<= 4
	}
	res /= float64(numOpen)
	if useTT {
		s.tt.put(board, needed, res)
	}
	return res
}

// scoreMove is the max node in SCORE units. A 2048-creating swipe is the terminal
// leaf (true final score, ends the line); any other legal swipe recurses through
// a spawn; a jam cashes the board as-is.
func (s *scoreSearcher) scoreMove(board uint64, cprob float64, curdepth int32) float64 {
	moves := executeAllMoves(board)
	best := math.Inf(-1)
	for dir := 0; dir < 4; dir++ {
		nb := moves[dir]
		if nb == board {
			continue
		}
		var v float64
		if hasRank11(nb) {
			v = sSumBoard(board) + moveScore(board, dir)
		} else {
			v = s.scoreChance(nb, cprob, curdepth+1)
		}
		if v > best {
			best = v
		}
	}
	if math.IsInf(best, -1) {
		return sSumBoard(board) // jam: no legal move, cash the board
	}
	return best
}

// bestScoreMove is the root of the endgame maximiser: it returns the swipe with
// the highest expected final score, and that value. It includes the immediate
// 2048 cash-out among its options, so bestVal >= the single-fire cash-out score.
func (s *scoreSearcher) bestScoreMove(board uint64) (int, float64) {
	s.tt.epoch++
	moves := executeAllMoves(board)
	bestDir, bestVal := -1, math.Inf(-1)
	for dir := 0; dir < 4; dir++ {
		nb := moves[dir]
		if nb == board {
			continue
		}
		var v float64
		if hasRank11(nb) {
			v = sSumBoard(board) + moveScore(board, dir)
		} else {
			v = s.scoreChance(nb, 1.0, 0)
		}
		if v > bestVal {
			bestDir, bestVal = dir, v
		}
	}
	return bestDir, bestVal
}

// runEndgame plays the cash-out endgame move by move under the score objective:
// re-search the best swipe, play it against a REAL random spawn, and repeat until
// the deliberate terminal 2048 (scored at its true final board) or a jam (scored
// at the board). Returns the extra moves played, the true final score, and the
// final board. Used by the SELF-PLAY driver (playCashout) only — the live serve
// path drives one move at a time against the phone's real spawns instead.
func (s *scoreSearcher) runEndgame(b uint64, r *rng) (moves int, finalScore float64, finalBoard uint64) {
	for {
		dir, _ := s.bestScoreMove(b)
		if dir < 0 {
			return moves, sSumBoard(b), b // jam
		}
		nb := executeAllMoves(b)[dir]
		moves++
		if hasRank11(nb) {
			return moves, sSumBoard(b) + moveScore(b, dir), nb // deliberate terminal 2048
		}
		b = spawn(nb, r)
	}
}

// ---------------------------------------------------------------------------
// Target-conditioned endgame solver (-exact), ported verbatim from arena
// exact.go. A record is a THRESHOLD event: we want P(final_score >= T) high, not
// a high mean. This computes, exactly, V(board) = P(final >= T | optimal play).
//
// RECURSION (expectimax):
//   - max node   : max over legal moves; a move creating a 2048 is a TERMINAL
//                  whose value is 1[final >= T]; a jam is a terminal (< T => 0).
//   - chance node: real spawn probs 0.9 for a "2", 0.1 for a "4", uniform.
//
// TWO PRUNES kill the (board,need) blow-up:
//  1. maxFinal(board) < T -> 0   (even the most optimistic banking loses).
//  2. sSumBoard(board) >= T -> 1 (score never decreases, so already banked).
//
// NODE CAP (nodeCap). On exceed we conservatively return 0 for the offending
// subtree and flag `capped` — never hang. Returning 0 (a P underestimate) is the
// safe direction: it can only make the gate keep playing the champion net (the
// fallback), never fire a phantom line.
// ---------------------------------------------------------------------------

// exactConfig carries the -exact levers, all env-configurable.
type exactConfig struct {
	on           bool
	target       float64 // BOT2048_TARGET         success threshold T (default 49601, STRICT >=)
	empties      int     // BOT2048_EXACT_EMPTIES   region-gate: solve only when countEmpty(b) <= this (default 5)
	nodeCap      int     // BOT2048_EXACT_NODES     max-node expansion cap per solve (default 3_000_000)
	pconf        float64 // BOT2048_TARGET_PCONF    min P* to enter a thin-margin irreversible fire (default 0.95)
	scoreDepth   int     // BOT2048_MAXCASH_DEPTH   depth of the score-objective tie-break search (default 8)
	earlyFire    float64 // BOT2048_EXACT_EARLYFIRE early maximiser-fire margin (points): fire the endgame maximiser once cashValue >= target+this. 0 = OFF
	earlyEmpties int     // BOT2048_EXACT_EARLY_EMPTIES min free cells required for an early maximiser fire (default 1)
}

// newExactConfig reads the -exact levers from the environment.
func newExactConfig(on bool) exactConfig {
	return exactConfig{
		on:           on,
		target:       evFloatFromEnv("BOT2048_TARGET", 49601),
		empties:      farmIntFromEnv("BOT2048_EXACT_EMPTIES", 5),
		nodeCap:      farmIntFromEnv("BOT2048_EXACT_NODES", 3_000_000),
		pconf:        evFloatFromEnv("BOT2048_TARGET_PCONF", 0.95),
		scoreDepth:   farmIntFromEnv("BOT2048_MAXCASH_DEPTH", 8),
		earlyFire:    evFloatFromEnv("BOT2048_EXACT_EARLYFIRE", 0),
		earlyEmpties: farmIntFromEnv("BOT2048_EXACT_EARLY_EMPTIES", 2),
	}
}

// exactSolver is one single-threaded target-conditioned expectimax context.
type exactSolver struct {
	target  float64
	nodeCap int
	memo    map[uint64]float64 // max-node board -> P(final >= T)
	nodes   int                // max-node expansions this solve
	capped  bool               // node cap hit at least once
	score   *scoreSearcher     // score objective, for the E[banked] tie-break only
	p2, p4  float64            // spawn probabilities (0.9 / 0.1)
}

func newExactSolver(cfg exactConfig, score *scoreSearcher) *exactSolver {
	return &exactSolver{
		target:  cfg.target,
		nodeCap: cfg.nodeCap,
		memo:    make(map[uint64]float64, 1<<16),
		score:   score,
		p2:      0.9,
		p4:      0.1,
	}
}

// achievableCeiling is a cheap PROVABLE UPPER BOUND on the best final score
// reachable from board:
//
//	final <= 9 * (massBoard + 4*countEmpty) + 2048
//
// Every bankable tile has rank <= 10 so S(rank) <= 9*face; 4*countEmpty is a
// generous near-term spawn allowance; +2048 credits the terminal cash-out. It is
// the provable abandonment ceiling: ceiling(b) < T => the board can NEVER bank T,
// so abandoning is always safe. (If the bound is ever violated for a genuinely-
// reachable multi-2048 line, the effect inside the solver is a P UNDERESTIMATE —
// prune returns 0 — the safe direction.)
func achievableCeiling(board uint64) float64 {
	return 9*(massBoard(board)+4*float64(countEmpty(board))) + 2048
}

// maxFinal is the solver's view of the same upper bound.
func (s *exactSolver) maxFinal(board uint64) float64 { return achievableCeiling(board) }

// exactP is the MAX node: P(final >= T | optimal play from `board`).
func (s *exactSolver) exactP(board uint64) float64 {
	if sSumBoard(board) >= s.target {
		return 1 // Prune 2: already worth >= T; score only grows.
	}
	if s.maxFinal(board) < s.target {
		return 0 // Prune 1: even optimistic banking cannot reach T.
	}
	if v, ok := s.memo[board]; ok {
		return v
	}
	if s.nodes >= s.nodeCap {
		if !s.capped {
			fmt.Fprintf(os.Stderr, "# exact: node cap %d hit, returning P=0 for the capped subtree(s)\n", s.nodeCap)
		}
		s.capped = true
		return 0
	}
	s.nodes++

	all := executeAllMoves(board)
	best := 0.0 // jam (no legal move): final = sSumBoard(board) < T => 0
	for dir := 0; dir < 4; dir++ {
		nb := all[dir]
		if nb == board {
			continue // illegal (no slide/merge)
		}
		var v float64
		if hasRank11(nb) {
			if sSumBoard(board)+moveScore(board, dir) >= s.target {
				v = 1
			}
		} else {
			v = s.exactChance(nb)
		}
		if v > best {
			best = v
		}
	}
	s.memo[board] = best
	return best
}

// exactChance is the CHANCE node: real 0.9/0.1 spawn of a 2/4 uniformly over the
// empty cells of `afterstate` (a post-move, pre-spawn board).
func (s *exactSolver) exactChance(afterstate uint64) float64 {
	numOpen := countEmpty(afterstate)
	if numOpen == 0 {
		return s.exactP(afterstate)
	}
	res := 0.0
	tmp := afterstate
	tile2 := uint64(1)
	for tile2 != 0 {
		if tmp&0xF == 0 {
			res += s.p2*s.exactP(afterstate|tile2) + s.p4*s.exactP(afterstate|(tile2<<1))
		}
		tmp >>= 4
		tile2 <<= 4
	}
	return res / float64(numOpen)
}

// eBanked is the SECONDARY objective for the lexicographic tie-break: the
// expected final banked score from an afterstate under SCORE-optimal play.
func (s *exactSolver) eBanked(afterstate uint64) float64 {
	return s.score.scoreChance(afterstate, 1.0, 0)
}

// bestExactMove is the ROOT: it returns the P-argmax move and P*. The tie-break
// is lexicographic — among moves within eps of the max P, it picks the one with
// the highest E[banked]. Returns (-1, 0) on a jammed board.
func (s *exactSolver) bestExactMove(board uint64) (dir int, pstar float64) {
	s.score.tt.epoch++ // invalidate the borrowed score TT for this root
	all := executeAllMoves(board)

	type cand struct {
		dir  int
		p, e float64
	}
	var cands []cand
	maxP := math.Inf(-1)
	for d := 0; d < 4; d++ {
		nb := all[d]
		if nb == board {
			continue
		}
		var p, e float64
		if hasRank11(nb) {
			fs := sSumBoard(board) + moveScore(board, d)
			if fs >= s.target {
				p = 1
			}
			e = fs
		} else {
			p = s.exactChance(nb)
			e = s.eBanked(nb)
		}
		cands = append(cands, cand{d, p, e})
		if p > maxP {
			maxP = p
		}
	}
	if len(cands) == 0 {
		return -1, 0
	}
	const eps = 1e-9
	bestDir, bestE := -1, math.Inf(-1)
	for _, c := range cands {
		if c.p >= maxP-eps && c.e > bestE {
			bestDir, bestE = c.dir, c.e
		}
	}
	return bestDir, maxP
}

// ---------------------------------------------------------------------------
// recordConfig: the master gate + the tuned levers. on == false leaves every
// caller (serve.go, main.go arena) on the golden path.
// ---------------------------------------------------------------------------

type recordConfig struct {
	on           bool
	exact        exactConfig
	maxcashDepth int // BOT2048_MAXCASH_DEPTH: endgame score-maximiser depth (default 8)
	// salvageKappa is a RESERVED, INERT stub (BOT2048_SALVAGE_KAPPA, default 0 =
	// no-op). A future SALVAGE regime would play risk-seeking (chance nodes ->
	// E[X]+kappa*sd(X)) when the record is unreachable by safe play. It is NOT
	// wired into any search path in v1 — carried only so a later lane test can
	// enable it without a code change.
	//
	// NB: v1 ships NO abandonment gate. A ROC analysis over 200 fresh seeds proved
	// doomed and record-reacher games are statistically indistinguishable in every
	// endgame checkpoint feature (k1024, support, score, provable-ceiling) until
	// the last ~40-120 plies — after the compute is already spent — and that the
	// "provable" ceiling is not even a valid whole-game bound (future spawns add
	// mass; the actual record game had ceiling < T until ply ~2275). So the
	// `features` line below is pure TELEMETRY for the SQLite pipeline; it makes no
	// abandon decision.
	salvageKappa float64
}

// newRecordConfig reads BOT2048_RECORD_ENGINE (master, "1"/"on"/"true" enable)
// plus the exact levers and the abandon-checkpoint threshold. Called once at
// startup.
func newRecordConfig() recordConfig {
	on := false
	switch os.Getenv("BOT2048_RECORD_ENGINE") {
	case "1", "on", "true", "yes":
		on = true
	}
	return recordConfig{
		on:           on,
		exact:        newExactConfig(on),
		maxcashDepth: farmIntFromEnv("BOT2048_MAXCASH_DEPTH", 8),
		salvageKappa: evFloatFromEnv("BOT2048_SALVAGE_KAPPA", 0), // reserved no-op
	}
}

// summary is the one-line provenance string for logs / version.
func (rc recordConfig) summary() string {
	e := rc.exact
	return fmt.Sprintf("T=%.0f empties<=%d nodeCap=%d pconf=%.2f scoreDepth=%d earlyFire=%.0f earlyEmpties=%d maxcashDepth=%d salvageKappa=%.3f",
		e.target, e.empties, e.nodeCap, e.pconf, e.scoreDepth, e.earlyFire, e.earlyEmpties, rc.maxcashDepth, rc.salvageKappa)
}

// featuresReply is the record-engine TELEMETRY line — NOT a gate. It reports the
// endgame structural features, the achievable potential (the maximiser's
// optimistic estimate of the final score), whether the bank path would engage /
// early-fire on this board, and the board identity, so the Python SQLite pipeline
// can record every endgame decision for postfactum audit (a would-have-been
// record must be visible in the logs). v1 ships no abandon verdict — see the
// recordConfig note — so this makes no decision; the runner logs it and plays on.
//
//	pot       = cashValue(board): the score-maximiser's expected optimal final
//	            score (an optimistic upper estimate).
//	ceiling   = achievableCeiling(board): a per-board upper bound (NOT a valid
//	            whole-game bound — future spawns add mass); logged for analysis.
//	engaged   = 1 iff the bank path would drive this move (ceiling >= T and
//	            countEmpty <= empties).
//	earlyfire = 1 iff the early maximiser-fire condition holds here.
func featuresReply(board uint64, score *scoreSearcher, rc recordConfig) string {
	e := rc.exact
	empty := countEmpty(board)
	k512 := countRank(board, 9)
	k256 := countRank(board, 8)
	pot := cashValue(board, score)
	engaged := achievableCeiling(board) >= e.target && empty <= e.empties
	earlyfire := e.earlyFire > 0 && pot >= e.target+e.earlyFire && empty >= e.earlyEmpties
	b2i := func(b bool) int {
		if b {
			return 1
		}
		return 0
	}
	return fmt.Sprintf("features board=%s k1024=%d empty=%d k512=%d k256=%d support=%d ssum=%.0f ceiling=%.0f pot=%.0f target=%.0f engaged=%d earlyfire=%d",
		formatBoardHex(board), rank10Count(board), empty, k512, k256, 2*k512+k256,
		sSumBoard(board), achievableCeiling(board), pot, e.target, b2i(engaged), b2i(earlyfire))
}

// earlyFireSSumGate is a cheap pre-gate on the cashValue-based early maximiser
// fire: cashValue (a depth-8 scoreSearcher) is only computed once the board's
// banked score sSumBoard(b) reaches this floor. A record early-fire needs
// cashValue >= target+earlyFire (~49901); no board with sSumBoard below this
// floor can reach that (the maximiser's expected final has sSumBoard as its
// running floor and the endgame cannot bank ~10k more from the few remaining
// empties), so the gate is BEHAVIORALLY IDENTICAL to computing cashValue every
// ply — proven empirically: the arena fix (gated at this floor) and the ungated
// bridge port produce byte-identical scores on all 10 reacher seeds. Its only
// effect is to skip the expensive cashValue search on ordinary mid-game boards,
// which keeps the phone move cadence under the UI budget. Matches the arena's
// BOT2048_PEAK_GATE_SSUM default (40000).
const earlyFireSSumGate = 40000.0

// ---------------------------------------------------------------------------
// Per-move record decision — the LIVE serve path.
//
// This is ONE iteration of the arena playCashout endgame block, isolated so the
// serve handler can call it per request against the phone's real board. It never
// simulates spawns; it returns the single move to play (and whether it is a
// deliberate 2048 fire). ok == false means "not engaged / vetoed" and the caller
// falls back to the golden net search.
//
//	engaged  <=>  maxFinal(b) >= T  AND  countEmpty(b) <= empties
//	earlyfire:    maxCash != nil && earlyFire > 0 && pot >= T+earlyFire &&
//	              countEmpty(b) >= earlyEmpties  ->  play the maximiser's move
//	else:         solver P-argmax move; a thin-margin 2048 fire needs P* >= pconf
//
// fired is true only when the returned move is the game-ending 2048 (telemetry).
// ---------------------------------------------------------------------------
func recordDecision(b uint64, exact exactConfig, exSolver *exactSolver, maxCash, exactScore *scoreSearcher) (dir int, val float64, fired, ok bool) {
	if !exact.on {
		return -1, 0, false, false
	}
	// EARLY MAXIMISER FIRE (cashValue-gated) — HOISTED above the maxFinal engage
	// guard. maxFinal is a MASS bound (9*(mass+4*empty)+2048) that UNDER-estimates
	// dense record boards, whose true bankable value counts future 1024->2048
	// cascades (which cashValue credits). So maxFinal < T on exactly the record
	// boards, and a nested earlyfire is dead there. Fire on cashValue directly;
	// self-gating to pot >= T+earlyFire (~record boards only), inert on normal play
	// (no rng consumed, move unchanged when it does not fire).
	if maxCash != nil && exact.earlyFire > 0 &&
		sSumBoard(b) >= earlyFireSSumGate &&
		countEmpty(b) <= exact.empties && countEmpty(b) >= exact.earlyEmpties {
		if pot := cashValue(b, exactScore); pot >= exact.target+exact.earlyFire {
			if d, v := maxCash.bestScoreMove(b); d >= 0 {
				return d, v, hasRank11(executeAllMoves(b)[d]), true
			}
		}
	}
	// GUARD (some line can still reach T) + REGION-GATE (dense board only).
	if exSolver.maxFinal(b) < exact.target || countEmpty(b) > exact.empties {
		return -1, 0, false, false
	}
	all := executeAllMoves(b)
	d, pstar := exSolver.bestExactMove(b)
	if d < 0 || pstar <= 0 {
		return -1, 0, false, false // region hopeless: fall back to the net
	}
	nb := all[d]
	if hasRank11(nb) {
		// Irreversible fire. On a thin margin require P* >= pconf.
		margin := cashValue(b, exactScore) - (exact.target - sSumBoard(b))
		if margin >= exactThinMargin || pstar >= exact.pconf {
			return d, pstar, true, true
		}
		return -1, 0, false, false // thin-margin low-confidence fire vetoed
	}
	// Solver-chosen SURVIVAL move: follow it toward T.
	return d, pstar, false, true
}

// ---------------------------------------------------------------------------
// Full-game self-play driver — used by the arena repro and the focused test.
//
// This reproduces the arena playCashout for the winning config (exact +
// maxcashout + earlyfire, with the ev/doom fire rules OFF, which is exactly how
// the winning arena arm ran). It drives its OWN rng for the spawns, so a given
// seed reproduces the arena trajectory (rng/spawn/board helpers are byte-
// identical between the trees), including the certified 50284 for seed 200280.
// ---------------------------------------------------------------------------

type recordResult struct {
	moves         int
	cashed        bool
	finalScore    float64
	finalBoard    uint64
	reachedTarget bool
}

// playCashout plays one full game. `s` is the survival searcher (the champion
// net); maxCash is the endgame maximiser (nil => single-fire cash-out);
// exactScore is the score searcher the solver borrows for its tie-break and the
// cashValue samples. All decisions are the arena's, minus the (unused) ev/doom.
func playCashout(s *searcher, r *rng, maxCash *scoreSearcher, exact exactConfig, exactScore *scoreSearcher) recordResult {
	b := initBoard(r)
	T := exact.target
	exSolver := newExactSolver(exact, exactScore)
	moves := 0
	for {
		all := executeAllMoves(b)
		// Classify legal moves into surviving (non-2048) and cash-out (2048).
		nSurvive := 0
		bestCashDir := -1
		bestCashScore := math.Inf(-1)
		for dir := 0; dir < 4; dir++ {
			nb := all[dir]
			if nb == b {
				continue
			}
			if hasRank11(nb) {
				if fs := sSumBoard(b) + moveScore(b, dir); fs > bestCashScore {
					bestCashScore, bestCashDir = fs, dir
				}
			} else {
				nSurvive++
			}
		}
		// EARLY MAXIMISER FIRE (cashValue-gated) — HOISTED above the maxFinal engage
		// guard (the mass-bound maxFinal excludes dense record boards; fire on the
		// cashValue estimate, which credits the future 1024->2048 cascades).
		if maxCash != nil && exact.earlyFire > 0 &&
			sSumBoard(b) >= earlyFireSSumGate &&
			countEmpty(b) <= exact.empties && countEmpty(b) >= exact.earlyEmpties {
			if pot := cashValue(b, exactScore); pot >= exact.target+exact.earlyFire {
				em, es, eb := maxCash.runEndgame(b, r)
				return recordResult{moves: moves + em, cashed: true, finalScore: es, finalBoard: eb, reachedTarget: es >= T}
			}
		}
		// EXACT solver: engage in-region and while some line can still reach T.
		engaged := false
		if exSolver.maxFinal(b) >= exact.target && countEmpty(b) <= exact.empties {
			engaged = true
			dir, pstar := exSolver.bestExactMove(b)
			if dir >= 0 && pstar > 0 {
				nb := all[dir]
				if hasRank11(nb) {
					margin := cashValue(b, exactScore) - (exact.target - sSumBoard(b))
					if margin >= exactThinMargin || pstar >= exact.pconf {
						moves++
						fs := sSumBoard(b) + moveScore(b, dir)
						return recordResult{moves: moves, cashed: true, finalScore: fs, finalBoard: nb, reachedTarget: fs >= T}
					}
					// thin-margin low-confidence fire vetoed: fall through.
				} else {
					moves++
					b = spawn(nb, r) // solver-chosen survival move
					continue
				}
			}
		}
		// Cash-out trigger: when engaged, only the about-to-die forced cash remains
		// (the solver owns the target-fire and its pconf veto must stand).
		cashTarget := bestCashScore >= T
		if engaged {
			cashTarget = false
		}
		if bestCashDir >= 0 && (nSurvive == 0 || cashTarget) {
			fireFloor := sSumBoard(b)
			if maxCash != nil {
				em, es, eb := maxCash.runEndgame(b, r)
				_ = fireFloor
				return recordResult{moves: moves + em, cashed: true, finalScore: es, finalBoard: eb, reachedTarget: es >= T}
			}
			moves++
			return recordResult{moves: moves, cashed: true, finalScore: bestCashScore, finalBoard: all[bestCashDir], reachedTarget: bestCashScore >= T}
		}
		// Survival policy — identical to playSearch.
		dir, _ := s.bestMove(b)
		if dir < 0 {
			return recordResult{moves: moves, cashed: false, finalScore: sSumBoard(b), finalBoard: b, reachedTarget: sSumBoard(b) >= T}
		}
		nb := all[dir]
		moves++
		if hasRank11(nb) {
			fs := sSumBoard(b) + moveScore(b, dir)
			return recordResult{moves: moves, cashed: true, finalScore: fs, finalBoard: nb, reachedTarget: fs >= T}
		}
		b = spawn(nb, r)
	}
}
