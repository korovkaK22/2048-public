package main

import (
	"math"
	"math/bits"
	"os"
	"strconv"
)

// SWAR nibble bit-planes. The board is 16 x 4-bit ranks, so every predicate
// "rank >= k" / "rank == k" is a boolean function of the four bit-planes and
// can be evaluated for all 16 cells at once; the tally is then one popcount.
// This replaces the 4/8/16-iteration scalar nibble loops the farm helpers used
// to run — they sit on the hottest path in the program (farmEvaluator.Eval runs
// on every leaf of the tree, hasRank11 on every move node).
//
// The results are EXACT, not approximate: each SWAR form is the same boolean
// predicate on the same bits, and every float is either a table lookup or an
// exactly-representable small power of two, so values are bit-for-bit identical
// to the scalar editions (proven differentially in farm_test.go).
const (
	// nibbleLowBits marks bit 0 of each of the 16 nibbles — the "one flag per
	// cell" lane layout every plane below shares.
	nibbleLowBits uint64 = 0x1111111111111111
	// nibbleHighBits marks bit 3 of each nibble. rank >= 8 is exactly "bit 3
	// set", so `popcount(board & mask & nibbleHighBits)` counts big tiles.
	nibbleHighBits uint64 = 0x8888888888888888
	// rank10Splat is 0xA (rank 10) replicated into all 16 nibbles, so
	// board ^ rank10Splat turns every 1024 tile into a zero nibble.
	rank10Splat uint64 = 0xAAAAAAAAAAAAAAAA
)

// nibblePlanes splits the board into its four per-cell bit-planes, each holding
// one flag per nibble in the nibble's low bit (lane layout = nibbleLowBits).
func nibblePlanes(board uint64) (b3, b2, b1, b0 uint64) {
	return (board >> 3) & nibbleLowBits,
		(board >> 2) & nibbleLowBits,
		(board >> 1) & nibbleLowBits,
		board & nibbleLowBits
}

// Farm mode: an alternative search objective for the monobank 2048 clone,
// whose rule is NON-classic — the move that first CREATES a 2048 tile
// (rank 11) ends the game immediately, and the final score is the sum over
// every tile on the board of S(rank) = (rank-1) * 2^rank.
//
// Consequences (why farm mode exists):
//   * Growth is capped at rank 10 (1024): merging two 1024s makes a 2048 and
//     ends the game, so the classic "grow one giant tile in a corner" plan is
//     wrong here. Once a 1024 exists it can never grow, so the ONLY way to
//     raise the score is to build MORE, separated 1024 "warehouses".
//   * The game always ends eventually (board jam, or a forced 2048). The true
//     objective is therefore to maximise the EXPECTED board S-sum at that
//     stopping moment.
//
// Design:
//   * S-sum is the exact score-if-the-game-stopped-now and is the dominant,
//     non-decreasing term of the leaf evaluator (a merge raises S-sum by
//     exactly the created tile's value; a spawn adds S(1)=0 or S(2)=4).
//   * The leaf adds only NON-NEGATIVE shaping terms (free space, mergeable
//     adjacency, big tiles in corners). Keeping the shaping >= 0 guarantees
//     farmEval(board) >= sSum(board), which in turn guarantees the search
//     never VOLUNTARILY creates a 2048: any non-terminal move is worth at
//     least the "cash out now" floor, so a 2048 is only ever played when it
//     is the sole legal move (see search.go).
//   * A move that would create a rank-11 tile is a terminal cash-out whose
//     value is the S-sum of the board BEFORE the move (holdings preserved;
//     the 2048 itself is never credited). Board jam is the same cash-out.

// Farm leaf-evaluation weights, in S-units (i.e. directly comparable to the
// exact S-sum term). They shape play at the margins; the S-sum term always
// dominates a real 1024 (worth 9216). Kept intentionally well below one
// tile's S so the engine never refuses a genuine merge to chase shape.
//
// Tunable two ways: rebuild (cheap, tools/engine2048/build.sh), or — for
// sweep runs — per-process env overrides read once in initFarmTables:
//
//	BOT2048_FARM_EMPTY_W       -> farmEmptyWeight      (default 340)
//	BOT2048_FARM_MERGE_W       -> farmMergeWeight      (default 140)
//	BOT2048_FARM_CORNER_W      -> farmCornerWeight     (default 640)
//	BOT2048_FARM_EDGE_W        -> farmEdgeWeight       (default 0)
//	BOT2048_FARM_STAIR_W       -> farmStairWeight      (default 120)
//	BOT2048_FARM_SNAKE_W       -> farmSnakeWeight      (default 0.25)
//	BOT2048_FARM_SNAKE_RATIO   -> farmSnakeRatio       (default 0.85)
//	BOT2048_FARM_CORNER_RANK_W -> farmCornerRankWeight (default 160)
//	BOT2048_FARM_CONVEX_W      -> farmConvexWeight     (default 80)
//	BOT2048_FARM_LATE_EMPTY_W  -> farmLateEmptyWeight  (default 120)
//	BOT2048_FARM_LATE_GRAD_MULT-> farmLateGradMult     (default 2.0)
//	BOT2048_FARM_LATE_CORNER_RANK_W -> farmLateCornerRankWeight (default 320)
//	BOT2048_FARM_HOARD_TRIGGER -> farmHoardTrigger     (default 99 = OFF)
//
// STAGE-SWITCHING "HOARD" MODE — DISABLED BY DEFAULT (farmHoardTrigger = 99 is
// unreachable: a board holds at most 16 tiles). It was research/
// strategy_candidates.md ★1, aimed at the 4x1024 / 50k right tail, on the
// theory that a single fixed weight set cannot both survive on 2x1024 AND go
// all-in on the 3rd/4th reservoir: two 1024s parked in corners with a few
// empties is a COMFORTABLE local optimum, and the static heuristic actively
// DISCOURAGES crowding the board to assemble a 3rd reservoir (dropping empties
// 6->2 costs 4*340 eval for a +1024 sSum that lies past the search horizon).
// So once the board holds >= farmHoardTrigger 1024 tiles (rank 10), Eval would
// switch to a LATE weight set that sharply LOWERS the empty weight (340 -> 120)
// to remove the "satisfied" pull and RAISES the snake gradient (x
// farmLateGradMult) and the rank-scaled corner reward (160 -> 320).
//
// The A/B netted -27k: the aggressive late weights killed a 4x1024 game and
// forced a 2048, so the stage switch is OFF and the shipped heuristic
// (HEUR1+2) never takes it. The machinery and its env knobs are kept for future
// experiments with gentler late weights — set BOT2048_FARM_HOARD_TRIGGER to a
// reachable count (e.g. 2) to re-enable it; anything > 16 leaves it off and
// the whole stage check out of the hot leaf path (see farmHoardEnabled).
// The switch touches only LIVE scalar weights + a multiplier on the already
// non-negative snake term, so no second table set is needed and the invariant
// below is preserved (all late weights stay >= 0).
//
// STRUCTURAL TERMS (added to break the ~2x1024 / ~32k ceiling — see
// docs/research/path_to_50k.md): a snake positional gradient that orders the big
// tiles into a descending wall from one corner (the "build lane" that feeds
// the next 1024 reservoir), plus a RANK-scaled corner bonus so a 1024 parked
// in a corner is worth far more than a 256 there (the flat corner term alone
// made 256-in-corner == 1024-in-corner and saturated after two corners). The
// staircase term is enabled by default (it packed 1024|512 walls in sweeps).
//
// ENDGAME EMPTY-CONVEXITY (docs/research/path_to_50k.md §3.4): once the board
// already holds >= 2 big reservoirs (rank >= 9) AND is running low on space,
// this term rewards the CONTIGUITY of the remaining empty cells — the number
// of orthogonally-adjacent empty|empty pairs — NOT their raw count (that is
// the flat farmEmptyWeight's job, which §2.3 found is a dead lever). The
// hypothesis it encodes: a jam on two 1024s is not caused by too few empties
// but by SCATTERED empties — a set of isolated holes has no room to assemble
// the 512+512 that becomes the 3rd/4th reservoir. A COMPACT empty pocket (a
// convex blob, which maximises empty|empty adjacencies for a given count)
// keeps a build lane open toward the next 1024. The term is gated to the
// endgame (>= 2 big tiles, <= farmConvexMaxEmpty free cells) so it never
// shapes the wide-open opening; there it is exactly 0.
//
// All overrides are clamped to >= 0: the shaping terms MUST stay
// non-negative to preserve the farmEval >= sSum invariant (see below),
// which is what guarantees the engine never voluntarily builds a 2048.
// (farmSnakeRatio is additionally clamped to (0, 1] — it is a per-step decay
// of a decreasing gradient, so a value <= 0 or > 1 is meaningless.)
const (
	// farmTerminalEpsilon: tiny bias subtracted from a forced-2048 cash-out
	// so that, on exact ties, the search always prefers to keep playing.
	// Negligible vs real scores (hundreds+); never affects reported scores
	// (those are tallied by the real-rules simulator, not this value).
	farmTerminalEpsilon = 1e-3

	// rank11 is the first "forbidden" rank: creating it (a 2048 tile) ends
	// the game. No legitimate farm board ever holds rank >= 11.
	rank11 = 11
)

var (
	// farmEmptyWeight: value of one free cell (survival room). Counted once
	// per board. Empty cells are the strongest classic survival signal.
	farmEmptyWeight = 340.0
	// farmMergeWeight: value of one adjacent equal pair with rank in 1..9
	// (a ready merge that keeps chains completable). Counted per orientation
	// (rows + columns). Rank-10 pairs are excluded — rewarding two adjacent
	// 1024s would invite the fatal merge.
	farmMergeWeight = 140.0
	// farmCornerWeight: value of one big tile (rank >= 8: 256/512/1024)
	// sitting in a board corner — the "warehouse" anchor for a 1024 chain.
	farmCornerWeight = 640.0
	// farmEdgeWeight: value of one big tile (rank >= 8) on a non-corner edge
	// cell. Corners are only 4; a multi-1024 farm needs edge warehouses too.
	// Default 0 = historical behaviour (corner-only shaping).
	farmEdgeWeight = 0.0
	// farmStairWeight: value of one adjacent "staircase" pair — two big tiles
	// (both rank >= 7: 128+) whose ranks differ by exactly 1 (e.g. 1024|512).
	// Rewards packing the next warehouse-in-progress flush against an already
	// parked big tile, so finished 1024s form dense walls instead of blocking
	// the interior. Counted per orientation (rows + columns), like merges.
	// Default 120: enabled (edge/stair were the positive levers on the tail in
	// the depth-5/6 sweeps; see docs/research/path_to_50k.md §2.3).
	farmStairWeight = 120.0
	// farmSnakeWeight: overall weight of the snake positional-gradient term,
	// Sum over cells of posW[cell] * 2^rank(cell), where posW decreases along a
	// boustrophedon snake path anchored at the top-left corner. This is the
	// missing STRUCTURAL term (farm had no monotonicity/gradient at all): it
	// pulls the biggest tiles into a descending, corner-ordered wall so the
	// supply chain that feeds the next 1024 stays organised instead of jamming.
	// In S-units per unit tile-value; default 0.25 keeps a corner 1024's snake
	// contribution (~256) well below its 9216 S-sum so a real merge always wins.
	farmSnakeWeight = 0.25
	// farmSnakeRatio: per-snake-step geometric decay of posW (anchor = 1.0,
	// each later snake cell * ratio). 0.85 is a GENTLE gradient — it orders the
	// big tiles locally without demanding a single-corner monolith (farm wants
	// SEVERAL separated 1024 reservoirs, not one giant tile). Clamped to (0, 1].
	farmSnakeRatio = 0.85
	// farmCornerRankWeight: value multiplier of the RANK-scaled corner bonus,
	// Sum over the 4 corners of 2^(rank-8) for rank >= 8 (256->1, 512->2,
	// 1024->4). Unlike the flat farmCornerWeight (which paid the same 640 for a
	// 256 or a 1024 in a corner, so the engine happily squatted mid tiles in
	// the corners a future 1024 needs), this makes a 1024-in-corner worth much
	// more than a 256-in-corner, so corners get reserved for real reservoirs.
	// Default 160: a 1024 in a corner earns +640 here, a 256 only +160.
	farmCornerRankWeight = 160.0
	// farmConvexWeight: value of one orthogonally-adjacent empty|empty cell
	// pair, counted (per orientation, rows + columns) ONLY when the board is in
	// the endgame regime — >= farmConvexMinBig big tiles (rank >= farmConvexBigRank)
	// and <= farmConvexMaxEmpty free cells. This rewards keeping the last few
	// empties as a COMPACT pocket (a convex blob has the most internal
	// adjacencies) instead of scattered holes, so the engine holds a build lane
	// open toward the next reservoir rather than settling on two 1024s. Zeroes
	// out entirely outside the endgame gate. In S-units; default 80 keeps a
	// realistic endgame contribution (a compact pocket is worth a few pairs,
	// ~a few hundred) well below one 1024's 9216 S so a real merge always wins.
	farmConvexWeight = 80.0

	// --- Stage-switching "hoard" late-phase weights (★1; OFF by default) ---
	// These REPLACE their early-phase counterparts in Eval once the board holds
	// >= farmHoardTrigger 1024 tiles — which, at the default trigger of 99, never
	// happens (see farmHoardTrigger / farmHoardEnabled). They are
	// intentionally more aggressive about building a 3rd/4th reservoir at the
	// cost of survival room. All stay >= 0, so the farmEval >= sSum invariant
	// still holds in the late phase (a bigger/smaller non-negative term is still
	// non-negative), and the engine still never VOLUNTARILY builds a 2048.

	// farmLateEmptyWeight: empty-cell value in the late (hoard) phase. Much lower
	// than the early 340 so the engine stops treating "two 1024s + a few empties"
	// as a comfortable optimum and is willing to crowd the board to assemble the
	// next reservoir. This is the single biggest lever of ★1.
	farmLateEmptyWeight = 120.0
	// farmLateGradMult: multiplier applied to the snake positional-gradient term
	// in the late phase (early phase = 1.0 implicitly). > 1 pulls the fresh
	// 512->1024 harder into the corner-ordered build wall so the supply chain for
	// the 3rd/4th warehouse stays organised while empties are scarce.
	farmLateGradMult = 2.0
	// farmLateCornerRankWeight: rank-scaled corner reward in the late phase,
	// raised from the early 160 so reserving a free corner for a real 1024 (worth
	// 4x here) outweighs the lower empty pull — the engine is pushed to PLACE the
	// third reservoir rather than sit on two.
	farmLateCornerRankWeight = 320.0
)

// farmHoardTrigger is the number of 1024 tiles (rank 10) that must be on the
// board for Eval to switch to the late (hoard) weight set. Default 99 = OFF
// (the HEUR1+2 baseline). The ★1 hoard A/B netted -27k — it destroyed the tail
// (killed a 4x1024 game) and forced 2048 with these aggressive late weights, so
// the stage-switch is disabled by default. Set BOT2048_FARM_HOARD_TRIGGER=2 to
// re-enable for gentler-weight experiments; 0 keeps it always on.
var farmHoardTrigger = 99

// farmHoardEnabled is the compiled-out switch for the whole stage check: a
// board never holds more than 16 tiles, so any trigger above 16 (the default
// 99) makes hoardStageActive constantly false. Eval runs on EVERY leaf of the
// tree — millions of times per move — so the trigger is folded into this bool
// once, in initFarmTables, and the per-leaf rank10Count popcount is skipped
// entirely while the mode is off. Always set through
// setFarmHoardTrigger, never by assigning farmHoardTrigger directly.
var farmHoardEnabled = false

// setFarmHoardTrigger sets the hoard trigger and the derived enable flag
// together (they must never disagree).
func setFarmHoardTrigger(n int) {
	farmHoardTrigger = n
	farmHoardEnabled = n <= 16
}

const (
	// farmConvexBigRank: minimum rank (512) counted as a "big reservoir" when
	// deciding whether the endgame empty-convexity gate is open.
	farmConvexBigRank = 9
	// farmConvexMinBig: the gate opens only once at least this many big tiles
	// (rank >= farmConvexBigRank) sit on the board — i.e. the multi-reservoir
	// endgame the term targets (the "sitting on two 1024s" trap and the phase
	// that must break out of it).
	farmConvexMinBig = 2
	// farmConvexMaxEmpty: the gate opens only while free space is scarce
	// (<= this many empties) — the "мало місця" regime where the SHAPE of the
	// remaining empties decides whether a 3rd/4th reservoir can still be built.
	// Above it the opening/midgame has ample room and the term stays 0.
	farmConvexMaxEmpty = 6
)

// Compile-time pin: bigTileCount9 hard-codes the rank-9 predicate into its SWAR
// bit-plane form, so it is only the right tally while farmConvexBigRank == 9.
// Either of these constants goes negative (and fails to convert to uint) the
// moment the two drift apart.
const (
	_ = uint(farmConvexBigRank - 9)
	_ = uint(9 - farmConvexBigRank)
)

// farmWeightFromEnv returns the weight override from environment variable
// name, or def if the variable is unset/unparsable. Negative values are
// clamped to 0 to preserve the non-negative-shaping invariant.
func farmWeightFromEnv(name string, def float64) float64 {
	s, ok := os.LookupEnv(name)
	if !ok || s == "" {
		return def
	}
	v, err := strconv.ParseFloat(s, 64)
	if err != nil || v < 0 {
		if err != nil {
			return def
		}
		return 0
	}
	return v
}

// farmIntFromEnv returns the integer override from environment variable name,
// or def if the variable is unset/unparsable. Used for the hoard-stage
// trigger (a tile count). Negative values fall back to def; 0 is allowed
// (late phase always on).
func farmIntFromEnv(name string, def int) int {
	s, ok := os.LookupEnv(name)
	if !ok || s == "" {
		return def
	}
	v, err := strconv.Atoi(s)
	if err != nil || v < 0 {
		return def
	}
	return v
}

// farmRatioFromEnv returns the snake-decay override from environment variable
// name, clamped to the valid (0, 1] range (a decreasing gradient's per-step
// decay). Unset/unparsable/out-of-range values fall back to def.
func farmRatioFromEnv(name string, def float64) float64 {
	s, ok := os.LookupEnv(name)
	if !ok || s == "" {
		return def
	}
	v, err := strconv.ParseFloat(s, 64)
	if err != nil || v <= 0 || v > 1 {
		return def
	}
	return v
}

var (
	// sSumTable[row] = sum of S(rank) over the row's four nibbles, where
	// S(r) = (r-1)*2^r for 1 <= r <= 10 and 0 otherwise (empty, and the
	// never-legitimately-held ranks >= 11). Per-cell, so a whole-board S-sum
	// is just the four board rows summed — no transpose needed.
	sSumTable [65536]float64
	// farmMergeTable[row] = farmMergeWeight * (number of adjacent equal pairs
	// in the row with rank in 1..9). Summed over rows AND columns at eval
	// time to reward mergeable adjacency in both orientations.
	farmMergeTable [65536]float64
	// farmStairTable[row] = farmStairWeight * (number of adjacent pairs in the
	// row where both ranks are >= 7 and they differ by exactly 1). Summed over
	// rows AND columns at eval time, mirroring farmMergeTable.
	farmStairTable [65536]float64
	// farmConvexTable[row] = farmConvexWeight * (number of adjacent empty|empty
	// pairs in the row). Summed over rows AND columns at eval time (like merges
	// and stairs), then GATED to the endgame regime in Eval — this per-row table
	// is the ungated adjacency count; the gate is a whole-board condition.
	farmConvexTable [65536]float64
	// snakeRowTable[r][row] = farmSnakeWeight * Sum over the row's four cells of
	// posW[4*r+c] * 2^rank, where posW is the boustrophedon snake gradient
	// (top-left anchor). Indexed by BOARD ROW INDEX r (0=top..3=bottom) because
	// a positional weight map is not row/column symmetric — the whole-board
	// snake term is just the four board rows summed (NO transpose), since the
	// map is separable across rows.
	snakeRowTable [4][65536]float64
)

// initFarmTables precomputes the farm per-row tables. Called from initTables
// so the farm evaluator is ready whenever the classic tables are.
func initFarmTables() {
	farmEmptyWeight = farmWeightFromEnv("BOT2048_FARM_EMPTY_W", farmEmptyWeight)
	farmMergeWeight = farmWeightFromEnv("BOT2048_FARM_MERGE_W", farmMergeWeight)
	farmCornerWeight = farmWeightFromEnv("BOT2048_FARM_CORNER_W", farmCornerWeight)
	farmEdgeWeight = farmWeightFromEnv("BOT2048_FARM_EDGE_W", farmEdgeWeight)
	farmStairWeight = farmWeightFromEnv("BOT2048_FARM_STAIR_W", farmStairWeight)
	farmSnakeWeight = farmWeightFromEnv("BOT2048_FARM_SNAKE_W", farmSnakeWeight)
	farmSnakeRatio = farmRatioFromEnv("BOT2048_FARM_SNAKE_RATIO", farmSnakeRatio)
	farmCornerRankWeight = farmWeightFromEnv("BOT2048_FARM_CORNER_RANK_W", farmCornerRankWeight)
	farmConvexWeight = farmWeightFromEnv("BOT2048_FARM_CONVEX_W", farmConvexWeight)
	farmLateEmptyWeight = farmWeightFromEnv("BOT2048_FARM_LATE_EMPTY_W", farmLateEmptyWeight)
	farmLateGradMult = farmWeightFromEnv("BOT2048_FARM_LATE_GRAD_MULT", farmLateGradMult)
	farmLateCornerRankWeight = farmWeightFromEnv("BOT2048_FARM_LATE_CORNER_RANK_W", farmLateCornerRankWeight)
	setFarmHoardTrigger(farmIntFromEnv("BOT2048_FARM_HOARD_TRIGGER", farmHoardTrigger))
	initSnakeTable()
	var sByRank [16]float64
	for r := 1; r <= 10; r++ {
		sByRank[r] = float64(r-1) * float64(uint64(1)<<uint(r))
	}
	// ranks 0 and >= 11 stay 0.
	for row := 0; row < 65536; row++ {
		a := row & 0xF
		b := (row >> 4) & 0xF
		c := (row >> 8) & 0xF
		d := (row >> 12) & 0xF
		sSumTable[row] = sByRank[a] + sByRank[b] + sByRank[c] + sByRank[d]
		pairs := 0
		if a == b && a >= 1 && a <= 9 {
			pairs++
		}
		if b == c && b >= 1 && b <= 9 {
			pairs++
		}
		if c == d && c >= 1 && c <= 9 {
			pairs++
		}
		farmMergeTable[row] = farmMergeWeight * float64(pairs)
		stairs := 0
		for _, p := range [3][2]int{{a, b}, {b, c}, {c, d}} {
			lo, hi := p[0], p[1]
			if lo > hi {
				lo, hi = hi, lo
			}
			if lo >= 7 && hi-lo == 1 {
				stairs++
			}
		}
		farmStairTable[row] = farmStairWeight * float64(stairs)
		emptyPairs := 0
		if a == 0 && b == 0 {
			emptyPairs++
		}
		if b == 0 && c == 0 {
			emptyPairs++
		}
		if c == 0 && d == 0 {
			emptyPairs++
		}
		farmConvexTable[row] = farmConvexWeight * float64(emptyPairs)
	}
}

// snakeCellOrder is the boustrophedon snake priority of each cell (row-major
// cell index -> priority, 15 = anchor = top-left, 0 = tail = bottom-left).
// The path runs L->R across the top row, R->L back across the second, and so
// on, so consecutive priorities are always board-adjacent — the classic
// "snake" that a descending wall of tiles should follow.
//
//	15 14 13 12
//	 8  9 10 11
//	 7  6  5  4
//	 0  1  2  3
var snakeCellOrder = [16]int{
	15, 14, 13, 12,
	8, 9, 10, 11,
	7, 6, 5, 4,
	0, 1, 2, 3,
}

// initSnakeTable precomputes snakeRowTable from the (env-resolved) snake
// weight and decay. posW[cell] = ratio^(15-order[cell]) is the positional
// weight (anchor = 1.0, decaying along the snake); the per-cell reward is
// posW[cell] * 2^rank so bigger tiles pull harder toward the anchor order.
// Empty cells (rank 0) contribute 0.
func initSnakeTable() {
	var posW [16]float64
	for cell := 0; cell < 16; cell++ {
		posW[cell] = math.Pow(farmSnakeRatio, float64(15-snakeCellOrder[cell]))
	}
	var gByRank [16]float64
	for r := 1; r <= 15; r++ {
		gByRank[r] = float64(uint64(1) << uint(r)) // tile value 2^rank; g(0)=0
	}
	for r := 0; r < 4; r++ {
		for row := 0; row < 65536; row++ {
			s := 0.0
			for c := 0; c < 4; c++ {
				rank := (row >> uint(4*c)) & 0xF
				s += posW[4*r+c] * gByRank[rank]
			}
			snakeRowTable[r][row] = farmSnakeWeight * s
		}
	}
}

// hasRank11 reports whether any cell holds rank >= 11 (a 2048 or larger) —
// i.e. whether this board is a game-ending state. Called on every move node of
// the tree, so it is SWAR: rank >= 11 == 0b1011 is, on the nibble bit-planes,
// exactly b3 & (b2 | (b1 & b0)) — the 4-bit values with bit 3 set and (bit 2
// set, i.e. >= 12) or (bits 1 and 0 set, i.e. == 11 or 15).
func hasRank11(board uint64) bool {
	b3, b2, b1, b0 := nibblePlanes(board)
	return b3&(b2|(b1&b0)) != 0
}

// sSumBoard returns the exact monobank score of a board: the sum of S(rank)
// over all tiles. This is the value cashed out on a forced 2048 or a jam.
func sSumBoard(board uint64) float64 {
	return sSumTable[board&rowMask] +
		sSumTable[(board>>16)&rowMask] +
		sSumTable[(board>>32)&rowMask] +
		sSumTable[(board>>48)&rowMask]
}

// cornerCellMask / edgeCellMask select bit 3 of the nibbles at, respectively,
// the four corners (cells 0, 3, 12, 15) and the eight non-corner edge cells
// (1, 2, 4, 7, 8, 11, 13, 14). Since "rank >= 8" is exactly "nibble bit 3 set",
// masking the board with these and popcounting IS the big-tile tally — no loop,
// no branches.
const (
	cornerCellMask uint64 = 0x8<<0 | 0x8<<12 | 0x8<<48 | 0x8<<60
	edgeCellMask   uint64 = 0x8<<4 | 0x8<<8 | 0x8<<16 | 0x8<<28 |
		0x8<<32 | 0x8<<44 | 0x8<<52 | 0x8<<56
)

// cornerBigCount counts big tiles (rank >= 8) sitting in the four board
// corners (cells 0, 3, 12, 15).
func cornerBigCount(board uint64) int {
	return bits.OnesCount64(board & cornerCellMask)
}

// cornerRankLUT[r] = 2^(r-8) for r >= 8, else 0 — the per-corner reward of
// cornerRankSum as a 16-entry table, so the ranks 0..15 are one indexed load
// instead of a compare + variable shift + int-to-float conversion. Every value
// (1..128) is exactly representable in float64, so the summed result is
// bit-identical to computing 1<<(r-8) per corner.
var cornerRankLUT = [16]float64{8: 1, 9: 2, 10: 4, 11: 8, 12: 16, 13: 32, 14: 64, 15: 128}

// cornerRankSum returns the rank-scaled corner reward: sum over the four
// corners of 2^(rank-8) for cells holding rank >= 8 (256->1, 512->2,
// 1024->4), 0 otherwise. Multiplied by farmCornerRankWeight at eval time so a
// 1024 in a corner is rewarded far more than a 256 there (see the flat
// cornerBigCount, which paid both the same).
func cornerRankSum(board uint64) float64 {
	return cornerRankLUT[board&0xF] +
		cornerRankLUT[(board>>12)&0xF] +
		cornerRankLUT[(board>>48)&0xF] +
		cornerRankLUT[(board>>60)&0xF]
}

// bigTileCount9 counts all cells whose rank is >= 9 (farmConvexBigRank) — the
// whole-board "big reservoir" tally that opens the endgame convexity gate. SWAR
// on the nibble bit-planes: rank >= 9 == 0b1001 is exactly b3 & (b2 | b1 | b0)
// (bit 3 set, and not exactly 8).
func bigTileCount9(board uint64) int {
	b3, b2, b1, b0 := nibblePlanes(board)
	return bits.OnesCount64(b3 & (b2 | b1 | b0))
}

// rank10Count counts the 1024 tiles on the board (rank exactly 10). It is the
// stage detector for ★1: >= farmHoardTrigger of them means the board is in the
// "sitting on multiple 1024s" comfort-lock the late (hoard) weight set breaks
// out of. Eval is never called on a rank-11 board (those are terminal), so
// rank == 10 and rank >= 10 coincide here; == 10 is used for exactness.
// It is SWAR: nibble == 10 is equivalent to (nibble XOR 0xA) == 0, so XOR-ing
// the whole board with rank10Splat turns exactly the 1024 tiles into zero
// nibbles and the existing zero-nibble popcount (countEmpty) tallies them.
func rank10Count(board uint64) int {
	return countEmpty(board ^ rank10Splat)
}

// hoardStageActive reports whether the late (hoard) weight set applies: the
// mode is enabled at all (see farmHoardEnabled) and the board already holds at
// least farmHoardTrigger 1024 tiles. Eval inlines this same expression rather
// than calling it, so the rank10Count scan stays out of the hot leaf path while
// the mode is off.
func hoardStageActive(board uint64) bool {
	return farmHoardEnabled && rank10Count(board) >= farmHoardTrigger
}

// endgameConvexGate reports whether the endgame empty-convexity term applies:
// the board already holds >= farmConvexMinBig big reservoirs (rank >=
// farmConvexBigRank) and is down to <= farmConvexMaxEmpty free cells. Outside
// this regime (the wide-open opening/midgame) the convexity term is 0, so it
// never rewards emptiness where space is not the binding constraint. Eval knows
// its empty count already and uses endgameConvexGateN.
func endgameConvexGate(board uint64) bool {
	return endgameConvexGateN(board, countEmpty(board))
}

// endgameConvexGateN is endgameConvexGate for a caller that has already counted
// the board's empty cells.
// The empty count is tested FIRST: it is already in hand (Eval needs it
// anyway), it is false on the overwhelming majority of leaves (a board with
// <= 6 empties is the rare endgame), and it short-circuits the bigTileCount9
// scan away on all of them.
func endgameConvexGateN(board uint64, empty int) bool {
	return empty <= farmConvexMaxEmpty &&
		bigTileCount9(board) >= farmConvexMinBig
}

// edgeBigCount counts big tiles (rank >= 8) sitting on non-corner edge
// cells (cells 1, 2, 4, 7, 8, 11, 13, 14) — secondary warehouse slots for
// a multi-1024 farm once the corners are taken.
func edgeBigCount(board uint64) int {
	return bits.OnesCount64(board & edgeCellMask)
}

// farmEvaluator scores a post-move board by its expected final S-sum
// potential: the exact current S-sum plus non-negative shaping (free space,
// mergeable adjacency, staircase packing, a snake positional gradient, a
// flat + rank-scaled corner bonus, and an endgame empty-convexity reward).
// Because every shaping term is >= 0, farmEval(board) >= sSumBoard(board)
// always — the property the search relies on to never voluntarily create a
// 2048.
//
// If the stage-switching hoard mode is enabled (OFF by default, see
// farmHoardTrigger), a board holding >= farmHoardTrigger 1024 tiles enters the
// late ("hoard") stage: the empty and corner-rank scalar weights and the
// snake-gradient multiplier switch to their more aggressive late-phase values
// to push the 3rd/4th reservoir. The late weights are still all >= 0, so the
// invariant above is unaffected.
//
// This runs on EVERY leaf of the search tree, millions of times per move: the
// board's whole-board scans (empty count, hoard stage) are each done at most
// once, and the hoard scan not at all while the mode is off.
type farmEvaluator struct{}

func (farmEvaluator) Eval(board uint64) float64 {
	t := transpose(board)
	empty := countEmpty(board)
	shape := farmMergeTable[board&rowMask] +
		farmMergeTable[(board>>16)&rowMask] +
		farmMergeTable[(board>>32)&rowMask] +
		farmMergeTable[(board>>48)&rowMask] +
		farmMergeTable[t&rowMask] +
		farmMergeTable[(t>>16)&rowMask] +
		farmMergeTable[(t>>32)&rowMask] +
		farmMergeTable[(t>>48)&rowMask] +
		farmStairTable[board&rowMask] +
		farmStairTable[(board>>16)&rowMask] +
		farmStairTable[(board>>32)&rowMask] +
		farmStairTable[(board>>48)&rowMask] +
		farmStairTable[t&rowMask] +
		farmStairTable[(t>>16)&rowMask] +
		farmStairTable[(t>>32)&rowMask] +
		farmStairTable[(t>>48)&rowMask]
	// Snake positional gradient: separable across board rows (no transpose),
	// because it is a 2D position map, not a row/column-symmetric term.
	snake := snakeRowTable[0][board&rowMask] +
		snakeRowTable[1][(board>>16)&rowMask] +
		snakeRowTable[2][(board>>32)&rowMask] +
		snakeRowTable[3][(board>>48)&rowMask]
	// Endgame empty-convexity: the summed empty|empty adjacency (rows +
	// columns), credited only inside the endgame gate. Zero elsewhere, so it
	// never inflates the opening — see §3.4 / farmConvexWeight.
	convex := 0.0
	if endgameConvexGateN(board, empty) {
		convex = farmConvexTable[board&rowMask] +
			farmConvexTable[(board>>16)&rowMask] +
			farmConvexTable[(board>>32)&rowMask] +
			farmConvexTable[(board>>48)&rowMask] +
			farmConvexTable[t&rowMask] +
			farmConvexTable[(t>>16)&rowMask] +
			farmConvexTable[(t>>32)&rowMask] +
			farmConvexTable[(t>>48)&rowMask]
	}
	// Stage-switching "hoard" mode (★1), OFF by default: once >= farmHoardTrigger
	// 1024 tiles are on the board, replace the LIVE scalar empty/corner-rank
	// weights with their lower-empty / higher-corner late-phase counterparts and
	// amplify the snake gradient by farmLateGradMult, so the engine crowds the
	// board to build a 3rd/4th reservoir instead of settling on two 1024s. All
	// late weights are >= 0, so farmEval >= sSum still holds (no voluntary 2048).
	//
	// farmHoardEnabled short-circuits the rank10Count scan while the mode is off
	// (the default), which is the only reason this whole-board scan is affordable
	// in a leaf evaluator at all.
	emptyW := farmEmptyWeight
	cornerRankW := farmCornerRankWeight
	gradMult := 1.0
	if farmHoardEnabled && rank10Count(board) >= farmHoardTrigger {
		emptyW = farmLateEmptyWeight
		cornerRankW = farmLateCornerRankWeight
		gradMult = farmLateGradMult
	}
	return sSumBoard(board) +
		emptyW*float64(empty) +
		shape +
		snake*gradMult +
		convex +
		farmCornerWeight*float64(cornerBigCount(board)) +
		cornerRankW*cornerRankSum(board) +
		farmEdgeWeight*float64(edgeBigCount(board))
}
