package main

import (
	"strings"
	"testing"
	"time"
)

// Protocol-2 search limits (limitedSearch + the k=v wire on the serve path).
// Everything here runs on the FARM leaf (mode 1), which needs no weight file,
// so the tests never skip; the net leaf shares every line of the limit code.

// farmBoards walks a shallow farm-search game and samples mid/late boards.
func farmBoards(n int) []uint64 {
	initTables()
	initFarmTables()
	r := &rng{s: 0xC0FFEE}
	s := newFarmSearcher(2, 0.1)
	out := make([]uint64, 0, n)
	b := initBoard(r)
	for len(out) < n {
		dir, _ := s.bestMove(b)
		if dir < 0 {
			b = initBoard(r)
			continue
		}
		nb := executeAllMoves(b)[dir]
		if hasRank11(nb) {
			b = initBoard(r)
			continue
		}
		b = spawn(nb, r)
		if countEmpty(b) <= 8 && countEmpty(b) >= 2 {
			out = append(out, b)
		}
	}
	return out
}

func farmLimited(b uint64, lim searchLimits) searchOutcome {
	s := newFarmSearcher(4, 0.1)
	return limitedSearch(s, b, lim, 1)
}

// TestLimitedCappedCompletedMatchesFixedDepth: the key property of the depth
// cap. A (maxdepth=D, budget=B) search that COMPLETES the direct attempt must
// be bit-identical to the plain fixed-depth-D bestMove — same direction, same
// root value — and say so with stop=depth (workers=1).
func TestLimitedCappedCompletedMatchesFixedDepth(t *testing.T) {
	const depth = 4
	for _, b := range farmBoards(12) {
		ref := newFarmSearcher(depth, 0.1)
		wantDir, wantVal := ref.bestMove(b)
		wantNodes := ref.nodes
		got := farmLimited(b, searchLimits{maxDepth: depth, budgetMs: 10000})
		if got.stop != stopDepth {
			t.Fatalf("board %016x: stop=%q, want depth (10 s budget, depth 4)", b, got.stop)
		}
		if got.dir != wantDir || got.val != wantVal || got.depth != depth {
			t.Fatalf("board %016x: capped (%d, %v, %d) != fixed (%d, %v, %d)",
				b, got.dir, got.val, got.depth, wantDir, wantVal, depth)
		}
		if got.nodes != wantNodes || got.cprob != cprobThresh {
			t.Fatalf("board %016x: telemetry nodes=%d/%d cprob=%v", b, got.nodes, wantNodes, got.cprob)
		}
	}
}

// TestLimitedTinyBudgetStopsOnBudget: a budget no depth-8 search can meet ->
// the direct attempt is discarded and the capped ladder answers with a legal
// move, depth <= cap and stop=budget (or depth on a board small enough to fit).
func TestLimitedTinyBudgetStopsOnBudget(t *testing.T) {
	budgetStops := 0
	for _, b := range farmBoards(8) {
		start := time.Now()
		out := farmLimited(b, searchLimits{maxDepth: 8, budgetMs: 0.5})
		el := time.Since(start)
		if out.dir < 0 || executeAllMoves(b)[out.dir] == b {
			t.Fatalf("board %016x: illegal dir %d", b, out.dir)
		}
		if out.depth > 8 {
			t.Fatalf("board %016x: depth %d exceeds the cap 8", b, out.depth)
		}
		switch out.stop {
		case stopBudget:
			budgetStops++
		case stopDepth:
		default:
			t.Fatalf("board %016x: stop=%q, want budget or depth", b, out.stop)
		}
		if el > 5*time.Second {
			t.Fatalf("board %016x: 0.5 ms budget took %v — deadline not honoured", b, el)
		}
	}
	if budgetStops == 0 {
		t.Fatal("no board stopped on budget — a 0.5 ms depth-8 cap should not be affordable")
	}
}

// TestLimitedNodeCapStopsOnNodes: a node cap far below a depth-6 search ends
// the request with stop=nodes and still returns a legal move.
func TestLimitedNodeCapStopsOnNodes(t *testing.T) {
	for _, b := range farmBoards(6) {
		out := farmLimited(b, searchLimits{maxDepth: 6, maxNodes: 1000})
		if out.dir < 0 || executeAllMoves(b)[out.dir] == b {
			t.Fatalf("board %016x: illegal dir %d", b, out.dir)
		}
		if out.stop != stopNodes {
			t.Fatalf("board %016x: stop=%q depth=%d nodes=%d, want nodes", b, out.stop, out.depth, out.nodes)
		}
	}
}

// TestLimitedUnboundedWithNodeCapTerminates: both limits None is the
// "unbounded" ladder; the node cap brings it back, reproducibly in serial.
func TestLimitedUnboundedWithNodeCapTerminates(t *testing.T) {
	for _, b := range farmBoards(4) {
		lim := searchLimits{maxNodes: 20000}
		start := time.Now()
		first := farmLimited(b, lim)
		el := time.Since(start)
		if first.stop != stopNodes && first.stop != stopLadderEnd {
			t.Fatalf("board %016x: stop=%q, want nodes or ladder_end", b, first.stop)
		}
		if el > 10*time.Second {
			t.Fatalf("board %016x: unbounded search with a 20k node cap took %v", b, el)
		}
		second := farmLimited(b, lim)
		if first.dir != second.dir || first.val != second.val || first.depth != second.depth {
			t.Fatalf("board %016x: node-capped serial search not reproducible: %+v vs %+v", b, first, second)
		}
	}
}

// TestServeProtocolV2Wire drives the serve handlers like a client (farm mode,
// so no net is needed): the old wire reply is the exact 4-field line; a tailed
// request appends the stop tail and agrees with the old wire on the positional
// fields; prefetch/lookup carry it; version reports protocol=2 engine=ntuple.
func TestServeProtocolV2Wire(t *testing.T) {
	b := farmBoards(1)[0]
	hex := formatBoardHex(b)
	sv := &server{}
	old := sv.handleLine("search " + hex + " 3 0.1 0 1")
	if n := len(strings.Fields(old)); n != 4 {
		t.Fatalf("old wire reply has %d fields, want 4: %q", n, old)
	}
	v2 := sv.handleLine("search " + hex + " 3 0.1 0 1 0 maxdepth=3 nodes=0")
	f := strings.Fields(v2)
	if len(f) != 7 || f[4] != "stop=depth" || !strings.HasPrefix(f[5], "nodes=") || f[6] != "cprob=0.0001" {
		t.Fatalf("v2 reply %q: want 4 positional + stop=depth nodes= cprob=0.0001", v2)
	}
	of := strings.Fields(old)
	if of[0] != f[0] || of[1] != f[1] || of[3] != f[3] {
		t.Fatalf("v2 fixed-depth %q disagrees with old wire %q", v2, old)
	}
	if got := sv.handleLine("search " + hex + " 3 0.1 0 1 0"); !strings.HasPrefix(got, "err") {
		t.Fatalf("old wire accepted budget_ms 0: %q", got)
	}
	if got := sv.handleLine("search " + hex + " 3 0.1 0 1 0 bogus=1"); !strings.HasPrefix(got, "err") {
		t.Fatalf("unknown limit key accepted: %q", got)
	}
	if got := sv.handleLine("search " + hex + " 3 0.1 0 1 0 maxdepth=6 nodes=500"); !strings.Contains(got, "stop=nodes") {
		t.Fatalf("node-capped reply %q lacks stop=nodes", got)
	}
	states := enumerateSpawnStates(b)
	if r := sv.handleLine("prefetch " + hex + " 2 0.1 0 1 0 maxdepth=2"); !strings.HasPrefix(r, "ok ") {
		t.Fatalf("v2 prefetch reply %q", r)
	}
	hit := waitServeLookup(t, sv, states[0])
	if !strings.Contains(hit, "stop=depth") {
		t.Fatalf("v2 lookup %q lacks the stop tail", hit)
	}
	if r := sv.handleLine("prefetch " + hex + " 2 0.1 0 1 0"); !strings.HasPrefix(r, "ok ") {
		t.Fatalf("old prefetch reply %q", r)
	}
	hit = waitServeLookup(t, sv, states[0])
	if n := len(strings.Fields(hit)); n != 4 {
		t.Fatalf("old-wire lookup %q has %d fields, want 4", hit, n)
	}
	sv.stopPrefetch()
	ver := sv.handleLine("version")
	if !strings.Contains(ver, " protocol=2 ") || !strings.Contains(ver, " engine=ntuple ") {
		t.Fatalf("version %q lacks protocol=2 / engine=ntuple", ver)
	}
}

func waitServeLookup(t *testing.T, sv *server, state uint64) string {
	t.Helper()
	deadline := time.Now().Add(30 * time.Second)
	for {
		r := sv.handleLine("lookup " + formatBoardHex(state))
		if r != "miss" {
			return r
		}
		if time.Now().After(deadline) {
			t.Fatal("lookup still misses after 30s")
		}
		time.Sleep(time.Millisecond)
	}
}
