package main

import (
	"math"
	"math/rand"
	"os"
	"testing"
)

// TestFarmEvalNeverBelowSSum is the invariant the whole farm objective rests
// on: with the (structural) default weights, every shaping term is >= 0, so
// farmEval(board) >= sSumBoard(board). This is what guarantees the search
// never VOLUNTARILY builds a 2048 (a forced-2048 cash-out is worth the
// pre-move S-sum; any real move must therefore be worth at least as much).
func TestFarmEvalNeverBelowSSum(t *testing.T) {
	rng := rand.New(rand.NewSource(20482048))
	ev := farmEvaluator{}
	for n := 0; n < 200000; n++ {
		b := randomBoard(rng)
		if hasRank11(b) {
			continue // rank>=11 boards are terminal, never leaf-evaluated
		}
		if e, s := ev.Eval(b), sSumBoard(b); e < s {
			t.Fatalf("farmEval(%016x)=%v < sSum=%v (shaping went negative)", b, e, s)
		}
	}
}

// TestCornerRankScaling: a 1024 parked in a corner must out-score a 256 in the
// same corner (the flat corner term alone paid both 640 — the bug the
// rank-scaled corner term fixes).
func TestCornerRankScaling(t *testing.T) {
	ev := farmEvaluator{}
	// Otherwise-empty board, one big tile in the top-left corner (cell 0).
	with := func(rank uint64) uint64 { return rank }
	e256 := ev.Eval(with(8))   // 256 in corner
	e512 := ev.Eval(with(9))   // 512 in corner
	e1024 := ev.Eval(with(10)) // 1024 in corner
	// The sSum term already grows with rank; what we assert here is that the
	// corner reward itself is rank-ordered (strictly increasing per rank), so
	// the engine prefers reserving corners for the biggest tiles.
	c256 := farmCornerRankWeight * cornerRankSum(with(8))
	c512 := farmCornerRankWeight * cornerRankSum(with(9))
	c1024 := farmCornerRankWeight * cornerRankSum(with(10))
	if !(c256 < c512 && c512 < c1024) {
		t.Fatalf("corner-rank reward not strictly increasing: 256=%v 512=%v 1024=%v",
			c256, c512, c1024)
	}
	if !(e256 < e512 && e512 < e1024) {
		t.Fatalf("eval not increasing with corner rank: 256=%v 512=%v 1024=%v",
			e256, e512, e1024)
	}
}

// TestSnakeGradientPrefersOrderedWall: a descending wall of big tiles laid
// along the snake path (biggest at the anchor corner) must score a higher
// snake contribution than the same tiles placed in the reverse (anti-snake)
// order. Isolate the snake term so the assertion does not depend on the other
// shaping terms.
func TestSnakeGradientPrefersOrderedWall(t *testing.T) {
	snakeOnly := func(board uint64) float64 {
		return snakeRowTable[0][board&rowMask] +
			snakeRowTable[1][(board>>16)&rowMask] +
			snakeRowTable[2][(board>>32)&rowMask] +
			snakeRowTable[3][(board>>48)&rowMask]
	}
	// Top row cells 0..3 hold ranks 10,9,8,7 in snake order (descending from
	// the anchor). The anti-snake board reverses them: 7,8,9,10.
	var ordered, reversed uint64
	ranks := [4]uint64{10, 9, 8, 7}
	for c := 0; c < 4; c++ {
		ordered |= ranks[c] << uint(4*c)
		reversed |= ranks[3-c] << uint(4*c)
	}
	if snakeOnly(ordered) <= snakeOnly(reversed) {
		t.Fatalf("snake did not prefer the ordered wall: ordered=%v reversed=%v",
			snakeOnly(ordered), snakeOnly(reversed))
	}
}

// TestConvexGateOnlyEndgame: the empty-convexity term must be exactly 0 unless
// the endgame gate is open (>= 2 big tiles AND <= farmConvexMaxEmpty empties),
// and strictly positive for an endgame board whose empties are contiguous.
func TestConvexGateOnlyEndgame(t *testing.T) {
	// convexOnly isolates the gated convex contribution the way Eval computes
	// it, so the assertions do not depend on the other shaping terms.
	convexOnly := func(board uint64) float64 {
		if !endgameConvexGate(board) {
			return 0
		}
		tr := transpose(board)
		return farmConvexTable[board&rowMask] +
			farmConvexTable[(board>>16)&rowMask] +
			farmConvexTable[(board>>32)&rowMask] +
			farmConvexTable[(board>>48)&rowMask] +
			farmConvexTable[tr&rowMask] +
			farmConvexTable[(tr>>16)&rowMask] +
			farmConvexTable[(tr>>32)&rowMask] +
			farmConvexTable[(tr>>48)&rowMask]
	}
	cell := func(idx, rank int) uint64 { return uint64(rank) << uint(4*idx) }

	// Opening-like board: one big tile, 15 empties -> gate shut (both because
	// too few big tiles AND too many empties) -> convex term 0.
	opening := cell(0, 10)
	if endgameConvexGate(opening) {
		t.Fatalf("gate should be shut in the opening (1 big tile, 15 empties)")
	}
	if v := convexOnly(opening); v != 0 {
		t.Fatalf("convex term must be 0 outside the gate, got %v", v)
	}

	// Endgame board: fill the top two rows (8 tiles, two of them big=rank>=9)
	// so exactly 8 cells are occupied -> 8 empties would fail the empty cap;
	// build one with <= farmConvexMaxEmpty empties and a contiguous empty
	// pocket. Occupy cells 0..9 (10 tiles) leaving cells 10..15 empty (6
	// empties, contiguous), with two big tiles among the filled cells.
	var endgame uint64
	filled := []int{0, 1, 2, 3, 4, 5, 6, 7, 8, 9}
	ranks := []int{10, 9, 8, 7, 6, 5, 4, 3, 2, 1} // two big (10,9) + mids
	for i, idx := range filled {
		endgame |= cell(idx, ranks[i])
	}
	if !endgameConvexGate(endgame) {
		t.Fatalf("gate should be open (2 big tiles, 6 empties), empties=%d big=%d",
			countEmpty(endgame), bigTileCount9(endgame))
	}
	if v := convexOnly(endgame); v <= 0 {
		t.Fatalf("contiguous endgame empties must earn a positive convex reward, got %v", v)
	}

	// Same tile multiset but empties scattered must earn no MORE than the
	// contiguous pocket: contiguity is what the term rewards. Scatter the two
	// big tiles and mids so empties are broken up (checkerboard-ish).
	var scattered uint64
	scatterIdx := []int{0, 2, 5, 7, 8, 10, 13, 15, 3, 12}
	for i, idx := range scatterIdx {
		scattered |= cell(idx, ranks[i])
	}
	if endgameConvexGate(scattered) { // if the gate is open, contiguity must win
		if convexOnly(scattered) > convexOnly(endgame) {
			t.Fatalf("scattered empties (%v) out-scored contiguous (%v) — term is not rewarding convexity",
				convexOnly(scattered), convexOnly(endgame))
		}
	}
}

// TestConvexPreservesInvariant: with the convex term at a deliberately large
// weight, farmEval must still never drop below sSum on endgame boards (the
// term is >= 0 by construction).
func TestConvexPreservesInvariant(t *testing.T) {
	saved := farmConvexWeight
	defer func() {
		farmConvexWeight = saved
		initFarmTables() // restore default tables (also re-reads env) for later tests
	}()
	farmConvexWeight = 5000 // absurdly high, still non-negative
	initFarmTables()
	ev := farmEvaluator{}
	rng := rand.New(rand.NewSource(112358))
	for n := 0; n < 200000; n++ {
		b := randomBoard(rng)
		if hasRank11(b) {
			continue
		}
		if e, s := ev.Eval(b), sSumBoard(b); e < s {
			t.Fatalf("farmEval(%016x)=%v < sSum=%v with high convex weight", b, e, s)
		}
	}
}

// TestConvexWeightZeroDisables: BOT2048_FARM_CONVEX_W=0 (the A/B "off" baseline)
// must zero the whole convex table.
func TestConvexWeightZeroDisables(t *testing.T) {
	saved := farmConvexWeight
	defer func() {
		farmConvexWeight = saved
		initFarmTables()
	}()
	farmConvexWeight = 0
	initFarmTables()
	for _, row := range []int{0x0000, 0x00f0, 0xf00f, 0x1234} {
		if v := farmConvexTable[row]; v != 0 {
			t.Fatalf("convex weight 0 left farmConvexTable[%04x]=%v", row, v)
		}
	}
}

// TestHoardStageDetector: rank10Count / hoardStageActive must count 1024 tiles
// (rank 10) exactly and open the late stage at the trigger threshold.
func TestHoardStageDetector(t *testing.T) {
	cell := func(idx, rank int) uint64 { return uint64(rank) << uint(4*idx) }
	saved := farmHoardTrigger
	defer setFarmHoardTrigger(saved)

	none := cell(0, 9) | cell(3, 8)                   // 512 + 256, no 1024
	one := cell(0, 10) | cell(3, 9)                   // one 1024
	two := cell(0, 10) | cell(3, 10)                  // two 1024s
	three := cell(0, 10) | cell(3, 10) | cell(12, 10) // three 1024s
	for _, tc := range []struct {
		name string
		b    uint64
		want int
	}{{"none", none, 0}, {"one", one, 1}, {"two", two, 2}, {"three", three, 3}} {
		if got := rank10Count(tc.b); got != tc.want {
			t.Fatalf("rank10Count(%s)=%d, want %d", tc.name, got, tc.want)
		}
	}

	setFarmHoardTrigger(2)
	if hoardStageActive(one) {
		t.Fatalf("stage must be inactive with one 1024 at trigger 2")
	}
	if !hoardStageActive(two) {
		t.Fatalf("stage must be active with two 1024s at trigger 2")
	}
	if !hoardStageActive(three) {
		t.Fatalf("stage must be active with three 1024s at trigger 2")
	}

	// The default trigger (99) is unreachable — a board holds at most 16 tiles —
	// so the stage must be off for EVERY board, and the derived flag must say so
	// (that flag is what keeps rank10Count out of the leaf evaluator).
	setFarmHoardTrigger(99)
	if farmHoardEnabled {
		t.Fatalf("trigger 99 (> 16 cells) must leave the hoard stage disabled")
	}
	for _, b := range []uint64{none, one, two, three} {
		if hoardStageActive(b) {
			t.Fatalf("hoard stage active at the unreachable default trigger")
		}
	}
}

// TestHoardStageSwitchesWeights: the late (hoard) weight set must affect Eval
// only when the stage gate is open. Changing farmLateEmptyWeight must move the
// eval of a board with >= trigger 1024s but leave a below-trigger board
// untouched (which still uses the early empty weight).
func TestHoardStageSwitchesWeights(t *testing.T) {
	cell := func(idx, rank int) uint64 { return uint64(rank) << uint(4*idx) }
	savedTrig, savedLateEmpty := farmHoardTrigger, farmLateEmptyWeight
	defer func() {
		setFarmHoardTrigger(savedTrig)
		farmLateEmptyWeight = savedLateEmpty
	}()
	setFarmHoardTrigger(2)
	ev := farmEvaluator{}

	// Two 1024s in corners + empties -> late stage active.
	late := cell(0, 10) | cell(3, 10)
	// One 1024 -> early stage.
	early := cell(0, 10) | cell(3, 9)

	lateBefore, earlyBefore := ev.Eval(late), ev.Eval(early)
	farmLateEmptyWeight = savedLateEmpty + 1000 // live scalar; no table rebuild needed
	lateAfter, earlyAfter := ev.Eval(late), ev.Eval(early)

	if lateAfter == lateBefore {
		t.Fatalf("late-empty change did not affect a hoard-stage board (%.1f == %.1f)",
			lateAfter, lateBefore)
	}
	if earlyAfter != earlyBefore {
		t.Fatalf("late-empty change leaked into a below-trigger board (%.1f != %.1f)",
			earlyAfter, earlyBefore)
	}
}

// TestHoardStagePreservesInvariant: with the late stage forced on (trigger 1)
// and deliberately extreme late weights, farmEval must still never drop below
// sSum — every late shaping term is >= 0 by construction.
func TestHoardStagePreservesInvariant(t *testing.T) {
	savedTrig := farmHoardTrigger
	savedEmpty, savedGrad, savedCorner := farmLateEmptyWeight, farmLateGradMult, farmLateCornerRankWeight
	defer func() {
		farmHoardTrigger = savedTrig
		farmLateEmptyWeight, farmLateGradMult, farmLateCornerRankWeight = savedEmpty, savedGrad, savedCorner
		initFarmTables() // restore default tables/env for later tests
	}()
	farmHoardTrigger = 1 // any 1024 opens the late stage
	farmLateEmptyWeight = 5000
	farmLateGradMult = 50
	farmLateCornerRankWeight = 5000
	ev := farmEvaluator{}
	rng := rand.New(rand.NewSource(999983))
	for n := 0; n < 200000; n++ {
		b := randomBoard(rng)
		if hasRank11(b) {
			continue
		}
		if e, s := ev.Eval(b), sSumBoard(b); e < s {
			t.Fatalf("farmEval(%016x)=%v < sSum=%v in forced late stage", b, e, s)
		}
	}
}

// TestSnakeTableWeightZeroDisables: setting the snake weight to 0 must zero the
// whole snake table (the env "off" switch used by sweeps / A-B baselines).
func TestSnakeTableWeightZeroDisables(t *testing.T) {
	savedW, savedR := farmSnakeWeight, farmSnakeRatio
	defer func() {
		farmSnakeWeight, farmSnakeRatio = savedW, savedR
		initSnakeTable() // restore the default table for later tests
	}()
	farmSnakeWeight = 0
	initSnakeTable()
	for r := 0; r < 4; r++ {
		for _, row := range []int{0x1234, 0xabcd, 0xffff, 0x0a0a} {
			if v := snakeRowTable[r][row]; v != 0 {
				t.Fatalf("snake weight 0 left snakeRowTable[%d][%04x]=%v", r, row, v)
			}
		}
	}
}

// --- SWAR differential tests -------------------------------------------------
//
// The farm helpers below were rewritten from scalar 4/8/16-iteration nibble
// loops to branch-free SWAR bit-plane / popcount / LUT forms, because
// farmEvaluator.Eval sits on every leaf of the search tree (56% of farm-mode
// profile time). The rewrite MUST be EXACT: any change to a single leaf value
// changes the engine's move choices, and every experiment result already
// collected would become incomparable.
//
// So each helper keeps its ORIGINAL implementation here as a reference, and the
// tests below assert BIT-FOR-BIT equality (integers, or math.Float64bits for
// floats — not an epsilon comparison) over millions of random boards. refEval
// reassembles the whole evaluator from the reference helpers, summing the terms
// in exactly the original order (float addition is not associative, so the
// order is part of the contract).

func refHasRank11(board uint64) bool {
	for i := 0; i < 16; i++ {
		if (board>>(4*i))&0xF >= rank11 {
			return true
		}
	}
	return false
}

func refCornerBigCount(board uint64) int {
	n := 0
	for _, sh := range [4]uint{0, 12, 48, 60} {
		if (board>>sh)&0xF >= 8 {
			n++
		}
	}
	return n
}

func refCornerRankSum(board uint64) float64 {
	s := 0.0
	for _, sh := range [4]uint{0, 12, 48, 60} {
		if r := (board >> sh) & 0xF; r >= 8 {
			s += float64(uint64(1) << uint(r-8))
		}
	}
	return s
}

func refEdgeBigCount(board uint64) int {
	n := 0
	for _, sh := range [8]uint{4, 8, 16, 28, 32, 44, 52, 56} {
		if (board>>sh)&0xF >= 8 {
			n++
		}
	}
	return n
}

// refBigTileCount is the original generic "count cells with rank >= minRank".
func refBigTileCount(board uint64, minRank uint64) int {
	n := 0
	for i := 0; i < 16; i++ {
		if (board>>(4*i))&0xF >= minRank {
			n++
		}
	}
	return n
}

func refRank10Count(board uint64) int {
	n := 0
	for i := 0; i < 16; i++ {
		if (board>>(4*i))&0xF == 10 {
			n++
		}
	}
	return n
}

// refEval is the pre-SWAR farmEvaluator.Eval, verbatim: same terms, same
// summation order, built only from the reference helpers above.
func refEval(board uint64) float64 {
	t := transpose(board)
	empty := countEmpty(board)
	shape := farmMergeTable[board&rowMask] +
		farmMergeTable[(board>>16)&rowMask] +
		farmMergeTable[(board>>32)&rowMask] +
		farmMergeTable[(board>>48)&rowMask] +
		farmMergeTable[t&rowMask] +
		farmMergeTable[(t>>16)&rowMask] +
		farmMergeTable[(t>>32)&rowMask] +
		farmMergeTable[(t>>48)&rowMask] +
		farmStairTable[board&rowMask] +
		farmStairTable[(board>>16)&rowMask] +
		farmStairTable[(board>>32)&rowMask] +
		farmStairTable[(board>>48)&rowMask] +
		farmStairTable[t&rowMask] +
		farmStairTable[(t>>16)&rowMask] +
		farmStairTable[(t>>32)&rowMask] +
		farmStairTable[(t>>48)&rowMask]
	snake := snakeRowTable[0][board&rowMask] +
		snakeRowTable[1][(board>>16)&rowMask] +
		snakeRowTable[2][(board>>32)&rowMask] +
		snakeRowTable[3][(board>>48)&rowMask]
	convex := 0.0
	if empty <= farmConvexMaxEmpty &&
		refBigTileCount(board, farmConvexBigRank) >= farmConvexMinBig {
		convex = farmConvexTable[board&rowMask] +
			farmConvexTable[(board>>16)&rowMask] +
			farmConvexTable[(board>>32)&rowMask] +
			farmConvexTable[(board>>48)&rowMask] +
			farmConvexTable[t&rowMask] +
			farmConvexTable[(t>>16)&rowMask] +
			farmConvexTable[(t>>32)&rowMask] +
			farmConvexTable[(t>>48)&rowMask]
	}
	emptyW := farmEmptyWeight
	cornerRankW := farmCornerRankWeight
	gradMult := 1.0
	if farmHoardEnabled && refRank10Count(board) >= farmHoardTrigger {
		emptyW = farmLateEmptyWeight
		cornerRankW = farmLateCornerRankWeight
		gradMult = farmLateGradMult
	}
	return sSumBoard(board) +
		emptyW*float64(empty) +
		shape +
		snake*gradMult +
		convex +
		farmCornerWeight*float64(refCornerBigCount(board)) +
		cornerRankW*refCornerRankSum(board) +
		farmEdgeWeight*float64(refEdgeBigCount(board))
}

// TestSWARHelpersMatchReference: every rewritten helper, bit-for-bit against
// its original scalar implementation, over 2M random boards plus every
// single-tile and all-same-rank board (the SWAR edge cases: rank 8 vs 9 vs 10
// vs 11, and the 0xF saturation).
func TestSWARHelpersMatchReference(t *testing.T) {
	check := func(b uint64) {
		t.Helper()
		if got, want := hasRank11(b), refHasRank11(b); got != want {
			t.Fatalf("hasRank11(%016x)=%v want %v", b, got, want)
		}
		if got, want := cornerBigCount(b), refCornerBigCount(b); got != want {
			t.Fatalf("cornerBigCount(%016x)=%d want %d", b, got, want)
		}
		if got, want := edgeBigCount(b), refEdgeBigCount(b); got != want {
			t.Fatalf("edgeBigCount(%016x)=%d want %d", b, got, want)
		}
		if got, want := bigTileCount9(b), refBigTileCount(b, farmConvexBigRank); got != want {
			t.Fatalf("bigTileCount9(%016x)=%d want %d", b, got, want)
		}
		if got, want := rank10Count(b), refRank10Count(b); got != want {
			t.Fatalf("rank10Count(%016x)=%d want %d", b, got, want)
		}
		g, w := cornerRankSum(b), refCornerRankSum(b)
		if math.Float64bits(g) != math.Float64bits(w) {
			t.Fatalf("cornerRankSum(%016x)=%v want %v (not bit-identical)", b, g, w)
		}
	}
	// Exhaustive over the structural edge cases: each rank alone in each cell,
	// and each rank filling the whole board.
	for r := uint64(0); r < 16; r++ {
		for cell := 0; cell < 16; cell++ {
			check(r << (4 * uint(cell)))
		}
		check(r * 0x1111111111111111)
	}
	// Uniform random nibbles: hits every rank in every cell, unlike randomBoard
	// (which biases toward empties).
	rng := rand.New(rand.NewSource(0x5A11ADE5))
	for n := 0; n < 2_000_000; n++ {
		check(rng.Uint64())
	}
	// Realistic boards too (~40% empty), so the tests also cover the density
	// the evaluator actually sees.
	for n := 0; n < 500_000; n++ {
		check(randomBoard(rng))
	}
}

// TestFarmEvalBitIdenticalToReference is the load-bearing proof of the SWAR
// rewrite: farmEvaluator.Eval must be BIT-FOR-BIT equal to the pre-SWAR
// evaluator on every board — with the hoard stage OFF (the shipped default,
// where the rank10Count scan is skipped) and ON (BOT2048_FARM_HOARD_TRIGGER=2,
// where the late weight set and the gradient multiplier are live). If this
// holds, no leaf value moved, so no search decision moved, so every experiment
// result collected before the rewrite stays comparable.
func TestFarmEvalBitIdenticalToReference(t *testing.T) {
	saved := farmHoardTrigger
	defer setFarmHoardTrigger(saved)

	ev := farmEvaluator{}
	for _, trigger := range []int{99, 2} { // 99 = hoard OFF (default), 2 = ON
		setFarmHoardTrigger(trigger)
		rng := rand.New(rand.NewSource(int64(1e9 + 7)))
		mismatch := 0
		n := 0
		for ; n < 2_000_000; n++ {
			var b uint64
			if n%2 == 0 {
				b = rng.Uint64() // uniform ranks: covers hoard/convex regimes
			} else {
				b = randomBoard(rng) // realistic density
			}
			g, w := ev.Eval(b), refEval(b)
			if math.Float64bits(g) != math.Float64bits(w) {
				mismatch++
				if mismatch <= 3 {
					t.Errorf("trigger=%d Eval(%016x)=%v (%016x) want %v (%016x)",
						trigger, b, g, math.Float64bits(g), w, math.Float64bits(w))
				}
			}
		}
		if mismatch != 0 {
			t.Fatalf("trigger=%d: %d/%d boards differ", trigger, mismatch, n)
		}
	}
}

// --- FUSED evaluator: differential tests -------------------------------------
//
// fusedFarmEvaluator folds every static term of farmEvaluator into two tables
// (farm.go). It is the shipped farm leaf, so it must agree with the per-cell
// evaluator to the last bit that MATTERS. It does NOT agree bit-for-bit: float
// addition is not associative and the fused table pre-adds the snake gradient
// (the only non-integer term) into a much larger running value, so the two sums
// round differently in the last mantissa bits. The tests below pin down exactly
// how large that difference can be (~1e-15 relative) and then prove the thing we
// actually care about: no search DECISION moves.

// fusedLeafDiff returns the relative difference between the fused and per-cell
// leaf values, and whether they are bit-identical.
func fusedLeafDiff(b uint64) (rel float64, identical bool) {
	f, p := fusedFarmEvaluator{}.Eval(b), farmEvaluator{}.Eval(b)
	if math.Float64bits(f) == math.Float64bits(p) {
		return 0, true
	}
	d := math.Abs(f - p)
	if p != 0 {
		d /= math.Abs(p)
	}
	return d, false
}

// TestFusedEvalMatchesPerCell: the fused leaf vs the per-cell leaf over 2M
// boards (uniform ranks + realistic density). Bit-identity is REPORTED, not
// required; the hard bound is that no leaf value moves by more than 1e-13
// relative — 13 orders of magnitude below the smallest weight in the evaluator,
// so no search comparison can flip on it (proven directly in
// TestFusedSearchDecisionsIdentical).
func TestFusedEvalMatchesPerCell(t *testing.T) {
	if !farmFusedReady {
		t.Fatal("fused tables not built with the default (hoard-off) config")
	}
	const tolerance = 1e-13
	rng := rand.New(rand.NewSource(0xF0DEDBEEF))
	bitIdentical, n := 0, 0
	maxRel, maxBoard := 0.0, uint64(0)
	for ; n < 2_000_000; n++ {
		var b uint64
		if n%2 == 0 {
			b = rng.Uint64() // uniform ranks: covers the convex/endgame regimes
		} else {
			b = randomBoard(rng) // realistic density
		}
		rel, ok := fusedLeafDiff(b)
		if ok {
			bitIdentical++
			continue
		}
		if rel > maxRel {
			maxRel, maxBoard = rel, b
		}
	}
	if maxRel > tolerance {
		t.Fatalf("fused leaf differs by %g (relative) on board %016x — over the %g bound",
			maxRel, maxBoard, tolerance)
	}
	t.Logf("fused vs per-cell over %d boards: %d bit-identical (%.1f%%), max relative diff %g",
		n, bitIdentical, 100*float64(bitIdentical)/float64(n), maxRel)
}

// TestFusedDisabledUnderHoard is the load-bearing SAFETY test of the fusion.
// The hoard stage-switch makes the leaf weights BOARD-DEPENDENT (a board with
// >= trigger 1024 tiles is scored with the late weight set), which a static
// fused table cannot express. So with hoard on, the fused tables must not be
// built, the search must dispatch to the per-cell farm leaf, and that leaf must
// still be the exact stage-switching evaluator.
func TestFusedDisabledUnderHoard(t *testing.T) {
	// The trigger comes from the environment, so drive the REAL init path — but
	// restore the env BEFORE the final rebuild (t.Setenv's own cleanup runs too
	// late for that: it fires after this function's defers).
	savedEnv, hadEnv := os.LookupEnv("BOT2048_FARM_HOARD_TRIGGER")
	savedTrigger := farmHoardTrigger
	restoreEnv := func() {
		if hadEnv {
			os.Setenv("BOT2048_FARM_HOARD_TRIGGER", savedEnv)
		} else {
			os.Unsetenv("BOT2048_FARM_HOARD_TRIGGER")
		}
		setFarmHoardTrigger(savedTrigger)
	}
	defer func() {
		restoreEnv()
		initFarmTables() // rebuild the fused tables for the rest of the suite
	}()

	os.Setenv("BOT2048_FARM_HOARD_TRIGGER", "2")
	initFarmTables()
	if farmHoardTrigger != 2 || !farmHoardEnabled {
		t.Fatalf("hoard not enabled: trigger=%d enabled=%v", farmHoardTrigger, farmHoardEnabled)
	}
	if farmFusedReady {
		t.Fatal("fused tables built while the hoard stage-switch is ON (weights are board-dependent)")
	}
	if k := evalKindOf(farmEvaluator{}, true); k != evalKindFarm {
		t.Fatalf("farm search dispatched to kind %d with hoard on, want the per-cell %d", k, evalKindFarm)
	}
	// And the leaf the search actually calls is the per-cell (stage-switching)
	// evaluator, bit-for-bit — over boards that DO hit the late stage.
	s := &searchState{evalKind: evalKindOf(farmEvaluator{}, true), farm: true}
	rng := rand.New(rand.NewSource(0xB01DFACE))
	late := 0
	for n := 0; n < 500_000; n++ {
		b := rng.Uint64()
		if hasRank11(b) {
			continue
		}
		if rank10Count(b) >= farmHoardTrigger {
			late++
		}
		g, w := s.leafEval(b), farmEvaluator{}.Eval(b)
		if math.Float64bits(g) != math.Float64bits(w) {
			t.Fatalf("hoard leaf(%016x)=%v want %v (not the per-cell evaluator)", b, g, w)
		}
	}
	if late == 0 {
		t.Fatal("no board hit the late stage — the fallback path was never exercised")
	}
	t.Logf("hoard fallback: per-cell leaf used, %d late-stage boards covered", late)

	// Back to the default: fused must come back.
	restoreEnv()
	initFarmTables()
	if farmHoardEnabled {
		t.Skip("ambient BOT2048_FARM_HOARD_TRIGGER keeps the hoard mode on")
	}
	if !farmFusedReady || evalKindOf(farmEvaluator{}, true) != evalKindFarmFused {
		t.Fatal("fused leaf not restored after the hoard mode was turned off")
	}
}

// farmPlayoutBoards self-plays a farm game with a shallow farm search and
// returns n realistic mid/late-game positions (the ones a real run decides on).
func farmPlayoutBoards(t testing.TB, seed int64, n int) []uint64 {
	rng := rand.New(rand.NewSource(seed))
	boards := make([]uint64, 0, n)
	b := spawnRandomTile(spawnRandomTile(0, rng), rng)
	for len(boards) < n {
		res := search(b, 2, 0.1, false, farmEvaluator{}, true)
		if res.bestDir < 0 {
			b = spawnRandomTile(spawnRandomTile(0, rng), rng)
			continue
		}
		nb := executeMove(b, res.bestDir)
		if hasRank11(nb) { // farm game over: start a fresh one
			b = spawnRandomTile(spawnRandomTile(0, rng), rng)
			continue
		}
		b = spawnRandomTile(nb, rng)
		if 16-countEmpty(b) >= 6 {
			boards = append(boards, b)
		}
	}
	return boards
}

// TestFusedSearchDecisionsIdentical is the proof that matters: on real farm
// positions, the fused leaf and the per-cell leaf choose the SAME move. The two
// leaves differ in the last mantissa bits (see TestFusedEvalMatchesPerCell), so
// this is what guarantees the engine's behaviour — and therefore every
// experiment result already collected — is unchanged by the fusion.
func TestFusedSearchDecisionsIdentical(t *testing.T) {
	if !farmFusedReady {
		t.Fatal("fused tables not built with the default (hoard-off) config")
	}
	if testing.Short() {
		t.Skip("search-heavy")
	}
	boards := farmPlayoutBoards(t, 0xFA24, 600)
	maxRel := 0.0
	for _, b := range boards {
		// fused (the shipped default)
		fused := search(b, 4, 0.1, false, farmEvaluator{}, true)
		// per-cell: same search, same everything, only the leaf swapped
		farmFusedReady = false
		perCell := search(b, 4, 0.1, false, farmEvaluator{}, true)
		farmFusedReady = true

		if fused.bestDir != perCell.bestDir {
			t.Fatalf("board %016x: fused picked dir %d (%.17g), per-cell picked dir %d (%.17g)",
				b, fused.bestDir, fused.bestValue, perCell.bestDir, perCell.bestValue)
		}
		if perCell.bestValue != 0 {
			if rel := math.Abs(fused.bestValue-perCell.bestValue) / math.Abs(perCell.bestValue); rel > maxRel {
				maxRel = rel
			}
		}
	}
	t.Logf("%d real farm positions, depth 4: 100%% identical directions; max relative root-value diff %g",
		len(boards), maxRel)
}

// refSafeDirs is the ground truth for the mobility term, written the slow,
// obvious way: play every direction with the real move code and count the ones
// that both CHANGE the board (legal) and do NOT create a rank-11 tile (safe
// under the tabu rule).
func refSafeDirs(board uint64) int {
	n := 0
	for dir := 0; dir < 4; dir++ {
		next := executeMove(board, dir)
		if next != board && !hasRank11(next) {
			n++
		}
	}
	return n
}

// refAdjRank10Pairs counts orthogonally adjacent 1024|1024 pairs the slow way.
func refAdjRank10Pairs(board uint64) int {
	rank := func(r, c int) uint64 { return (board >> uint(4*(4*r+c))) & 0xF }
	n := 0
	for r := 0; r < 4; r++ {
		for c := 0; c < 4; c++ {
			if c < 3 && rank(r, c) == 10 && rank(r, c+1) == 10 {
				n++
			}
			if r < 3 && rank(r, c) == 10 && rank(r+1, c) == 10 {
				n++
			}
		}
	}
	return n
}

// refMobilityScore is farmMobilityScore built from the reference helpers.
func refMobilityScore(board uint64) float64 {
	s := float64(refSafeDirs(board)) - farmMobPairPenalty*float64(refAdjRank10Pairs(board))
	if s <= 0 {
		return 0
	}
	return farmMobilityWeight * s
}

// TestMobilityScoreMatchesReference: the eight-lookup row-table form must equal
// the play-every-move reference bit-for-bit, on uniform-rank boards (which hit
// the 1024/2048 ranks the term is about) and realistic ones.
func TestMobilityScoreMatchesReference(t *testing.T) {
	saved := farmMobilityWeight
	defer setFarmMobilityWeight(saved)
	setFarmMobilityWeight(300)

	check := func(b uint64) {
		t.Helper()
		g, w := farmMobilityScore(b), refMobilityScore(b)
		if math.Float64bits(g) != math.Float64bits(w) {
			t.Fatalf("farmMobilityScore(%016x)=%v want %v (safe=%d pairs=%d)",
				b, g, w, refSafeDirs(b), refAdjRank10Pairs(b))
		}
	}
	// Structural edge cases first: every farm rank alone in every cell, and
	// every uniform board (an all-1024 board is the extreme of the pair
	// penalty). The domain is ranks 0..10 — see farmMobilityScore's
	// precondition: a rank-11 tile ends the game, so no leaf ever holds one.
	for r := uint64(0); r <= 10; r++ {
		for cell := 0; cell < 16; cell++ {
			check(r << (4 * uint(cell)))
		}
		check(r * 0x1111111111111111)
	}
	rng := rand.New(rand.NewSource(0x0B1717))
	for n := 0; n < 1_500_000; n++ {
		check(randomFarmBoard(rng))
	}
}

// TestMobilitySeesTheDeadBoard is the measurement that motivated the term: two
// ADJACENT 1024s make every legal move create a 2048 — the board is dead — yet
// the pre-term evaluator scored it ABOVE the same mass with the 1024s apart.
// Mobility must now separate them.
func TestMobilityDeadBoardScoresBelowLiveBoard(t *testing.T) {
	saved := farmMobilityWeight
	defer setFarmMobilityWeight(saved)

	// Two 1024s side by side, nothing else: both LEFT and RIGHT merge them into
	// a 2048, and UP/DOWN do not move anything -> zero safe directions.
	dead := uint64(0xA)<<(4*0) | uint64(0xA)<<(4*1)
	// Same mass, opposite corners: nothing merges, all real moves are safe.
	live := uint64(0xA)<<(4*0) | uint64(0xA)<<(4*15)

	setFarmMobilityWeight(0)
	if got := farmMobilityScore(dead); got != 0 {
		t.Fatalf("mobility must be 0 while the weight is 0, got %v", got)
	}
	setFarmMobilityWeight(300)
	if d := farmMobilityScore(dead); d != 0 {
		t.Fatalf("dead board (0 safe dirs, 1 adjacent 1024 pair) must score 0, got %v", d)
	}
	l := farmMobilityScore(live)
	if l <= 0 {
		t.Fatalf("live board must score > 0, got %v", l)
	}
	ev := farmEvaluator{}
	if ev.Eval(dead) >= ev.Eval(live) {
		t.Fatalf("with mobility on, the DEAD board still evaluates >= the live one: %v vs %v",
			ev.Eval(dead), ev.Eval(live))
	}
}

// TestMobilityPreservesInvariant: the term is clamped at 0, so it can never
// push a leaf below its cash-out floor (which is what stops the engine from
// ever VOLUNTARILY creating a 2048) — even with a huge pair penalty.
func TestMobilityPreservesInvariant(t *testing.T) {
	savedW, savedP := farmMobilityWeight, farmMobPairPenalty
	defer func() {
		setFarmMobilityWeight(savedW)
		farmMobPairPenalty = savedP
	}()
	setFarmMobilityWeight(500)
	farmMobPairPenalty = 10

	ev := farmEvaluator{}
	rng := rand.New(rand.NewSource(4242))
	for n := 0; n < 300_000; n++ {
		b := randomFarmBoard(rng)
		if got, floor := ev.Eval(b), sSumBoard(b); got < floor {
			t.Fatalf("Eval(%016x)=%v < sSum=%v", b, got, floor)
		}
	}
}

// refSnakeOnlyRowTable rebuilds the positional table from the snake gradient
// ALONE, independently of initSnakeTable.
func refSnakeOnlyRowTable() [4][65536]float64 {
	var posW [16]float64
	for cell := 0; cell < 16; cell++ {
		posW[cell] = math.Pow(farmSnakeRatio, float64(15-snakeCellOrder[cell]))
	}
	var g [16]float64
	for r := 1; r <= 15; r++ {
		g[r] = float64(uint64(1) << uint(r))
	}
	var out [4][65536]float64
	for r := 0; r < 4; r++ {
		for row := 0; row < 65536; row++ {
			s := 0.0
			for c := 0; c < 4; c++ {
				s += posW[4*r+c] * g[(row>>uint(4*c))&0xF]
			}
			out[r][row] = farmSnakeWeight * s
		}
	}
	return out
}

// TestQuadCornerZeroLeavesPositionalTableIntact: with the four-corner weight at
// its default 0, every one of the 4 x 65536 positional-table entries must be
// BIT-for-bit the snake-only value. The quad term is folded into that table
// (which is why it costs the leaf nothing), so this is the proof that folding
// it in moved no leaf value while it is off.
func TestQuadCornerZeroLeavesPositionalTableIntact(t *testing.T) {
	savedQ := farmQuadCornerWeight
	defer func() {
		farmQuadCornerWeight = savedQ
		initSnakeTable()
	}()
	farmQuadCornerWeight = 0
	initSnakeTable()

	want := refSnakeOnlyRowTable()
	for r := 0; r < 4; r++ {
		for row := 0; row < 65536; row++ {
			if math.Float64bits(snakeRowTable[r][row]) != math.Float64bits(want[r][row]) {
				t.Fatalf("snakeRowTable[%d][%04x]=%v want %v (not bit-identical)",
					r, row, snakeRowTable[r][row], want[r][row])
			}
		}
	}
}

// TestQuadCornerIsSymmetric: the whole point of the term. The snake pays a 1024
// 256.0 in the top-left corner and 22.4 in the bottom-left one; the four-corner
// gradient must pay the SAME in all four corners, and less in the centre.
func TestQuadCornerIsSymmetric(t *testing.T) {
	savedS, savedQ := farmSnakeWeight, farmQuadCornerWeight
	defer func() {
		farmSnakeWeight, farmQuadCornerWeight = savedS, savedQ
		initSnakeTable()
	}()
	farmSnakeWeight = 0 // quad REPLACES the snake in this configuration
	farmQuadCornerWeight = 0.25
	initSnakeTable()

	pos := func(cell int) float64 {
		b := uint64(10) << (4 * uint(cell)) // a 1024 in that cell, nothing else
		return snakeRowTable[0][b&rowMask] +
			snakeRowTable[1][(b>>16)&rowMask] +
			snakeRowTable[2][(b>>32)&rowMask] +
			snakeRowTable[3][(b>>48)&rowMask]
	}
	corners := []int{0, 3, 12, 15}
	base := pos(corners[0])
	if base <= 0 {
		t.Fatalf("corner weight must be > 0, got %v", base)
	}
	for _, c := range corners[1:] {
		if math.Float64bits(pos(c)) != math.Float64bits(base) {
			t.Fatalf("corner %d pays %v, corner %d pays %v — not symmetric", c, pos(c), corners[0], base)
		}
	}
	for _, c := range []int{5, 6, 9, 10} { // the four centre cells
		if pos(c) >= base {
			t.Fatalf("centre cell %d pays %v >= corner %v — gradient does not descend", c, pos(c), base)
		}
	}
}

// TestQuadCornerWeightZeroDisables: with the weight at 0 the term contributes
// nothing to a leaf, whatever the ratio.
func TestQuadCornerWeightZeroDisables(t *testing.T) {
	savedQ, savedR := farmQuadCornerWeight, farmQuadRatio
	defer func() {
		farmQuadCornerWeight, farmQuadRatio = savedQ, savedR
		initSnakeTable()
	}()
	ev := farmEvaluator{}
	rng := rand.New(rand.NewSource(777))
	boards := make([]uint64, 2000)
	for i := range boards {
		boards[i] = randomBoard(rng)
	}
	before := make([]float64, len(boards))
	for i, b := range boards {
		before[i] = ev.Eval(b)
	}
	farmQuadCornerWeight = 0
	farmQuadRatio = 0.1 // a different ratio must not matter while the weight is 0
	initSnakeTable()
	for i, b := range boards {
		if math.Float64bits(ev.Eval(b)) != math.Float64bits(before[i]) {
			t.Fatalf("quad weight 0 changed Eval(%016x): %v -> %v", b, before[i], ev.Eval(b))
		}
	}
}

// BenchmarkFarmEval isolates the leaf evaluator itself (it is ~52% of the
// engine's time, so a term's cost is decided here). Run it with and without
// BOT2048_FARM_MOBILITY_W set to price the mobility term.
func BenchmarkFarmEval(b *testing.B) {
	rng := rand.New(rand.NewSource(9))
	boards := make([]uint64, 4096)
	for i := range boards {
		boards[i] = randomFarmBoard(rng)
	}
	ev := farmEvaluator{}
	sink := 0.0
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		sink += ev.Eval(boards[i&4095])
	}
	_ = sink
}

// randomFarmBoard draws a board from the domain the farm evaluator actually
// sees: ranks 0..10 only (a rank-11 tile ends the game, so no leaf ever holds
// one, and rank 15 is therefore unreachable too). ~35% empty.
func randomFarmBoard(rng *rand.Rand) uint64 {
	var b uint64
	for i := 0; i < 16; i++ {
		var v uint64
		if rng.Float64() >= 0.35 {
			v = uint64(rng.Intn(10) + 1)
		}
		b |= v << (4 * i)
	}
	return b
}

// TestEmptyCurveOffIsLinear: with the curve weight at 0 (the default), the
// empty term must be the original farmEmptyWeight * count product, bit for bit.
func TestEmptyCurveOffIsLinear(t *testing.T) {
	saved := farmEmptyCurveWeight
	defer setFarmEmptyCurveWeight(saved)
	setFarmEmptyCurveWeight(0)

	ev := farmEvaluator{}
	rng := rand.New(rand.NewSource(31337))
	for n := 0; n < 200_000; n++ {
		b := randomFarmBoard(rng)
		if math.Float64bits(ev.Eval(b)) != math.Float64bits(refEval(b)) {
			t.Fatalf("curve off changed Eval(%016x)", b)
		}
	}
}

// TestEmptyCurveIsConvexAndDominatesLinear: switched on at scale 1.0 the term
// must (a) be strictly increasing in free space, (b) be CONVEX in the scarce
// regime (each extra empty cell is worth more than the previous one — that is
// the whole point: the hazard is convex), and (c) price the last few empties
// ABOVE the linear term it replaces.
func TestEmptyCurveIsConvexAndDominatesLinear(t *testing.T) {
	saved := farmEmptyCurveWeight
	defer setFarmEmptyCurveWeight(saved)
	setFarmEmptyCurveWeight(1.0)

	for e := 1; e <= 16; e++ {
		if farmEmptyCurve[e] <= farmEmptyCurve[e-1] {
			t.Fatalf("curve not increasing at e=%d: %v <= %v", e, farmEmptyCurve[e], farmEmptyCurve[e-1])
		}
	}
	// CONVEX means the steps GROW. The first version of this test asserted the
	// opposite (d2 > d1 was the failure) and so pinned a CONCAVE table — it was
	// written against a hand-guessed curve whose increments shrank (700, 600,
	// 400, 350, 200 ...). The measured hazard says the reverse: at k=3 it runs
	// 57% -> 36% -> 19% -> 10% for e = 0,1,2,3, i.e. it HALVES per free cell, so
	// expected remaining life — and the value of a cell — DOUBLES. The shipped
	// table is A*(2^e - 1), whose steps double.
	for e := 2; e <= 8; e++ {
		d1 := farmEmptyCurve[e-1] - farmEmptyCurve[e-2]
		d2 := farmEmptyCurve[e] - farmEmptyCurve[e-1]
		if d2 < d1 {
			t.Fatalf("curve not convex at e=%d: step %v < previous step %v", e, d2, d1)
		}
	}
	// The curve must beat the linear term exactly where survival is binding.
	// e=1 is the anchor (A = farmEmptyWeight, so the curve MEETS the line there
	// and the term is a no-op on a wide-open board); it bends upward from e=2.
	if farmEmptyCurve[1] != farmEmptyWeight {
		t.Fatalf("anchor: curve[1] = %v, want farmEmptyWeight = %v", farmEmptyCurve[1], farmEmptyWeight)
	}
	for _, e := range []int{2, 3, 4} {
		if lin := farmEmptyWeight * float64(e); farmEmptyCurve[e] <= lin {
			t.Fatalf("scarce regime e=%d: curve %v must exceed the linear %v", e, farmEmptyCurve[e], lin)
		}
	}
}

// TestEmptyCurvePreservesInvariant: the curve is non-negative and monotone, so
// farmEval >= sSum still holds (no voluntary 2048).
func TestEmptyCurvePreservesInvariant(t *testing.T) {
	saved := farmEmptyCurveWeight
	defer setFarmEmptyCurveWeight(saved)
	setFarmEmptyCurveWeight(1.0)

	ev := farmEvaluator{}
	rng := rand.New(rand.NewSource(5150))
	for n := 0; n < 200_000; n++ {
		b := randomFarmBoard(rng)
		if got, floor := ev.Eval(b), sSumBoard(b); got < floor {
			t.Fatalf("Eval(%016x)=%v < sSum=%v", b, got, floor)
		}
	}
}
