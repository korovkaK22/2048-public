package main

// Regression coverage for the fused-gather eval in net.go: evalRef is the
// original interleaved implementation, kept here as the reference oracle.
// eval must stay bit-identical to it (same summation order), only faster.

import (
	"math/rand"
	"testing"
)

func benchNet(tb testing.TB) *net {
	initTables()
	initFarmTables()
	initPatterns()
	n, err := loadNet("run/w-l0.003.bin")
	if err != nil {
		tb.Skipf("champion weights not present: %v", err)
	}
	return n
}

// evalRef: the pre-fusion eval — idx() interleaved with each weight load.
func (n *net) evalRef(board uint64) float64 {
	var sum float32
	for p := 0; p < nPat; p++ {
		w := n.w[p]
		for s := 0; s < nSym; s++ {
			sum += w[idx(board, p, s)]
		}
	}
	return float64(sum)
}

// randBoards: cells hold ranks 0..10 (base-1), matching the trained domain.
func randBoards(count int, seed int64) []uint64 {
	rng := rand.New(rand.NewSource(seed))
	out := make([]uint64, count)
	for i := range out {
		var bb uint64
		for j := 0; j < 16; j++ {
			r := rng.Intn(2 * base)
			if r >= base { // ~half the cells empty, like real boards
				r = 0
			}
			bb |= uint64(r) << (4 * j)
		}
		out[i] = bb
	}
	return out
}

func TestEvalMatchesRef(t *testing.T) {
	n := benchNet(t)
	for _, bb := range randBoards(10000, 42) {
		if a, b := n.eval(bb), n.evalRef(bb); a != b {
			t.Fatalf("mismatch on %016x: eval=%v ref=%v", bb, a, b)
		}
	}
}

// TestEvalMLPMatchesRef proves the memory-level-parallel leaf is bit-identical
// to the reference add-chain over 200k random boards. Any single differing bit
// fails the build, so BOT2048_EVAL_MLP=1 cannot silently alter a decision.
func TestEvalMLPMatchesRef(t *testing.T) {
	n := benchNet(t)
	for _, bb := range randBoards(200000, 42) {
		mlp, ref, ev := n.evalMLP(bb), n.evalRef(bb), n.eval(bb)
		if mlp != ref || mlp != ev {
			t.Fatalf("mismatch on %016x: evalMLP=%v ref=%v eval=%v", bb, mlp, ref, ev)
		}
	}
}

var benchSink float64

func BenchmarkEvalRef(b *testing.B) {
	n := benchNet(b)
	boards := randBoards(4096, 1)
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		benchSink += n.evalRef(boards[i&4095])
	}
}

func BenchmarkEval(b *testing.B) {
	n := benchNet(b)
	boards := randBoards(4096, 1)
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		benchSink += n.eval(boards[i&4095])
	}
}

func BenchmarkEvalMLP(b *testing.B) {
	n := benchNet(b)
	boards := randBoards(4096, 1)
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		benchSink += n.evalMLP(boards[i&4095])
	}
}
