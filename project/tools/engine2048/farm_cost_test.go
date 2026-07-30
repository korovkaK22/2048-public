package main

import (
	"fmt"
	"testing"
	"time"
)

// TestTermCost prices the terms with the arms INTERLEAVED round by round and
// scored by their MINIMUM round time: `go test -bench` runs each arm in a
// block, and on this machine the CPU heats up enough over a block that the
// free (table-folded) quad term "cost" 12%. Minimum-of-interleaved-rounds is
// immune to that.
func TestTermCost(t *testing.T) {
	if testing.Short() {
		t.Skip("cost measurement: slow by construction (9 interleaved rounds)")
	}
	boards := getBenchBoards()
	savedM, savedQ, savedC := farmMobilityWeight, farmQuadCornerWeight, farmEmptyCurveWeight
	defer func() {
		setFarmMobilityWeight(savedM)
		setFarmEmptyCurveWeight(savedC)
		farmQuadCornerWeight = savedQ
		initSnakeTable()
	}()
	arms := []struct {
		name    string
		m, q, c float64
	}{
		{"off", 0, 0, 0},
		{"mobility", 300, 0, 0},
		{"quad", 0, 25, 0},
		{"curve", 0, 0, 1},
		{"all", 300, 25, 1},
	}
	best := make([]time.Duration, len(arms))
	for i := range best {
		best[i] = time.Hour
	}
	const rounds, per = 7, 20
	for r := 0; r < rounds; r++ {
		for i, arm := range arms {
			setFarmMobilityWeight(arm.m)
			setFarmEmptyCurveWeight(arm.c)
			farmQuadCornerWeight = arm.q
			initSnakeTable()
			start := time.Now()
			for k := 0; k < per; k++ {
				search(boards[k%len(boards)], 6, 0.1, true, tableEvaluator{}, true)
			}
			if d := time.Since(start); d < best[i] {
				best[i] = d
			}
		}
	}
	base := float64(best[0])
	for i, arm := range arms {
		fmt.Printf("%-10s %8.2f ms/search   %+6.1f%%\n",
			arm.name, float64(best[i].Milliseconds())/per, 100*(float64(best[i])/base-1))
	}
}
