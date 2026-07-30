package main

import (
	"bufio"
	"flag"
	"fmt"
	"math"
	"os"
	"runtime"
	"runtime/pprof"
	"sort"
	"time"
)

// ---------------------------------------------------------------------------
// perfbench: a REPEATABLE, FIXED-WORK endgame benchmark + bit-identity harness
// for the speed track. It is not part of the wire protocol; it exists to make
// every optimization measurable and every decision verifiable.
//
// Two modes:
//
//	-mode fixed  (default): run a FIXED (depth, cprob) expectimax on every
//	    corpus board with NO deadline, so the work is identical every run and
//	    nodes/sec is the pure compute-throughput signal. workers==1 uses the
//	    serial exact-float64 searcher (the bit-identity path) and prints a
//	    decision CHECKSUM + exact node/leaf counts + cache hit rate; workers>1
//	    drives the same rung through the parallel frontier pool and prints wall
//	    time only (the shared-TT path is nondeterministic by design).
//
//	-mode budget: run the real budget ladder (budgetSearch) at a fixed
//	    per-board budget and `-workers` width, and report the MEAN deepest rung
//	    reached — the "endgame depth per budget" the workers knob actually buys.
//
// The decision checksum (mode fixed, workers==1) is the bit-identity gate: two
// builds, or cache-on vs cache-off, MUST print the identical checksum.
func cmdPerfBench(args []string) {
	fs := flag.NewFlagSet("perfbench", flag.ExitOnError)
	wf := fs.String("weights", defaultWeightsPath(), "weight file for the learned leaf")
	corpusPath := fs.String("corpus", "", "path to a corpus file (one 16-hex board per line)")
	mode := fs.String("mode", "fixed", "fixed | budget")
	depth := fs.Int("depth", 16, "fixed-mode depth limit")
	cprob := fs.Float64("cprob", 1e-6, "fixed-mode cprob horizon")
	budgetMs := fs.Float64("budget", 300, "budget-mode per-board budget (ms)")
	workers := fs.Int("workers", 1, "search workers (1 = serial exact path)")
	reps := fs.Int("reps", 3, "timed repetitions; median reported")
	cache := fs.Bool("cache", true, "enable the leaf-value cache")
	cpuprofile := fs.String("cpuprofile", "", "write a CPU profile of the timed reps to this file")
	fs.Parse(args)

	initTables()
	initFarmTables()
	initPatterns()

	n, err := loadNet(*wf)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}

	boards, err := readCorpus(*corpusPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}

	leaf := evalFn(n.leafW)
	var lc *leafCache
	if *cache {
		leaf, lc = newCachedLeaf(leaf)
		countLeafCache = true
	}

	fmt.Printf("# perfbench mode=%s boards=%d depth=%d cprob=%g workers=%d cache=%v reps=%d go=%s\n",
		*mode, len(boards), *depth, *cprob, *workers, *cache, *reps, runtime.Version())

	if *cpuprofile != "" {
		f, err := os.Create(*cpuprofile)
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		defer f.Close()
		if err := pprof.StartCPUProfile(f); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		defer pprof.StopCPUProfile()
	}

	switch *mode {
	case "fixed":
		perfFixed(n, leaf, lc, boards, *depth, *cprob, *workers, *reps)
	case "budget":
		perfBudget(leaf, boards, *budgetMs, *workers, *reps)
	default:
		fmt.Fprintln(os.Stderr, "mode must be fixed or budget")
		os.Exit(2)
	}
}

// perfFixed runs the fixed-work benchmark. Serial (workers==1) is deterministic:
// it reports exact node/leaf totals and a decision checksum. Parallel reports
// wall time only.
func perfFixed(n *net, leaf evalFn, lc *leafCache, boards []uint64, depth int, cprob float64, workers, reps int) {
	type repResult struct {
		ms       float64
		nodes    uint64
		leaves   uint64
		checksum uint64
	}
	results := make([]repResult, 0, reps)

	for rep := 0; rep < reps; rep++ {
		if lc != nil {
			lc.probes.Store(0)
			lc.hits.Store(0)
		}
		var rr repResult
		t0 := time.Now()
		if workers == 1 {
			s := newSearcher(leaf, depth, 0.1)
			s.depthLimit = int32(depth)
			s.cprobThresh = cprob
			var sum uint64 = 1469598103934665603 // FNV-1a offset basis
			for _, b := range boards {
				s.beginSearch()
				dir, val := s.rootSearch(b)
				sum = fnvMix(sum, uint64(int64(dir)))
				sum = fnvMix(sum, math.Float64bits(val))
			}
			rr.nodes = s.nodes
			rr.leaves = s.leaves
			rr.checksum = sum
		} else {
			// Parallel fixed-work: one full rung per board, far deadline so it
			// always completes. Same tree as serial, split across workers.
			driver := newSearcher(leaf, depth, 0.1)
			driver.pstt = newSharedTT()
			driver.depthLimit = int32(depth)
			driver.cprobThresh = cprob
			far := time.Now().Add(time.Hour)
			for _, b := range boards {
				driver.pstt.nextEpoch()
				_, _, completed := driver.parallelRung(b, far, workers)
				if !completed {
					fmt.Fprintln(os.Stderr, "unexpected abort with far deadline")
					os.Exit(1)
				}
			}
		}
		rr.ms = float64(time.Since(t0).Nanoseconds()) / 1e6
		results = append(results, rr)
		hitInfo := ""
		if lc != nil {
			p := lc.probes.Load()
			h := lc.hits.Load()
			if p > 0 {
				hitInfo = fmt.Sprintf("  leaf-probes=%d hits=%d (%.1f%%)", p, h, 100*float64(h)/float64(p))
			}
		}
		if workers == 1 {
			fmt.Printf("  rep %d: %.1f ms  nodes=%d leaves=%d  checksum=%016x%s\n",
				rep, rr.ms, rr.nodes, rr.leaves, rr.checksum, hitInfo)
		} else {
			fmt.Printf("  rep %d: %.1f ms  (parallel w=%d)%s\n", rep, rr.ms, workers, hitInfo)
		}
	}

	sort.Slice(results, func(a, b int) bool { return results[a].ms < results[b].ms })
	med := results[len(results)/2]
	fmt.Printf("MEDIAN: %.1f ms", med.ms)
	if workers == 1 {
		nps := float64(med.nodes) / (med.ms / 1000)
		lps := float64(med.leaves) / (med.ms / 1000)
		fmt.Printf("  nodes=%d leaves=%d  nodes/s=%.3g  leaves/s=%.3g  checksum=%016x",
			med.nodes, med.leaves, nps, lps, med.checksum)
	}
	fmt.Println()
}

// perfBudget runs the real budget ladder and reports the mean deepest rung —
// the endgame horizon the given worker width buys inside the fixed budget.
func perfBudget(leaf evalFn, boards []uint64, budgetMs float64, workers, reps int) {
	type repResult struct {
		ms        float64
		meanDepth float64
	}
	results := make([]repResult, 0, reps)
	for rep := 0; rep < reps; rep++ {
		s := newSearcher(leaf, 3, 0.1)
		t0 := time.Now()
		sumDepth := 0
		for _, b := range boards {
			_, _, deepest := budgetSearch(s, b, budgetMs, workers)
			sumDepth += deepest
		}
		ms := float64(time.Since(t0).Nanoseconds()) / 1e6
		results = append(results, repResult{ms: ms, meanDepth: float64(sumDepth) / float64(len(boards))})
		fmt.Printf("  rep %d: %.1f ms  mean-deepest-rung=%.2f\n", rep, ms, results[rep].meanDepth)
	}
	sort.Slice(results, func(a, b int) bool { return results[a].meanDepth < results[b].meanDepth })
	med := results[len(results)/2]
	fmt.Printf("MEDIAN: mean-deepest-rung=%.2f  (%.1f ms, budget=%.0f ms/board, w=%d)\n",
		med.meanDepth, med.ms, budgetMs, workers)
}

func fnvMix(h, v uint64) uint64 {
	const prime = 1099511628211
	for i := 0; i < 8; i++ {
		h ^= (v >> (8 * i)) & 0xFF
		h *= prime
	}
	return h
}

func readCorpus(path string) ([]uint64, error) {
	if path == "" {
		return nil, fmt.Errorf("perfbench requires -corpus <file>")
	}
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	var boards []uint64
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		line := sc.Text()
		if len(line) == 0 {
			continue
		}
		b, err := parseBoardHex(line)
		if err != nil {
			return nil, fmt.Errorf("corpus line %q: %w", line, err)
		}
		boards = append(boards, b)
	}
	return boards, sc.Err()
}
