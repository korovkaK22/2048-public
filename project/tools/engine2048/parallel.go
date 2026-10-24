package main

import (
	"math"
	"os"
	"runtime"
	"strconv"
	"sync"
	"sync/atomic"
)

// ---------------------------------------------------------------------------
// Parallel search driver (P20).
//
// The fixed-depth search (`search`, used by every simulation) fans out over the
// 4 root moves and nothing else: 4 goroutines, of which the branches are wildly
// unequal, so it saturates ~2-3 cores at best. That is the right shape for
// simulations, where 13 games run side by side with GOMAXPROCS=1 each and the
// engine must stay bit-for-bit deterministic.
//
// LIVE play is the opposite regime: ONE game, ~500 ms of thinking time per
// move, and 13 idle cores. This file adds the driver for that regime.
//
// Shape: a PARALLEL FRONTIER, not a root split.
//
//	  1. The driver expands the top of the tree itself — chance node -> max
//	     kids -> chance grandkids — until it holds at least `parTaskTarget`
//	     per worker INDEPENDENT chance subtrees (a chance node with e empty
//	     cells has 2*e independent spawn outcomes, so one ply of expansion
//	     multiplies the frontier by up to ~8x*4 dirs; two plies is plenty).
//	  2. Those subtrees go into a work-stealing pool of W goroutines, pulled
//	     off a shared atomic cursor, so a fat subtree cannot leave 12 cores
//	     idle the way the 4-way root split does.
//	  3. Results are folded back up the (tiny) frontier tree with exactly the
//	     arithmetic the serial search does — same order of float additions per
//	     chance node, same max order per max node — so the frontier itself
//	     introduces no numeric drift.
//
// The pool shares ONE transposition table, lock-free (see sharedTT below).
// Per-worker tables were tried first and measured: they cost 2.43x the nodes for
// the same answer, because a subtree evaluated by worker 3 cannot reuse
// worker 7's entries — a hard 13/2.43 = 5.3x ceiling on any speedup. Sharing the
// table brings the work back to 1.04x the serial search's.
//
// Determinism: tasks are pulled dynamically, so WHICH worker evaluates a given
// subtree — and hence the order in which the shared table is populated — varies
// run to run, and a value can differ in its last bits between runs. That is
// acceptable here and ONLY here: the parallel driver is reachable exclusively
// from the time-budgeted (live) search, which is already nondeterministic by
// construction (it returns whatever the clock allowed). The fixed-depth path
// that simulations use never touches this file, and is proven bit-for-bit
// identical to the pre-change engine.

const (
	// parTaskTarget: how many frontier subtrees we want per worker before we
	// stop expanding. >1 so the pool can smooth out the (very) unequal subtree
	// costs; the frontier tree itself is driver-side overhead, so we do not
	// want thousands of them.
	parTaskTarget = 6
	// parTaskCap bounds the frontier so a wide board (15 empties) cannot
	// explode the driver-side expansion.
	parTaskCap = 8192
	// parPullChunk: tasks claimed per atomic bump. Tasks are big (whole
	// subtrees), so 1 is right — the cursor is not a bottleneck.
	parPullChunk = 1
)

// searchWorkers is the width of the live search pool. Default: GOMAXPROCS
// (14 on the live box). BOT2048_SEARCH_WORKERS overrides it; setting it to 1
// disables the parallel driver entirely and falls the budget search back to
// the plain 4-way root split.
var searchWorkers = func() int {
	if s := os.Getenv("BOT2048_SEARCH_WORKERS"); s != "" {
		if n, err := strconv.Atoi(s); err == nil && n >= 1 && n <= 1024 {
			return n
		}
	}
	return runtime.GOMAXPROCS(0)
}()

// ---------------------------------------------------------------------------
// Frontier tree.

const (
	pnTask   = iota // an unexpanded chance node: one unit of work for the pool
	pnChance        // an expanded chance node; kids are max nodes / constants
	pnMax           // an expanded max node; kids are chance nodes / constants
	pnConst         // value already known (cprob cut, depth cut, terminal, jam)
)

// pnode is one node of the small tree the driver expands before handing work
// to the pool. Only pnTask nodes cost anything to evaluate.
type pnode struct {
	kind     int
	board    uint64
	cprob    float64
	curdepth int32

	kids    []*pnode
	weights []float64 // pnChance: the spawn probability of each kid, in kid order
	numOpen float64   // pnChance: countEmpty(board), as a float

	farm  bool    // pnMax: use the monobank stop-rule objective
	floor float64 // pnMax + farm: the pre-move S-sum (jam / forced-2048 cash-out)

	value float64 // pnConst: known value. pnTask: filled in by the worker.
	nodes uint64  // pnTask: chance nodes the worker spent on it
}

// parBuilder holds the (immutable) search configuration the frontier expansion
// needs. It never touches a transposition table: the driver's expansion is a
// handful of nodes, and keeping it TT-free means the frontier cannot perturb a
// worker's table.
type parBuilder struct {
	depthLimit  int32
	cprobThresh float64
	p4          float64
	eval        Evaluator
	evalKind    int
	farm        bool
}

func (b *parBuilder) leafEval(board uint64) float64 {
	switch b.evalKind {
	case evalKindTable:
		return tableEvaluator{}.Eval(board)
	case evalKindFarm:
		return farmEvaluator{}.Eval(board)
	default:
		return b.eval.Eval(board)
	}
}

// newChance mirrors the head of scoreTilechooseNode: a chance node that is past
// the cprob threshold or the depth limit is a static leaf; otherwise it is a
// unit of work.
func (b *parBuilder) newChance(board uint64, cprob float64, curdepth int32) *pnode {
	if cprob < b.cprobThresh || curdepth >= b.depthLimit {
		return &pnode{kind: pnConst, value: b.leafEval(board)}
	}
	return &pnode{kind: pnTask, board: board, cprob: cprob, curdepth: curdepth}
}

// newMax mirrors scoreMoveNode: the max over the legal moves, with the farm
// stop-rule specials (a move that makes a 2048 is a terminal cash-out at the
// pre-move S-sum; a jam cashes out likewise). Max nodes have at most 4 kids, so
// the driver always expands them — they are never units of work themselves.
func (b *parBuilder) newMax(board uint64, cprob float64, curdepth int32) *pnode {
	n := &pnode{kind: pnMax, farm: b.farm}
	moves := executeAllMoves(board)
	if b.farm {
		n.floor = sSumBoard(board)
		for dir := 0; dir < 4; dir++ {
			nb := moves[dir]
			if nb == board {
				continue
			}
			if hasRank11(nb) {
				n.kids = append(n.kids, &pnode{kind: pnConst, value: n.floor - farmTerminalEpsilon})
				continue
			}
			n.kids = append(n.kids, b.newChance(nb, cprob, curdepth+1))
		}
		return n
	}
	for dir := 0; dir < 4; dir++ {
		if nb := moves[dir]; nb != board {
			n.kids = append(n.kids, b.newChance(nb, cprob, curdepth+1))
		}
	}
	return n
}

// expand turns one pnTask (an unexpanded chance node) into a pnChance whose
// kids are max nodes — reproducing scoreTilechooseNode's enumeration exactly:
// cells low-to-high, and for each cell the 2-spawn before the 4-spawn, with
// cprob divided by the open count. The fold then re-runs the same additions in
// the same order.
func (b *parBuilder) expand(n *pnode) {
	numOpen := countEmpty(n.board)
	cprob := n.cprob / float64(numOpen)
	p4 := b.p4
	p2 := 1.0 - p4

	n.kind = pnChance
	n.numOpen = float64(numOpen)
	n.kids = make([]*pnode, 0, 2*numOpen)
	n.weights = make([]float64, 0, 2*numOpen)

	tmp := n.board
	tile2 := uint64(1)
	for tile2 != 0 {
		if tmp&0xF == 0 {
			n.kids = append(n.kids, b.newMax(n.board|tile2, cprob*p2, n.curdepth))
			n.weights = append(n.weights, p2)
			n.kids = append(n.kids, b.newMax(n.board|(tile2<<1), cprob*p4, n.curdepth))
			n.weights = append(n.weights, p4)
		}
		tmp >>= 4
		tile2 <<= 4
	}
}

// collectTasks appends every pnTask leaf of the subtree to dst.
func collectTasks(n *pnode, dst []*pnode) []*pnode {
	switch n.kind {
	case pnTask:
		return append(dst, n)
	case pnChance, pnMax:
		for _, k := range n.kids {
			dst = collectTasks(k, dst)
		}
	}
	return dst
}

// growFrontier expands whole plies of the frontier until there are at least
// `want` independent subtrees to hand out (or the tree runs out of expandable
// nodes: a shallow depthLimit turns the grandkids into static leaves and the
// frontier stops growing on its own).
func (b *parBuilder) growFrontier(roots []*pnode, want int) []*pnode {
	tasks := []*pnode(nil)
	for _, r := range roots {
		tasks = collectTasks(r, tasks)
	}
	for len(tasks) > 0 && len(tasks) < want {
		next := make([]*pnode, 0, len(tasks)*4)
		for _, t := range tasks {
			b.expand(t)
			next = collectTasks(t, next)
		}
		if len(next) <= len(tasks) && len(next) == 0 {
			break
		}
		tasks = next
		if len(tasks) >= parTaskCap {
			break
		}
	}
	return tasks
}

// fold computes the value of the frontier tree from the (already evaluated)
// task values, using exactly the serial search's arithmetic: a chance node sums
// its kids in enumeration order and then divides by the open count; a max node
// takes the running max in direction order, from 0.0 (classic) or from -Inf
// with a jam floor (farm).
func fold(n *pnode) float64 {
	switch n.kind {
	case pnConst, pnTask:
		return n.value
	case pnChance:
		res := 0.0
		for i, k := range n.kids {
			res += fold(k) * n.weights[i]
		}
		return res / n.numOpen
	default: // pnMax
		if n.farm {
			best := math.Inf(-1)
			for _, k := range n.kids {
				if v := fold(k); v > best {
					best = v
				}
			}
			if math.IsInf(best, -1) {
				return n.floor // jam: no legal move
			}
			return best
		}
		best := 0.0
		for _, k := range n.kids {
			if v := fold(k); v > best {
				best = v
			}
		}
		return best
	}
}

// ---------------------------------------------------------------------------
// Shared lock-free transposition table (the parallel driver's table).
//
// Per-worker tables were the obvious race-free choice, and they were measurably
// the wrong one: a subtree evaluated by worker 3 cannot reuse worker 7's
// entries, and the pool ended up visiting 2.43x the nodes the serial search
// does for the same answer — a hard ceiling of 13/2.43 = 5.3x on any speedup.
// So the pool shares ONE table, and shares it without locks.
//
// The trick is Hyatt's lockless XOR (of Crafty fame). A slot is two atomic
// words: `data` (the payload) and `xorKey` (board XOR data). A reader that
// catches a slot mid-update sees a payload and a key from different writes, and
// `xorKey ^ data` then does not equal the board it is looking for — the torn
// entry is detected and treated as a miss. No mutex, no CAS loop, and every
// access is a real atomic op, so the race detector has nothing to report.
//
// Fitting the payload in 64 bits costs the heuristic its float64 tail: it is
// stored as a float32 (~6e-8 relative error). That is 300x smaller than the
// ~2e-5 value drift the parallel split already introduces by reshuffling which
// cprob path populates which entry, and this table is only ever used by the live
// search — the fixed-depth simulation path keeps its exact float64 ttTable,
// untouched.
//
//	bits  0..31  heuristic, float32
//	bits 32..39  remaining depth (0..255)
//	bits 40..63  epoch (24 bits: 16M searches before a wrap could alias)

const (
	// sttLogSize: 4M slots x 16 B = 64 MB. The live process is alone on the
	// box and one budgeted search visits tens of millions of nodes, so a table
	// 16x the serial one's 2^18 entries pays for itself in hit rate.
	sttLogSize   = 22
	sttSize      = 1 << sttLogSize
	sttMask      = sttSize - 1
	sttProbe     = 4
	sttMaxRem    = 255
	sttEpochWrap = 1 << 24
)

type sttSlot struct {
	xorKey atomic.Uint64
	data   atomic.Uint64
}

type sharedTT struct {
	slots []sttSlot
	epoch uint64 // bumped between searches, read-only while workers run
}

func newSharedTT() *sharedTT {
	return &sharedTT{slots: make([]sttSlot, sttSize), epoch: 1}
}

func (t *sharedTT) nextEpoch() { t.epoch = (t.epoch + 1) % sttEpochWrap }

func sttPack(heur float64, remaining int32, epoch uint64) uint64 {
	if remaining > sttMaxRem {
		remaining = sttMaxRem
	}
	return uint64(math.Float32bits(float32(heur))) |
		uint64(uint8(remaining))<<32 |
		epoch<<40
}

func sttRemaining(d uint64) int32 { return int32((d >> 32) & 0xFF) }
func sttEpoch(d uint64) uint64    { return d >> 40 }
func sttHeur(d uint64) float64    { return float64(math.Float32frombits(uint32(d))) }

func (t *sharedTT) get(board uint64, needed int32) (float64, bool) {
	i := ttIndex(board) & sttMask
	for k := uint64(0); k < sttProbe; k++ {
		s := &t.slots[(i+k)&sttMask]
		d := s.data.Load()
		x := s.xorKey.Load()
		// The XOR check validates the payload AGAINST the key it was written
		// with: a slot caught mid-update fails it and reads as a miss.
		if x^d != board {
			continue
		}
		if sttEpoch(d) == t.epoch && sttRemaining(d) >= needed {
			return sttHeur(d), true
		}
	}
	return 0, false
}

func (t *sharedTT) put(board uint64, remaining int32, heur float64) {
	i := ttIndex(board) & sttMask
	victim := uint64(0)
	victimRem := int32(1 << 30)
	found := false
	for k := uint64(0); k < sttProbe; k++ {
		j := (i + k) & sttMask
		s := &t.slots[j]
		d := s.data.Load()
		x := s.xorKey.Load()
		if sttEpoch(d) != t.epoch { // free slot, or one from an older search
			victim, found = j, true
			break
		}
		if x^d == board {
			if sttRemaining(d) >= remaining {
				return // an equally or better informed value is already there
			}
			victim, found = j, true
			break
		}
		if r := sttRemaining(d); r < victimRem {
			victim, victimRem, found = j, r, true
		}
	}
	if !found {
		victim = i
	}
	data := sttPack(heur, remaining, t.epoch)
	s := &t.slots[victim]
	// Publish the payload first, then the key. A reader that lands between the
	// two sees the OLD key against the NEW payload, whose XOR cannot match the
	// old board — a detected miss, never a wrong value.
	s.data.Store(data)
	s.xorKey.Store(board ^ data)
}

// sharedTable lazily allocates the parallel driver's table. Called on the driver
// goroutine before any worker starts, so no synchronisation is needed here (the
// same discipline as searchTables.table). A process that never runs a live
// search never pays the 64 MB.
func (s *searchTables) sharedTable() *sharedTT {
	if s.stt == nil {
		s.stt = newSharedTT()
	}
	return s.stt
}

// ---------------------------------------------------------------------------
// The pool.

// runRootIterationPar is runRootIteration's parallel twin: same contract (per
// direction values + legality, completed=false meaning an abort source fired
// and the values are garbage), but it fans the work out over `workers`
// goroutines pulling independent chance subtrees off a shared cursor instead of
// over the 4 root moves.
func runRootIterationPar(board uint64, cfg iterCfg, tabs *searchTables) (values [4]float64, legal [4]bool, completed bool) {
	b := &parBuilder{
		depthLimit:  cfg.depthLimit,
		cprobThresh: cfg.cprobThresh,
		p4:          cfg.p4,
		eval:        cfg.eval,
		evalKind:    cfg.evalKind,
		farm:        cfg.farm,
	}

	// Root: one chance node per legal move (the position after the swipe, before
	// the spawn) — the same entry point as runRootIteration's per-dir goroutine.
	var roots [4]*pnode
	moves := executeAllMoves(board)
	for dir := 0; dir < 4; dir++ {
		nb := moves[dir]
		if nb == board {
			continue
		}
		legal[dir] = true
		if cfg.farm && hasRank11(nb) {
			values[dir] = sSumBoard(board) - farmTerminalEpsilon
			continue
		}
		roots[dir] = b.newChance(nb, 1.0, 0)
	}

	live := roots[:0:0]
	for _, r := range roots {
		if r != nil {
			live = append(live, r)
		}
	}
	if len(live) == 0 {
		return values, legal, true
	}

	workers := cfg.workers
	tasks := b.growFrontier(live, workers*parTaskTarget)
	if len(tasks) == 0 {
		// Everything was cut to a static leaf (depthLimit 0 / cprob): nothing to
		// hand out, the fold already has every value.
		for dir := 0; dir < 4; dir++ {
			if roots[dir] != nil {
				values[dir] = fold(roots[dir])
			}
		}
		return values, legal, true
	}

	if workers > len(tasks) {
		workers = len(tasks)
	}
	stt := tabs.sharedTable() // allocate on the driver goroutine

	abort := new(atomic.Bool)
	var cursor atomic.Int64
	var wg sync.WaitGroup
	var nodes atomic.Uint64
	for w := 0; w < workers; w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			state := cfg.newState(nil, stt, abort)
			defer func() {
				searchNodesCounted.Add(state.nodes)
				nodes.Add(state.nodes)
			}()
			for {
				i := int(cursor.Add(parPullChunk)) - parPullChunk
				if i >= len(tasks) || abort.Load() {
					return
				}
				t := tasks[i]
				// A previous task on this worker may have tripped the abort and
				// left state.stopped set; clear it so an un-aborted iteration is
				// never poisoned by a stale flag (and so a fresh task actually
				// searches instead of returning 0 immediately).
				state.stopped = false
				n0 := state.nodes
				t.value = state.scoreTilechooseNode(t.board, t.cprob, t.curdepth)
				t.nodes = state.nodes - n0
			}
		}()
	}
	wg.Wait()
	tabs.lastIterNodes = nodes.Load()

	// Load-balance telemetry, written on the driver goroutine after the join (so
	// it races with nothing). total/max is the CRITICAL-PATH bound: no scheduler,
	// on any number of cores, can finish this iteration faster than its single
	// biggest task, so it is the honest ceiling on the speedup — and it is
	// measured in NODES, which makes it independent of what else the box is
	// running. That matters: on a contended machine wall-clock speedup measures
	// the machine, not the algorithm.
	tabs.lastTaskCount = len(tasks)
	tabs.lastTaskNodesSum, tabs.lastTaskNodesMax = 0, 0
	for _, t := range tasks {
		tabs.lastTaskNodesSum += t.nodes
		if t.nodes > tabs.lastTaskNodesMax {
			tabs.lastTaskNodesMax = t.nodes
		}
	}

	if abort.Load() {
		return values, legal, false
	}
	for dir := 0; dir < 4; dir++ {
		if roots[dir] != nil {
			values[dir] = fold(roots[dir])
		}
	}
	return values, legal, true
}
