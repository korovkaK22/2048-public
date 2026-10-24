package main

import (
	"math"
	"sync/atomic"
	"time"
)

// ---------------------------------------------------------------------------
// Expectimax over the learned value function.
//
// SCALE / TABOO. The production search (engine2048/search.go) has to defend a
// hand-built invariant — `farmEval >= sSum`, so that the "cash out at the
// pre-move S-sum" value of a 2048-creating move can never beat a real move. The
// learned V does not need it, because it is already denominated in the units of
// the objective:
//
//	moveNode(b)   = expected number of moves still to be played from board b
//	              = max over legal d of  1 + (0 if the move makes a 2048
//	                                          else chanceNode(afterstate))
//	chanceNode(a) = E_spawn[ moveNode(a + spawn) ],  or V(a) at the depth limit
//	no legal move = 0
//
// A 2048-creating move is therefore worth exactly 1 (you make the move, the game
// ends) and every other legal move is worth 1 + V >= 1, because the leaf is
// clamped to V >= 0 (net.evalNonNeg). The taboo is a THEOREM here, not a tuned
// floor: `V >= 0` plays exactly the role `farmEval >= sSum` plays there. The
// epsilon below only breaks the (measure-zero) tie where every real move has
// V == 0.
//
// The cprob cutoff and the transposition table are the same devices the
// production engine uses, with the same constants.
// ---------------------------------------------------------------------------

const (
	// terminal2048 is the value of a move that creates a 2048: the one move
	// itself, minus a hair so that on an exact tie a real move always wins.
	terminal2048 = 1.0 - 1e-6

	cacheDepthLimit = 15
	ttLogSize       = 18
	ttSize          = 1 << ttLogSize
	ttMask          = ttSize - 1
	ttProbe         = 2

	// abortCheckMask: budget searches poll the deadline / shared abort flag
	// every (mask+1) interior chance nodes — cheap, and bounds the overshoot
	// past the deadline to well under a millisecond.
	abortCheckMask = 511
)

var cprobThresh = 0.0001

// nodeBudget is the per-request node cap (protocol 2 `nodes=N`): a hardware-
// independent abort source shared by every searcher of one request (the serial
// searcher, or all workers of a parallel rung). It is charged at the abort poll
// (every abortCheckMask+1 interior chance nodes), so in the serial search
// whether a rung trips the cap is a pure function of the position — which is
// what makes the both-limits-None "unbounded" mode reproducible.
type nodeBudget struct {
	used atomic.Uint64
	cap  uint64
}

// spend charges n nodes and reports whether the cap is now exceeded.
func (nb *nodeBudget) spend(n uint64) bool { return nb.used.Add(n) > nb.cap }

// exceeded reports whether the cap has been crossed (by any searcher).
func (nb *nodeBudget) exceeded() bool { return nb.used.Load() > nb.cap }

type ttEntry struct {
	key       uint64
	val       float64
	epoch     uint32
	remaining int32
}

type ttTable struct {
	entries []ttEntry
	epoch   uint32
}

func newTTTable() *ttTable { return &ttTable{entries: make([]ttEntry, ttSize), epoch: 1} }

func ttIndex(board uint64) uint64 { return (board * 0x9E3779B97F4A7C15) >> (64 - ttLogSize) }

func (t *ttTable) get(board uint64, needed int32) (float64, bool) {
	i := ttIndex(board)
	for k := uint64(0); k < ttProbe; k++ {
		e := &t.entries[(i+k)&ttMask]
		if e.epoch == t.epoch && e.key == board && e.remaining >= needed {
			return e.val, true
		}
	}
	return 0, false
}

func (t *ttTable) put(board uint64, remaining int32, val float64) {
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
		return
	}
	*victim = ttEntry{key: board, val: val, epoch: t.epoch, remaining: remaining}
}

// searcher is one single-threaded expectimax context. Parallelism in this tool
// is at the GAME level (one searcher per worker goroutine), not at the root:
// we are measuring throughput over hundreds of games, so per-game searchers give
// perfect scaling and keep each transposition table hot.
type searcher struct {
	tt *ttTable
	// stt, when non-nil, REPLACES tt: the lock-free table the parallel budget
	// driver (parallel.go) shares across its whole worker pool. Only the
	// parallel path sets it — on the per-worker searchers it spawns; every
	// serial searcher (arena, bench, probe, fixed-depth serve, the insurance
	// pass) leaves it nil and runs on the exact-float64 tt, bit-reproducibly.
	stt *sharedTT
	// pstt is the persistent pool table a budget searcher OWNS and hands to the
	// per-worker searchers of every parallel rung (so rungs reuse each other's
	// entries). Lazily allocated on first parallel use; a searcher that never
	// runs a parallel budget search never pays its 64 MB.
	pstt       *sharedTT
	depthLimit int32
	// cprobThresh is the probability horizon: a chance branch whose cumulative
	// probability has fallen below it is cut and replaced by the leaf eval. It
	// is a PER-SEARCHER parameter (seeded from the package default) because it
	// — not depthLimit — is what actually stops the tree on most boards, and
	// the budget ladder (serve.go) widens it rung by rung.
	cprobThresh float64
	p4          float64
	leaf        evalFn
	leaves      uint64
	// kappa > 0 turns every CHANCE node from an expectation into a risk-seeking
	// certainty equivalent  E[X] + kappa*sd(X)  over the spawn lottery. This is
	// the first-order (mean-variance) form of the exponential utility, applied at
	// SEARCH time to a net trained on the mean — the cheap variant that needs no
	// retraining. It is only meaningful for the mean objective; the exponential
	// net already carries the risk preference inside its leaf.
	kappa float64
	// Budget-abort machinery (hasDeadline=false => fixed-depth search, no
	// polling at all). `abort` is shared so that, under the parallel driver,
	// every worker of one iteration stops together; `stopped` makes the unwind
	// after an abort O(1) per frame. Values returned after an abort are
	// garbage by design — the whole rung is discarded (rootSearchDeadline
	// reports completed=false and no caller may use them).
	hasDeadline bool
	deadline    time.Time
	abort       *atomic.Bool
	// nodeCap, when non-nil, is the request's node budget (protocol 2
	// `nodes=`), charged at the same poll as the deadline; crossing it aborts
	// the rung exactly like the deadline does.
	nodeCap *nodeBudget
	stopped bool
	// nodes counts interior chance nodes; it doubles as the abort-poll clock.
	nodes uint64
	// lastAchievedRung is the index into budgetLadder that the previous budget
	// search on this searcher completed — the starting hint for the next one
	// (positions evolve slowly, so the affordable rung rarely moves by more
	// than one per move).
	lastAchievedRung int
	// ladderFirst is the direct-first mitigation of the capped search
	// (limitedSearch, serve.go): set when the previous move's direct
	// fixed-depth attempt failed or ate most of the budget, so this move climbs
	// the capped ladder first and tries the direct search only with what is
	// left.
	ladderFirst bool
	// farm switches the max node to the PRODUCTION objective (engine2048/
	// search.go): maximise the expected board S-sum at the stopping moment, with
	// a 2048 or a jam cashing out at the pre-move S-sum. The two objectives must
	// not be mixed — the "+1 move" recursion below is only meaningful when the
	// leaf is denominated in moves — so the baseline arm runs the real farm
	// semantics here rather than a farm evaluator dropped into the moves
	// recursion, which would be a different (and much worse) engine.
	farm bool
}

func newSearcher(leaf evalFn, depth int, p4 float64) *searcher {
	return &searcher{tt: newTTTable(), depthLimit: int32(depth),
		cprobThresh: cprobThresh, p4: p4, leaf: leaf}
}

// newFarmSearcher reproduces the production engine's farm search: same leaf,
// same cprob cutoff, same TT geometry, same terminal cash-out rule.
func newFarmSearcher(depth int, p4 float64) *searcher {
	return &searcher{tt: newTTTable(), depthLimit: int32(depth),
		cprobThresh: cprobThresh, p4: p4, leaf: farmEvaluator{}.Eval, farm: true}
}

// beginSearch invalidates every transposition entry in O(1). bestMove calls it
// per request; the budget ladder (serve.go) calls it once per budget search and
// again whenever a rung changes the cprob horizon — an entry computed under a
// coarser horizon must never satisfy a finer-horizon lookup, or the widening
// the rung paid for is silently undone.
func (s *searcher) beginSearch() { s.tt.epoch++ }

func (s *searcher) chanceNode(board uint64, cprob float64, curdepth int32) float64 {
	if cprob < s.cprobThresh || curdepth >= s.depthLimit {
		s.leaves++
		return s.leaf(board)
	}
	// One increment per interior chance node: the abort-poll clock.
	s.nodes++
	if s.hasDeadline || s.nodeCap != nil {
		if s.stopped {
			return 0
		}
		if s.nodes&abortCheckMask == 0 {
			if s.abort.Load() ||
				(s.hasDeadline && !time.Now().Before(s.deadline)) ||
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
	if numOpen == 0 {
		// A full post-move board still has moves if adjacent equals remain;
		// there is nothing to spawn, so recurse straight into the max node.
		return s.moveNode(board, cprob, curdepth)
	}
	cprob /= float64(numOpen)
	p4 := s.p4
	p2 := 1.0 - p4
	res := 0.0
	sq := 0.0 // second moment, only used when kappa > 0
	tmp := board
	tile2 := uint64(1)
	for tile2 != 0 {
		if tmp&0xF == 0 {
			v2 := s.moveNode(board|tile2, cprob*p2, curdepth)
			v4 := s.moveNode(board|(tile2<<1), cprob*p4, curdepth)
			res += v2*p2 + v4*p4
			if s.kappa != 0 {
				sq += v2*v2*p2 + v4*v4*p4
			}
		}
		tmp >>= 4
		tile2 <<= 4
	}
	res /= float64(numOpen)
	if s.kappa != 0 {
		sq /= float64(numOpen)
		if v := sq - res*res; v > 0 {
			res += s.kappa * math.Sqrt(v)
		}
	}
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

// moveNode is the max node in units of REMAINING MOVES: every legal move is
// worth the move itself (+1) plus whatever the resulting position is worth. A
// jam is worth 0; a 2048 is worth 1 (see terminal2048).
func (s *searcher) moveNode(board uint64, cprob float64, curdepth int32) float64 {
	moves := executeAllMoves(board)
	if s.farm {
		floor := sSumBoard(board) // cash out now: a jam or a forced 2048 keeps this
		best := math.Inf(-1)
		for dir := 0; dir < 4; dir++ {
			nb := moves[dir]
			if nb == board {
				continue
			}
			var v float64
			if hasRank11(nb) {
				v = floor - farmTerminalEpsilon
			} else {
				v = s.chanceNode(nb, cprob, curdepth+1)
			}
			if v > best {
				best = v
			}
		}
		if math.IsInf(best, -1) {
			return floor
		}
		return best
	}
	if obj.exp {
		// Multiplicative form of the same recursion: one more move multiplies the
		// utility by e^lambda, a jam is worth exp(-lambda*c), and a 2048-creating
		// move is worth "the move itself, then death" = e^lambda * exp(-lambda*c),
		// shaved by an epsilon so a real move (leaf clamped to >= exp(-lambda*c))
		// always wins an exact tie. Same theorem, different units.
		best := obj.term // jam: no moves left
		for dir := 0; dir < 4; dir++ {
			nb := moves[dir]
			if nb == board {
				continue
			}
			var v float64
			if hasRank11(nb) {
				v = obj.step * obj.term * (1 - 1e-9)
			} else {
				v = obj.step * s.chanceNode(nb, cprob, curdepth+1)
			}
			if v > best {
				best = v
			}
		}
		return best
	}
	best := 0.0 // jam: no moves left
	for dir := 0; dir < 4; dir++ {
		nb := moves[dir]
		if nb == board {
			continue
		}
		var v float64
		if hasRank11(nb) {
			v = terminal2048
		} else {
			v = 1.0 + s.chanceNode(nb, cprob, curdepth+1)
		}
		if v > best {
			best = v
		}
	}
	return best
}

// bestMove runs the root expectimax and returns the chosen direction (-1 if
// the board is jammed) and its value in expected remaining moves. Epochs are
// bumped per call, so the result depends only on (board, parameters) — never
// on what was searched before.
func (s *searcher) bestMove(board uint64) (int, float64) {
	s.beginSearch()
	return s.rootSearch(board)
}

// rootSearchDeadline runs one rung under a hard deadline: the abort poll in
// chanceNode fires within one abortCheckMask window of the deadline, the
// unwind is O(1) per frame, and completed=false means the returned values are
// garbage by design — the caller keeps the previous rung's answer instead.
// The searcher is disarmed on return, so fixed-depth callers never poll.
func (s *searcher) rootSearchDeadline(board uint64, deadline time.Time) (dir int, val float64, completed bool) {
	return s.rootSearchLimited(board, deadline, true, nil)
}

// rootSearchLimited is rootSearchDeadline with both abort sources explicit: an
// optional deadline and an optional node cap (protocol 2). Polling never changes
// a value, so a limited search that COMPLETES is bit-identical to the plain
// rootSearch on the same table state — the property the capped live search
// (limitedSearch) is built on. The searcher is disarmed on return.
func (s *searcher) rootSearchLimited(board uint64, deadline time.Time, hasDeadline bool, nb *nodeBudget) (dir int, val float64, completed bool) {
	s.hasDeadline, s.deadline, s.nodeCap = hasDeadline, deadline, nb
	s.abort = new(atomic.Bool)
	s.stopped = false
	dir, val = s.rootSearch(board)
	aborted := s.abort.Load()
	s.hasDeadline, s.nodeCap, s.abort, s.stopped = false, nil, nil, false
	if !aborted && nb != nil && nb.exceeded() {
		// Crossed the cap inside the last poll window: the values are complete,
		// but "completed within the cap" must be a pure function of the position.
		aborted = true
	}
	return dir, val, !aborted
}

// rootSearch is bestMove WITHOUT the epoch bump: the budget ladder calls it
// once per rung so that deeper rungs reuse exact values computed by shallower
// ones where still valid (`remaining` in the TT entry guards validity across
// depth changes; a cprob change invalidates via beginSearch instead).
func (s *searcher) rootSearch(board uint64) (int, float64) {
	moves := executeAllMoves(board)
	floor := 0.0
	if s.farm {
		floor = sSumBoard(board)
	}
	bestDir, bestVal := -1, math.Inf(-1)
	for dir := 0; dir < 4; dir++ {
		nb := moves[dir]
		if nb == board {
			continue
		}
		var v float64
		switch {
		case hasRank11(nb) && s.farm:
			v = floor - farmTerminalEpsilon
		case hasRank11(nb) && obj.exp:
			v = obj.step * obj.term * (1 - 1e-9)
		case hasRank11(nb):
			v = terminal2048
		case s.farm:
			v = s.chanceNode(nb, 1.0, 0)
		case obj.exp:
			v = obj.step * s.chanceNode(nb, 1.0, 0)
		default:
			v = 1.0 + s.chanceNode(nb, 1.0, 0)
		}
		if v > bestVal {
			bestDir, bestVal = dir, v
		}
	}
	if bestDir == -1 {
		return -1, 0
	}
	return bestDir, bestVal
}

// playSearch plays one full game from an empty board and reports the moves
// survived, whether the game ended on a (forced) 2048, and the final board.
// `s` must be a fresh searcher; it is reused across the whole game so the
// transposition table stays warm, exactly as the production engine does.
func playSearch(s *searcher, r *rng) (moves int, forced2048 bool, finalBoard uint64) {
	b := initBoard(r)
	for {
		dir, _ := s.bestMove(b)
		if dir < 0 {
			return moves, false, b
		}
		nb := executeAllMoves(b)[dir]
		moves++
		if hasRank11(nb) {
			return moves, true, b
		}
		b = spawn(nb, r)
	}
}
