package main

// serve: the stdin line protocol of tools/engine2048, backed by the learned
// n-tuple net (or the farm leaf, per-request), so the live runner
// (src/bot2048/ai/go_engine.py) can drive this engine unchanged.
//
// Protocol (one request line -> one reply line, mirroring engine2048/main.go):
//
//	search <16hex> <depth> <p4> <adaptive 0|1> [mode 0|1] [budget_ms]
//	    -> <dir> <value> <ms> <depth_limit>
//	    (mode 0/absent = the LEARNED n-tuple leaf in the objective carried by
//	     the weight file's trailer; mode 1 = the farm S-sum objective, i.e.
//	     this module's port of the production search. `adaptive` is accepted
//	     for wire compatibility but ignored — this searcher has no adaptive
//	     depth. budget_ms > 0 switches to iterative deepening over the
//	     (depth, cprob) budgetLadder: rungs deepen first, then widen the
//	     probability horizon, run until the budget would be blown, and
//	     <depth_limit> reports the deepest completed rung's depth.)
//	move <16hex> <dir>
//	    -> <16hex-newboard> <score> <moved 0|1>
//	prefetch <16hex> <depth> <p4> <adaptive 0|1> <mode 0|1> <budget_ms>
//	    -> ok <n_states>
//	    (speculative: enumerates every spawn state of the post-swipe board
//	     — each empty cell x {exp1 w.p. 1-p4, exp2 w.p. p4}, 2-spawns first —
//	     and computes, across a bounded pool of goroutines (one single-threaded
//	     net-leaf searcher each), the decision a `search` with the same
//	     parameters would return, caching it by board. Replies immediately;
//	     the next real `search`/`prefetch` aborts the in-flight speculation
//	     (between-candidate granularity, since the leaf search is atomic here)
//	     before it runs, so a stale prefetch never serves a wrong board.)
//	lookup <16hex>
//	    -> hit <dir> <value> <depth> | miss
//	    (served from the speculation cache; never triggers a search)
//	version
//	    -> version protocol=<n> go=<ver> <KEY>=<value> ...
//	ping -> pong
//	anything else -> err <message>
//
// <16hex>: 16 hex digits, digit j (leftmost = j=0) = exponent of cell j
// (row-major) — identical to engine2048 and go_engine.py's board_to_hex.
// EOF on stdin -> exit 0.

import (
	"bufio"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// serveProtocolVersion mirrors engine2048's engineProtocolVersion: the wire
// shapes above are the same protocol 1 the production engine speaks.
const serveProtocolVersion = 1

// ---------------------------------------------------------------------------
// The budget ladder (ported from engine2048/search.go's live ladder).
//
// Iterative deepening on DEPTH ALONE saturates: every chance node divides the
// branch probability by the number of empty cells, so after 4-5 chance levels
// the branch has fallen under the 1e-4 cprob cutoff and is cut no matter how
// much depth is left — handing the search a bigger `depth` buys nothing past
// ~depth 7-8. The lever that DOES convert budget into horizon is the cprob
// threshold itself.
//
// So the ladder has two axes: it deepens first (cheap, and depth still matters
// on the crowded boards where cprob does NOT cut early — exactly the dangerous
// positions), then widens the probability horizon by ~half a decade per rung.
// Each rung is strictly stronger than the one before, so stopping at any rung
// leaves a usable, complete answer.
type ladderRung struct {
	depth int
	cprob float64
}

var budgetLadder = []ladderRung{
	{3, 1e-4}, {4, 1e-4}, {5, 1e-4}, {6, 1e-4}, {7, 1e-4}, {8, 1e-4},
	{9, 3e-5}, {10, 1e-5}, {11, 3e-6}, {12, 1e-6},
	{14, 3e-7}, {16, 1e-7},
}

const (
	// budgetInsuranceDepth: an always-completed cheap pass (well under a
	// millisecond), so even a tiny budget returns a legal move.
	budgetInsuranceDepth = 2
	// Growth predictor bounds: the cost of the next rung is estimated as
	// last * ratio, where ratio is the growth actually observed between the
	// two most recent rungs of THIS call, clamped to
	// [budgetMinRatio, budgetMaxRatio]. Until two rungs have been timed,
	// budgetDefaultRatio is assumed (conservative for this tree — cprob
	// pruning usually keeps real growth below ~6x).
	budgetDefaultRatio = 5.0
	budgetMinRatio     = 2.0
	budgetMaxRatio     = 10.0
)

// searchWorkers is the width of the live budget-search frontier pool: the
// number of goroutines the parallel driver drains ladder-rung tasks with.
// Default = NumCPU()-2 (leave the OS + the stdio/prefetch driver a core),
// overridable via BOT2048_SEARCH_WORKERS. Setting it to 1 disables the
// parallel driver entirely and keeps the budget search on the serial path.
// Read ONCE at package init, so a search is never reshaped mid-game.
var searchWorkers = func() int {
	if s := os.Getenv("BOT2048_SEARCH_WORKERS"); s != "" {
		if n, err := strconv.Atoi(s); err == nil && n >= 1 && n <= 1024 {
			return n
		}
	}
	n := runtime.NumCPU() - 2
	if n < 1 {
		n = 1
	}
	return n
}()

// parseBoardHex decodes 16 hex digits into a bitboard (digit j -> bits
// 4*j..4*j+3, matching the row-major cell order). Same as engine2048.
func parseBoardHex(s string) (uint64, error) {
	if len(s) != 16 {
		return 0, fmt.Errorf("board must be 16 hex digits, got %d", len(s))
	}
	var bb uint64
	for j := 0; j < 16; j++ {
		c := s[j]
		var v uint64
		switch {
		case c >= '0' && c <= '9':
			v = uint64(c - '0')
		case c >= 'a' && c <= 'f':
			v = uint64(c-'a') + 10
		case c >= 'A' && c <= 'F':
			v = uint64(c-'A') + 10
		default:
			return 0, fmt.Errorf("invalid hex digit %q", c)
		}
		bb |= v << (4 * j)
	}
	return bb, nil
}

func formatBoardHex(bb uint64) string {
	const digits = "0123456789abcdef"
	var buf [16]byte
	for j := 0; j < 16; j++ {
		buf[j] = digits[(bb>>(4*j))&0xF]
	}
	return string(buf[:])
}

// prefetchEntry is the fully-computed answer for one speculative spawn state.
type prefetchEntry struct {
	dir   int
	value float64
	depth int
}

// server holds the loaded net and one persistent searcher per leaf, so the
// transposition table stays allocated across requests (bestMove bumps the TT
// epoch, so no stale values leak between boards).
//
// It also owns the speculative-prefetch machinery: a cache of pre-computed
// spawn-state decisions plus a reusable pool of per-worker searchers (one TT
// each, so concurrent speculative searches share no mutable state with each
// other or with the real ntupleS/farmS). At most one prefetch runs at a time —
// startPrefetch joins the previous worker set first — so the pools and cache
// need no lock against themselves across prefetch calls; prefetchMu only guards
// the cache map against a concurrent `lookup`.
type server struct {
	n           *net
	weightsPath string
	// ntupleLeaf is the learned-leaf evaluator installed for mode-0 (net) play.
	// It is sv.n.leafW (the exact champion) unless HEUR5 taboo-mobility is
	// enabled via BOT2048_NTUPLE_MOB_W > 0, in which case it is the penalty-
	// wrapped leaf (see mobility.go). Computed once in cmdServe so every
	// searcher — persistent and speculative — shares one leaf function.
	ntupleLeaf evalFn
	mobW       float64    // 0 = OFF (champion); reported in handleVersion
	leafCache  *leafCache // non-nil when the leaf-value cache is enabled
	ntupleS    *searcher
	farmS      *searcher

	// Record engine (record.go). All nil / off unless BOT2048_RECORD_ENGINE=1.
	// When on, the ENDGAME (countEmpty <= exact.empties, maxFinal >= T) is decided
	// by the target-conditioned solver + score-maximiser instead of the net's
	// budget ladder. maxCash / exactScore are single-threaded and reused across
	// requests (their TT epoch is bumped per root); serve handles one real request
	// at a time on the main goroutine, so they need no lock.
	record     recordConfig
	maxCash    *scoreSearcher // endgame score-maximiser (earlyfire + runEndgame source)
	exactScore *scoreSearcher // E[banked] tie-break + cashValue samples
	featScore  *scoreSearcher // lazy score searcher for `features` when record is OFF

	prefetchMu    sync.Mutex
	prefetchCache map[uint64]prefetchEntry
	prefetchWG    sync.WaitGroup
	prefetchAbort atomic.Bool
	specNtuple    []*searcher // reusable per-worker searchers, learned leaf
	specFarm      []*searcher // reusable per-worker searchers, farm leaf
}

// searcherFor returns the persistent searcher for the requested leaf with its
// per-request parameters applied.
func (sv *server) searcherFor(farm bool, depth int, p4 float64) *searcher {
	var s *searcher
	if farm {
		if sv.farmS == nil {
			sv.farmS = newFarmSearcher(depth, p4)
		}
		s = sv.farmS
	} else {
		if sv.ntupleS == nil {
			sv.ntupleS = newSearcher(sv.ntupleLeaf, depth, p4)
		}
		s = sv.ntupleS
	}
	s.depthLimit = int32(depth)
	s.p4 = p4
	// Reset the horizon to the configured default: a previous budget search may
	// have left the ladder's widened cprob behind, and a fixed-depth request
	// must be a pure function of its own parameters.
	s.cprobThresh = cprobThresh
	return s
}

// budgetSearch climbs the (depth, cprob) ladder until the wall-clock budget is
// spent. The insurance pass always completes (so a legal move is always
// found); every further rung runs under a hard in-node deadline, so a rung
// that turns out too expensive is aborted at the deadline and its (garbage)
// result discarded — the previous rung's answer stands. That safety net lets
// the climb gamble: a rung whose measured-growth estimate overshoots is still
// attempted when it completed on the previous move (positions evolve slowly)
// or while more than half the budget remains unspent, converting budget that
// the old conservative estimate left on the table into extra horizon.
// Returns the deepest completed rung's decision and its depth.
//
// The TT epoch is bumped once per call (not per rung), so deeper rungs reuse
// exact values computed by shallower ones — the `remaining` field in the TT
// entry keeps a shallow value from satisfying a deeper lookup. A rung that
// changes the cprob HORIZON invalidates the table instead: a transposition
// entry is keyed by (board, remaining depth), but the value stored there was
// computed under the cprob of ITS rung, and reusing a coarse value in a
// finer-horizon pass would silently undo the widening — the whole point of
// the rung.
// When workers > 1 each ladder rung is driven by the parallel frontier pool
// (parallel.go) over its own shared lock-free table; the insurance pass stays
// serial and exact on the searcher's private ttTable. workers == 1 keeps the
// whole climb on the serial path. Only the live serve/budget path ever passes
// workers > 1 — every fixed-depth caller stays serial and bit-reproducible.
func budgetSearch(s *searcher, board uint64, budgetMs float64, workers int) (dir int, val float64, deepest int) {
	total := time.Duration(budgetMs * float64(time.Millisecond))
	deadline := time.Now().Add(total)
	base := s.cprobThresh
	defer func() { s.cprobThresh = base }() // never leak a widened horizon
	s.beginSearch()                         // bump tt for the serial insurance pass
	if workers > 1 {
		// One pool-table epoch per call, so rungs reuse each other's entries.
		if s.pstt == nil {
			s.pstt = newSharedTT()
		} else {
			s.pstt.nextEpoch()
		}
	}

	// Insurance pass: microseconds, always completes. Always serial + exact.
	s.depthLimit = int32(budgetInsuranceDepth)
	s.cprobThresh = base
	dir, val = s.rootSearch(board)
	deepest = budgetInsuranceDepth
	if dir == -1 {
		return dir, val, deepest
	}

	var last, prev time.Duration
	prevCprob := base
	achieved := -1
	for i, r := range budgetLadder {
		if last > 0 {
			ratio := budgetDefaultRatio
			if prev > 0 {
				ratio = float64(last) / float64(prev)
				if ratio < budgetMinRatio {
					ratio = budgetMinRatio
				} else if ratio > budgetMaxRatio {
					ratio = budgetMaxRatio
				}
			}
			remaining := time.Until(deadline)
			if time.Duration(float64(last)*ratio) > remaining {
				// Predicted to overshoot. Gamble anyway when the hard abort
				// caps the downside: either this rung completed on the
				// previous move, or over half the budget is still unspent.
				gamble := (i <= s.lastAchievedRung && remaining > last) ||
					remaining > total/2
				if !gamble {
					break
				}
			}
		}
		if r.cprob != prevCprob {
			// Horizon change: coarse values must not be reused. Invalidate the
			// table the rungs actually run on.
			if workers > 1 {
				s.pstt.nextEpoch()
			} else {
				s.beginSearch()
			}
		}
		t0 := time.Now()
		s.depthLimit = int32(r.depth)
		s.cprobThresh = r.cprob
		var d int
		var v float64
		var completed bool
		if workers > 1 {
			d, v, completed = s.parallelRung(board, deadline, workers)
		} else {
			d, v, completed = s.rootSearchDeadline(board, deadline)
		}
		if !completed {
			break // aborted mid-rung: d/v are garbage; the previous rung stands
		}
		prev, last = last, time.Since(t0)
		dir, val = d, v
		deepest = r.depth
		prevCprob = r.cprob
		achieved = i
		if !time.Now().Before(deadline) {
			break
		}
	}
	s.lastAchievedRung = achieved
	return dir, val, deepest
}

func (sv *server) handleSearch(fields []string) string {
	if len(fields) < 5 || len(fields) > 7 {
		return "err usage: search <16hex> <depth> <p4> <adaptive 0|1> [mode 0|1] [budget_ms]"
	}
	board, err := parseBoardHex(fields[1])
	if err != nil {
		return "err " + err.Error()
	}
	depth, err := strconv.Atoi(fields[2])
	if err != nil || depth < 1 {
		return "err depth must be an integer >= 1"
	}
	p4, err := strconv.ParseFloat(fields[3], 64)
	if err != nil || p4 < 0.0 || p4 > 1.0 {
		return "err p4 must be a float in [0, 1]"
	}
	switch fields[4] {
	case "0", "1":
		// accepted for wire compatibility; this searcher has no adaptive depth
	default:
		return "err adaptive must be 0 or 1"
	}
	farm := false
	if len(fields) >= 6 {
		switch fields[5] {
		case "0":
			farm = false
		case "1":
			farm = true
		default:
			return "err mode must be 0 (classic) or 1 (farm)"
		}
	}
	budgetMs := 0.0
	if len(fields) == 7 {
		budgetMs, err = strconv.ParseFloat(fields[6], 64)
		if err != nil || budgetMs <= 0.0 || budgetMs > 600000.0 {
			return "err budget_ms must be a float in (0, 600000]"
		}
	}
	start := time.Now()
	var dir, depthLimit int
	var val float64
	// RECORD ENGINE ROUTING. In the endgame (dense board, some line still reaching
	// T) the certified banker OWNS the decision — the expensive 15s budget goes to
	// the deterministic solver, not the net's anti-tail deep cprob ladder. Only the
	// mode-0 (learned net) path is routed; a mode-1 farm request is left untouched.
	// recordSearch returns ok == false when disabled, out of region, or its thin-
	// margin fire is vetoed, and the golden path below runs unchanged.
	if sv.record.on && !farm {
		if rdir, rval, rdl, ok := sv.recordSearch(board); ok {
			ms := float64(time.Since(start).Nanoseconds()) / 1e6
			return fmt.Sprintf("%d %.6f %.3f %d", rdir, rval, ms, rdl)
		}
	}
	s := sv.searcherFor(farm, depth, p4)
	if budgetMs > 0.0 {
		dir, val, depthLimit = budgetSearch(s, board, budgetMs, searchWorkers)
	} else {
		dir, val = s.bestMove(board)
		depthLimit = depth
	}
	ms := float64(time.Since(start).Nanoseconds()) / 1e6
	return fmt.Sprintf("%d %.6f %.3f %d", dir, val, ms, depthLimit)
}

// Record-engine sentinels reported in the <depth_limit> field of a search reply,
// so a run's move log shows WHICH banker path decided each endgame move. The
// client parses this field as opaque int metadata (go_engine.py), so any value
// is contract-safe.
const (
	recordDepthSolver    = 990 // exact-solver move (survival or gated 2048 fire)
	recordDepthEarlyfire = 991 // early score-maximiser fire
)

// recordSearch is the endgame banking decision for one serve request. It builds
// a FRESH exactSolver per call (memo + node budget reset, so every move gets the
// full nodeCap — the live game is one continuous game, unlike the arena's many),
// then delegates to recordDecision. Returns ok == false to defer to the net.
//
// A fresh solver is cheap relative to the solve itself and keeps the decision a
// pure function of (board, config): no wall-clock, no cross-move state, fully
// deterministic and reproducible.
func (sv *server) recordSearch(board uint64) (dir int, val float64, depthLimit int, ok bool) {
	exact := sv.record.exact
	exSolver := newExactSolver(exact, sv.exactScore)
	d, v, fired, engaged := recordDecision(board, exact, exSolver, sv.maxCash, sv.exactScore)
	if !engaged {
		return -1, 0, 0, false
	}
	dl := recordDepthSolver
	// An early-fire move is a maximiser move on a non-2048 swipe that the solver
	// would not itself have fired; recordDecision does not distinguish the two in
	// its return, so re-derive the marker: a fire on a 2048 swipe is a solver/
	// forced fire (Solver), any other maximiser-driven move is Earlyfire.
	if !fired && sv.maxCash != nil && exact.earlyFire > 0 {
		pot := cashValue(board, sv.exactScore)
		if pot >= exact.target+exact.earlyFire && countEmpty(board) >= exact.earlyEmpties {
			dl = recordDepthEarlyfire
		}
	}
	return d, v, dl, true
}

// handleFeatures answers the record-engine TELEMETRY query (see featuresReply in
// record.go): `features <16hex>` -> a line of endgame features + the achievable
// potential + the board id, which the runner records into the SQLite pipeline so
// every endgame decision is auditable postfactum. v1 makes no abandon decision.
// Pure board arithmetic + one score search; works whether or not the record
// engine master flag is enabled.
func (sv *server) handleFeatures(fields []string) string {
	if len(fields) < 2 {
		return "err usage: features <16hex>"
	}
	board, err := parseBoardHex(fields[1])
	if err != nil {
		return "err " + err.Error()
	}
	// Reuse the record engine's score searcher when present; else lazily build a
	// dedicated one (the score tables are initialised in cmdServe). Allocating it
	// does not change any search output, so the golden path stays byte-identical.
	score := sv.exactScore
	if score == nil {
		if sv.featScore == nil {
			sv.featScore = newScoreSearcher(sv.record.maxcashDepth, 0.1)
		}
		score = sv.featScore
	}
	return featuresReply(board, score, sv.record)
}

func handleServeMove(fields []string) string {
	if len(fields) != 3 {
		return "err usage: move <16hex> <dir>"
	}
	board, err := parseBoardHex(fields[1])
	if err != nil {
		return "err " + err.Error()
	}
	dir, err := strconv.Atoi(fields[2])
	if err != nil || dir < 0 || dir > 3 {
		return "err direction must be 0 (UP), 1 (RIGHT), 2 (DOWN) or 3 (LEFT)"
	}
	newboard := executeMove(board, dir)
	moved := 0
	score := int64(0)
	if newboard != board {
		moved = 1
		score = int64(moveScore(board, dir))
	}
	return fmt.Sprintf("%s %d %d", formatBoardHex(newboard), score, moved)
}

// handleServePrefetch validates exactly like the production engine, then kicks
// off concurrent speculation over the post-swipe board's spawn states and
// replies immediately with the number of states scheduled. `adaptive` is
// accepted for wire compatibility but ignored (this searcher has no adaptive
// depth).
func (sv *server) handleServePrefetch(fields []string) string {
	if len(fields) != 7 {
		return "err usage: prefetch <16hex> <depth> <p4> <adaptive 0|1> <mode 0|1> <budget_ms>"
	}
	board, err := parseBoardHex(fields[1])
	if err != nil {
		return "err " + err.Error()
	}
	depth, err := strconv.Atoi(fields[2])
	if err != nil || depth < 1 {
		return "err depth must be an integer >= 1"
	}
	p4, err := strconv.ParseFloat(fields[3], 64)
	if err != nil || p4 < 0.0 || p4 > 1.0 {
		return "err p4 must be a float in [0, 1]"
	}
	if fields[4] != "0" && fields[4] != "1" {
		return "err adaptive must be 0 or 1"
	}
	var farm bool
	switch fields[5] {
	case "0":
		farm = false
	case "1":
		farm = true
	default:
		return "err mode must be 0 (classic) or 1 (farm)"
	}
	budgetMs, err := strconv.ParseFloat(fields[6], 64)
	if err != nil || budgetMs < 0.0 || budgetMs > 600000.0 {
		return "err budget_ms must be a float in [0, 600000] (0 = fixed depth)"
	}
	n := sv.startPrefetch(board, depth, p4, farm, budgetMs)
	return fmt.Sprintf("ok %d", n)
}

// handleServeLookup serves a pre-computed decision from the speculation cache,
// in the client's lookup-hit shape `hit <dir> <value> <depth>` (NOT the search
// shape — the client's _try_lookup expects the `hit` prefix), or `miss`. The
// client sends only `lookup <hex>`; any trailing tokens (present in manual
// smokes) are ignored.
func (sv *server) handleServeLookup(fields []string) string {
	if len(fields) < 2 {
		return "err usage: lookup <16hex>"
	}
	board, err := parseBoardHex(fields[1])
	if err != nil {
		return "err " + err.Error()
	}
	if e, ok := sv.lookupPrefetch(board); ok {
		return fmt.Sprintf("hit %d %.6f %d", e.dir, e.value, e.depth)
	}
	return "miss"
}

// enumerateSpawnStates lists every board reachable from board by one random
// spawn: each empty cell x {exp1, exp2}. All exp1 (value-2) spawns come first —
// they carry 1-p4 (~90%) of the probability mass, so under a tight window they
// are the states worth having computed first.
func enumerateSpawnStates(board uint64) []uint64 {
	states := make([]uint64, 0, 32)
	for rank := uint64(1); rank <= 2; rank++ {
		for j := 0; j < 16; j++ {
			if (board>>(4*j))&0xF == 0 {
				states = append(states, board|rank<<(4*uint(j)))
			}
		}
	}
	return states
}

// ensureSpecPool grows the reusable per-worker searcher pool for the given leaf
// to at least n entries. Called on the request goroutine BEFORE the workers
// launch, so appends never race; workers then only read their own slot.
func (sv *server) ensureSpecPool(farm bool, n, depth int, p4 float64) {
	if farm {
		for len(sv.specFarm) < n {
			sv.specFarm = append(sv.specFarm, newFarmSearcher(depth, p4))
		}
	} else {
		for len(sv.specNtuple) < n {
			sv.specNtuple = append(sv.specNtuple, newSearcher(sv.ntupleLeaf, depth, p4))
		}
	}
}

func (sv *server) specSearcher(farm bool, w int) *searcher {
	if farm {
		return sv.specFarm[w]
	}
	return sv.specNtuple[w]
}

// startPrefetch aborts any previous speculation, replaces the cache, and fans
// the spawn states of board out over a bounded pool of goroutines (each with
// its own single-threaded searcher, so the leaf searches truly overlap). It
// returns the number of states scheduled and does NOT wait for them — the
// searches fill the cache in the background, to be collected by later lookups.
func (sv *server) startPrefetch(board uint64, depth int, p4 float64, farm bool, budgetMs float64) int {
	sv.stopPrefetch() // never two worker sets at once
	states := enumerateSpawnStates(board)
	sv.prefetchMu.Lock()
	sv.prefetchCache = make(map[uint64]prefetchEntry, len(states))
	sv.prefetchMu.Unlock()
	if len(states) == 0 {
		return 0
	}
	nWorkers := runtime.GOMAXPROCS(0)
	if nWorkers > len(states) {
		nWorkers = len(states)
	}
	if nWorkers < 1 {
		nWorkers = 1
	}
	sv.ensureSpecPool(farm, nWorkers, depth, p4)
	sv.prefetchAbort.Store(false)
	var next int32 = -1
	for w := 0; w < nWorkers; w++ {
		sv.prefetchWG.Add(1)
		go func(w int) {
			defer sv.prefetchWG.Done()
			s := sv.specSearcher(farm, w)
			for {
				if sv.prefetchAbort.Load() {
					return
				}
				i := int(atomic.AddInt32(&next, 1))
				if i >= len(states) {
					return
				}
				b := states[i]
				s.depthLimit = int32(depth)
				s.p4 = p4
				s.cprobThresh = cprobThresh
				var dir, dl int
				var val float64
				if budgetMs > 0 {
					// Speculation already fans out over spawn states across this
					// worker pool, so each per-state budget search stays SERIAL
					// (workers=1) — nesting the frontier pool here would
					// oversubscribe the box. The real request drives the pool.
					dir, val, dl = budgetSearch(s, b, budgetMs, 1)
				} else {
					dir, val = s.bestMove(b)
					dl = depth
				}
				// A real request may have preempted us while this candidate ran;
				// its searcher state is fine (per-worker), but don't publish a
				// result the driver has already decided to abort.
				if sv.prefetchAbort.Load() {
					return
				}
				sv.prefetchMu.Lock()
				sv.prefetchCache[b] = prefetchEntry{dir: dir, value: val, depth: dl}
				sv.prefetchMu.Unlock()
			}
		}(w)
	}
	return len(states)
}

// stopPrefetch signals abort and joins the workers. Because the net-leaf search
// is atomic (no in-search abort hook without touching search.go), a worker only
// checks the flag between candidates, so the join waits at most for the
// currently-running candidates (<= nWorkers of them) to finish — a fixed-depth
// leaf search, not the whole fan-out. A no-op when nothing is running.
func (sv *server) stopPrefetch() {
	sv.prefetchAbort.Store(true)
	sv.prefetchWG.Wait()
}

// lookupPrefetch returns the cached decision for board, if speculation reached
// it. Safe to call concurrently with the background workers.
func (sv *server) lookupPrefetch(board uint64) (prefetchEntry, bool) {
	sv.prefetchMu.Lock()
	defer sv.prefetchMu.Unlock()
	e, ok := sv.prefetchCache[board]
	return e, ok
}

// handleVersion reports what this engine IS and what it actually applied at
// startup, in the same `version protocol=... go=... KEY=VALUE ...` shape
// go_engine.py's version parser splits: the toolchain, the weight file and
// its objective trailer, and the farm weights this module resolved from the
// BOT2048_FARM_* env overrides (the farm leaf is selectable per request via
// mode 1, so its provenance belongs in the record too).
func (sv *server) handleVersion() string {
	num := func(v float64) string { return strconv.FormatFloat(v, 'g', -1, 64) }
	objective := "mean"
	if obj.exp {
		objective = "exp"
	}
	return strings.Join([]string{
		"version",
		"protocol=" + strconv.Itoa(serveProtocolVersion),
		"go=" + runtime.Version(),
		"engine=ntuple",
		// The reply is space-separated KEY=VALUE, so the path must not carry
		// spaces; substitute rather than corrupt the whole line.
		"NTUPLE_WEIGHTS=" + strings.ReplaceAll(sv.weightsPath, " ", "_"),
		"NTUPLE_OBJECTIVE=" + objective,
		"NTUPLE_LAMBDA=" + num(obj.lambda),
		"NTUPLE_BASE=" + num(obj.cBase),
		// A real search (fixed-depth or budget) is single-threaded; only
		// speculative prefetch fans out, up to GOMAXPROCS worker searchers.
		"BOT2048_SEARCH_WORKERS=" + strconv.Itoa(searchWorkers),
		"NTUPLE_PREFETCH_WORKERS=" + strconv.Itoa(runtime.GOMAXPROCS(0)),
		"GOMAXPROCS=" + strconv.Itoa(runtime.GOMAXPROCS(0)),
		"BOT2048_FARM_EMPTY_W=" + num(farmEmptyWeight),
		"BOT2048_FARM_MERGE_W=" + num(farmMergeWeight),
		"BOT2048_FARM_CORNER_W=" + num(farmCornerWeight),
		"BOT2048_FARM_EDGE_W=" + num(farmEdgeWeight),
		"BOT2048_FARM_STAIR_W=" + num(farmStairWeight),
		"BOT2048_FARM_SNAKE_W=" + num(farmSnakeWeight),
		"BOT2048_FARM_SNAKE_RATIO=" + num(farmSnakeRatio),
		"BOT2048_FARM_CORNER_RANK_W=" + num(farmCornerRankWeight),
		"BOT2048_FARM_CONVEX_W=" + num(farmConvexWeight),
		"BOT2048_FARM_LATE_EMPTY_W=" + num(farmLateEmptyWeight),
		"BOT2048_FARM_LATE_GRAD_MULT=" + num(farmLateGradMult),
		"BOT2048_FARM_LATE_CORNER_RANK_W=" + num(farmLateCornerRankWeight),
		"BOT2048_FARM_HOARD_TRIGGER=" + strconv.Itoa(farmHoardTrigger),
		"BOT2048_CPROB_THRESH=" + num(cprobThresh),
		// HEUR5 taboo-mobility (0 = OFF = champion leaf) and the evalMLP leaf
		// kill-switch, so a run's provenance records exactly which leaf played.
		"BOT2048_NTUPLE_MOB_W=" + num(sv.mobW),
		"BOT2048_EVAL_MLP=" + map[bool]string{true: "1", false: "0"}[useEvalMLP],
		"BOT2048_LEAF_CACHE=" + map[bool]string{true: "1", false: "0"}[sv.leafCache != nil],
		// Record engine (0 = OFF = golden endgame). When on, the endgame is
		// decided by the target-conditioned banker; the levers follow.
		"BOT2048_RECORD_ENGINE=" + map[bool]string{true: "1", false: "0"}[sv.record.on],
		"BOT2048_TARGET=" + num(sv.record.exact.target),
		"BOT2048_EXACT_EMPTIES=" + strconv.Itoa(sv.record.exact.empties),
		"BOT2048_EXACT_NODES=" + strconv.Itoa(sv.record.exact.nodeCap),
		"BOT2048_TARGET_PCONF=" + num(sv.record.exact.pconf),
		"BOT2048_EXACT_EARLYFIRE=" + num(sv.record.exact.earlyFire),
		"BOT2048_EXACT_EARLY_EMPTIES=" + strconv.Itoa(sv.record.exact.earlyEmpties),
		"BOT2048_MAXCASH_DEPTH=" + strconv.Itoa(sv.record.maxcashDepth),
		"BOT2048_SALVAGE_KAPPA=" + num(sv.record.salvageKappa), // reserved no-op in v1
	}, " ")
}

func (sv *server) handleLine(line string) string {
	fields := strings.Fields(line)
	if len(fields) == 0 {
		return "err empty request"
	}
	switch fields[0] {
	case "ping":
		return "pong"
	case "version":
		return sv.handleVersion()
	case "search":
		return sv.handleSearch(fields)
	case "move":
		return handleServeMove(fields)
	case "prefetch":
		return sv.handleServePrefetch(fields)
	case "lookup":
		return sv.handleServeLookup(fields)
	case "features":
		return sv.handleFeatures(fields)
	default:
		return "err unknown command " + strconv.Quote(fields[0])
	}
}

// safeHandle turns any panic into an err reply — the server never crashes
// on a bad request.
func (sv *server) safeHandle(line string) (reply string) {
	defer func() {
		if r := recover(); r != nil {
			reply = fmt.Sprintf("err internal panic: %v", r)
		}
	}()
	return sv.handleLine(line)
}

// weightCandidates are the net files tried, in order, when the caller gives no
// explicit -weights. w-l0.003.bin is the historical champion filename (kept
// first so existing local setups keep resolving to the same net);
// champion-wall-snap-0316.bin is the record-engine artifact published with the
// repo — the weights are NOT in git (56 MB), fetch them with
// scripts/fetch_weights.sh.
var weightCandidates = []string{"w-l0.003.bin", "champion-wall-snap-0316.bin"}

// defaultWeightsPath resolves the weight file used when the caller gives no
// explicit -weights (the no-arg live-runner launch, which spawns the binary as
// just `ntuple.exe`): the BOT2048_NTUPLE_WEIGHTS env override if set, else the
// first candidate that actually exists under run/, looked up next to the
// EXECUTABLE (os.Executable) first — the runner's cwd is the project dir, not
// tools/ntuple — then cwd-relative.
//
// Probing for existence rather than hardcoding one name is deliberate: the
// previous version always returned w-l0.003.bin, so a fresh clone that only had
// the wall snapshot died with exit 1 on an arg-less launch.
func defaultWeightsPath() string {
	if p := os.Getenv("BOT2048_NTUPLE_WEIGHTS"); p != "" {
		return p
	}
	roots := make([]string, 0, 2)
	if exe, err := os.Executable(); err == nil {
		roots = append(roots, filepath.Join(filepath.Dir(exe), "run"))
	}
	roots = append(roots, "run")
	for _, root := range roots {
		for _, name := range weightCandidates {
			p := filepath.Join(root, name)
			if st, err := os.Stat(p); err == nil && !st.IsDir() {
				return p
			}
		}
	}
	// Nothing on disk: return the canonical path so the loader's error names a
	// concrete file the operator can go fetch.
	return filepath.Join(roots[0], weightCandidates[0])
}

func cmdServe(args []string) {
	fs := flag.NewFlagSet("serve", flag.ExitOnError)
	wf := fs.String("weights", defaultWeightsPath(), "weight file for the learned leaf (mode 0)")
	fs.Parse(args)

	initTables()
	initFarmTables()
	initPatterns()

	// Same knob the production engine exposes; validated to a sane open range.
	if s := os.Getenv("BOT2048_CPROB_THRESH"); s != "" {
		if v, err := strconv.ParseFloat(s, 64); err == nil && v > 0 && v < 1 {
			cprobThresh = v
		}
	}

	n, err := loadNet(*wf)
	if err != nil {
		fmt.Fprintf(os.Stderr, "cannot load n-tuple weights: %v\n", err)
		fmt.Fprintf(os.Stderr, "the 56 MB net is not in git; fetch it with scripts/fetch_weights.sh, "+
			"or pass -weights <file> / set BOT2048_NTUPLE_WEIGHTS\n")
		os.Exit(1)
	}
	sv := &server{n: n, weightsPath: *wf}
	// HEUR5 taboo-mobility leaf (mobility.go). Default 0 = OFF = the exact
	// champion leaf and code path, so a default launch is bit-identical to the
	// pre-HEUR5 engine. Installed once here and shared by every searcher.
	sv.ntupleLeaf = n.leafW
	if w := farmWeightFromEnv("BOT2048_NTUPLE_MOB_W", 0); w > 0 {
		sv.mobW = w
		sv.ntupleLeaf = newMobLeaf(n, w)
		fmt.Fprintf(os.Stderr, "# HEUR5 taboo-mobility ACTIVE (serve): W=%.1f moves/hazard\n", w)
	}
	// Leaf-value cache (perf #1): memoize the leaf at the search cutoff. Pure
	// function of the board given the fixed weights, so it never invalidates and
	// survives rungs AND moves. Default ON (bit-exact: stores full float64);
	// BOT2048_LEAF_CACHE=0 is the kill switch. Wrapping sv.ntupleLeaf means every
	// searcher — persistent, speculative, and each parallel-rung worker via
	// s.leaf — shares the one cache.
	if os.Getenv("BOT2048_LEAF_CACHE") != "0" {
		sv.ntupleLeaf, sv.leafCache = newCachedLeaf(sv.ntupleLeaf)
	}

	// Record engine (record.go). OFF unless BOT2048_RECORD_ENGINE=1, in which case
	// the endgame is routed to the target-conditioned banker. When off, no searcher
	// is allocated and handleSearch never enters the record branch, so the default
	// launch is byte-for-byte the golden engine.
	sv.record = newRecordConfig()
	if sv.record.on {
		sv.maxCash = newScoreSearcher(sv.record.maxcashDepth, 0.1)
		sv.exactScore = newScoreSearcher(sv.record.exact.scoreDepth, 0.1)
		fmt.Fprintf(os.Stderr, "# RECORD ENGINE ACTIVE (serve): %s\n", sv.record.summary())
	}

	in := bufio.NewScanner(os.Stdin)
	in.Buffer(make([]byte, 0, 64*1024), 64*1024)
	out := bufio.NewWriter(os.Stdout)
	for in.Scan() {
		line := in.Text()
		// A real request preempts speculation: abort + join the background
		// workers before handling anything that searches or mutates. `ping` and
		// `lookup` are passive reads; `prefetch` does its own stop-then-restart
		// inside startPrefetch — so only those three skip the abort.
		if f := strings.Fields(line); len(f) > 0 {
			switch f[0] {
			case "ping", "lookup", "prefetch":
			default:
				sv.stopPrefetch()
			}
		}
		fmt.Fprintln(out, sv.safeHandle(line))
		out.Flush()
	}
	sv.stopPrefetch()
	os.Exit(0)
}
