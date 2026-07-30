package main

// Focused coverage for the ported LIVE record engine (record.go). These tests
// are NET-INDEPENDENT: the banking machinery (target-conditioned solver + score-
// maximiser + early-fire + abandonment gate) is pure board arithmetic, so they
// exercise the exact code the serve path routes to without needing the 56 MB
// champion weight file. The full net-in-the-loop arena repro (seed 200280 =>
// 50284) is documented in docs/record-engine.md.

import (
	"strings"
	"testing"
)

func recordTestInit() {
	initTables()
	initFarmTables()
	initPatterns()
}

// buildBoard packs 16 row-major cell ranks into a bitboard (cell j -> nibble j).
func buildBoard(cells [16]int) uint64 {
	var b uint64
	for j := 0; j < 16; j++ {
		b |= uint64(cells[j]&0xF) << (4 * uint(j))
	}
	return b
}

// reacherBoard is a clean, dense endgame "reacher": sSum = 48640 (just under the
// T=49601 record), with ONE mergeable 1024 pair (cells 0,1) whose LEFT-swipe
// 1024+1024->2048 banks the true final 48640+2048 = 50688 >= T. The three other
// 1024s sit in distinct rows/columns so no second 2048 forms in the firing swipe
// (keeps the maxFinal upper bound honest). countEmpty = 8, so the region gate
// (empties <= 8 in the winning config) engages.
//
//	row0: 1024 1024  512  256
//	row1: 1024  512    .    .
//	row2: 1024  256    .    .
//	row3:    .    .    .    .
func reacherBoard() uint64 {
	return buildBoard([16]int{
		10, 10, 9, 8,
		10, 9, 0, 0,
		10, 8, 0, 0,
		0, 0, 0, 0,
	})
}

const recordTarget = 49601.0

// testExactConfig is the VALIDATED winning config (13/16 banked in the arena):
// EMPTIES=8 NODES=6e6 PCONF=0.6 EARLYFIRE=300 EARLY_EMPTIES=1.
func testExactConfig() exactConfig {
	return exactConfig{
		on: true, target: recordTarget, empties: 8, nodeCap: 6_000_000,
		pconf: 0.6, scoreDepth: 8, earlyFire: 300, earlyEmpties: 1,
	}
}

func TestReacherBoardIsWellFormed(t *testing.T) {
	recordTestInit()
	b := reacherBoard()
	if got := sSumBoard(b); got != 48640 {
		t.Fatalf("reacher sSum = %.0f, want 48640 (board math drifted)", got)
	}
	if got := countEmpty(b); got != 8 {
		t.Fatalf("reacher countEmpty = %d, want 8", got)
	}
	// A LEFT swipe must create a 2048 whose banked final clears T.
	all := executeAllMoves(b)
	found := false
	for dir := 0; dir < 4; dir++ {
		if all[dir] == b || !hasRank11(all[dir]) {
			continue
		}
		if fs := sSumBoard(b) + moveScore(b, dir); fs >= recordTarget {
			found = true
			if fs != 50688 {
				t.Errorf("firing dir %d final = %.0f, want 50688", dir, fs)
			}
		}
	}
	if !found {
		t.Fatal("no 2048-firing swipe banks >= T from the reacher board")
	}
}

// TestExactSolverBanksReacher is the core proof: the target-conditioned solver
// KNOWS the reacher can bank the record (P* == 1, since firing is a certain line
// needing no spawn), and picks a legal move toward it.
func TestExactSolverBanksReacher(t *testing.T) {
	recordTestInit()
	b := reacherBoard()
	cfg := testExactConfig()
	score := newScoreSearcher(cfg.scoreDepth, 0.1)
	solver := newExactSolver(cfg, score)

	if mf := solver.maxFinal(b); mf < recordTarget {
		t.Fatalf("maxFinal = %.0f < T: reacher wrongly flagged unreachable", mf)
	}
	if p := solver.exactP(b); p != 1.0 {
		t.Fatalf("exactP = %.6f, want 1.0 (a certain fire banks >= T)", p)
	}
	dir, pstar := solver.bestExactMove(b)
	if dir < 0 {
		t.Fatal("bestExactMove returned no move on a live reacher")
	}
	if pstar != 1.0 {
		t.Fatalf("bestExactMove pstar = %.6f, want 1.0", pstar)
	}
}

// TestScoreMaximiserBanksReacher proves the maximiser's expected optimal final
// score from the reacher is >= T, and that runEndgame ACTUALLY realises a banked
// final >= T (it fires the certain 2048).
func TestScoreMaximiserBanksReacher(t *testing.T) {
	recordTestInit()
	b := reacherBoard()
	sc := newScoreSearcher(8, 0.1)

	if _, v := sc.bestScoreMove(b); v < recordTarget {
		t.Fatalf("bestScoreMove value = %.0f < T: maximiser cannot see the record", v)
	}
	var seed uint64 = 200280
	r := &rng{s: seed*0x9E3779B97F4A7C15 + 1}
	moves, final, fb := sc.runEndgame(b, r)
	if final < recordTarget {
		t.Fatalf("runEndgame banked %.0f < T=%.0f after %d moves", final, recordTarget, moves)
	}
	if !hasRank11(fb) {
		t.Errorf("runEndgame final board has no 2048 tile (did not cash out): %016x", fb)
	}
}

// TestRecordDecisionFires checks the per-request serve decision engages on the
// reacher and returns a legal banking move.
func TestRecordDecisionFires(t *testing.T) {
	recordTestInit()
	b := reacherBoard()
	cfg := testExactConfig()
	score := newScoreSearcher(cfg.scoreDepth, 0.1)
	maxCash := newScoreSearcher(8, 0.1)
	solver := newExactSolver(cfg, score)

	dir, val, _, ok := recordDecision(b, cfg, solver, maxCash, score)
	if !ok {
		t.Fatal("recordDecision did not engage on a live reacher board")
	}
	if dir < 0 || dir > 3 {
		t.Fatalf("recordDecision returned illegal dir %d", dir)
	}
	if executeAllMoves(b)[dir] == b {
		t.Fatalf("recordDecision chose a non-moving direction %d", dir)
	}
	if val < recordTarget {
		t.Errorf("recordDecision value = %.0f, expected >= T on this reacher", val)
	}
}

// TestRecordDisabledIsInert confirms the default-OFF contract: a zero (off)
// config never engages, so the serve/arena golden path is untouched.
func TestRecordDisabledIsInert(t *testing.T) {
	recordTestInit()
	b := reacherBoard()
	off := exactConfig{on: false}
	score := newScoreSearcher(8, 0.1)
	solver := newExactSolver(off, score)
	if _, _, _, ok := recordDecision(b, off, solver, nil, score); ok {
		t.Fatal("recordDecision engaged with on=false (default path would diverge)")
	}
}

// TestFeaturesTelemetry covers the record-engine telemetry line (v1 emits NO
// abandon verdict): a reacher reports engaged=1 with pot >= T, and the board id +
// structural features round-trip. A hopeless board reports engaged=0 (its ceiling
// is below T), and the solver correctly rates it exactP == 0.
func TestFeaturesTelemetry(t *testing.T) {
	recordTestInit()

	rc := newRecordConfig() // defaults: target 49601
	// Force earlyfire levers so the telemetry earlyfire flag is exercised.
	rc.exact = testExactConfig()
	score := newScoreSearcher(8, 0.1)

	// Reacher: in-region, pot above T, engaged + earlyfire flagged, id round-trips.
	rep := featuresReply(reacherBoard(), score, rc)
	for _, want := range []string{
		"board=" + formatBoardHex(reacherBoard()), "k1024=4", "engaged=1", "earlyfire=1",
	} {
		if !strings.Contains(rep, want) {
			t.Errorf("reacher telemetry missing %q in %q", want, rep)
		}
	}

	// Hopeless board: ceiling below T, so the bank path would not engage.
	hopeless := buildBoard([16]int{9, 8, 7, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0})
	rep2 := featuresReply(hopeless, score, rc)
	if !strings.Contains(rep2, "engaged=0") {
		t.Errorf("hopeless telemetry should report engaged=0: %q", rep2)
	}
	solver := newExactSolver(rc.exact, score)
	if p := solver.exactP(hopeless); p != 0 {
		t.Fatalf("hopeless exactP = %.6f, want 0", p)
	}
}
