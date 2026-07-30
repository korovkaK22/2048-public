package main

import (
	"fmt"
	"math"
	"os"
	"runtime"
	"sort"
	"testing"
	"time"
)

// searchPar runs a FIXED-DEPTH search through the parallel driver. It exists
// for the tests and measurements only: production reaches the parallel driver
// exclusively through searchBudget (the live, time-budgeted path).
func searchPar(board uint64, depth int, p4 float64, eval Evaluator, farm bool, workers int) searchResult {
	return searchParC(board, depth, cprobThreshBase, p4, eval, farm, workers)
}

// searchParC is searchPar with an explicit probability horizon.
func searchParC(board uint64, depth int, cprob, p4 float64, eval Evaluator, farm bool, workers int) searchResult {
	tabs := &searchTables{}
	if farm {
		eval = farmEvaluator{}
	}
	tabs.beginSearch(workers)
	cfg := iterCfg{
		depthLimit:  int32(depth),
		cprobThresh: cprob,
		p4:          p4,
		eval:        eval,
		evalKind:    evalKindOf(eval, farm),
		farm:        farm,
		workers:     workers,
	}
	values, legal, completed := runIteration(board, cfg, tabs)
	if !completed {
		panic("fixed-depth parallel search aborted with no abort source")
	}
	dir, val := pickBest(values, legal)
	return searchResult{bestDir: dir, bestValue: val, depthLimit: depth}
}

// serialRootValues reproduces the serial fixed-depth search's per-direction
// root values exactly: same cfg as searchExt (workers: 1, cprobThreshBase),
// fresh epoch-bumped tables — and the serial search is a pure function of
// (board, parameters), pinned by TestSearchDeterministic.
func serialRootValues(board uint64, depth int, p4 float64, eval Evaluator, farm bool) ([4]float64, [4]bool) {
	tabs := &searchTables{}
	if farm {
		eval = farmEvaluator{}
	}
	tabs.beginSearch(1)
	cfg := iterCfg{
		depthLimit:  int32(depth),
		cprobThresh: cprobThreshBase,
		p4:          p4,
		eval:        eval,
		evalKind:    evalKindOf(eval, farm),
		farm:        farm,
		workers:     1,
	}
	values, legal, completed := runIteration(board, cfg, tabs)
	if !completed {
		panic("serial fixed-depth search aborted with no abort source")
	}
	return values, legal
}

// TestParallelAgreesWithSerial: the parallel frontier must reproduce the serial
// search's decision QUALITY. Exact argmax identity is NOT a sound assertion
// here: the values legitimately differ in their last bits — the two shapes
// partition the transposition table differently, and a TT entry stored under
// one cprob path is reused under another, which is already true of the serial
// search — so when two root moves are a near-tie (closer than that wobble,
// e.g. exp(14.32001) vs exp(14.31999) on windows/amd64), the argmax may flip
// on a legitimately equivalent move. So we assert within-epsilon-of-optimal:
// the move the parallel driver picks must have a SERIAL value within a small
// relative epsilon of the serial optimum. A genuinely wrong parallel result
// (broken fold, dropped subtree, misrouted task) picks a move whose serial
// value is off by orders of magnitude more than the TT wobble, so this still
// catches real bugs while tolerating tie-order. Epsilon: 2e-2 relative — the
// same bound the value wiring check below already uses. Observed legitimate
// flips: ~4e-5 relative in classic mode, up to ~1.2e-2 in farm mode on
// windows/amd64 (farm values are small and pass through discontinuous stage
// gates, so a last-bit TT difference amplifies); a broken fold picks moves
// whose serial value is wrong by far more than 2%.
func TestParallelAgreesWithSerial(t *testing.T) {
	for _, farm := range []bool{false, true} {
		for _, b := range getBenchBoards() {
			want := search(b, 5, 0.1, false, tableEvaluator{}, farm)
			got := searchPar(b, 5, 0.1, tableEvaluator{}, farm, 8)
			if want.bestDir != got.bestDir {
				serValues, serLegal := serialRootValues(b, 5, 0.1, tableEvaluator{}, farm)
				if got.bestDir < 0 || !serLegal[got.bestDir] {
					t.Fatalf("farm=%v board %016x: parallel picked dir %d, not a legal serial move (serial dir %d)",
						farm, b, got.bestDir, want.bestDir)
				}
				chosen := serValues[got.bestDir]
				if d := want.bestValue - chosen; d > 2e-2*math.Abs(want.bestValue)+1e-6 {
					t.Fatalf("farm=%v board %016x: serial dir %d (%v), parallel dir %d whose serial value %v is %v below optimum — not a near-tie",
						farm, b, want.bestDir, want.bestValue, got.bestDir, chosen, d)
				}
			}
			// Tolerance, not equality: the transposition table caches a value
			// under (board, remaining depth) but that value was computed at some
			// cprob, and a later lookup at a DIFFERENT cprob reuses it — so the
			// answer already depends on traversal order, in the serial search as
			// much as here. Splitting the tree across per-worker tables reshuffles
			// which of those reuses happen. Observed drift: ~2e-5 relative in
			// classic mode, ~2e-3 in farm mode (smaller values, same absolute
			// wobble). The bound is loose on purpose — it is a wiring check (a
			// broken fold is off by orders of magnitude); the DECISION check above
			// is the one that has to be exact.
			if d := math.Abs(want.bestValue - got.bestValue); d > 2e-2*math.Abs(want.bestValue)+1e-6 {
				t.Fatalf("farm=%v board %016x: value %v vs %v (diff %v)",
					farm, b, want.bestValue, got.bestValue, d)
			}
		}
	}
}

// TestParallelWorkerCountDoesNotChangeDecision: every fold width must land on
// a move of the same QUALITY. Exact argmax identity across widths is unsound
// for the same reason as in TestParallelAgreesWithSerial: different widths
// partition the per-worker transposition tables differently, so on a near-tie
// the argmax may flip between equivalent moves. Any width that picks a move
// whose SERIAL value is more than epsilon below the serial optimum is a real
// bug and still fails.
func TestParallelWorkerCountDoesNotChangeDecision(t *testing.T) {
	for _, b := range getBenchBoards()[:12] {
		base := searchPar(b, 5, 0.1, tableEvaluator{}, false, 2)
		haveSer := false
		var serValues [4]float64
		var serLegal [4]bool
		var serBest float64
		for _, w := range []int{4, 8, 16, 32} {
			got := searchPar(b, 5, 0.1, tableEvaluator{}, false, w)
			if got.bestDir == base.bestDir {
				continue
			}
			if !haveSer {
				serValues, serLegal = serialRootValues(b, 5, 0.1, tableEvaluator{}, false)
				_, serBest = pickBest(serValues, serLegal)
				haveSer = true
			}
			for _, dir := range []int{base.bestDir, got.bestDir} {
				if dir < 0 || !serLegal[dir] {
					t.Fatalf("board %016x: %d workers -> dir %d, 2 workers -> dir %d; dir %d is not a legal serial move",
						b, w, got.bestDir, base.bestDir, dir)
				}
				if d := serBest - serValues[dir]; d > 2e-2*math.Abs(serBest)+1e-6 {
					t.Fatalf("board %016x: %d workers -> dir %d, 2 workers -> dir %d; dir %d has serial value %v, %v below optimum %v — not a near-tie",
						b, w, got.bestDir, base.bestDir, dir, serValues[dir], d, serBest)
				}
			}
		}
	}
}

// TestBudgetSearchRespectsBudget: the live search must not blow through its
// deadline, and it must come back with a usable move and a depth at least as
// deep as the insurance pass.
func TestBudgetSearchRespectsBudget(t *testing.T) {
	for _, b := range getBenchBoards()[:6] {
		start := time.Now()
		res := searchBudget(b, 0.1, tableEvaluator{}, false, 60)
		el := time.Since(start)
		if res.bestDir < 0 || executeMove(b, res.bestDir) == b {
			t.Fatalf("board %016x: budget search returned illegal dir %d", b, res.bestDir)
		}
		if res.depthLimit < budgetInsuranceDepth {
			t.Fatalf("board %016x: depth %d below insurance", b, res.depthLimit)
		}
		// A SMOKE check that the deadline is armed at all, not a timing test:
		// this runs on a shared box, and under -race every node is ~15x slower,
		// so any tight bound here measures the machine and flakes. What it does
		// catch is the failure that matters — a budget search with no working
		// abort, which would climb the ladder for minutes.
		if el > 5*time.Second {
			t.Fatalf("board %016x: 60 ms budget took %v — deadline not honoured", b, el)
		}
	}
}

// TestSimulationPathIsSerial pins the hard requirement: the fixed-depth search
// simulations use must not go anywhere near the parallel driver, whatever
// BOT2048_SEARCH_WORKERS says. If someone wires searchWorkers into `search`,
// this fails — the node count of a fixed-depth search must be identical for any
// worker setting, because the worker setting must not reach it.
func TestSimulationPathIsSerial(t *testing.T) {
	b := getBenchBoards()[3]
	saved := searchWorkers
	defer func() { searchWorkers = saved }()

	nodesFor := func(w int) uint64 {
		searchWorkers = w
		before := searchNodesCounted.Load()
		search(b, 6, 0.1, true, tableEvaluator{}, false)
		return searchNodesCounted.Load() - before
	}
	n1 := nodesFor(1)
	n13 := nodesFor(13)
	if n1 != n13 {
		t.Fatalf("fixed-depth search saw the worker knob: %d nodes at workers=1, %d at workers=13", n1, n13)
	}
}

// ---------------------------------------------------------------------------
// Measurements (run with -run XXX -bench, or as the reporting tests below with
// `BOT2048_MEASURE=1 go test -run 'TestMeasure' -v`). They print; they never
// assert timings — the build machine is shared.

// skipUnlessMeasuring gates the measurement tests behind an explicit opt-in.
// They never assert — they exist to be read — and by design they spend up to
// 30 s per rung (medianMs bails at 30 s, per rung, per empties bucket), so a
// plain `go test ./...` would blow the test binary's default 10-minute
// timeout long before printing anything useful.
func skipUnlessMeasuring(t *testing.T) {
	t.Helper()
	if testing.Short() || os.Getenv("BOT2048_MEASURE") == "" {
		t.Skip("measurement — set BOT2048_MEASURE=1 to run")
	}
}

func cpuBoardsByEmpties() map[int][]uint64 {
	byE := map[int][]uint64{}
	for _, b := range playoutBoards(0xC0FFEE, 400) {
		e := countEmpty(b)
		if len(byE[e]) < 6 {
			byE[e] = append(byE[e], b)
		}
	}
	return byE
}

// TestMeasureLadder prints the cost of each rung of the live ladder, split by
// how many cells are empty — the honest picture of the WORST case, not the
// average. Run it TWICE, once with GOMAXPROCS=1 (the true 1-core baseline) and
// once with GOMAXPROCS=14 BOT2048_SEARCH_WORKERS=13, and divide.
//
// One rung of the ladder is (depth, cprob): depth alone saturates around 8, and
// the cprob horizon is what actually keeps buying search after that.
func TestMeasureLadder(t *testing.T) {
	skipUnlessMeasuring(t)
	byE := cpuBoardsByEmpties()
	es := []int{}
	for e := range byE {
		es = append(es, e)
	}
	sort.Ints(es)

	w := searchWorkers
	rungs := []ladderRung{{6, 1e-4}, {8, 1e-4}, {10, 1e-5}, {12, 1e-6}, {16, 1e-7}}
	fmt.Printf("\n=== median ms per move  (workers=%d, GOMAXPROCS=%d) ===\n", w, runtime.GOMAXPROCS(0))
	fmt.Printf("%-8s", "empties")
	for _, r := range rungs {
		fmt.Printf(" %11s", fmt.Sprintf("d%d/%.0e", r.depth, r.cprob))
	}
	fmt.Println()
	for _, e := range es {
		if e > 10 || len(byE[e]) < 3 {
			continue
		}
		fmt.Printf("%-8d", e)
		for _, r := range rungs {
			// Give up on a rung once the previous one already cost seconds: on one
			// core the deep/wide rungs of the crowded boards run for minutes, and
			// the point of the table is that they are UNAFFORDABLE, not their exact
			// price.
			v := medianMs(byE[e], r, w)
			if v < 0 {
				fmt.Printf(" %11s", ">30000")
				continue
			}
			fmt.Printf(" %11.1f", v)
		}
		fmt.Println()
	}
}

// medianMs times one rung over `boards`, bailing out (-1) once the total spent
// exceeds 30 s — a rung that slow has already answered the question.
func medianMs(boards []uint64, r ladderRung, workers int) float64 {
	ms := []float64{}
	budget := 30 * time.Second
	spent := time.Duration(0)
	for _, b := range boards {
		start := time.Now()
		searchParC(b, r.depth, r.cprob, 0.1, tableEvaluator{}, false, workers)
		el := time.Since(start)
		spent += el
		ms = append(ms, float64(el.Microseconds())/1000)
		if spent > budget {
			return -1
		}
	}
	sort.Float64s(ms)
	return ms[len(ms)/2]
}

// TestMeasureBudgetDepth answers the question the live bot actually asks: at a
// 400 ms budget, how deep does it get?
func TestMeasureBudgetDepth(t *testing.T) {
	skipUnlessMeasuring(t)
	boards := playoutBoards(0xD00D, 40)
	for _, budget := range []float64{200, 400, 500} {
		hist := map[int]int{}
		var sumMs float64
		worst := 0.0
		minD, maxD := 99, 0
		for _, b := range boards {
			start := time.Now()
			res := searchBudget(b, 0.1, tableEvaluator{}, false, budget)
			el := float64(time.Since(start).Microseconds()) / 1000
			sumMs += el
			if el > worst {
				worst = el
			}
			hist[res.depthLimit]++
			if res.depthLimit < minD {
				minD = res.depthLimit
			}
			if res.depthLimit > maxD {
				maxD = res.depthLimit
			}
		}
		ds := []int{}
		for d := range hist {
			ds = append(ds, d)
		}
		sort.Ints(ds)
		fmt.Printf("budget %3.0f ms (workers=%d): depth min=%d max=%d  avg_wall=%.0f ms worst_wall=%.0f ms  hist=",
			budget, searchWorkers, minD, maxD, sumMs/float64(len(boards)), worst)
		for _, d := range ds {
			fmt.Printf(" d%d:%d", d, hist[d])
		}
		fmt.Println()
	}
}

// TestMeasureSpeedup is the scaling number: the SAME rung, timed on one core and
// on the pool, over the same boards. Run it with BOT2048_SEARCH_WORKERS=13 and
// GOMAXPROCS=14; it does its own 1-worker baseline in-process, so both halves see
// the same machine load (which matters — this box is shared).
//
// Note the baseline is workers=1, which still uses the 4-goroutine ROOT SPLIT.
// Under GOMAXPROCS=14 that baseline is itself parallel (~2-3 cores), so the ratio
// printed here is the gain OVER the shipped engine, which is what we care about.
func TestMeasureSpeedup(t *testing.T) {
	skipUnlessMeasuring(t)
	boards := playoutBoards(0xFEED, 10)
	w := searchWorkers
	fmt.Printf("\n=== scaling, workers=1 vs workers=%d (GOMAXPROCS=%d), 10 boards ===\n",
		w, runtime.GOMAXPROCS(0))
	fmt.Println("cores = CPU-time/wall-time actually achieved. On a CONTENDED box (this one)")
	fmt.Println("it UNDERSTATES the idle-machine speedup — it is a floor, not a ceiling.")
	for _, r := range []ladderRung{{6, 1e-4}, {8, 1e-4}, {10, 1e-5}, {12, 1e-6}} {
		serWall, serCPU := timeRung(boards, r, 1)
		parWall, parCPU := timeRung(boards, r, w)
		n := float64(len(boards))
		fmt.Printf("d%-2d/%.0e | 1w: %7.1f ms wall %7.1f ms cpu (%.1f cores) | %2dw: %7.1f ms wall %7.1f ms cpu (%.1f cores) | wall speedup %5.2fx  cpu overhead %4.2fx\n",
			r.depth, r.cprob,
			ms(serWall)/n, ms(serCPU)/n, float64(serCPU)/float64(serWall),
			w, ms(parWall)/n, ms(parCPU)/n, float64(parCPU)/float64(parWall),
			float64(serWall)/float64(parWall), float64(parCPU)/float64(serCPU))
	}
}

// TestMeasureCriticalPath is the scaling number that a busy machine cannot
// distort. Wall-clock speedup on a box already running 13 other jobs measures
// the BOX (a single CPU-bound search gets 0.5 of a core there), not the search.
// So measure the shape of the work instead:
//
//	total task nodes / biggest task nodes
//
// is the critical-path bound — the speedup an infinite number of cores could
// reach, since no schedule finishes before its single largest task does. If this
// is comfortably above 13, the frontier admits full 13-core parallelism and the
// only thing standing between us and it is idle cores.
func TestMeasureCriticalPath(t *testing.T) {
	skipUnlessMeasuring(t)
	byE := cpuBoardsByEmpties()
	es := []int{}
	for e := range byE {
		es = append(es, e)
	}
	sort.Ints(es)
	w := searchWorkers

	fmt.Printf("\n=== critical-path bound on the speedup (workers=%d) ===\n", w)
	fmt.Printf("%-8s %-12s %8s %12s %12s %10s\n", "empties", "rung", "tasks", "nodes total", "nodes max", "max x")
	for _, e := range es {
		if e > 8 || len(byE[e]) < 3 {
			continue
		}
		for _, r := range []ladderRung{{8, 1e-4}, {10, 1e-5}, {12, 1e-6}} {
			var tasks int
			var sum, max uint64
			for _, b := range byE[e][:3] {
				tabs := &searchTables{}
				tabs.beginSearch(w)
				cfg := iterCfg{
					depthLimit: int32(r.depth), cprobThresh: r.cprob, p4: 0.1,
					eval: tableEvaluator{}, evalKind: evalKindTable, workers: w,
				}
				runIteration(b, cfg, tabs)
				tasks += tabs.lastTaskCount
				sum += tabs.lastTaskNodesSum
				max += tabs.lastTaskNodesMax
			}
			if sum == 0 {
				continue
			}
			fmt.Printf("%-8d %-12s %8d %12d %12d %9.1fx\n",
				e, fmt.Sprintf("d%d/%.0e", r.depth, r.cprob), tasks/3, sum/3, max/3,
				float64(sum)/float64(max))
		}
	}
}

func ms(d time.Duration) float64 { return float64(d.Microseconds()) / 1000 }

// cpuTime (the process's total user+system CPU across all threads) lives in
// the OS-tagged cpu_time_*_test.go files: getrusage(RUSAGE_SELF) on unix,
// GetProcessTimes on windows, a zero fallback elsewhere.

func timeRung(boards []uint64, r ladderRung, workers int) (wall, cpu time.Duration) {
	c0, w0 := cpuTime(), time.Now()
	for _, b := range boards {
		searchParC(b, r.depth, r.cprob, 0.1, tableEvaluator{}, false, workers)
	}
	return time.Since(w0), cpuTime() - c0
}

// TestMeasureNodeInflation quantifies the ONE cost of per-worker transposition
// tables: a subtree evaluated by worker 3 cannot reuse worker 7's entries, so
// the pool visits more nodes than the serial search for the same answer. This is
// the number that caps the parallel speedup from above.
func TestMeasureNodeInflation(t *testing.T) {
	skipUnlessMeasuring(t)
	boards := playoutBoards(0xBEEF, 24)
	for _, d := range []int{6, 8, 10} {
		var ser, par uint64
		for _, b := range boards {
			n0 := searchNodesCounted.Load()
			search(b, d, 0.1, false, tableEvaluator{}, false)
			n1 := searchNodesCounted.Load()
			searchPar(b, d, 0.1, tableEvaluator{}, false, searchWorkers)
			n2 := searchNodesCounted.Load()
			ser += n1 - n0
			par += n2 - n1
		}
		fmt.Printf("depth %2d: serial %12d nodes, parallel(%d) %12d nodes -> %.2fx work\n",
			d, ser, searchWorkers, par, float64(par)/float64(ser))
	}
}
