package main

// Manual measurement harness (run with: go test -run TestParallelScaling -v).
// Not an assertion test — it prints the wall-clock speedup of the parallel
// frontier driver vs the serial searcher at a FIXED deep rung, on a real
// late-game board, so the parallel work-per-core scaling can be read off. At a
// fixed (depth, cprob) both paths do ~the same number of chance nodes, so the
// wall-time ratio IS the effective parallel speedup (nodes/s scaling).

import (
	"fmt"
	"runtime"
	"testing"
	"time"
)

// lateGameBoard walks a learned-greedy game and returns a crowded late-game
// board (few empties, several 1024s) — where the deep rungs actually cost
// something and the pool has work to spread.
func lateGameBoard(n *net) uint64 {
	r := &rng{s: 13572468}
	pol := greedyPolicy(func(b uint64) float64 { return n.eval(b) })
	b := initBoard(r)
	best := b
	for i := 0; i < 3000; i++ {
		d := pol(b, r)
		if d < 0 {
			break
		}
		b = spawn(executeAllMoves(b)[d], r)
		// Crowded but NOT jammed: a handful of empties (so the chance node
		// still fans out and the deep rung is expensive) with several 1024s.
		if e := countEmpty(b); rank10Count(b) >= 2 && e >= 3 && e <= 6 {
			best = b
		}
	}
	return best
}

func TestParallelScaling(t *testing.T) {
	if testing.Short() {
		t.Skip("scaling measurement; skipped in -short")
	}
	n := benchNet(t)
	board := lateGameBoard(n)
	const depth = 8
	const cprob = 1e-5

	// Serial reference (exact float64 table), timed.
	ser := newSearcher(n.leafW, depth, 0.1)
	ser.cprobThresh = cprob
	t0 := time.Now()
	sdir, sval := ser.bestMove(board)
	serialMs := time.Since(t0).Seconds() * 1000
	fmt.Printf("board=%016x empties=%d k1024=%d depth=%d cprob=%.0e\n",
		board, countEmpty(board), rank10Count(board), depth, cprob)
	fmt.Printf("serial:        dir=%d val=%.4f  %.1f ms\n", sdir, sval, serialMs)

	fmt.Printf("NumCPU=%d\n", runtime.NumCPU())
	for _, w := range []int{1, 2, 4, 6, 8} {
		if w > runtime.NumCPU() {
			continue
		}
		par := newSearcher(n.leafW, depth, 0.1)
		par.cprobThresh = cprob
		par.pstt = newSharedTT()
		par.depthLimit = depth
		t1 := time.Now()
		pdir, pval, completed := par.parallelRung(board, farAwayDeadline(), w)
		ms := time.Since(t1).Seconds() * 1000
		fmt.Printf("parallel w=%-2d:  dir=%d val=%.4f  %.1f ms  speedup=%.2fx  match=%v completed=%v\n",
			w, pdir, pval, ms, serialMs/ms, pdir == sdir, completed)
	}
}
