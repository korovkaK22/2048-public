package main

// Equal-DEPTH full-game score invariance (run with:
//   go test -run TestParallelGameScoreInvariance -v -timeout 30m).
// Plays whole games where every move is decided at a FIXED depth+cprob (no
// budget, no deadline) by either the serial searcher or the parallel frontier
// pool, on the SAME seeds. The parallel driver stores interior values as
// float32 in the shared table, so a handful of near-tie moves may flip and a
// game can diverge — but the AGGREGATE (mean moves / mean score) must not move
// beyond that quantization noise. A real bug (wrong objective arm, bad fold,
// dropped task) would shift the mean sharply.

import (
	"fmt"
	"math"
	"testing"
)

// playParallel is playSearch's parallel twin: fixed depth, one rung per move
// via the frontier pool, pstt epoch bumped per move (as bestMove bumps tt).
func playParallel(n *net, depth, workers int, r *rng) (moves int, forced bool, final uint64) {
	s := newSearcher(n.leafW, depth, 0.1)
	s.pstt = newSharedTT()
	s.depthLimit = int32(depth)
	b := initBoard(r)
	for {
		s.pstt.nextEpoch()
		dir, _, _ := s.parallelRung(b, farAwayDeadline(), workers)
		if dir < 0 {
			return moves, false, b
		}
		nb := executeAllMoves(b)[dir]
		moves++
		if hasRank11(nb) {
			return moves, true, b
		}
		b = spawn(nb, r)
	}
}

func TestParallelGameScoreInvariance(t *testing.T) {
	if testing.Short() {
		t.Skip("full-game invariance; skipped in -short")
	}
	n := benchNet(t)
	const depth = 3
	const workers = 6
	const games = 6
	const seed0 = 1000

	var sMoves, pMoves, sScore, pScore float64
	for g := 0; g < games; g++ {
		seed := uint64(seed0+g)*0x9E3779B97F4A7C15 + 1
		rs := &rng{s: seed}
		sm, _, sfb := playSearch(newSearcher(n.leafW, depth, 0.1), rs)
		rp := &rng{s: seed}
		pm, _, pfb := playParallel(n, depth, workers, rp)
		fmt.Printf("game %d seed0=%d  serial: moves=%4d score=%6d   parallel: moves=%4d score=%6d\n",
			g, seed0+g, sm, int(sSumBoard(sfb)), pm, int(sSumBoard(pfb)))
		sMoves += float64(sm)
		pMoves += float64(pm)
		sScore += sSumBoard(sfb)
		pScore += sSumBoard(pfb)
	}
	sMoves /= games
	pMoves /= games
	sScore /= games
	pScore /= games
	fmt.Printf("MEAN  serial moves=%.0f score=%.0f | parallel moves=%.0f score=%.0f | dMoves=%.1f%% dScore=%.1f%%\n",
		sMoves, sScore, pMoves, pScore,
		100*(pMoves-sMoves)/sMoves, 100*(pScore-sScore)/sScore)

	// The two engines are the same policy modulo float32-quantized near-ties;
	// over 6 games the mean must stay within a few percent. A blown objective
	// arm or fold bug would move it by tens of percent.
	if d := math.Abs(pMoves-sMoves) / sMoves; d > 0.05 {
		t.Fatalf("mean moves moved %.1f%% (serial %.0f, parallel %.0f) — exceeds 5%%", 100*d, sMoves, pMoves)
	}
	if d := math.Abs(pScore-sScore) / sScore; d > 0.05 {
		t.Fatalf("mean score moved %.1f%% (serial %.0f, parallel %.0f) — exceeds 5%%", 100*d, sScore, pScore)
	}
}
