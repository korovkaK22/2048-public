package main

import (
	"strings"
	"testing"
	"time"
)

// Protocol-2 search limits (searchLimitedExt + the k=v wire). Sizes are kept
// -short friendly: a handful of benchmark boards, depth 4, millisecond budgets.

// limitedBoards: the first n benchmark boards that are not jammed.
func limitedBoards(n int) []uint64 {
	out := make([]uint64, 0, n)
	for _, b := range getBenchBoards() {
		if search(b, 1, 0.1, false, tableEvaluator{}, false).bestDir >= 0 {
			out = append(out, b)
		}
		if len(out) == n {
			break
		}
	}
	return out
}

func limited(b uint64, lim searchLimits) searchResult {
	res, ok := searchLimitedExt(b, 0.1, false, tableEvaluator{}, false, lim, &searchTables{}, nil)
	if !ok {
		panic("limited search reported !completed with no ext flag")
	}
	return res
}

// TestLimitedCappedCompletedMatchesFixedDepth: the key property of the depth
// cap. A (maxdepth=D, budget=B) search that COMPLETES the direct attempt must
// be bit-identical to the plain fixed-depth-D search — same direction, same
// root value, same depth — and say so with stop=depth. Serial (workers=1 is
// what the direct attempt always uses).
func TestLimitedCappedCompletedMatchesFixedDepth(t *testing.T) {
	const depth = 4
	for _, farm := range []bool{false, true} {
		for _, b := range limitedBoards(12) {
			want := search(b, depth, 0.1, false, tableEvaluator{}, farm)
			got, ok := searchLimitedExt(b, 0.1, false, tableEvaluator{}, farm,
				searchLimits{maxDepth: depth, budgetMs: 10000}, &searchTables{}, nil)
			if !ok {
				t.Fatalf("farm=%v board %016x: limited search did not complete", farm, b)
			}
			if got.stop != stopDepth {
				t.Fatalf("farm=%v board %016x: stop=%q, want depth (10 s budget, depth 4)", farm, b, got.stop)
			}
			if got.bestDir != want.bestDir || got.bestValue != want.bestValue || got.depthLimit != want.depthLimit {
				t.Fatalf("farm=%v board %016x: capped (%d, %v, %d) != fixed (%d, %v, %d)",
					farm, b, got.bestDir, got.bestValue, got.depthLimit, want.bestDir, want.bestValue, want.depthLimit)
			}
			if got.nodes != want.nodes || got.cprob != cprobThreshBase {
				t.Fatalf("farm=%v board %016x: telemetry nodes=%d/%d cprob=%v", farm, b, got.nodes, want.nodes, got.cprob)
			}
		}
	}
}

// TestLimitedDepthOnlyIsFixedPath: (D, none) with no node cap is literally the
// fixed-depth search, adaptive included.
func TestLimitedDepthOnlyIsFixedPath(t *testing.T) {
	for _, b := range limitedBoards(6) {
		for _, adaptive := range []bool{false, true} {
			want := search(b, 3, 0.1, adaptive, tableEvaluator{}, false)
			got, _ := searchLimitedExt(b, 0.1, adaptive, tableEvaluator{}, false,
				searchLimits{maxDepth: 3}, &searchTables{}, nil)
			if got != want {
				t.Fatalf("board %016x adaptive=%v: %+v != %+v", b, adaptive, got, want)
			}
		}
	}
}

// TestLimitedTinyBudgetStopsOnBudget: with a budget no depth-8 search can meet,
// the direct attempt is discarded and the capped ladder answers: a legal move,
// depth <= cap, stop=budget (or depth on a board small enough to finish).
func TestLimitedTinyBudgetStopsOnBudget(t *testing.T) {
	budgetStops := 0
	for _, b := range limitedBoards(8) {
		start := time.Now()
		res := limited(b, searchLimits{maxDepth: 8, budgetMs: 0.5})
		el := time.Since(start)
		if res.bestDir < 0 || executeMove(b, res.bestDir) == b {
			t.Fatalf("board %016x: illegal dir %d", b, res.bestDir)
		}
		if res.depthLimit > 8 {
			t.Fatalf("board %016x: depth %d exceeds the cap 8", b, res.depthLimit)
		}
		switch res.stop {
		case stopBudget:
			budgetStops++
		case stopDepth:
		default:
			t.Fatalf("board %016x: stop=%q, want budget or depth", b, res.stop)
		}
		if el > 5*time.Second { // smoke: the deadline is armed (see TestBudgetSearchRespectsBudget)
			t.Fatalf("board %016x: 0.5 ms budget took %v", b, el)
		}
	}
	if budgetStops == 0 {
		t.Fatal("no board stopped on budget — a 0.5 ms depth-8 cap should not be affordable")
	}
}

// TestLimitedNodeCapStopsOnNodes: a node cap far below a depth-6 search ends
// the request with stop=nodes and still returns a legal move (the insurance
// pass is never capped).
func TestLimitedNodeCapStopsOnNodes(t *testing.T) {
	for _, b := range limitedBoards(6) {
		res := limited(b, searchLimits{maxDepth: 6, maxNodes: 1000})
		if res.bestDir < 0 || executeMove(b, res.bestDir) == b {
			t.Fatalf("board %016x: illegal dir %d", b, res.bestDir)
		}
		if res.stop != stopNodes {
			t.Fatalf("board %016x: stop=%q depth=%d nodes=%d, want nodes", b, res.stop, res.depthLimit, res.nodes)
		}
	}
}

// TestLimitedUnboundedWithNodeCapTerminates: both limits None is the
// "unbounded" ladder; the node cap must be the stop that brings it back — and
// in serial the answer is reproducible.
func TestLimitedUnboundedWithNodeCapTerminates(t *testing.T) {
	saved := searchWorkers
	searchWorkers = 1
	defer func() { searchWorkers = saved }()
	for _, b := range limitedBoards(4) {
		lim := searchLimits{maxNodes: 20000}
		start := time.Now()
		first := limited(b, lim)
		el := time.Since(start)
		if first.stop != stopNodes && first.stop != stopLadderEnd {
			t.Fatalf("board %016x: stop=%q, want nodes or ladder_end", b, first.stop)
		}
		if el > 10*time.Second {
			t.Fatalf("board %016x: unbounded search with a 20k node cap took %v", b, el)
		}
		second := limited(b, lim)
		if first.bestDir != second.bestDir || first.bestValue != second.bestValue || first.depthLimit != second.depthLimit {
			t.Fatalf("board %016x: node-capped serial search not reproducible: %+v vs %+v", b, first, second)
		}
	}
}

// TestProtocolV2Wire drives the handlers like a client: the old wire reply is
// the exact 4-field line it always was; a tailed request appends the stop tail
// and agrees with the old wire on the first 4 fields; prefetch/lookup carry it
// too; version reports protocol=2 and engine=engine2048.
func TestProtocolV2Wire(t *testing.T) {
	b := limitedBoards(1)[0]
	hex := formatBoardHex(b)
	old := handleLine("search "+hex+" 3 0.1 0", tableEvaluator{})
	if n := len(strings.Fields(old)); n != 4 {
		t.Fatalf("old wire reply has %d fields, want 4: %q", n, old)
	}
	v2 := handleLine("search "+hex+" 3 0.1 0 0 0 maxdepth=3 nodes=0", tableEvaluator{})
	f := strings.Fields(v2)
	if len(f) != 7 || f[4] != "stop=depth" || !strings.HasPrefix(f[5], "nodes=") || f[6] != "cprob=0.0001" {
		t.Fatalf("v2 reply %q: want 4 positional + stop=depth nodes= cprob=0.0001", v2)
	}
	of := strings.Fields(old)
	if of[0] != f[0] || of[1] != f[1] || of[3] != f[3] {
		t.Fatalf("v2 fixed-depth %q disagrees with old wire %q", v2, old)
	}
	// budget_ms 0 is legal only with a tail.
	if got := handleLine("search "+hex+" 3 0.1 0 0 0", tableEvaluator{}); !strings.HasPrefix(got, "err") {
		t.Fatalf("old wire accepted budget_ms 0: %q", got)
	}
	if got := handleLine("search "+hex+" 3 0.1 0 0 0 bogus=1", tableEvaluator{}); !strings.HasPrefix(got, "err") {
		t.Fatalf("unknown limit key accepted: %q", got)
	}
	nodes := handleLine("search "+hex+" 3 0.1 0 0 0 maxdepth=6 nodes=500", tableEvaluator{})
	if !strings.Contains(nodes, "stop=nodes") {
		t.Fatalf("node-capped reply %q lacks stop=nodes", nodes)
	}
	// prefetch + lookup carry the tail for a v2 prefetch, and not for an old one.
	states := enumerateSpawnStates(b)
	if r := handleLine("prefetch "+hex+" 2 0.1 0 0 0 maxdepth=2", tableEvaluator{}); !strings.HasPrefix(r, "ok ") {
		t.Fatalf("v2 prefetch reply %q", r)
	}
	hit := waitLookup(t, states[0])
	if !strings.Contains(hit, "stop=depth") {
		t.Fatalf("v2 lookup %q lacks the stop tail", hit)
	}
	if r := handleLine("prefetch "+hex+" 2 0.1 0 0 0", tableEvaluator{}); !strings.HasPrefix(r, "ok ") {
		t.Fatalf("old prefetch reply %q", r)
	}
	hit = waitLookup(t, states[0])
	if n := len(strings.Fields(hit)); n != 4 {
		t.Fatalf("old-wire lookup %q has %d fields, want 4", hit, n)
	}
	stopPrefetch()
	ver := handleLine("version", tableEvaluator{})
	if !strings.Contains(ver, " protocol=2 ") || !strings.Contains(ver, " engine=engine2048 ") {
		t.Fatalf("version %q lacks protocol=2 / engine=engine2048", ver)
	}
}

func waitLookup(t *testing.T, state uint64) string {
	t.Helper()
	deadline := time.Now().Add(30 * time.Second)
	for {
		r := handleLine("lookup "+formatBoardHex(state), tableEvaluator{})
		if r != "miss" {
			return r
		}
		if time.Now().After(deadline) {
			t.Fatal("lookup still misses after 30s")
		}
		time.Sleep(time.Millisecond)
	}
}
