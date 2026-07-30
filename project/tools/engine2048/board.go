// Package main implements a fast 2048 expectimax engine, ported from
// nneonneo/2048-ai (https://github.com/nneonneo/2048-ai, MIT license,
// see LICENSE-2048-ai).
//
// Bitboard layout (identical to bot2048.core.engine._pack):
// cell i (row-major, 0 = top-left) occupies bits 4*i..4*i+3 of a uint64,
// so row 0 is the least-significant 16 bits and, within a row, column 0
// is the least-significant nibble. A cell value k means tile 2^k; k=0 is
// empty; k is capped at 15 (two rank-15 tiles never merge).
package main

import "math/bits"

const (
	rowMask uint64 = 0xFFFF
	colMask uint64 = 0x000F000F000F000F

	maxRank = 15
)

// Direction ints match bot2048.core.types.Direction (stable, stored in DB).
const (
	dirUp    = 0
	dirRight = 1
	dirDown  = 2
	dirLeft  = 3
)

var (
	// rowLeftTable[row] = row XOR result_of_left_move(row) (16-bit diff).
	rowLeftTable [65536]uint16
	// rowRightTable[row] = row XOR result_of_right_move(row).
	rowRightTable [65536]uint16
	// colUpTable[row] = unpackCol(rowLeftTable[row]): diff for moving a
	// column (given as a row of the transposed board) toward row 0.
	colUpTable [65536]uint64
	// colDownTable[row] = unpackCol(rowRightTable[row]).
	colDownTable [65536]uint64
	// scoreTable[row] = game score of a left move on that row
	// (sum of 1<<(rank+1) per merge). Used only by the "move" command.
	scoreTable [65536]float64
	// heurTable[row] = static heuristic contribution of one row/column.
	heurTable [65536]float64
)

// transpose swaps rows and columns of the 4x4 nibble matrix.
// Ported verbatim from nneonneo's transpose (same magic masks as
// bot2048.core.engine._transpose).
func transpose(x uint64) uint64 {
	a1 := x & 0xF0F00F0FF0F00F0F
	a2 := x & 0x0000F0F00000F0F0
	a3 := x & 0x0F0F00000F0F0000
	a := a1 | (a2 << 12) | (a3 >> 12)
	b1 := a & 0xFF00FF0000FF00FF
	b2 := a & 0x00FF00FF00000000
	b3 := a & 0x00000000FF00FF00
	return b1 | (b2 >> 24) | (b3 << 24)
}

// reverseRow reverses the 4 nibbles of a 16-bit row.
func reverseRow(row uint16) uint16 {
	return (row >> 12) | ((row >> 4) & 0x00F0) | ((row << 4) & 0x0F00) | (row << 12)
}

// unpackCol spreads the 4 nibbles of a 16-bit value to bits 0, 16, 32, 48,
// i.e. to the cells of column 0 (nneonneo style).
func unpackCol(row uint16) uint64 {
	t := uint64(row)
	return (t | (t << 12) | (t << 24) | (t << 36)) & colMask
}

// mergeRowLeft is the reference merge of one row toward index 0, matching
// bot2048.core.engine._merge_row_left exactly: tiles slide toward the move
// edge, each pair merges at most once, merges resolve from the leading
// edge, rank-15 tiles never merge, merged rank = rank+1,
// score += 1<<(rank+1). Used only to build tables.
func mergeRowLeft(line [4]uint16) (result [4]uint16, score float64) {
	var tiles []uint16
	for _, c := range line {
		if c != 0 {
			tiles = append(tiles, c)
		}
	}
	n := 0
	for i := 0; i < len(tiles); {
		if i+1 < len(tiles) && tiles[i] == tiles[i+1] && tiles[i] < maxRank {
			result[n] = tiles[i] + 1
			score += float64(uint64(1) << (tiles[i] + 1))
			i += 2
		} else {
			result[n] = tiles[i]
			i++
		}
		n++
	}
	return result, score
}

// initTables precomputes all move/score/heuristic tables. Must be called
// once before any other function in this package is used.
func initTables() {
	for row := 0; row < 65536; row++ {
		var line [4]uint16
		for i := 0; i < 4; i++ {
			line[i] = uint16((row >> (4 * i)) & 0xF)
		}
		merged, score := mergeRowLeft(line)
		result := merged[0] | merged[1]<<4 | merged[2]<<8 | merged[3]<<12
		rowLeftTable[row] = uint16(row) ^ result
		scoreTable[row] = score
		heurTable[row] = heurRow(line)
	}
	for row := 0; row < 65536; row++ {
		rev := reverseRow(uint16(row))
		// reverse(rev ^ left(rev)) == row ^ right(row): reverse is a nibble
		// permutation, so it commutes with XOR.
		rowRightTable[row] = reverseRow(rowLeftTable[rev])
		colUpTable[row] = unpackCol(rowLeftTable[row])
		colDownTable[row] = unpackCol(rowRightTable[row])
	}
	initFarmTables()
	initSearchParams()
}

// executeMove applies one move (no spawn). Returns the board unchanged if
// the move does not slide or merge anything (illegal move).
func executeMove(board uint64, dir int) uint64 {
	ret := board
	switch dir {
	case dirLeft:
		ret ^= uint64(rowLeftTable[board&rowMask])
		ret ^= uint64(rowLeftTable[(board>>16)&rowMask]) << 16
		ret ^= uint64(rowLeftTable[(board>>32)&rowMask]) << 32
		ret ^= uint64(rowLeftTable[(board>>48)&rowMask]) << 48
	case dirRight:
		ret ^= uint64(rowRightTable[board&rowMask])
		ret ^= uint64(rowRightTable[(board>>16)&rowMask]) << 16
		ret ^= uint64(rowRightTable[(board>>32)&rowMask]) << 32
		ret ^= uint64(rowRightTable[(board>>48)&rowMask]) << 48
	case dirUp:
		t := transpose(board)
		ret ^= colUpTable[t&rowMask]
		ret ^= colUpTable[(t>>16)&rowMask] << 4
		ret ^= colUpTable[(t>>32)&rowMask] << 8
		ret ^= colUpTable[(t>>48)&rowMask] << 12
	case dirDown:
		t := transpose(board)
		ret ^= colDownTable[t&rowMask]
		ret ^= colDownTable[(t>>16)&rowMask] << 4
		ret ^= colDownTable[(t>>32)&rowMask] << 8
		ret ^= colDownTable[(t>>48)&rowMask] << 12
	}
	return ret
}

// moveScore returns the game score gained by one move (sum of the values
// of the tiles created by merges). Exact integer, held in a float64.
func moveScore(board uint64, dir int) float64 {
	b := board
	if dir == dirUp || dir == dirDown {
		b = transpose(board)
	}
	rev := dir == dirRight || dir == dirDown
	score := 0.0
	for r := 0; r < 4; r++ {
		row := uint16(b >> (16 * r))
		if rev {
			row = reverseRow(row)
		}
		score += scoreTable[row]
	}
	return score
}

// executeAllMoves computes all four move results at once, sharing a single
// transpose between UP and DOWN (executeMove would compute it twice). The
// result is indexed by the Direction ints; an illegal move returns the
// board unchanged, exactly like executeMove.
func executeAllMoves(board uint64) [4]uint64 {
	t := transpose(board)
	up := board ^
		colUpTable[t&rowMask] ^
		colUpTable[(t>>16)&rowMask]<<4 ^
		colUpTable[(t>>32)&rowMask]<<8 ^
		colUpTable[(t>>48)&rowMask]<<12
	down := board ^
		colDownTable[t&rowMask] ^
		colDownTable[(t>>16)&rowMask]<<4 ^
		colDownTable[(t>>32)&rowMask]<<8 ^
		colDownTable[(t>>48)&rowMask]<<12
	left := board ^
		uint64(rowLeftTable[board&rowMask]) ^
		uint64(rowLeftTable[(board>>16)&rowMask])<<16 ^
		uint64(rowLeftTable[(board>>32)&rowMask])<<32 ^
		uint64(rowLeftTable[(board>>48)&rowMask])<<48
	right := board ^
		uint64(rowRightTable[board&rowMask]) ^
		uint64(rowRightTable[(board>>16)&rowMask])<<16 ^
		uint64(rowRightTable[(board>>32)&rowMask])<<32 ^
		uint64(rowRightTable[(board>>48)&rowMask])<<48
	return [4]uint64{dirUp: up, dirRight: right, dirDown: down, dirLeft: left}
}

// countEmpty returns the number of empty cells (branchless SWAR, per
// nneonneo's count_empty): bit 0 of each nibble ends up set iff any bit of
// that nibble is set, then the zero nibbles are popcounted.
func countEmpty(board uint64) int {
	x := board
	x |= (x >> 2) & 0x3333333333333333
	x |= x >> 1
	empty := ^x & 0x1111111111111111
	return bits.OnesCount64(empty)
}

// countDistinctTiles returns the number of distinct nonzero ranks present
// (nneonneo's count_distinct_tiles).
func countDistinctTiles(board uint64) int {
	var bitset uint16
	for b := board; b != 0; b >>= 4 {
		bitset |= 1 << (b & 0xF)
	}
	bitset >>= 1 // don't count empty
	count := 0
	for bitset != 0 {
		bitset &= bitset - 1
		count++
	}
	return count
}
