package main

import "math"

// Evaluator scores a post-move board position (higher is better).
//
// The only current implementation is the nneonneo table heuristic below.
// The intended future second implementation is an n-tuple network
// evaluator (TDL2048+ / AutoPlay2048 style) — slot it in here without
// touching the search.
type Evaluator interface {
	Eval(board uint64) float64
}

// Heuristic weights, per nneonneo/2048-ai score_heur_board tables.
const (
	heurLostPenalty        = 200000.0
	heurEmptyWeight        = 270.0
	heurMergesWeight       = 700.0
	heurMonotonicityPower  = 4.0
	heurMonotonicityWeight = 47.0
	heurSumPower           = 3.5
	heurSumWeight          = 11.0
)

// heurRow computes the heuristic contribution of a single row/column with
// ranks line[0..3]. math.Pow runs only here, at table build time; the hot
// loop does table lookups only.
func heurRow(line [4]uint16) float64 {
	sum := 0.0
	empty := 0
	merges := 0

	prev := 0
	counter := 0
	for i := 0; i < 4; i++ {
		rank := int(line[i])
		sum += math.Pow(float64(rank), heurSumPower)
		if rank != 0 {
			if prev == rank {
				counter++
			} else if counter > 0 {
				merges += 1 + counter
				counter = 0
			}
			prev = rank
		} else {
			empty++
			prev = 0
		}
	}
	if counter > 0 {
		merges += 1 + counter
	}

	monoL := 0.0
	monoR := 0.0
	for i := 1; i < 4; i++ {
		if line[i-1] > line[i] {
			monoL += math.Pow(float64(line[i-1]), heurMonotonicityPower) -
				math.Pow(float64(line[i]), heurMonotonicityPower)
		} else {
			monoR += math.Pow(float64(line[i]), heurMonotonicityPower) -
				math.Pow(float64(line[i-1]), heurMonotonicityPower)
		}
	}

	return heurLostPenalty +
		heurEmptyWeight*float64(empty) +
		heurMergesWeight*float64(merges) -
		heurMonotonicityWeight*math.Min(monoL, monoR) -
		heurSumWeight*sum
}

// tableEvaluator is the nneonneo heuristic: sum of heurTable over the four
// rows of the board plus the four rows of its transpose (i.e. columns).
type tableEvaluator struct{}

func (tableEvaluator) Eval(board uint64) float64 {
	t := transpose(board)
	return heurTable[board&rowMask] +
		heurTable[(board>>16)&rowMask] +
		heurTable[(board>>32)&rowMask] +
		heurTable[(board>>48)&rowMask] +
		heurTable[t&rowMask] +
		heurTable[(t>>16)&rowMask] +
		heurTable[(t>>32)&rowMask] +
		heurTable[(t>>48)&rowMask]
}
