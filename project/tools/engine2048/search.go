package main

import (
	"math"
	"os"
	"strconv"
	"sync"
	"sync/atomic"
	"time"
)

// cprobThreshBase: chance branches below this cumulative probability are cut
// and replaced by a static evaluation (nneonneo's CPROB_THRESH_BASE).
//
// It is the REAL depth knob, not `depth`: at 1e-4 an open mid-game position
// (8 empty cells) has a mean leaf depth of 4.60 and NO leaf at depth 6 — the
// nominal depth-6 search is paid for but never reached. Lowering it (1e-5,
// 1e-6) buys the missing plies at a super-linear cost; raising it makes the
// search shallower and faster. Overridable via BOT2048_CPROB_THRESH so an
// experiment config can pin it (default unchanged: 1e-4).
var cprobThreshBase = 0.0001

// initSearchParams reads the search-shape overrides once, at process startup.
// Called from initTables (board.go) alongside initFarmTables, so the value is
// pinned before any search runs and can be reported by `version`.
//
// Only a value in (0, 1] is accepted: 0 would disable the cutoff entirely and
// a negative/garbage value would silently turn every chance node into a leaf.
func initSearchParams() {
	if s, ok := os.LookupEnv("BOT2048_CPROB_THRESH"); ok && s != "" {
		if v, err := strconv.ParseFloat(s, 64); err == nil && v > 0 && v <= 1 {
			cprobThreshBase = v
		}
	}
}

const (
	// cacheDepthLimit: transposition entries are only read/written for
	// nodes shallower than this (nneonneo's CACHE_DEPTH_LIMIT).
	cacheDepthLimit = 15

	// Transposition table geometry. One fixed-size open-addressing table
	// per root direction, allocated once per process and recycled between
	// searches by bumping an epoch counter (no clearing, no allocation in
	// the hot path — the map-based TT used to dominate the search cost).
	ttLogSize = 18
	ttSize    = 1 << ttLogSize
	ttMask    = ttSize - 1
	// ttProbe: linear-probe window. 2 slots is enough at our fill rates.
	ttProbe = 2

	// abortCheckMask: budget searches poll the deadline / shared abort
	// flag every (mask+1) chance nodes — cheap, and bounds the overshoot
	// past the deadline to well under a millisecond.
	abortCheckMask = 511

	// Budget-mode iterative deepening bounds.
	budgetInsuranceDepth = 2 // always-completed cheap pass (microseconds)
	// budgetDefaultRatio: assumed rung-to-rung time growth before we have a
	// measured ratio; measured ratios are clamped to [budgetMinRatio,
	// budgetMaxRatio]. A mispredicted "go" is bounded by the hard abort.
	budgetDefaultRatio = 5.0
	budgetMinRatio     = 2.0
	budgetMaxRatio     = 10.0
)

// nodeBudget is the per-request node cap (protocol 2 `nodes=N`): a hardware-
// independent abort source shared by every goroutine of one search. Each
// goroutine charges it at its abort poll (every abortCheckMask+1 interior
// chance nodes), so the accounting is at poll granularity, and in the serial
// search whether a given rung trips the cap is a pure function of the position
// — which is what makes the both-limits-None "unbounded" mode reproducible.
type nodeBudget struct {
	used atomic.Uint64
	cap  uint64
}

// spend charges n nodes and reports whether the cap is now exceeded.
func (nb *nodeBudget) spend(n uint64) bool { return nb.used.Add(n) > nb.cap }

// exceeded reports whether the cap has been crossed (by any goroutine).
func (nb *nodeBudget) exceeded() bool { return nb.used.Load() > nb.cap }

// ttEntry is one transposition-table slot. `remaining` is the search depth
// still ahead of the node when the value was computed (depthLimit -
// curdepth); a stored value is valid for any lookup that needs at most
// that much depth. This is exactly the old map condition
// `e.depth <= curdepth` rewritten to survive depth changes between
// iterative-deepening passes.
type ttEntry struct {
	key       uint64
	heur      float64
	epoch     uint32
	remaining int32
}

// ttTable is a fixed-size open-addressing transposition table. Epoch
// bumping invalidates all entries in O(1); entries whose epoch does not
// match the table's are free slots.
type ttTable struct {
	entries []ttEntry
	epoch   uint32
}

func newTTTable() *ttTable {
	return &ttTable{entries: make([]ttEntry, ttSize), epoch: 1}
}

func (t *ttTable) nextEpoch() { t.epoch++ }

// ttIndex spreads the bitboard over the table (Fibonacci hashing).
func ttIndex(board uint64) uint64 {
	return (board * 0x9E3779B97F4A7C15) >> (64 - ttLogSize)
}

func (t *ttTable) get(board uint64, needed int32) (float64, bool) {
	i := ttIndex(board)
	for k := uint64(0); k < ttProbe; k++ {
		e := &t.entries[(i+k)&ttMask]
		if e.epoch == t.epoch && e.key == board && e.remaining >= needed {
			return e.heur, true
		}
	}
	return 0, false
}

func (t *ttTable) put(board uint64, remaining int32, heur float64) {
	i := ttIndex(board)
	victim := &t.entries[i&ttMask]
	for k := uint64(0); k < ttProbe; k++ {
		e := &t.entries[(i+k)&ttMask]
		if e.epoch != t.epoch || e.key == board {
			victim = e
			break
		}
		if e.remaining < victim.remaining {
			victim = e
		}
	}
	if victim.epoch == t.epoch && victim.key == board && victim.remaining >= remaining {
		return // keep the deeper (more informed) value
	}
	*victim = ttEntry{key: board, heur: heur, epoch: t.epoch, remaining: remaining}
}

// searchTables is all the state a chain of searches carries between calls: one
// persistent TT per root direction (so the 4 root goroutines of one search
// share nothing mutable, and results at a fixed depth stay deterministic),
// plus the iterative-deepening depth hint. It is a PARAMETER of every search,
// never package state read from the hot path: two search chains that may run
// concurrently — the driver's real searches and the speculative prefetch
// worker — each own one, so they share nothing mutable by construction rather
// than by the driver's call order.
//
// Tables are allocated lazily (6 MB each), so a process that never speculates
// never pays for the speculative set.
type searchTables struct {
	tables [4]*ttTable
	// stt is the lock-free table the parallel (live) driver's whole pool shares
	// (see parallel.go). Allocated lazily on the driver goroutine; a process that
	// never runs a live search never pays for it.
	stt *sharedTT
	// lastAchievedRung is the index into budgetLadder that the previous budget
	// search completed on these tables — the starting hint for the next one
	// (positions evolve slowly, so the affordable rung rarely moves by more than
	// one per move).
	lastAchievedRung int
	// ladderFirst is the direct-first mitigation of the capped search
	// (searchLimitedExt): when the previous move's direct fixed-depth attempt
	// failed or ate more than 70 % of the budget, the next move climbs the capped
	// ladder first and only tries the direct search with what is left. Positions
	// evolve slowly, so last move's affordability predicts this move's.
	ladderFirst bool
	// lastIterNodes is the interior-chance-node count of the most recent
	// iteration on these tables (sum over its goroutines). Written by the driver
	// goroutine after the join; the request handlers report it as `nodes=`.
	lastIterNodes uint64

	// Load-balance telemetry of the last parallel iteration (see the end of
	// runRootIterationPar). Written by the driver goroutine after its join, read
	// by the measurements; production ignores them.
	lastTaskCount    int
	lastTaskNodesSum uint64
	lastTaskNodesMax uint64
}

func (s *searchTables) table(dir int) *ttTable {
	if s.tables[dir] == nil {
		s.tables[dir] = newTTTable()
	}
	return s.tables[dir]
}

// realTables backs every non-speculative search. The stdio server handles one
// request at a time on the driver goroutine, so plain package-level state is
// safe here; lazy init happens on that goroutine before any worker is spawned.
var realTables = &searchTables{}

// Leaf-evaluator kinds: the hot leaf call is dispatched with a switch on a
// small int instead of an interface call, so the two built-in evaluators
// stay statically dispatched (and inlinable).
const (
	evalKindTable = iota
	evalKindFarm
	// evalKindFarmFused is the farm leaf with every static term folded into two
	// tables (see farm.go). It is the farm default; the per-cell evalKindFarm is
	// the fallback for the stage-switching hoard mode, which makes the leaf
	// weights board-dependent — something static fused tables cannot express.
	evalKindFarmFused
	evalKindCustom
)

// evalKindOf picks the leaf dispatch ONCE per search, on the calling goroutine,
// so the workers never read farmFusedReady (or any other package state) on the
// hot path.
func evalKindOf(eval Evaluator, farm bool) int {
	if farm {
		if farmFusedReady {
			return evalKindFarmFused
		}
		return evalKindFarm
	}
	if _, ok := eval.(tableEvaluator); ok {
		return evalKindTable
	}
	return evalKindCustom
}

// searchState is the per-root-move search context. Each of the 4 root
// moves gets its OWN state (own transposition table).
type searchState struct {
	tt *ttTable
	// stt, when non-nil, replaces tt: the lock-free table the parallel (live)
	// driver shares across its whole pool. Only the parallel driver sets it; the
	// fixed-depth simulation path always runs on the exact float64 `tt`.
	stt        *sharedTT
	depthLimit int32
	// cprobThresh is the probability horizon: a chance branch whose cumulative
	// probability has fallen below it is cut and replaced by a static eval. It is
	// a PARAMETER, not the old constant, because it — not depthLimit — is what
	// actually stops the tree on most boards, and the live search widens it.
	cprobThresh float64
	p4          float64
	eval        Evaluator
	evalKind    int
	// farm switches the max node to the monobank stop-rule objective:
	// a move that creates a 2048 (rank 11) is a terminal cash-out valued at
	// the S-sum of the board BEFORE the move, and a jam cashes out likewise.
	farm bool

	// Budget-abort machinery (hasDeadline=false and ext=nil => fixed-depth
	// search, no polling at all). `abort` is shared by the 4 goroutines of one
	// iteration so they all stop together; `stopped` makes the unwind
	// after an abort O(1) per frame. Values returned after an abort are
	// garbage by design — the whole iteration is discarded (runRootIteration
	// reports completed=false and no caller may use the values).
	hasDeadline bool
	deadline    time.Time
	abort       *atomic.Bool
	// ext, when non-nil, is an additional abort source polled alongside the
	// deadline — the hook that lets the stdio driver cut short speculative
	// (prefetch) searches the moment a real request arrives. It is supplied by
	// the CALLER (runRootIteration's ext argument), so only a search that was
	// started as speculative can ever be aborted by speculation's flag.
	ext *atomic.Bool
	// nodeCap, when non-nil, is the request's node budget (protocol 2 `nodes=`),
	// charged at the same poll as the deadline; crossing it aborts the iteration
	// exactly like the deadline does.
	nodeCap *nodeBudget
	stopped bool
	nodes   uint64
}

// searchNodesCounted is the total number of interior chance nodes visited by
// every search since the process started. It is written once per goroutine at
// the END of its work (never in the hot loop), so it costs nothing measurable
// and stays race-free. Used by the benchmarks to compare the WORK two search
// shapes do — the only load-independent metric on a busy machine.
var searchNodesCounted atomic.Uint64

func (s *searchState) leafEval(board uint64) float64 {
	switch s.evalKind {
	case evalKindTable:
		return tableEvaluator{}.Eval(board)
	case evalKindFarmFused:
		return fusedFarmEvaluator{}.Eval(board)
	case evalKindFarm:
		return farmEvaluator{}.Eval(board)
	default:
		return s.eval.Eval(board)
	}
}

// scoreTilechooseNode evaluates a chance node: the expected value over all
// (empty cell, spawn rank) outcomes on a post-move board.
func (s *searchState) scoreTilechooseNode(board uint64, cprob float64, curdepth int32) float64 {
	if cprob < s.cprobThresh || curdepth >= s.depthLimit {
		return s.leafEval(board)
	}
	// One increment per interior chance node. It doubles as the abort-poll clock
	// and as the work counter the parallel driver reports (nodes, not wall time,
	// is the load-independent way to compare the work two search shapes do).
	s.nodes++
	if s.hasDeadline || s.ext != nil || s.nodeCap != nil {
		if s.stopped {
			return 0
		}
		if s.nodes&abortCheckMask == 0 {
			if s.abort.Load() ||
				(s.hasDeadline && !time.Now().Before(s.deadline)) ||
				(s.ext != nil && s.ext.Load()) ||
				(s.nodeCap != nil && s.nodeCap.spend(abortCheckMask+1)) {
				s.abort.Store(true)
				s.stopped = true
				return 0
			}
		}
	}
	needed := s.depthLimit - curdepth
	useTT := curdepth < cacheDepthLimit
	if useTT {
		if s.stt != nil {
			if v, ok := s.stt.get(board, needed); ok {
				return v
			}
		} else if v, ok := s.tt.get(board, needed); ok {
			return v
		}
	}

	numOpen := countEmpty(board)
	cprob /= float64(numOpen)
	p4 := s.p4
	p2 := 1.0 - p4
	res := 0.0
	tmp := board
	tile2 := uint64(1)
	for tile2 != 0 {
		if tmp&0xF == 0 {
			res += s.scoreMoveNode(board|tile2, cprob*p2, curdepth) * p2
			res += s.scoreMoveNode(board|(tile2<<1), cprob*p4, curdepth) * p4
		}
		tmp >>= 4
		tile2 <<= 4
	}
	res /= float64(numOpen)

	// Never publish a value whose subtree was cut short by an abort.
	if useTT && !s.stopped {
		if s.stt != nil {
			s.stt.put(board, needed, res)
		} else {
			s.tt.put(board, needed, res)
		}
	}
	return res
}

// scoreMoveNode evaluates a max node: the best value over the player's
// moves that change the board (0.0 if none — a lost position).
//
// In farm mode the objective is the monobank stop-rule score: a move that
// creates a 2048 (rank 11) ends the game, cashing out at the S-sum of the
// board BEFORE the move; a jam (no legal move) cashes out the same way. The
// non-negativity of the farm leaf shaping guarantees every non-terminal move
// is worth at least that floor, so a 2048 is only ever chosen when forced.
func (s *searchState) scoreMoveNode(board uint64, cprob float64, curdepth int32) float64 {
	moves := executeAllMoves(board)
	if s.farm {
		// `floor` (the board's S-sum) is only ever READ on two rare paths: the
		// forced-2048 cash-out and the jam. Computing it eagerly cost four table
		// lookups on EVERY move node of the tree — millions per move — to serve a
		// branch that almost never fires, so it is computed lazily.
		//
		// `best` is likewise seeded from the first legal move and tracked with a
		// bool rather than started at -Inf: the -Inf sentinel forced every
		// comparison down the slow denormal/infinity path and cost a math.IsInf
		// call per move node. Together: 1.18x on BenchmarkSearchDepth6Farm, and
		// bit-for-bit identical decisions AND root values (600 depth-6 farm
		// positions, see TestFarmMoveNodeLazyFloorIsExact).
		var floor float64
		haveFloor := false
		best := 0.0
		haveMove := false
		for dir := 0; dir < 4; dir++ {
			newboard := moves[dir]
			if newboard == board {
				continue
			}
			var v float64
			if hasRank11(newboard) {
				if !haveFloor {
					floor, haveFloor = sSumBoard(board), true
				}
				v = floor - farmTerminalEpsilon // forced-2048 cash-out
			} else {
				v = s.scoreTilechooseNode(newboard, cprob, curdepth+1)
			}
			if !haveMove || v > best {
				best = v
				haveMove = true
			}
		}
		if !haveMove {
			return sSumBoard(board) // jam: no legal move
		}
		return best
	}
	best := 0.0
	for dir := 0; dir < 4; dir++ {
		if newboard := moves[dir]; newboard != board {
			if v := s.scoreTilechooseNode(newboard, cprob, curdepth+1); v > best {
				best = v
			}
		}
	}
	return best
}

type searchResult struct {
	bestDir    int // Direction int of the best move, -1 if no legal move
	bestValue  float64
	depthLimit int
	// Protocol-2 telemetry (reported only on requests that carried the k=v
	// tail; the old 4-field wire ignores them). stop is WHY the search ended:
	// "depth" (the fixed-depth search completed — bit-identical to a plain
	// fixed-depth request), "budget" (deadline), "nodes" (node cap),
	// "ladder_end" (no deeper rung to climb). nodes is the interior chance nodes
	// visited over the whole request; cprob the horizon of the returned answer.
	stop  string
	nodes uint64
	cprob float64
}

// stop reasons (see searchResult.stop).
const (
	stopDepth     = "depth"
	stopBudget    = "budget"
	stopNodes     = "nodes"
	stopLadderEnd = "ladder_end"
)

// searchLimits are the protocol-2 search limits of one request (search.go
// `searchLimitedExt`): two INDEPENDENT optional stop conditions plus a guard.
// Zero means "none" everywhere (cprob 0 = the compiled-in cprobThreshBase).
type searchLimits struct {
	maxDepth int     // depth ceiling; 0 = none
	budgetMs float64 // wall-clock ceiling; 0 = none
	maxNodes uint64  // node cap, hardware-independent; 0 = none
	cprob    float64 // base probability horizon; 0 = cprobThreshBase
}

func (l searchLimits) baseCprob() float64 {
	if l.cprob > 0 {
		return l.cprob
	}
	return cprobThreshBase
}

// iterCfg is everything one fixed-(depth, cprob) iteration needs. It is passed
// BY VALUE down the call chain and never read from package state: `ext` is the
// only way a caller can arm the abort poll (so a search started without one
// cannot be cut short by an unrelated goroutine), and `tabs` is the only mutable
// memory the workers touch.
type iterCfg struct {
	depthLimit  int32
	cprobThresh float64
	p4          float64
	eval        Evaluator
	evalKind    int
	farm        bool

	deadline    time.Time
	hasDeadline bool
	ext         *atomic.Bool
	nodeCap     *nodeBudget

	// workers > 1 selects the parallel frontier driver (parallel.go). Only the
	// time-budgeted (live) search ever sets it; the fixed-depth path that
	// simulations use always leaves it at 1.
	workers int
}

// newState builds the per-goroutine search context for cfg.
func (cfg iterCfg) newState(tt *ttTable, stt *sharedTT, abort *atomic.Bool) *searchState {
	return &searchState{
		tt:          tt,
		stt:         stt,
		depthLimit:  cfg.depthLimit,
		cprobThresh: cfg.cprobThresh,
		p4:          cfg.p4,
		eval:        cfg.eval,
		evalKind:    cfg.evalKind,
		farm:        cfg.farm,
		hasDeadline: cfg.hasDeadline,
		deadline:    cfg.deadline,
		abort:       abort,
		ext:         cfg.ext,
		nodeCap:     cfg.nodeCap,
	}
}

// runRootIteration evaluates the 4 root moves in 4 goroutines, one per root
// move, each on its own transposition table. completed=false means an abort
// source fired (the deadline, the caller's ext flag, or another worker) and the
// values must be discarded. With hasDeadline=false and ext==nil there is no
// abort source at all, so it always completes.
//
// This is the shape simulations run: 4 goroutines on a GOMAXPROCS=1 process, so
// really one core, and bit-for-bit deterministic.
func runRootIteration(board uint64, cfg iterCfg, tabs *searchTables) (values [4]float64, legal [4]bool, completed bool) {
	abort := new(atomic.Bool)
	moves := executeAllMoves(board)
	var wg sync.WaitGroup
	var nodes atomic.Uint64
	for dir := 0; dir < 4; dir++ {
		newboard := moves[dir]
		if newboard == board {
			continue
		}
		legal[dir] = true
		if cfg.farm && hasRank11(newboard) {
			// Terminal cash-out: keep it legal (playable when forced) but
			// worth only the pre-move holdings, so any real move wins.
			values[dir] = sSumBoard(board) - farmTerminalEpsilon
			continue
		}
		tt := tabs.table(dir) // init on the calling goroutine, not the worker
		wg.Add(1)
		go func(dir int, nb uint64, tt *ttTable) {
			defer wg.Done()
			state := cfg.newState(tt, nil, abort)
			values[dir] = state.scoreTilechooseNode(nb, 1.0, 0)
			searchNodesCounted.Add(state.nodes)
			nodes.Add(state.nodes)
		}(dir, newboard, tt)
	}
	wg.Wait()
	tabs.lastIterNodes = nodes.Load()
	return values, legal, !abort.Load()
}

// runIteration dispatches one iteration to the serial 4-way root split
// (workers <= 1) or to the parallel frontier driver (workers > 1).
func runIteration(board uint64, cfg iterCfg, tabs *searchTables) (values [4]float64, legal [4]bool, completed bool) {
	if cfg.workers > 1 {
		return runRootIterationPar(board, cfg, tabs)
	}
	return runRootIteration(board, cfg, tabs)
}

// beginSearch bumps the epoch of every table the coming search will touch,
// invalidating the previous search's entries in O(1).
func (s *searchTables) beginSearch(workers int) {
	if workers > 1 {
		s.sharedTable().nextEpoch()
		return
	}
	for dir := 0; dir < 4; dir++ {
		s.table(dir).nextEpoch()
	}
}

func pickBest(values [4]float64, legal [4]bool) (int, float64) {
	bestDir := -1
	bestValue := math.Inf(-1)
	for dir := 0; dir < 4; dir++ {
		if legal[dir] && values[dir] > bestValue {
			bestDir, bestValue = dir, values[dir]
		}
	}
	if bestDir == -1 {
		bestValue = 0.0
	}
	return bestDir, bestValue
}

// search runs the top-level fixed-depth expectimax. Ties break toward the
// lower Direction int. Epochs are bumped per call, so the result depends
// only on (board, parameters) — never on what was searched before.
//
// When farm is true the objective becomes the monobank stop-rule score
// (see farm.go / scoreMoveNode): the leaf evaluator is the farm evaluator,
// and a root move that creates a 2048 (rank 11) is a terminal cash-out valued
// at the pre-move S-sum — selectable, but only ever chosen when it is the
// sole legal move.
func search(board uint64, depth int, p4 float64, adaptive bool, eval Evaluator, farm bool) searchResult {
	res, completed := searchExt(board, depth, p4, adaptive, eval, farm, realTables, nil)
	if !completed {
		// Unreachable: with no deadline and no ext there is no abort source, so
		// the iteration cannot be cut short. Fail loudly rather than return the
		// values of a truncated search — an aborted subtree evaluates to 0, so a
		// silent result here would be an arbitrary move, indistinguishable from a
		// good one. (The stdio server turns this into an `err` reply, and callers
		// can retry; see safeHandle in main.go.)
		panic("engine2048: fixed-depth search aborted with no abort source armed")
	}
	return res
}

// searchExt is search on an explicit table set and with an explicit external
// abort source. ext == nil means no abort source at all (the plain fixed-depth
// search: it always completes). A non-nil ext arms the abort poll, and
// completed=false then means ext fired mid-search — the returned values are
// garbage by design and MUST be discarded. Only the speculative prefetch worker
// passes a non-nil ext (and its own table set).
func searchExt(
	board uint64,
	depth int,
	p4 float64,
	adaptive bool,
	eval Evaluator,
	farm bool,
	tabs *searchTables,
	ext *atomic.Bool,
) (searchResult, bool) {
	return searchFixedExt(board, depth, p4, adaptive, eval, farm, cprobThreshBase, tabs, ext, time.Time{}, false, nil)
}

// searchFixedExt is the one fixed-depth iteration every fixed-depth caller runs,
// with its abort sources explicit: ext (speculation), an optional deadline and
// an optional node cap. With none armed (searchExt) there is no polling at all
// and the iteration always completes; with a deadline or node cap armed,
// completed=false means the limit fired and the values are garbage. Polling
// never changes a value, so a limited search that COMPLETES is bit-identical to
// the unlimited one — the property the capped live search (searchLimitedExt)
// is built on.
func searchFixedExt(
	board uint64,
	depth int,
	p4 float64,
	adaptive bool,
	eval Evaluator,
	farm bool,
	cprob float64,
	tabs *searchTables,
	ext *atomic.Bool,
	deadline time.Time,
	hasDeadline bool,
	nb *nodeBudget,
) (searchResult, bool) {
	depthLimit := depth
	if adaptive {
		if d := countDistinctTiles(board) - 2; d > depthLimit {
			depthLimit = d
		}
	}
	if farm {
		eval = farmEvaluator{}
	}
	evalKind := evalKindOf(eval, farm)
	tabs.beginSearch(1)
	// workers: 1 — the fixed-depth search is the SIMULATION path and stays on the
	// serial root split, whatever BOT2048_SEARCH_WORKERS says. Only searchBudget
	// (live) fans out. cprobThresh: the compiled-in default unless the request
	// pinned one, so a fixed-depth result is a pure function of (board, depth,
	// p4, adaptive, eval, farm, cprob) — which is what makes a seeded simulation
	// reproducible.
	cfg := iterCfg{
		depthLimit:  int32(depthLimit),
		cprobThresh: cprob,
		p4:          p4,
		eval:        eval,
		evalKind:    evalKind,
		farm:        farm,
		deadline:    deadline,
		hasDeadline: hasDeadline,
		ext:         ext,
		nodeCap:     nb,
		workers:     1,
	}
	values, legal, completed := runIteration(board, cfg, tabs)
	if !completed {
		return searchResult{}, false
	}
	if nb != nil && nb.exceeded() {
		// Crossed the cap inside the last poll window of some goroutine: the
		// values are complete, but "completed within the cap" must be a pure
		// function of the position, not of which goroutine polled last.
		return searchResult{}, false
	}
	bestDir, bestValue := pickBest(values, legal)
	return searchResult{
		bestDir: bestDir, bestValue: bestValue, depthLimit: depthLimit,
		stop: stopDepth, nodes: tabs.lastIterNodes, cprob: cprob,
	}, true
}

// ---------------------------------------------------------------------------
// The live ladder.
//
// Iterative deepening on DEPTH ALONE saturates, and measurement says so
// bluntly: on the benchmark boards a depth-6 search visits 4.17M nodes and a
// depth-10 search visits 4.20M — a 0.8% difference. Past ~depth 7 it is not the
// depth limit that stops the tree, it is the cprob threshold: every chance node
// divides the branch probability by the number of empty cells, so after 4-5
// chance levels the branch has fallen under 1e-4 and is cut no matter how much
// depth is left. Handing the live search a bigger `depth` buys nothing.
//
// The lever that DOES buy more search is the probability horizon — and that is
// the lever behind the best game of the session (41 164 points at cprob 1e-5,
// 15.6x the cost, unaffordable in simulation and perfectly affordable in a
// 400 ms live budget).
//
// So the ladder has two axes. It deepens first (cheap, and depth still matters
// on the crowded boards where cprob does NOT cut early — those are exactly the
// dangerous positions), then widens the horizon by ~half a decade per rung. Each
// rung is strictly stronger than the one before, so stopping at any rung leaves
// a usable, complete answer.
type ladderRung struct {
	depth int
	cprob float64
}

var budgetLadder = []ladderRung{
	{4, 1e-4}, {5, 1e-4}, {6, 1e-4}, {7, 1e-4}, {8, 1e-4},
	{9, 3e-5}, {10, 1e-5}, {11, 3e-6}, {12, 1e-6},
	{14, 3e-7}, {16, 1e-7}, {18, 3e-8},
}

// searchBudget runs time-budgeted iterative deepening: complete a cheap
// insurance pass, then deepen while the next iteration is predicted to fit
// in the remaining budget. An in-flight iteration that overruns the budget
// is hard-aborted and discarded, so the wall time stays within budgetMs
// plus the (sub-millisecond) abort latency. Returns the deepest COMPLETED
// level; reported depthLimit is that achieved depth.
//
// The TT epoch is bumped once per call (not per iteration), so deeper
// iterations reuse exact values computed by shallower ones where valid.
// `depth` and `adaptive` from the request are intentionally ignored: the
// budget decides how deep we go.
func searchBudget(board uint64, p4 float64, eval Evaluator, farm bool, budgetMs float64) searchResult {
	res, _ := searchBudgetExt(board, p4, eval, farm, budgetMs, realTables, nil)
	return res
}

// searchBudgetExt is searchBudget with an explicit external abort source (see
// searchExt). completed=false means ext fired during the insurance pass — the
// one iteration whose result the budget search cannot discard — so there is no
// usable answer and the caller must drop it. A deadline overrun inside the
// deepening loop is NOT a failure: the last completed iteration is returned, as
// designed. Only the speculative prefetch worker passes a non-nil ext.
func searchBudgetExt(
	board uint64,
	p4 float64,
	eval Evaluator,
	farm bool,
	budgetMs float64,
	tabs *searchTables,
	ext *atomic.Bool,
) (searchResult, bool) {
	start := time.Now()
	budget := time.Duration(budgetMs * float64(time.Millisecond))
	res, _, ok := ladderExt(board, p4, eval, farm, ladderLimits{
		start: start, budget: budget, deadline: start.Add(budget), hasDeadline: true,
		cprob: cprobThreshBase,
	}, tabs, ext)
	return res, ok
}

// ladderLimits is what bounds one climb of the ladder (ladderExt). With
// hasDeadline=false and nb=nil the climb runs to the top of the (capped)
// ladder — the "unbounded" mode, which only a node cap makes safe.
type ladderLimits struct {
	start       time.Time
	budget      time.Duration
	deadline    time.Time
	hasDeadline bool
	maxDepth    int         // rungs deeper than this are skipped; 0 = none
	cprob       float64     // base horizon: a rung is never COARSER than this
	nb          *nodeBudget // node cap shared with the rest of the request; nil = none
}

// ladderStats is the per-climb telemetry the capped search's direct-first
// mitigation reads back (see searchLimitedExt).
type ladderStats struct {
	topRungTime time.Duration // wall time of the deepest COMPLETED rung (0 = insurance only)
}

// ladderExt is the generalised ladder behind searchBudgetExt: the same
// insurance-then-climb with the same predictor and the same gamble, plus the
// protocol-2 limits — a depth ceiling (rungs above it are never run), an
// OPTIONAL deadline, a node cap shared with the caller, and a base horizon the
// rungs may widen but never coarsen. With the ladderLimits searchBudgetExt
// passes (deadline only, base = cprobThreshBase) every branch below is exactly
// the pre-protocol-2 budget search. The returned stop says why the climb ended.
func ladderExt(
	board uint64,
	p4 float64,
	eval Evaluator,
	farm bool,
	lim ladderLimits,
	tabs *searchTables,
	ext *atomic.Bool,
) (searchResult, ladderStats, bool) {
	start, budget, deadline := lim.start, lim.budget, lim.deadline
	nb := lim.nb
	if farm {
		eval = farmEvaluator{}
	}
	evalKind := evalKindOf(eval, farm)
	// The live search is the ONLY caller that fans out over the whole machine
	// (searchWorkers, default GOMAXPROCS). Simulations never get here — they run
	// the fixed-depth `search`, whose serial root split is untouched.
	workers := searchWorkers
	tabs.beginSearch(workers)

	base := iterCfg{
		p4:       p4,
		eval:     eval,
		evalKind: evalKind,
		farm:     farm,
		ext:      ext,
		workers:  workers,
	}
	var totalNodes uint64
	runRung := func(r ladderRung, dl time.Time, hasDL bool, cap *nodeBudget) ([4]float64, [4]bool, bool) {
		cfg := base
		cfg.depthLimit = int32(r.depth)
		cfg.cprobThresh = r.cprob
		cfg.deadline = dl
		cfg.hasDeadline = hasDL
		cfg.nodeCap = cap
		v, l, c := runIteration(board, cfg, tabs)
		totalNodes += tabs.lastIterNodes
		return v, l, c
	}
	// rungCprob: the ladder widens the horizon only past depth 8; a request that
	// pinned a finer base keeps it on every rung (never coarser than the base).
	rungCprob := func(r ladderRung) float64 {
		if lim.cprob < r.cprob {
			return lim.cprob
		}
		return r.cprob
	}

	// Insurance pass: microseconds, no deadline and no node cap, so it always
	// completes unless ext fires — and there is no shallower result to fall back
	// on, so that case is reported as a failed search rather than papered over.
	values, legal, insured := runRung(ladderRung{budgetInsuranceDepth, lim.cprob}, time.Time{}, false, nil)
	if !insured {
		return searchResult{}, ladderStats{}, false
	}
	achieved := budgetInsuranceDepth
	achievedRung := -1
	achievedCprob := lim.cprob
	stop := stopLadderEnd
	var stats ladderStats

	// Start one rung below the previous move's achieved rung: positions evolve
	// slowly, so the affordable rung rarely moves by more than one, and walking
	// up from the bottom every move wastes budget re-searching what we already
	// know we can afford.
	i := 0
	if h := tabs.lastAchievedRung - 1; h > 0 {
		i = h
	}
	prevIterTime := time.Duration(0)
	prevRung := -1
	prevCprob := 0.0
	gambling := i > 0 // the starting rung is a GUESS, and it can be wrong
	for ; i < len(budgetLadder); i++ {
		r := budgetLadder[i]
		if lim.maxDepth > 0 && r.depth > lim.maxDepth {
			break // the ladder is capped: nothing deeper is ever run
		}
		r.cprob = rungCprob(r)
		// A transposition entry is keyed by (board, remaining depth), but the value
		// stored there was computed under the cprob of ITS rung. Reusing a coarse
		// value in a finer-horizon pass would silently undo the widening — the
		// whole point of the rung. So a change of horizon invalidates the table.
		if prevCprob != 0.0 && r.cprob != prevCprob {
			tabs.beginSearch(workers)
		}
		// Never gamble the WHOLE budget on the guessed starting rung. If the hint
		// is too deep for this position (a jam is far more expensive than the open
		// board before it), a hard deadline there would spend everything and leave
		// us returning the depth-2 insurance move. Cap the guess at half the
		// budget; the other half is enough to walk up from the bottom.
		dl := deadline
		if gambling && lim.hasDeadline {
			if half := start.Add(budget / 2); half.Before(dl) {
				dl = half
			}
		}
		iterStart := time.Now()
		v, l, completed := runRung(r, dl, lim.hasDeadline, nb)
		iterTime := time.Since(iterStart)
		if completed && nb != nil && nb.exceeded() {
			completed = false // crossed the cap in the last poll window: discard (deterministic)
		}
		if !completed {
			if nb != nil && nb.exceeded() {
				stop = stopNodes
				break // the cap is spent: climbing from the bottom would abort at once
			}
			if gambling {
				// The guess was too rich. Fall back to the bottom of the ladder and
				// climb honestly on the remaining budget.
				gambling = false
				tabs.beginSearch(workers)
				prevCprob, prevIterTime, prevRung = 0.0, 0, -1
				i = -1 // the loop's i++ makes this 0
				continue
			}
			stop = stopBudget
			break // deadline hit mid-rung: keep the last complete answer
		}
		gambling = false
		values, legal = v, l
		achieved, achievedRung, achievedCprob = r.depth, i, r.cprob
		stats.topRungTime = iterTime
		prevCprob = r.cprob

		// Predict the next rung from the growth we just measured (clamped), and
		// only start it if it is predicted to fit. A mispredicted "go" is bounded
		// by the hard abort above, so the cost of being wrong is one wasted rung,
		// never an overrun.
		ratio := budgetDefaultRatio
		if prevRung == i-1 && prevIterTime > 0 {
			ratio = float64(iterTime) / float64(prevIterTime)
			if ratio < budgetMinRatio {
				ratio = budgetMinRatio
			} else if ratio > budgetMaxRatio {
				ratio = budgetMaxRatio
			}
		}
		prevIterTime, prevRung = iterTime, i
		if lim.hasDeadline && time.Since(start)+time.Duration(ratio*float64(iterTime)) > budget {
			stop = stopBudget
			break
		}
	}
	// The hint is only ever what we ACTUALLY completed, so a position that got
	// suddenly expensive resets it instead of gambling again next move.
	tabs.lastAchievedRung = 0
	if achievedRung >= 0 {
		tabs.lastAchievedRung = achievedRung
	}

	bestDir, bestValue := pickBest(values, legal)
	return searchResult{
		bestDir: bestDir, bestValue: bestValue, depthLimit: achieved,
		stop: stop, nodes: totalNodes, cprob: achievedCprob,
	}, stats, true
}

// directFirstBudgetFrac: a direct attempt that ate more than this share of the
// budget (or failed outright) switches the NEXT move to ladder-first.
const directFirstBudgetFrac = 0.7

// searchLimitedExt is the protocol-2 search: two independent, individually
// optional limits (maxDepth D, budgetMs B) — the search stops at whichever hits
// first — plus the maxNodes guard. Semantics by row:
//
//	D, none:    today's fixed-depth path (bit-identical to `search`), the node cap
//	            as the only abort; stop=depth|nodes.
//	none, B:    today's budget ladder; stop=budget|ladder_end|nodes.
//	D, B:       DIRECT-FIRST: the fixed-depth-D search under deadline B. Completed
//	            -> returned as is, stop=depth, bit-identical to the (D, none) row
//	            on any machine. Aborted -> discarded; the ladder climbs the rungs
//	            with depth <= D in the remaining time (insurance first);
//	            stop=budget|nodes|ladder_end.
//	none, none: "unbounded" — the whole ladder, the node cap the only stop;
//	            stop=ladder_end|nodes. Reproducible in serial (a node cap is
//	            hardware-independent).
//
// Why direct-first rather than "climb and stop at D": a capped climb reuses TT
// values across rungs and is NOT bit-identical to a fresh fixed-depth-D search,
// so the property that makes a depth cap worth having — capped and completed
// means the same move as fixed depth D on every machine — would not hold.
// Cost: on a move where D is unaffordable the failed attempt burns budget before
// the fallback. Mitigation (tabs.ladderFirst): when the previous move's direct
// attempt failed or exceeded directFirstBudgetFrac of B, this move runs the
// capped ladder FIRST and tries the direct search only with what is left (and
// only when the ladder's own top rung says D is affordable now).
//
// A depth-capped ladder that completes rung D reports stop=ladder_end, never
// stop=depth: stop=depth is reserved for the bit-identical fixed-depth answer.
// `adaptive` is honoured by the direct attempt exactly as by `search` (so the
// (D, B) and (D, none) rows agree) and ignored by the ladder.
func searchLimitedExt(
	board uint64,
	p4 float64,
	adaptive bool,
	eval Evaluator,
	farm bool,
	lim searchLimits,
	tabs *searchTables,
	ext *atomic.Bool,
) (searchResult, bool) {
	start := time.Now()
	base := lim.baseCprob()
	var nb *nodeBudget
	if lim.maxNodes > 0 {
		nb = &nodeBudget{cap: lim.maxNodes}
	}
	hasDL := lim.budgetMs > 0
	budget := time.Duration(lim.budgetMs * float64(time.Millisecond))
	deadline := start.Add(budget)
	ll := ladderLimits{
		start: start, budget: budget, deadline: deadline, hasDeadline: hasDL,
		maxDepth: lim.maxDepth, cprob: base, nb: nb,
	}
	if lim.maxDepth <= 0 {
		res, _, ok := ladderExt(board, p4, eval, farm, ll, tabs, ext)
		return res, ok
	}
	if !hasDL && nb == nil {
		// No clock, no cap: the plain fixed-depth search, byte for byte.
		return searchFixedExt(board, lim.maxDepth, p4, adaptive, eval, farm, base, tabs, ext, time.Time{}, false, nil)
	}
	// direct runs the fixed-depth-D attempt under the request's limits and
	// records its affordability for the next move. extFired distinguishes a
	// speculative abort (the whole request must be dropped) from a limit abort.
	direct := func() (res searchResult, ok bool, extFired bool) {
		t0 := time.Now()
		res, ok = searchFixedExt(board, lim.maxDepth, p4, adaptive, eval, farm, base, tabs, ext, deadline, hasDL, nb)
		if ext != nil && ext.Load() {
			return searchResult{}, false, true
		}
		if !ok {
			res.nodes = tabs.lastIterNodes // the failed attempt's work still counts
		}
		tabs.ladderFirst = !ok || (hasDL && time.Since(t0) > time.Duration(float64(budget)*directFirstBudgetFrac))
		return res, ok, false
	}

	var spent uint64
	if !tabs.ladderFirst {
		res, ok, extFired := direct()
		if extFired {
			return searchResult{}, false
		}
		if ok {
			return res, true
		}
		spent = res.nodes
	}
	res, stats, ok := ladderExt(board, p4, eval, farm, ll, tabs, ext)
	if !ok {
		return searchResult{}, false
	}
	res.nodes += spent
	if spent == 0 {
		// Ladder-first move. Retry the direct search only if the ladder reached
		// rung D and, under a clock, more than twice that rung's time is left: a
		// fresh fixed-depth search gets no TT reuse, so the rung time is a floor.
		reachedCap := res.depthLimit >= lim.maxDepth
		switch {
		case hasDL && reachedCap && stats.topRungTime > 0 && time.Until(deadline) > 2*stats.topRungTime:
			ladderRes := res
			res, ok, extFired := direct()
			if extFired {
				return searchResult{}, false
			}
			if ok {
				res.nodes += ladderRes.nodes
				return res, true
			}
			ladderRes.nodes += res.nodes
			return ladderRes, true
		case !hasDL && reachedCap:
			tabs.ladderFirst = false // the cap fit under the node budget: try direct next move
		}
	}
	return res, true
}

// ---------------------------------------------------------------------------
// Speculative prefetch (P19a).
//
// After a swipe the merge result is deterministic; only the random spawn
// (empty cell x {2, 4}) is unknown. While the host is busy animating /
// perceiving (~200 ms), the driver can call startPrefetch on the post-swipe
// board: a single background goroutine computes, for every possible spawn
// state, exactly the decision a direct request would produce, and stores it
// in prefetchCache. Once the real spawn is observed, lookupPrefetch returns
// the answer with no search at all.
//
// Priority: the stdio driver calls stopPrefetch before any real command, and
// every speculative search polls prefetchAbort — passed as the ext ARGUMENT of
// searchExt/searchBudgetExt, so only the speculative call chain can ever be
// aborted by it — so a real request preempts speculation within one abort-poll
// window (well under a millisecond); speculation never competes with a real
// search for CPU, and a real search can never be truncated by speculation's
// abort flag no matter what order the driver issues commands in.

// prefetchEntry is the fully-computed answer for one speculative spawn state.
// stop/nodes/cprob are the protocol-2 tail (reported by `lookup` only when the
// prefetch request carried one — v2); the old wire never sees them.
type prefetchEntry struct {
	dir   int
	value float64
	depth int
	stop  string
	nodes uint64
	cprob float64
	v2    bool
}

// lookupTail formats the protocol-2 reply tail of a cached entry ("" for an
// entry computed by an old-wire prefetch).
func (e prefetchEntry) lookupTail() string {
	if !e.v2 {
		return ""
	}
	return " " + formatSearchTail(e.stop, e.nodes, e.cprob)
}

// formatSearchTail is the protocol-2 reply tail shared by search and lookup.
func formatSearchTail(stop string, nodes uint64, cprob float64) string {
	return "stop=" + stop + " nodes=" + strconv.FormatUint(nodes, 10) +
		" cprob=" + strconv.FormatFloat(cprob, 'g', -1, 64)
}

var (
	// prefetchMu guards prefetchCache: the worker writes entries while the
	// driver may serve lookups concurrently.
	prefetchMu    sync.Mutex
	prefetchCache map[uint64]prefetchEntry
	prefetchWG    sync.WaitGroup
	prefetchAbort atomic.Bool
	// specTables is the transposition-table set of the speculative worker —
	// separate from realTables, so a speculative search shares NO mutable memory
	// with a real one and the two may safely overlap in time. Only ever touched
	// from a prefetch goroutine, and at most one of those runs at a time
	// (startPrefetch joins the previous worker first, which also supplies the
	// happens-before edge between successive workers). Its 4 tables are allocated
	// on first speculative use, so a process that never prefetches (every
	// simulation) never pays for them.
	specTables = &searchTables{}
)

// enumerateSpawnStates lists every board reachable from board by one random
// spawn: each empty cell x {2, 4}. All 2-spawns come first — they carry 90%
// of the probability mass, so under a tight window they are the states worth
// having computed.
func enumerateSpawnStates(board uint64) []uint64 {
	states := make([]uint64, 0, 32)
	for rank := uint64(1); rank <= 2; rank++ {
		for j := 0; j < 16; j++ {
			if (board>>(4*j))&0xF == 0 {
				states = append(states, board|rank<<(4*j))
			}
		}
	}
	return states
}

// startPrefetch aborts any previous speculation, clears the cache and starts
// one background goroutine that searches every spawn state of board with
// exactly the configuration a direct request would use (budgetMs > 0 selects
// the time-budgeted search, otherwise fixed depth/adaptive). Returns the
// number of states scheduled. The cache is bounded by construction: at most
// 2*16 entries, replaced wholesale on the next call.
func startPrefetch(
	board uint64,
	depth int,
	p4 float64,
	adaptive bool,
	eval Evaluator,
	farm bool,
	budgetMs float64,
) int {
	return startPrefetchLimited(board, depth, p4, adaptive, eval, farm, budgetMs, nil)
}

// startPrefetchLimited is startPrefetch for a protocol-2 request: lim non-nil
// selects searchLimitedExt with those limits (and marks the entries v2, so
// `lookup` reports the stop tail); lim nil is the old wire — budgetMs > 0 the
// budget ladder, else fixed depth/adaptive — unchanged.
func startPrefetchLimited(
	board uint64,
	depth int,
	p4 float64,
	adaptive bool,
	eval Evaluator,
	farm bool,
	budgetMs float64,
	lim *searchLimits,
) int {
	stopPrefetch() // never two workers at once
	states := enumerateSpawnStates(board)
	prefetchMu.Lock()
	prefetchCache = make(map[uint64]prefetchEntry, len(states))
	prefetchMu.Unlock()
	if len(states) == 0 {
		return 0
	}
	prefetchAbort.Store(false)
	prefetchWG.Add(1)
	go func() {
		defer prefetchWG.Done()
		for _, b := range states {
			if prefetchAbort.Load() {
				return
			}
			var res searchResult
			var completed bool
			switch {
			case lim != nil:
				res, completed = searchLimitedExt(
					b, p4, adaptive, eval, farm, *lim, specTables, &prefetchAbort)
			case budgetMs > 0:
				res, completed = searchBudgetExt(
					b, p4, eval, farm, budgetMs, specTables, &prefetchAbort)
			default:
				res, completed = searchExt(
					b, depth, p4, adaptive, eval, farm, specTables, &prefetchAbort)
			}
			if !completed || prefetchAbort.Load() {
				return // aborted mid-search: the value is garbage by design
			}
			prefetchMu.Lock()
			prefetchCache[b] = prefetchEntry{
				dir:   res.bestDir,
				value: res.bestValue,
				depth: res.depthLimit,
				stop:  res.stop,
				nodes: res.nodes,
				cprob: res.cprob,
				v2:    lim != nil,
			}
			prefetchMu.Unlock()
		}
	}()
	return len(states)
}

// stopPrefetch aborts in-flight speculation and joins the worker. Latency is
// bounded by one abort-poll window of the speculative search (sub-ms); a no-op
// when nothing is running.
func stopPrefetch() {
	prefetchAbort.Store(true)
	prefetchWG.Wait()
}

// lookupPrefetch returns the cached decision for board, if speculation got
// to it.
func lookupPrefetch(board uint64) (prefetchEntry, bool) {
	prefetchMu.Lock()
	defer prefetchMu.Unlock()
	e, ok := prefetchCache[board]
	return e, ok
}
