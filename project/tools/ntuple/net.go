package main

import (
	"bufio"
	"encoding/binary"
	"fmt"
	"math"
	"os"
	"sync"
)

// ---------------------------------------------------------------------------
// 8 x 6-tuple network over ranks 0..10 (rank 11 = 2048 is terminal and never
// evaluated), 8 board symmetries sharing one weight table per pattern.
//
// The value is the EXPECTED NUMBER OF REMAINING MOVES from an afterstate: the
// TD(0) reward is +1 per move and the terminal value is 0. That choice is what
// makes the network drop straight into expectimax (see search.go): the leaf is
// already in the units of the objective, so a 2048-creating move — which ends
// the game — is simply worth "the one move it takes" and nothing after, and no
// hand-tuned cash-out floor is needed to keep the engine off it.
// ---------------------------------------------------------------------------

const (
	maxTileRank = 10 // 1024; creating rank 11 (2048) ends the game
	base        = 11 // ranks 0..10
	nPat        = 8
	patLen      = 6
	nSym        = 8
	nFeat       = nPat * nSym // 64 lookups per evaluation
)

var patSize = ipow(base, patLen) // 11^6 = 1_771_561

// ---------------------------------------------------------------------------
// OBJECTIVE: mean vs risk-seeking exponential utility
//
// The mean objective V(a) = E[remaining moves] is what TD(0) converges to, and
// it is a VARIANCE-MINIMISING target: the optimal mean policy is the one that
// most reliably survives. Measured: sigma falls 228 -> 163 as the search gets
// deeper, while the mean rises. Our real objective is P(moves >= 2569), a ~4
// sigma tail event, so a taller, narrower distribution is a WORSE distribution.
//
// The exponential (entropic / risk-seeking) utility replaces the target with
//
//	W(a) = E[ exp(lambda * (G - c)) ],   G = remaining moves from afterstate a,
//
// for lambda > 0. exp is convex, so W rewards the UPPER tail super-linearly: a
// policy maximising W accepts a lower mean in exchange for probability mass far
// out on the right. Its certainty equivalent CE = c + ln(W)/lambda is the mean
// plus (lambda/2)*variance to first order — exactly the "mean plus risk" knob we
// want, and lambda is the knob's units (1/moves).
//
// WHAT THE NET PREDICTS. The Bellman operator for W is multiplicative:
//
//	W(a) = E_spawn[ max_d  e^lambda * W(a'_d) ],   W(jam) = exp(-lambda*c)
//
// but an n-tuple net is an ADDITIVE sum of 64 weights, and a sum of 64 terms is
// a terrible way to represent an exponential. Training the net on W directly was
// tried and it stalls: 1-ply median 1090 moves against 1410 for the mean net,
// flat from 60k episodes on — the late game, where W is ~1e-4, contributes
// updates four orders of magnitude smaller than the opening and never gets
// fitted.
//
// So the NET STAYS IN MOVE UNITS: it predicts the CERTAINTY EQUIVALENT
//
//	U(a) = c + ln W(a) / lambda,
//
// which is mean-remaining-moves plus (lambda/2)*variance to first order — a
// smooth, additive, ~0..2600 quantity that the same architecture, the same
// optimistic init and the same alpha already fit well. The risk enters in
// exactly two places, both exact:
//
//  1. THE TD TARGET (below, in playTrain). Regressing W onto its sampled target
//     with a step proportional to 1/W gives, in U units,
//
//     dU  =  alpha * (1/lambda) * ( exp(lambda*delta) - 1 ),
//     delta = (1 + U(a*)) - U(a)      // the ordinary TD error, in moves
//
//     whose fixed point is E_spawn[W_target] = W(a) — the exponential-utility
//     Bellman equation, EXACTLY. One spawn sample suffices because W enters that
//     expectation linearly. lambda -> 0 recovers plain TD(0) (exp(x)-1 -> x).
//     The asymmetry IS the risk seeking: a pleasant surprise (delta > 0) is
//     amplified exponentially, an unpleasant one saturates at -1/lambda.
//
//  2. THE SEARCH BACKUP (search.go). Chance nodes must average in W space, so
//     the leaf exponentiates once — leafW(a) = exp(lambda*(U(a) - c)) — and the
//     tree runs multiplicatively from there. A plain expectation over W IS the
//     risk-sensitive backup; no log-sum-exp is needed inside the tree.
//
// NUMERICS. exp(lambda*2600) overflows, hence the baseline c ~ 1800 (near the
// current mean): W is O(1) for a typical board, exp(-lambda*c) at death,
// exp(lambda*(2600-c)) for a record game. All of it fits float64 comfortably for
// lambda <= 0.02.
// ---------------------------------------------------------------------------

type objective struct {
	exp    bool    // false: U = E[moves]; true: U = certainty equiv. of exp utility
	lambda float64 // risk-seeking parameter, 1/moves
	cBase  float64 // baseline moves subtracted before exponentiating

	step float64 // e^lambda: what one more move multiplies W by (search, W space)
	term float64 // W of "0 moves remaining": exp(-lambda*c); 0 for the mean objective
}

// obj is process-global: one binary run trains or plays exactly one objective,
// and the value it is loaded/trained with must be the value the search backs up.
// It is set by setObjective (or by loadNet, from the file's trailer) before any
// net or searcher is created.
var obj objective

// tdClipHi bounds lambda*delta from above before exponentiating: an early,
// wildly optimistic TD error would otherwise produce a step of exp(lambda*delta)
// with no ceiling. e^2 = 7.4x amplification of a pleasant surprise is plenty of
// risk seeking and keeps Hogwild stable.
const tdClipHi = 2.0

func setObjective(useExp bool, lambda, cBase float64) {
	if !useExp {
		obj = objective{}
		return
	}
	obj = objective{
		exp: true, lambda: lambda, cBase: cBase,
		step: math.Exp(lambda),
		term: math.Exp(-lambda * cBase),
	}
}

// tdErr maps the ordinary TD error (in moves) to the update the objective wants.
// Mean: identity. Exponential: (exp(lambda*delta)-1)/lambda, the 1/W-scaled
// regression of W onto its target, expressed back in move units.
func (o objective) tdErr(delta float64) float64 {
	if !o.exp {
		return delta
	}
	x := o.lambda * delta
	if x > tdClipHi {
		x = tdClipHi
	}
	return math.Expm1(x) / o.lambda
}

// leafW converts a leaf's certainty equivalent (moves) into the W the search
// tree multiplies through. Identity for the mean objective.
func (o objective) leafW(u float64) float64 {
	if !o.exp {
		return u
	}
	return math.Exp(o.lambda * (u - o.cBase))
}

func ipow(a, b int) int {
	r := 1
	for i := 0; i < b; i++ {
		r *= a
	}
	return r
}

// basePatterns: the 8 x 6-tuple set of Jaśkowski (arXiv:1604.05085 Fig. 2) /
// TDL2048+ — 4 rectangles + 4 "hooks" covering rows, columns and 2x3 blocks.
// Cell ids are row-major, 0 = top-left.
var basePatterns = [nPat][patLen]int{
	{0, 1, 2, 3, 4, 5},   // top two rows, left 2x3-ish
	{4, 5, 6, 7, 8, 9},   // middle band
	{0, 1, 2, 4, 5, 6},   // 2x3 block, top-left
	{4, 5, 6, 8, 9, 10},  // 2x3 block, centre
	{0, 1, 2, 3, 4, 7},   // row + column hooks
	{0, 1, 5, 6, 7, 10},  // diagonal snake
	{0, 1, 2, 5, 9, 10},  // corner hook
	{0, 1, 5, 9, 13, 14}, // left column + corner
}

// symCells[p][s][i] = cell id used by pattern p under symmetry s.
var symCells [nPat][nSym][patLen]int

func initPatterns() {
	tr := func(s, r, c int) (int, int) {
		if s >= 4 { // reflect over the vertical axis first
			c = 3 - c
		}
		switch s % 4 {
		case 0:
			return r, c
		case 1: // rot90
			return c, 3 - r
		case 2:
			return 3 - r, 3 - c
		default:
			return 3 - c, r
		}
	}
	for p := 0; p < nPat; p++ {
		for s := 0; s < nSym; s++ {
			for i, cell := range basePatterns[p] {
				r, c := cell/4, cell%4
				r2, c2 := tr(s, r, c)
				symCells[p][s][i] = r2*4 + c2
			}
		}
	}
}

// net holds the weights plus, for TC-learning, the per-weight error sum E and
// absolute-error sum A. The TC step size of weight i is |E_i| / A_i in [0, 1]:
// a weight whose updates all pull the same way keeps the full learning rate, a
// weight that is pulled both ways (an aliased, over-generalised feature) has
// its rate annealed automatically. Jaśkowski reports ~1.77x over plain TD.
//
// All three arrays are written LOCK-FREE from every training worker (Hogwild):
// updates touch 64 of 14.2M float32 slots, so collisions are rare and the lost
// updates are indistinguishable from TD noise. Go's race detector would flag
// this; it is intentional and standard for TD n-tuple training.
type net struct {
	w  [nPat][]float32
	e  [nPat][]float32 // TC: signed error sum
	a  [nPat][]float32 // TC: absolute error sum
	tc bool
}

// vInit is the optimistic initial value of every board: with reward +1/move a
// fresh board really is worth ~2000 moves at target strength, and starting the
// table there (rather than at 0) makes the very first episodes push values DOWN
// off bad states instead of having to discover value from nothing. Literature
// reports optimistic init as a clear win for 2048 n-tuple TD.
const vInit = 2000.0

func newNet(tc bool) *net {
	n := &net{tc: tc}
	w0 := float32(vInit / nFeat)
	for p := 0; p < nPat; p++ {
		n.w[p] = make([]float32, patSize)
		for i := range n.w[p] {
			n.w[p][i] = w0
		}
		if tc {
			n.e[p] = make([]float32, patSize)
			n.a[p] = make([]float32, patSize)
		}
	}
	return n
}

// idx returns the feature index of pattern p under symmetry s.
func idx(board uint64, p, s int) int {
	c := &symCells[p][s]
	v := 0
	for i := 0; i < patLen; i++ {
		v = v*base + int((board>>(4*uint(c[i])))&0xF)
	}
	return v
}

func (n *net) eval(board uint64) float64 {
	// Two-phase gather: all 64 feature indices are computed first, then the
	// weights are loaded in one tight loop. Interleaving idx() with the load
	// serialized each (DRAM-bound, 57MB table) gather behind the next index
	// computation; issuing the loads back-to-back lets them overlap in the
	// memory pipeline — ~40% faster per eval. The summation order is
	// unchanged, so the result stays bit-identical (net_eval_test.go).
	var ids [nFeat]int32
	k := 0
	for p := 0; p < nPat; p++ {
		for s := 0; s < nSym; s++ {
			ids[k] = int32(idx(board, p, s))
			k++
		}
	}
	var sum float32
	k = 0
	for p := 0; p < nPat; p++ {
		w := n.w[p]
		for s := 0; s < nSym; s++ {
			sum += w[ids[k]]
			k++
		}
	}
	return float64(sum)
}

// evalMLP is the memory-level-parallel variant of eval. It is BIT-EXACT with
// eval: the float32 accumulation runs in the SAME order (pattern-major,
// symmetry-minor), so only the *scheduling* of the 64 weight loads changes, not
// the arithmetic (float add is not associative, so the order is load-bearing).
//
// Phase A computes all 64 feature indices — pure ALU / board-bit extraction with
// NO weight loads — into a stack array. Phase B is then a tight gather+sum whose
// load addresses are all already available, so an out-of-order core can issue
// many independent misses at once against the 57 MB table instead of stalling on
// one dependent miss per index computation. Phase B's inner symmetry loop is
// unrolled 8x (nSym==8) so the add chain is a flat sequence of gathers with no
// loop-counter overhead between them.
//
// It REGRESSES ~8-15% on Apple M4 Pro (its wide OoO window already overlaps the
// loads in eval), but should HELP on x86 where the premise holds. It is gated
// behind BOT2048_EVAL_MLP=1 (default OFF -> eval) so champion behaviour is
// unchanged; because it is bit-exact, even ON the search result is identical.
func (n *net) evalMLP(board uint64) float64 {
	// Phase A: 64 indices, no weight table touched.
	var ix [nFeat]int32
	for p := 0; p < nPat; p++ {
		for s := 0; s < nSym; s++ {
			ix[p*nSym+s] = int32(idx(board, p, s))
		}
	}
	// Phase B: gather + accumulate, strictly left-to-right to match eval's add
	// chain exactly. The loads are independent (all addresses precomputed above),
	// so they overlap even though the add chain is serial.
	var sum float32
	for p := 0; p < nPat; p++ {
		w := n.w[p]
		b := p * nSym
		sum += w[ix[b]]
		sum += w[ix[b+1]]
		sum += w[ix[b+2]]
		sum += w[ix[b+3]]
		sum += w[ix[b+4]]
		sum += w[ix[b+5]]
		sum += w[ix[b+6]]
		sum += w[ix[b+7]]
	}
	return float64(sum)
}

// useEvalMLP routes the search leaf (evalNonNeg) through evalMLP. It is
// DEFAULT-ON on this x86 lineage: evalMLP measured ~4-10% faster per leaf than
// the rolled two-phase eval on this box (avx2/fma, GOMAXPROCS=1), and is
// bit-identical (TestEvalMLPMatchesRef, 200k boards) so decisions are unchanged.
// BOT2048_EVAL_MLP=0 is a kill switch back to the rolled two-phase eval; any
// other value (incl. unset) uses evalMLP. Read once at process start.
var useEvalMLP = os.Getenv("BOT2048_EVAL_MLP") != "0"

// evalNonNeg is the net's value in MOVE units, clamped to >= 0 (0 remaining
// moves is the worst any position can be).
//
// This clamp is the n-tuple analogue of the farm evaluator's `farmEval >= sSum`
// invariant. It is what guarantees the engine never VOLUNTARILY builds a 2048:
// a 2048-creating move is worth exactly "the move itself, then nothing", while
// any other legal move is worth "the move itself, then U(afterstate) >= that
// nothing". An untrained/garbage negative U could otherwise make suicide look
// attractive. Under the exponential objective the clamp maps to leafW >=
// exp(-lambda*c) = the W of death, so the taboo stays a theorem there too.
func (n *net) evalNonNeg(board uint64) float64 {
	var v float64
	if useEvalMLP {
		v = n.evalMLP(board) // bit-exact with eval; x86 load-scheduling variant
	} else {
		v = n.eval(board)
	}
	if v < 0 {
		return 0
	}
	return v
}

// leafW is the value the SEARCH backs up: W = exp(lambda*(U - c)) for the
// exponential objective, U itself for the mean one.
func (n *net) leafW(board uint64) float64 { return obj.leafW(n.evalNonNeg(board)) }

// update applies the TD error `err` (target - V(board)) to the 64 active
// weights, either plain (err/64 each) or with the TC per-weight step.
func (n *net) update(board uint64, alpha, err float64) {
	d := float32(alpha * err / nFeat)
	if !n.tc {
		for p := 0; p < nPat; p++ {
			w := n.w[p]
			for s := 0; s < nSym; s++ {
				w[idx(board, p, s)] += d
			}
		}
		return
	}
	ad := d
	if ad < 0 {
		ad = -ad
	}
	for p := 0; p < nPat; p++ {
		w, e, a := n.w[p], n.e[p], n.a[p]
		for s := 0; s < nSym; s++ {
			i := idx(board, p, s)
			beta := float32(1.0)
			if a[i] > 0 {
				beta = abs32(e[i]) / a[i]
			}
			w[i] += beta * d
			e[i] += d
			a[i] += ad
		}
	}
}

func abs32(x float32) float32 {
	if x < 0 {
		return -x
	}
	return x
}

// ---------------------------------------------------------------------------
// persistence
// ---------------------------------------------------------------------------

func (n *net) save(path string) error {
	f, err := os.Create(path)
	if err != nil {
		return err
	}
	defer f.Close()
	bw := bufio.NewWriterSize(f, 1<<20)
	hdr := []int64{int64(nPat), int64(patLen), int64(base), int64(patSize)}
	for _, h := range hdr {
		if err := binary.Write(bw, binary.LittleEndian, h); err != nil {
			return err
		}
	}
	for p := 0; p < nPat; p++ {
		if err := binary.Write(bw, binary.LittleEndian, n.w[p]); err != nil {
			return err
		}
	}
	// Objective trailer. Appended AFTER the weights so that the original
	// (mean-objective) w8.bin, which has no trailer, still loads: a missing
	// trailer means "mean". A weight file is meaningless without the lambda it
	// was trained for, so it travels with the weights rather than in a flag.
	if err := binary.Write(bw, binary.LittleEndian, objMagic); err != nil {
		return err
	}
	mode := int64(0)
	if obj.exp {
		mode = 1
	}
	for _, v := range []float64{float64(mode), obj.lambda, obj.cBase} {
		if err := binary.Write(bw, binary.LittleEndian, v); err != nil {
			return err
		}
	}
	return bw.Flush()
}

const objMagic uint64 = 0x4F424A5F544C5231 // "OBJ_TLR1"

func loadNet(path string) (*net, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	br := bufio.NewReaderSize(f, 1<<20)
	var hdr [4]int64
	for i := range hdr {
		if err := binary.Read(br, binary.LittleEndian, &hdr[i]); err != nil {
			return nil, err
		}
	}
	if hdr[0] != nPat || hdr[1] != patLen || hdr[2] != base || hdr[3] != int64(patSize) {
		return nil, fmt.Errorf("weights shape %v does not match this build (%d,%d,%d,%d)",
			hdr, nPat, patLen, base, patSize)
	}
	n := &net{}
	for p := 0; p < nPat; p++ {
		n.w[p] = make([]float32, patSize)
		if err := binary.Read(br, binary.LittleEndian, n.w[p]); err != nil {
			return nil, err
		}
	}
	// Optional objective trailer; absent in the pre-existing mean weights.
	var magic uint64
	if err := binary.Read(br, binary.LittleEndian, &magic); err != nil || magic != objMagic {
		setObjective(false, 0, 0)
		return n, nil
	}
	var t [3]float64
	for i := range t {
		if err := binary.Read(br, binary.LittleEndian, &t[i]); err != nil {
			return nil, fmt.Errorf("truncated objective trailer: %w", err)
		}
	}
	setObjective(t[0] == 1, t[1], t[2])
	return n, nil
}

// ---------------------------------------------------------------------------
// carousel: a pool of late-stage restart states
//
// Jaśkowski §V-E: only a tiny fraction of episodes ever reaches the late stages,
// so the weights that decide the game there are trained on almost no data. The
// carousel fixes the sampling, not the algorithm: a share of episodes is
// restarted from a stored LATE state instead of an empty board, so the network
// actually sees the k=3 / k=4 reservoir positions where our game is decided.
//
// The pool is filled from training play itself (bucketed by the number of 1024
// tiles on the board) and grows richer as the policy improves — a bootstrap
// curriculum that needs no external expert games.
// ---------------------------------------------------------------------------

const (
	carouselBuckets = 5    // k = 0..4 1024-tiles
	carouselCap     = 8192 // states kept per bucket (ring)
)

type carousel struct {
	mu   sync.Mutex
	buf  [carouselBuckets][]uint64
	next [carouselBuckets]int
	seen [carouselBuckets]int64
}

func newCarousel() *carousel {
	c := &carousel{}
	for k := range c.buf {
		c.buf[k] = make([]uint64, 0, carouselCap)
	}
	return c
}

func (c *carousel) add(b uint64) {
	k := rank10Count(b)
	if k < 1 || k >= carouselBuckets {
		return // k=0 states are what an empty-board start already produces
	}
	c.mu.Lock()
	c.seen[k]++
	if len(c.buf[k]) < carouselCap {
		c.buf[k] = append(c.buf[k], b)
	} else {
		c.buf[k][c.next[k]] = b
		c.next[k] = (c.next[k] + 1) % carouselCap
	}
	c.mu.Unlock()
}

// sample returns a restart state, preferring the SCARCEST non-empty bucket:
// sampling uniformly over states would just reproduce the policy's own
// distribution (which is the problem), so we sample uniformly over the BUCKETS
// that have any content at all. k=4 then gets ~1/3 of all carousel restarts
// even though it is <1% of the visited states.
func (c *carousel) sample(r *rng) (uint64, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	var avail []int
	for k := 1; k < carouselBuckets; k++ {
		if len(c.buf[k]) > 0 {
			avail = append(avail, k)
		}
	}
	if len(avail) == 0 {
		return 0, false
	}
	k := avail[int(r.next()%uint64(len(avail)))]
	return c.buf[k][int(r.next()%uint64(len(c.buf[k])))], true
}

func (c *carousel) counts() [carouselBuckets]int {
	c.mu.Lock()
	defer c.mu.Unlock()
	var out [carouselBuckets]int
	for k := range c.buf {
		out[k] = len(c.buf[k])
	}
	return out
}

// ---------------------------------------------------------------------------
// misc
// ---------------------------------------------------------------------------

func countRank(b uint64, rank uint64) int {
	c := 0
	for i := 0; i < 16; i++ {
		if (b>>(4*uint(i)))&0xF == rank {
			c++
		}
	}
	return c
}

var _ = math.Inf
