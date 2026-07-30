package main

import (
	"fmt"
	"math/rand"
	"testing"
	"time"
)

// spawnRandomTile drops a 2 (90%) or 4 (10%) on a uniformly random empty
// cell, mimicking the game's spawn model. Returns the board unchanged when
// it is full.
func spawnRandomTile(b uint64, rng *rand.Rand) uint64 {
	var empties [16]uint
	n := 0
	for i := uint(0); i < 16; i++ {
		if (b>>(4*i))&0xF == 0 {
			empties[n] = i
			n++
		}
	}
	if n == 0 {
		return b
	}
	cell := empties[rng.Intn(n)]
	rank := uint64(1)
	if rng.Float64() < 0.1 {
		rank = 2
	}
	return b | rank<<(4*cell)
}

// playoutBoards deterministically generates realistic mid/late-game boards
// by self-playing with a shallow search and sampling positions that are at
// least half full (the expensive kind for a depth-6 search).
func playoutBoards(seed int64, n int) []uint64 {
	rng := rand.New(rand.NewSource(seed))
	boards := make([]uint64, 0, n)
	b := spawnRandomTile(spawnRandomTile(0, rng), rng)
	for len(boards) < n {
		res := search(b, 2, 0.1, false, tableEvaluator{}, false)
		if res.bestDir < 0 {
			b = spawnRandomTile(spawnRandomTile(0, rng), rng)
			continue
		}
		b = spawnRandomTile(executeMove(b, res.bestDir), rng)
		if 16-countEmpty(b) >= 8 {
			boards = append(boards, b)
		}
	}
	return boards
}

var benchBoards []uint64

func getBenchBoards() []uint64 {
	if benchBoards == nil {
		benchBoards = playoutBoards(0xBEEF, 32)
	}
	return benchBoards
}

// BenchmarkSearchDepth6 mirrors the production configuration of
// GoExpectimaxStrategy(depth=6): adaptive on, classic evaluator.
func BenchmarkSearchDepth6(b *testing.B) {
	boards := getBenchBoards()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		search(boards[i%len(boards)], 6, 0.1, true, tableEvaluator{}, false)
	}
}

// BenchmarkSearchDepth6Farm is the same load in farm mode.
func BenchmarkSearchDepth6Farm(b *testing.B) {
	boards := getBenchBoards()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		search(boards[i%len(boards)], 6, 0.1, true, tableEvaluator{}, true)
	}
}

// TestSearchReturnsLegalMove sanity-checks the search on the benchmark
// boards: the returned direction must actually change the board.
func TestSearchReturnsLegalMove(t *testing.T) {
	for _, b := range getBenchBoards() {
		res := search(b, 3, 0.1, false, tableEvaluator{}, false)
		if res.bestDir == -1 {
			continue
		}
		if executeMove(b, res.bestDir) == b {
			t.Fatalf("search returned illegal move %d for %016x", res.bestDir, b)
		}
	}
}

// TestSearchDeterministic: same position + same parameters => same answer,
// regardless of what was searched before (TT state must not leak across
// fixed-depth calls).
func TestSearchDeterministic(t *testing.T) {
	boards := getBenchBoards()
	for _, b := range boards[:8] {
		first := search(b, 4, 0.1, false, tableEvaluator{}, false)
		// Perturb any persistent state with unrelated searches.
		search(boards[len(boards)-1], 4, 0.1, false, tableEvaluator{}, false)
		second := search(b, 4, 0.1, false, tableEvaluator{}, false)
		if first.bestDir != second.bestDir || first.bestValue != second.bestValue {
			t.Fatalf("nondeterministic search on %016x: (%d, %v) vs (%d, %v)",
				b, first.bestDir, first.bestValue, second.bestDir, second.bestValue)
		}
	}
}

var _ = time.Now // placeholder: keeps the import when budget tests are absent

// TestSpeculativeAbortCannotTruncateRealSearch: the speculative abort flag is
// reachable ONLY through the ext argument, so a set prefetchAbort (a driver
// that skipped stopPrefetch, a reordered defer — the failure mode the package
// global `specExt` made possible) can no longer cut a real search short and
// hand back the garbage values of a truncated tree. An ext-armed search, by
// contrast, must honour it AND report the truncation instead of returning a
// silently wrong move.
func TestSpeculativeAbortCannotTruncateRealSearch(t *testing.T) {
	var board uint64
	for _, b := range getBenchBoards() {
		if countEmpty(b) >= 4 { // enough branching to cross an abort-poll window
			board = b
			break
		}
	}
	if board == 0 {
		t.Skip("no benchmark board with >= 4 empty cells")
	}
	want := search(board, 5, 0.1, false, tableEvaluator{}, false)

	prefetchAbort.Store(true)
	defer prefetchAbort.Store(false)

	got := search(board, 5, 0.1, false, tableEvaluator{}, false)
	if got != want {
		t.Fatalf("a set speculative abort changed a real search: %+v vs %+v", got, want)
	}
	if _, completed := searchExt(
		board, 5, 0.1, false, tableEvaluator{}, false, specTables, &prefetchAbort); completed {
		t.Fatal("searchExt reported completion though its abort flag was already set")
	}
}

// TestPrefetchLookupMatchesSearch: every spawn state prefetched at a fixed
// depth must resolve to exactly the decision a direct fixed-depth search
// produces (determinism is guaranteed by the per-call TT epoch bump).
func TestPrefetchLookupMatchesSearch(t *testing.T) {
	board := getBenchBoards()[0]
	states := enumerateSpawnStates(board)
	if len(states) == 0 {
		t.Fatal("benchmark board has no empty cells")
	}
	n := startPrefetch(board, 3, 0.1, false, tableEvaluator{}, false, 0)
	if n != len(states) {
		t.Fatalf("startPrefetch scheduled %d states, want %d", n, len(states))
	}
	deadline := time.Now().Add(30 * time.Second)
	got := make(map[uint64]prefetchEntry, n)
	for len(got) < n {
		if time.Now().After(deadline) {
			t.Fatalf("prefetch not finished after 30s: %d/%d states", len(got), n)
		}
		for _, s := range states {
			if _, ok := got[s]; ok {
				continue
			}
			if e, ok := lookupPrefetch(s); ok {
				got[s] = e
			}
		}
		time.Sleep(time.Millisecond)
	}
	for _, s := range states {
		want := search(s, 3, 0.1, false, tableEvaluator{}, false)
		e := got[s]
		if e.dir != want.bestDir || e.value != want.bestValue || e.depth != want.depthLimit {
			t.Fatalf("prefetch mismatch on %016x: got (%d, %v, %d) want (%d, %v, %d)",
				s, e.dir, e.value, e.depth, want.bestDir, want.bestValue, want.depthLimit)
		}
	}
}

// TestStopPrefetchIsFast: aborting deep speculative searches must join the
// worker within the abort-poll latency (sub-ms per search), not after the
// whole speculative queue drains.
func TestStopPrefetchIsFast(t *testing.T) {
	board := getBenchBoards()[1]
	startPrefetch(board, 8, 0.1, false, tableEvaluator{}, false, 0)
	time.Sleep(20 * time.Millisecond) // let the worker get into a deep search
	begin := time.Now()
	stopPrefetch()
	if elapsed := time.Since(begin); elapsed > 100*time.Millisecond {
		t.Fatalf("stopPrefetch took %v, want < 100ms", elapsed)
	}
}

// TestPrefetchProtocol drives prefetch/lookup through the stdio handler the
// way a real client would.
func TestPrefetchProtocol(t *testing.T) {
	board := getBenchBoards()[2]
	states := enumerateSpawnStates(board)
	reply := handleLine(
		"prefetch "+formatBoardHex(board)+" 2 0.1 0 0 0", tableEvaluator{})
	if reply != fmt.Sprintf("ok %d", len(states)) {
		t.Fatalf("prefetch reply %q, want \"ok %d\"", reply, len(states))
	}
	target := formatBoardHex(states[0])
	deadline := time.Now().Add(30 * time.Second)
	for {
		reply = handleLine("lookup "+target, tableEvaluator{})
		if reply != "miss" {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("lookup still misses after 30s")
		}
		time.Sleep(time.Millisecond)
	}
	var dir, depth int
	var value float64
	if _, err := fmt.Sscanf(reply, "hit %d %f %d", &dir, &value, &depth); err != nil {
		t.Fatalf("malformed lookup reply %q: %v", reply, err)
	}
	want := search(states[0], 2, 0.1, false, tableEvaluator{}, false)
	if dir != want.bestDir || depth != want.depthLimit {
		t.Fatalf("protocol lookup (%d, %d) != direct search (%d, %d)",
			dir, depth, want.bestDir, want.depthLimit)
	}
	stopPrefetch()
	if got := handleLine("lookup zzz", tableEvaluator{}); got[:3] != "err" {
		t.Fatalf("bad-board lookup reply %q, want err", got)
	}
	if got := handleLine("lookup "+formatBoardHex(board), tableEvaluator{}); got != "miss" {
		t.Fatalf("unprefetched board reply %q, want miss", got)
	}
}
