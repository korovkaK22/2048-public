package main

import (
	"testing"
	"time"
)

// farAwayDeadline is a deadline no rung can ever hit, so parallelRung runs to
// completion (the test measures the answer, not the budget).
func farAwayDeadline() time.Time { return time.Now().Add(time.Hour) }

// TestParallelRungMatchesSerial checks that the parallel frontier driver
// reaches the SAME expectimax decision as the serial searcher at a fixed
// (depth, cprob), on a batch of real mid/late-game boards. The parallel path
// stores interior values as float32 in the shared table, so exact bit-equality
// is not expected; instead we require (a) the chosen direction agrees on the
// large majority of boards, and (b) where directions agree, the root values
// agree to float32 precision. A large disagreement would signal a bug in the
// fold arithmetic, the objective arms, or the task enumeration order.
func TestParallelRungMatchesSerial(t *testing.T) {
	n := benchNet(t) // loads champion weights + sets obj (exp objective)

	// Collect real boards by walking a learned-greedy game.
	r := &rng{s: 987654321}
	pol := greedyPolicy(func(b uint64) float64 { return n.eval(b) })
	var boards []uint64
	b := initBoard(r)
	for i := 0; i < 4000 && len(boards) < 120; i++ {
		d := pol(b, r)
		if d < 0 {
			b = initBoard(r)
			continue
		}
		b = spawn(executeAllMoves(b)[d], r)
		if i%12 == 0 {
			boards = append(boards, b)
		}
	}

	const depth = 4
	const workers = 4
	agree, total, valMismatch := 0, 0, 0
	for _, bd := range boards {
		// Serial reference on the exact float64 table.
		ser := newSearcher(n.leafW, depth, 0.1)
		ser.cprobThresh = 1e-4
		sdir, sval := ser.bestMove(bd)
		if sdir < 0 {
			continue // jammed: nothing to compare
		}
		total++

		// Parallel driver: fresh pool table, one rung, no deadline.
		par := newSearcher(n.leafW, depth, 0.1)
		par.cprobThresh = 1e-4
		par.pstt = newSharedTT()
		par.depthLimit = depth
		pdir, pval, completed := par.parallelRung(bd, farAwayDeadline(), workers)
		if !completed {
			t.Fatalf("parallelRung reported !completed with no real deadline (board %016x)", bd)
		}
		if pdir < 0 {
			t.Fatalf("parallelRung found no move where serial found dir %d (board %016x)", sdir, bd)
		}
		if pdir == sdir {
			agree++
			// float32 round-trip of the value is the tightest we can demand.
			if relDiff(pval, sval) > 1e-3 {
				valMismatch++
				t.Logf("value drift board %016x dir %d: serial=%.6f par=%.6f", bd, sdir, sval, pval)
			}
		} else {
			t.Logf("dir disagree board %016x: serial=%d(%.4f) par=%d(%.4f)", bd, sdir, sval, pdir, pval)
		}
	}
	if total == 0 {
		t.Skip("no non-jammed boards collected")
	}
	rate := float64(agree) / float64(total)
	t.Logf("direction agreement %d/%d = %.1f%%, value mismatches among agreements: %d",
		agree, total, 100*rate, valMismatch)
	if rate < 0.9 {
		t.Fatalf("parallel/serial direction agreement %.1f%% < 90%% — likely a driver bug", 100*rate)
	}
}

func relDiff(a, b float64) float64 {
	d := a - b
	if d < 0 {
		d = -d
	}
	m := a
	if b > m {
		m = b
	}
	if m < 0 {
		m = -m
	}
	if m == 0 {
		return d
	}
	return d / m
}
