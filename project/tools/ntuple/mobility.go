package main

import (
	"math/bits"
	"sync"
)

// ---------------------------------------------------------------------------
// HEUR5: taboo-aware mobility — an ADDITIVE, env-gated, search-time leaf
// penalty on top of the FROZEN champion net. DEFAULT OFF (weight 0 = the
// champion leaf, bit-for-bit).
//
// Why: score ~ 19.84*moves - 2164 and the record needs ~2609 moves; we plateau
// ~2000. Death is either an ordinary jam or a FORCED 2048 (the taboo), and the
// depth-3 tree often cannot see either coming: the net's U is a smooth average
// that underweights the handful of concrete, mechanical precursors of death.
// HEUR5 subtracts, in MOVE units and only inside the cramped endgame
// (empty <= mobMaxEmpty, same gate philosophy as farmConvexMaxEmpty), a
// penalty proportional to a small "hazard" score built from exactly those
// precursors:
//
//	hazMob:   few legal non-2048 moves (3 - m for m < 3). Mobility is the
//	          direct precursor of both death modes.
//	hazCramp: <= 2 empties AND no safe merge anywhere (no adjacent equal
//	          pair of rank 1..9): nothing can be compacted, the next spawns
//	          decide everything.
//	hazTrap:  big tiles (rank >= 9, i.e. 512/1024) in the 4 interior cells —
//	          a large tile away from the rim blocks compaction lanes and can
//	          rarely be re-parked. 0.5 per tile.
//	hazForce: adjacent 1024|1024 pairs — literally one squeeze away from the
//	          game-ending merge; the taboo can only refuse it while another
//	          legal move exists, which is exactly what low mobility removes.
//	          1.5 per pair.
//
// SOFT, not HARD (the 3x-confirmed project rule: soft leaf nudges the net can
// override HELP — adj-1024 median 2067->2108; hard vetoes/risk-backups HURT —
// safety bundle 2067->1982, best 47812->42292): this is a bounded leaf-side
// subtraction the value network can always out-vote with a genuinely better
// U, never a move veto.
//
// TABOO THEOREM PRESERVED: the penalised U is clamped to >= 0, so after
// obj.leafW the leaf still satisfies leafW >= W(death) and "a 2048-creating
// move is only ever played when it is the sole legal move" stays a theorem
// (see net.evalNonNeg / search.go). Under the champion's exponential
// objective (lambda = 0.003) the penalty is applied to the certainty
// equivalent U BEFORE exponentiation, so both objectives get the same
// move-unit semantics.
//
// Activation: BOT2048_NTUPLE_MOB_W (moves per hazard unit), read once in
// cmdArena. Unset/0 = OFF = the exact champion leaf function value AND code
// path (the wrapper is not even installed), so default behaviour is
// bit-identical to the pre-HEUR5 binary.
// ---------------------------------------------------------------------------

// mobMaxEmpty gates the whole term to the cramped endgame: with > 6 empties
// mobility is never the binding constraint and the term is exactly 0, so the
// opening/midgame search is untouched (and the extra work is skipped on the
// overwhelming majority of leaves).
const mobMaxEmpty = 6

// mobInteriorLanes selects, in nibble-low-bit lane layout (see nibblePlanes),
// the 4 interior cells 5, 6, 9, 10 (row-major): lane bits 4*cell.
const mobInteriorLanes uint64 = 1<<20 | 1<<24 | 1<<36 | 1<<40

// Internal hazard mix (fixed; the single swept knob is the overall weight W).
const (
	mobTrapCoef  = 0.5
	mobForceCoef = 1.5
)

var (
	// mobSafePairTable[row] = number of adjacent equal pairs with rank 1..9 in
	// the row — merges that COMPACT the board without touching the taboo.
	mobSafePairTable [65536]uint8
	// mob1024PairTable[row] = number of adjacent rank-10 (1024) pairs in the
	// row — each is one forced squeeze away from the game-ending 2048.
	mob1024PairTable [65536]uint8

	mobTablesOnce sync.Once
)

// initMobTables builds the two per-row pair tables. Lazy (sync.Once from
// newMobLeaf) so the OFF path pays nothing, not even startup work.
func initMobTables() {
	mobTablesOnce.Do(func() {
		for row := 0; row < 65536; row++ {
			a := row & 0xF
			b := (row >> 4) & 0xF
			c := (row >> 8) & 0xF
			d := (row >> 12) & 0xF
			safe, p10 := 0, 0
			for _, p := range [3][2]int{{a, b}, {b, c}, {c, d}} {
				if p[0] == p[1] {
					if p[0] >= 1 && p[0] <= 9 {
						safe++
					}
					if p[0] == 10 {
						p10++
					}
				}
			}
			mobSafePairTable[row] = uint8(safe)
			mob1024PairTable[row] = uint8(p10)
		}
	})
}

// tabooHazard returns the dimensionless danger score of a post-move board,
// 0 outside the endgame gate. Typical hazardous values are 1..4; the leaf
// subtracts W * hazard moves.
func tabooHazard(board uint64) float64 {
	empty := countEmpty(board)
	if empty > mobMaxEmpty {
		return 0
	}
	h := 0.0

	// Mobility: legal non-2048 moves from this board.
	_, mask := legalAfterstates(board)
	if m := bits.OnesCount(uint(mask)); m < 3 {
		h += float64(3 - m)
	}

	t := transpose(board)

	// Cramped with no safe merge: nothing can be compacted.
	if empty <= 2 {
		safe := int(mobSafePairTable[board&rowMask]) +
			int(mobSafePairTable[(board>>16)&rowMask]) +
			int(mobSafePairTable[(board>>32)&rowMask]) +
			int(mobSafePairTable[(board>>48)&rowMask]) +
			int(mobSafePairTable[t&rowMask]) +
			int(mobSafePairTable[(t>>16)&rowMask]) +
			int(mobSafePairTable[(t>>32)&rowMask]) +
			int(mobSafePairTable[(t>>48)&rowMask])
		if safe == 0 {
			h += float64(3 - empty)
		}
	}

	// Big tile (rank >= 9) trapped in the interior. SWAR: rank >= 9 is
	// b3 & (b2|b1|b0) on the nibble bit-planes (same predicate as
	// bigTileCount9), masked to the 4 interior lanes.
	b3, b2, b1, b0 := nibblePlanes(board)
	if trap := bits.OnesCount64(b3 & (b2 | b1 | b0) & mobInteriorLanes); trap > 0 {
		h += mobTrapCoef * float64(trap)
	}

	// Adjacent 1024|1024 pairs (rows + columns): one squeeze from the taboo.
	p10 := int(mob1024PairTable[board&rowMask]) +
		int(mob1024PairTable[(board>>16)&rowMask]) +
		int(mob1024PairTable[(board>>32)&rowMask]) +
		int(mob1024PairTable[(board>>48)&rowMask]) +
		int(mob1024PairTable[t&rowMask]) +
		int(mob1024PairTable[(t>>16)&rowMask]) +
		int(mob1024PairTable[(t>>32)&rowMask]) +
		int(mob1024PairTable[(t>>48)&rowMask])
	if p10 > 0 {
		h += mobForceCoef * float64(p10)
	}
	return h
}

// newMobLeaf wraps the champion net leaf with the taboo-mobility penalty:
// U' = max(0, U - w*hazard), then the objective's usual leaf transform
// (identity for the mean net, exp(lambda*(U'-c)) for the exponential one).
// The clamp is what keeps the taboo a theorem — see the header comment.
func newMobLeaf(n *net, w float64) evalFn {
	initMobTables()
	return func(b uint64) float64 {
		u := n.evalNonNeg(b)
		if h := tabooHazard(b); h > 0 {
			u -= w * h
			if u < 0 {
				u = 0
			}
		}
		return obj.leafW(u)
	}
}
