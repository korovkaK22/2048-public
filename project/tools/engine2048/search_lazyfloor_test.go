package main

import (
	"math"
	"testing"
)

// scoreMoveNodeEager is the ORIGINAL farm max-node: it computed the S-sum floor
// eagerly on every node and used a -Inf sentinel + math.IsInf to detect a jam.
// It is kept here, and only here, as the reference the optimised
// scoreMoveNode must reproduce EXACTLY — the optimisation is a pure
// strength-reduction and is only allowed to be faster, never different.
func (s *searchState) scoreMoveNodeEager(board uint64, cprob float64, curdepth int32) float64 {
	moves := executeAllMoves(board)
	floor := sSumBoard(board)
	best := math.Inf(-1)
	for dir := 0; dir < 4; dir++ {
		newboard := moves[dir]
		if newboard == board {
			continue
		}
		var v float64
		if hasRank11(newboard) {
			v = floor - farmTerminalEpsilon
		} else {
			v = s.scoreTilechooseNode(newboard, cprob, curdepth+1)
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

// TestFarmMoveNodeLazyFloorIsExact: the lazy-floor / no-sentinel max node must
// return a BIT-IDENTICAL float64 to the eager one on every board, including the
// two rare paths it defers (the forced-2048 cash-out and the jam).
func TestFarmMoveNodeLazyFloorIsExact(t *testing.T) {
	boards := playoutBoards(0xC0FFEE, 400)

	// Hand-picked degenerate boards that exercise the deferred paths: a jam (no
	// legal move) and a position where every move merges two 1024s into a 2048.
	boards = append(boards,
		0x12345678_9abc1234, // dense, no two neighbours equal -> jam
		0xaa00000000000000,  // adjacent 1024s -> the only move forces a 2048
		0x000000000000aa00,
		0xa0a0000000000000,
	)

	newState := func() *searchState {
		return &searchState{
			tt:         newTTTable(),
			depthLimit: 6,
			p4:         0.1,
			eval:       tableEvaluator{},
			evalKind:   evalKindOf(tableEvaluator{}, true),
			farm:       true,
		}
	}

	for _, b := range boards {
		for _, depth := range []int32{1, 2, 3} {
			got := newState().scoreMoveNode(b, 1.0, 6-depth)
			want := newState().scoreMoveNodeEager(b, 1.0, 6-depth)

			if math.Float64bits(got) != math.Float64bits(want) {
				t.Fatalf("board %016x depth %d: lazy=%v (%016x) eager=%v (%016x)",
					b, depth, got, math.Float64bits(got), want, math.Float64bits(want))
			}
		}
	}
}
