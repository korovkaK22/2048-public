package main

import (
	"math"
	"sync"
	"sync/atomic"
	"time"
)

// ---------------------------------------------------------------------------
// Parallel frontier pool (Change 4), ported from engine2048/parallel.go and
// adapted to the n-tuple searcher's THREE objective arms (mean / exp / farm).
//
// The serial budget search (search.go) is one goroutine: on the ~20-core live
// box it leaves ~19 cores idle while the 9 s move budget elapses. This driver
// spends the whole machine WITHIN the same wall-clock budget, converting cores
// into a deeper cprob horizon.
//
// Shape: a PARALLEL FRONTIER, not a root split. The driver expands the top of
// the tree itself — chance -> max kids -> chance grandkids — until it holds at
// least workers*parTaskTarget INDEPENDENT chance subtrees, then a work-stealing
// pool of W goroutines drains them off a shared atomic cursor (a fat subtree
// cannot leave 19 cores idle the way a 4-way root split would). Results fold
// back up the tiny frontier tree with EXACTLY the serial add/max order, so the
// only numeric difference from the serial search is the shared table's float32
// quantization of interior values.
//
// The pool shares ONE lock-free transposition table (sharedTT, Hyatt's lockless
// XOR): a subtree evaluated by worker 3 must be reusable by worker 7, or the
// pool visits multiples of the serial node count for the same answer.
//
// Determinism: tasks are pulled dynamically, so which worker populates which
// table slot varies run to run and a value can differ in its last bits between
// runs. That is acceptable HERE ONLY: this driver is reachable exclusively from
// the time-budgeted serve/budget path, which is already nondeterministic (it
// returns whatever the clock allowed). Every fixed-depth path (arena, bench,
// probe, playSearch) stays on the serial exact-float64 searcher, untouched.
//
// net.eval is READ-ONLY during search (no writes in the eval path), so the
// concurrent leaf calls need no synchronization — that is what makes sharing
// the weights across the pool safe.
// ---------------------------------------------------------------------------

const (
	// parTaskTarget: frontier subtrees wanted per worker before expansion
	// stops. >1 so the pool can smooth out the (very) unequal subtree costs.
	parTaskTarget = 6
	// parTaskCap bounds the frontier so a 15-empty board cannot explode the
	// driver-side expansion.
	parTaskCap = 8192
)

// objective arm of a max node / root move. Fixed once per search from the
// package-global obj and the searcher's farm flag.
const (
	objMean = iota // value = 1 + chance; jam floor 0; 2048 = terminal2048
	objExp         // value = e^lambda * chance; jam floor obj.term; 2048 special
	objFarm        // value = chance (S-sum units); jam/2048 cash out at pre-move S-sum
)

func objModeOf(farm bool) int {
	switch {
	case farm:
		return objFarm
	case obj.exp:
		return objExp
	default:
		return objMean
	}
}

// applyMove wraps a chance-node value into the move value its parent max node
// takes the max over — the per-move objective transform, identical to the
// wrapping in serial moveNode / rootSearch.
func applyMove(mode int, chance float64) float64 {
	switch mode {
	case objExp:
		return obj.step * chance
	case objFarm:
		return chance
	default: // objMean
		return 1.0 + chance
	}
}

// terminalMoveValue is the value of a move that CREATES a 2048 (ends the game),
// matching serial moveNode / rootSearch. `floor` is the pre-move S-sum, used
// only by the farm arm.
func terminalMoveValue(mode int, floor float64) float64 {
	switch mode {
	case objExp:
		return obj.step * obj.term * (1 - 1e-9)
	case objFarm:
		return floor - farmTerminalEpsilon
	default: // objMean
		return terminal2048
	}
}

// maxInit is the starting value of a max node's running best: the JAM value
// (no legal move) for mean/exp, and -Inf for farm (which falls back to the
// board's S-sum floor when no move exists).
func maxInit(mode int) float64 {
	switch mode {
	case objExp:
		return obj.term
	case objFarm:
		return math.Inf(-1)
	default: // objMean
		return 0.0
	}
}

// ---------------------------------------------------------------------------
// Frontier tree.

const (
	pnTask   = iota // an unexpanded chance node: one unit of work for the pool
	pnChance        // an expanded chance node; kids are max nodes / constants
	pnMax           // an expanded max node; kids are chance nodes / terminals
	pnConst         // a CUT chance node: leaf value, TO BE transformed by parent
	pnTerm          // a terminal MOVE value: already final, NOT transformed
)

// pnode is one node of the small tree the driver expands before handing work to
// the pool. Only pnTask nodes cost anything to evaluate.
type pnode struct {
	kind     int
	board    uint64
	cprob    float64
	curdepth int32

	kids    []*pnode
	numOpen float64 // pnChance: countEmpty(board), as a float

	mode  int     // pnMax: objMean/objExp/objFarm
	floor float64 // pnMax + farm: pre-move S-sum (jam / forced-2048 cash-out)

	value float64 // pnConst/pnTerm: known value; pnTask: filled by the worker
	nodes uint64  // pnTask: chance nodes the worker spent on it
}

// parBuilder holds the immutable search configuration the frontier expansion
// needs. It never touches a transposition table: the expansion is a handful of
// nodes, and keeping it TT-free means the frontier cannot perturb a worker's
// table.
type parBuilder struct {
	depthLimit  int32
	cprobThresh float64
	p4          float64
	leaf        evalFn
	mode        int
}

// newChance mirrors the head of chanceNode: a chance node past the cprob
// threshold or the depth limit is a static (cut) leaf; a FULL board (no spawn
// possible) is really a max node — the serial numOpen==0 special case — so it
// is built as one; otherwise it is a unit of work.
func (b *parBuilder) newChance(board uint64, cprob float64, curdepth int32) *pnode {
	if cprob < b.cprobThresh || curdepth >= b.depthLimit {
		return &pnode{kind: pnConst, value: b.leaf(board)}
	}
	if countEmpty(board) == 0 {
		return b.newMax(board, cprob, curdepth)
	}
	return &pnode{kind: pnTask, board: board, cprob: cprob, curdepth: curdepth}
}

// newMax mirrors moveNode: the max over the legal moves that change the board,
// with the objective's terminal specials (a 2048-creating move is a terminal
// cash-out; a jam is handled by maxInit / the farm floor). Max nodes have at
// most 4 kids, so the driver always expands them — they are never work units.
func (b *parBuilder) newMax(board uint64, cprob float64, curdepth int32) *pnode {
	n := &pnode{kind: pnMax, mode: b.mode}
	if b.mode == objFarm {
		n.floor = sSumBoard(board)
	}
	moves := executeAllMoves(board)
	for dir := 0; dir < 4; dir++ {
		nb := moves[dir]
		if nb == board {
			continue
		}
		if hasRank11(nb) {
			n.kids = append(n.kids, &pnode{kind: pnTerm, value: terminalMoveValue(b.mode, n.floor)})
			continue
		}
		n.kids = append(n.kids, b.newChance(nb, cprob, curdepth+1))
	}
	return n
}

// expand turns one pnTask (an unexpanded chance node) into a pnChance whose
// kids are max nodes — reproducing chanceNode's enumeration exactly: cells
// low-to-high, and for each cell the 2-spawn's max node immediately followed by
// the 4-spawn's, with cprob divided by the open count. The fold then re-runs the
// same per-cell (v2*p2 + v4*p4) additions in the same order.
func (b *parBuilder) expand(n *pnode) {
	numOpen := countEmpty(n.board)
	cprob := n.cprob / float64(numOpen)
	p4 := b.p4
	p2 := 1.0 - p4

	n.kind = pnChance
	n.numOpen = float64(numOpen)
	n.kids = make([]*pnode, 0, 2*numOpen)

	tmp := n.board
	tile2 := uint64(1)
	for tile2 != 0 {
		if tmp&0xF == 0 {
			n.kids = append(n.kids, b.newMax(n.board|tile2, cprob*p2, n.curdepth))
			n.kids = append(n.kids, b.newMax(n.board|(tile2<<1), cprob*p4, n.curdepth))
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

// growFrontier expands whole plies until there are at least `want` independent
// subtrees to hand out (or the tree runs out of expandable nodes: a shallow
// depthLimit / tight cprob turns the grandkids into static leaves and the
// frontier stops growing on its own).
func (b *parBuilder) growFrontier(roots []*pnode, want int) []*pnode {
	var tasks []*pnode
	for _, r := range roots {
		tasks = collectTasks(r, tasks)
	}
	for len(tasks) > 0 && len(tasks) < want {
		next := make([]*pnode, 0, len(tasks)*4)
		for _, t := range tasks {
			b.expand(t)
			next = collectTasks(t, next)
		}
		if len(next) == 0 {
			break
		}
		tasks = next
		if len(tasks) >= parTaskCap {
			break
		}
	}
	return tasks
}

// fold computes the value of the frontier tree from the (already evaluated) task
// values, using exactly the serial search's arithmetic: a chance node sums its
// per-cell (v2*p2 + v4*p4) contributions in enumeration order then divides by
// the open count; a max node applies the per-move objective transform to each
// non-terminal kid (terminal kids are already final) and takes the running max
// from maxInit, falling back to the S-sum floor for a jammed farm node.
func (b *parBuilder) foldP4() (p2, p4 float64) { return 1.0 - b.p4, b.p4 }

func fold(n *pnode, p2, p4 float64) float64 {
	switch n.kind {
	case pnConst, pnTerm, pnTask:
		return n.value
	case pnChance:
		res := 0.0
		// kids come in (2-spawn, 4-spawn) pairs per empty cell; combine each
		// pair exactly as serial chanceNode does before accumulating.
		for i := 0; i+1 < len(n.kids); i += 2 {
			res += fold(n.kids[i], p2, p4)*p2 + fold(n.kids[i+1], p2, p4)*p4
		}
		return res / n.numOpen
	default: // pnMax
		best := maxInit(n.mode)
		for _, k := range n.kids {
			var v float64
			if k.kind == pnTerm {
				v = k.value // terminal move: already final, no transform
			} else {
				v = applyMove(n.mode, fold(k, p2, p4))
			}
			if v > best {
				best = v
			}
		}
		if n.mode == objFarm && math.IsInf(best, -1) {
			return n.floor // jam: no legal move
		}
		return best
	}
}

// ---------------------------------------------------------------------------
// The pool.

// parallelRung runs ONE ladder rung (fixed depthLimit + cprobThresh, already
// set on s) across `workers` goroutines under a hard deadline. It returns the
// chosen direction, its value, and completed=false if the deadline (or the
// shared abort) fired mid-rung — in which case the values are garbage and the
// caller keeps the previous rung's answer. The driver searcher s is only read
// (its params + s.pstt); the workers own separate searcher state.
func (s *searcher) parallelRung(board uint64, deadline time.Time, workers int) (dir int, val float64, completed bool) {
	return s.parallelRungLimited(board, deadline, true, nil, workers)
}

// parallelRungLimited is parallelRung with both abort sources explicit: an
// optional deadline and an optional node cap shared by every worker (protocol
// 2). The workers' node counts are summed into s.nodes after the join, so the
// driver searcher's counter is the request total on either path.
func (s *searcher) parallelRungLimited(board uint64, deadline time.Time, hasDeadline bool, nb *nodeBudget, workers int) (dir int, val float64, completed bool) {
	b := &parBuilder{
		depthLimit:  s.depthLimit,
		cprobThresh: s.cprobThresh,
		p4:          s.p4,
		leaf:        s.leaf,
		mode:        objModeOf(s.farm),
	}
	p2, p4 := b.foldP4()

	// Root: one chance node per legal move (post-swipe, pre-spawn), the same
	// entry point as serial rootSearch's per-direction chanceNode(nb, 1, 0).
	var values [4]float64
	var legal [4]bool
	var roots [4]*pnode
	moves := executeAllMoves(board)
	rootFloor := 0.0
	if b.mode == objFarm {
		rootFloor = sSumBoard(board)
	}
	for d := 0; d < 4; d++ {
		nb := moves[d]
		if nb == board {
			continue
		}
		legal[d] = true
		if hasRank11(nb) {
			values[d] = terminalMoveValue(b.mode, rootFloor)
			continue
		}
		roots[d] = b.newChance(nb, 1.0, 0)
	}

	var live []*pnode
	for _, r := range roots {
		if r != nil {
			live = append(live, r)
		}
	}

	foldRoots := func() {
		for d := 0; d < 4; d++ {
			if roots[d] != nil {
				values[d] = applyMove(b.mode, fold(roots[d], p2, p4))
			}
		}
	}

	if len(live) == 0 {
		// Every legal move was a forced 2048 (values already set) or the board
		// is jammed. Nothing to search.
		return pickBestDir(values, legal)
	}

	tasks := b.growFrontier(live, workers*parTaskTarget)
	if len(tasks) == 0 {
		// Everything was cut to a static leaf: the fold has every value already.
		foldRoots()
		return pickBestDirVal(values, legal)
	}
	if workers > len(tasks) {
		workers = len(tasks)
	}

	abort := new(atomic.Bool)
	var cursor atomic.Int64
	var wg sync.WaitGroup
	var nodes atomic.Uint64
	for w := 0; w < workers; w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			ws := &searcher{
				stt:         s.pstt,
				depthLimit:  s.depthLimit,
				cprobThresh: s.cprobThresh,
				p4:          s.p4,
				leaf:        s.leaf,
				farm:        s.farm,
				hasDeadline: hasDeadline,
				deadline:    deadline,
				abort:       abort,
				nodeCap:     nb,
			}
			defer func() { nodes.Add(ws.nodes) }()
			for {
				i := int(cursor.Add(1)) - 1
				if i >= len(tasks) || abort.Load() {
					return
				}
				t := tasks[i]
				// A previous task on this worker may have tripped the abort and
				// left ws.stopped set; clear it so a fresh task actually searches.
				ws.stopped = false
				t.value = ws.chanceNode(t.board, t.cprob, t.curdepth)
			}
		}()
	}
	wg.Wait()
	s.nodes += nodes.Load()

	if abort.Load() || (nb != nil && nb.exceeded()) {
		return -1, 0, false
	}
	foldRoots()
	dir, val, _ = pickBestDirVal(values, legal)
	return dir, val, true
}

// pickBestDir / pickBestDirVal choose the highest-valued legal direction (ties
// break toward the lower direction int), matching serial rootSearch. The two
// spellings only differ in whether they report completion.
func pickBestDir(values [4]float64, legal [4]bool) (int, float64, bool) {
	return pickBestDirVal(values, legal)
}

func pickBestDirVal(values [4]float64, legal [4]bool) (int, float64, bool) {
	bestDir := -1
	bestVal := math.Inf(-1)
	for d := 0; d < 4; d++ {
		if legal[d] && values[d] > bestVal {
			bestDir, bestVal = d, values[d]
		}
	}
	if bestDir == -1 {
		return -1, 0, true
	}
	return bestDir, bestVal, true
}

// ---------------------------------------------------------------------------
// Shared lock-free transposition table (the parallel driver's table).
//
// Per-worker tables were the obvious race-free choice and measurably the wrong
// one: a subtree evaluated by worker 3 cannot reuse worker 7's entries, so the
// pool visits multiples of the serial node count for the same answer. So the
// pool shares ONE table, without locks, via Hyatt's lockless XOR (of Crafty
// fame). A slot is two atomic words: `data` (the payload) and `xorKey`
// (board XOR data). A reader that catches a slot mid-update sees a payload and
// a key from different writes, and xorKey^data then does not equal the board it
// wants — the torn entry is detected and treated as a miss. No mutex, no CAS
// loop, and every access is a real atomic op, so the race detector is quiet.
//
// Fitting the payload in 64 bits costs the value its float64 tail: it is stored
// as float32 (~6e-8 relative error). This table is used ONLY by the live
// budget/serve path; every fixed-depth path keeps its exact float64 ttTable.
//
//	bits  0..31  value, float32
//	bits 32..39  remaining depth (0..255)
//	bits 40..63  epoch (24 bits: 16M searches before a wrap could alias)
const (
	// sttLogSize: 4M slots x 16 B = 64 MB. The live process is alone on the box
	// and one budgeted search visits tens of millions of nodes, so a table 16x
	// the serial one's 2^18 entries pays for itself in hit rate.
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

func newSharedTT() *sharedTT { return &sharedTT{slots: make([]sttSlot, sttSize), epoch: 1} }

func (t *sharedTT) nextEpoch() { t.epoch = (t.epoch + 1) % sttEpochWrap }

func sttPack(val float64, remaining int32, epoch uint64) uint64 {
	if remaining > sttMaxRem {
		remaining = sttMaxRem
	}
	return uint64(math.Float32bits(float32(val))) |
		uint64(uint8(remaining))<<32 |
		epoch<<40
}

func sttRemaining(d uint64) int32 { return int32((d >> 32) & 0xFF) }
func sttEpoch(d uint64) uint64    { return d >> 40 }
func sttVal(d uint64) float64     { return float64(math.Float32frombits(uint32(d))) }

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
			return sttVal(d), true
		}
	}
	return 0, false
}

func (t *sharedTT) put(board uint64, remaining int32, val float64) {
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
	data := sttPack(val, remaining, t.epoch)
	s := &t.slots[victim]
	// Publish the payload first, then the key. A reader that lands between the
	// two sees the OLD key against the NEW payload, whose XOR cannot match the
	// old board — a detected miss, never a wrong value.
	s.data.Store(data)
	s.xorKey.Store(board ^ data)
}
