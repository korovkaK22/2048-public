// Command ntuple trains an n-tuple value network for the monobank 2048 variant
// (creating a 2048 ENDS the game) with TD(0)/TC on afterstates, target =
// expected REMAINING MOVES, and plugs the result into expectimax as the leaf
// evaluator.
//
// Subcommands:
//
//	train  -- Hogwild-parallel TD(0)/TC self-play, optional carousel restarts
//	probe  -- Spearman(V, measured death-risk table), including the k=4 row
//	bench  -- ns/leaf-eval and nodes/s for the learned vs the handwritten leaf
//	arena  -- play games with expectimax at a given depth, emit arena jsonl
//	serve  -- persistent stdin engine speaking the engine2048 line protocol,
//	          so the live runner (go_engine.py) can play the learned net
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"sync"
	"sync/atomic"
	"time"
)

// ---------------------------------------------------------------------------
// game rules (2048-taboo variant)
// ---------------------------------------------------------------------------

// legalAfterstates returns the afterstate for each legal move. A move that
// creates a 2048 is LEGAL (it can be forced) but ends the game; it is excluded
// here because the greedy/training players treat it as terminal-with-value-0.
func legalAfterstates(b uint64) (out [4]uint64, mask int) {
	all := executeAllMoves(b)
	for d := 0; d < 4; d++ {
		if all[d] != b && !hasRank11(all[d]) {
			out[d] = all[d]
			mask |= 1 << d
		}
	}
	return
}

type rng struct{ s uint64 }

func (r *rng) next() uint64 {
	r.s ^= r.s << 13
	r.s ^= r.s >> 7
	r.s ^= r.s << 17
	return r.s
}

func spawn(b uint64, r *rng) uint64 {
	e := countEmpty(b)
	if e == 0 {
		return b
	}
	k := int(r.next() % uint64(e))
	rank := uint64(1)
	if r.next()%10 == 0 {
		rank = 2
	}
	for i := 0; i < 16; i++ {
		if (b>>(4*uint(i)))&0xF == 0 {
			if k == 0 {
				return b | rank<<(4*uint(i))
			}
			k--
		}
	}
	return b
}

func initBoard(r *rng) uint64 { return spawn(spawn(0, r), r) }

type evalFn func(uint64) float64

// ---------------------------------------------------------------------------
// training
// ---------------------------------------------------------------------------

// playTrain plays one self-play episode (1-ply greedy on V, TD(0) on
// afterstates) and updates the shared weights in place. `start` is the initial
// board — an empty-board start for a normal episode, a stored late state for a
// carousel restart.
func playTrain(n *net, r *rng, alpha float64, car *carousel, start uint64) int {
	b := start
	moves := 0
	var prevAfter uint64
	havePrev := false
	for {
		after, mask := legalAfterstates(b)
		if mask == 0 {
			if havePrev {
				// terminal: 0 remaining moves.
				n.update(prevAfter, alpha, obj.tdErr(0-n.eval(prevAfter)))
			}
			return moves
		}
		best := -1
		bestV := math.Inf(-1)
		for d := 0; d < 4; d++ {
			if mask&(1<<d) == 0 {
				continue
			}
			if v := n.eval(after[d]); v > bestV {
				bestV, best = v, d
			}
		}
		if havePrev {
			// TD(0) on afterstates over the single sampled spawn. delta is the
			// ordinary move-units TD error; obj.tdErr bends it into the
			// risk-seeking step (identity when lambda = 0). The greedy action is
			// argmax over U, which is argmax over W too (exp is increasing), so the
			// behaviour policy needs no change — only the target does.
			n.update(prevAfter, alpha, obj.tdErr(1+bestV-n.eval(prevAfter)))
		}
		prevAfter = after[best]
		havePrev = true
		moves++
		b = spawn(after[best], r)
		// Feed the carousel: 1-in-32 of the states with a 1024 on the board.
		if car != nil && r.next()&31 == 0 {
			car.add(b)
		}
	}
}

type trainStat struct {
	moves int
	fresh bool // started from an empty board (the only comparable episodes)
	maxK  int
}

func cmdTrain(args []string) {
	fs := flag.NewFlagSet("train", flag.ExitOnError)
	games := fs.Int("games", 5000000, "training episodes")
	minutes := fs.Float64("minutes", 40, "wall-clock cap")
	alpha := fs.Float64("alpha", 0.1, "learning rate (divided by 64 features)")
	workers := fs.Int("workers", 8, "goroutines (Hogwild)")
	tc := fs.Bool("tc", true, "temporal-coherence adaptive step")
	carouselP := fs.Float64("carousel", 0.2, "fraction of episodes restarted from a late state")
	out := fs.String("out", "weights.bin", "weight file")
	logEvery := fs.Int("logevery", 100000, "log median moves every N fresh episodes")
	lambda := fs.Float64("lambda", 0, "risk-seeking exponential utility: V=E[exp(lambda*(moves-base))]; 0 = mean objective")
	cbase := fs.Float64("base", 1800, "baseline moves subtracted before exp (numerical anchor)")
	initW := fs.String("init", "", "warm-start from this weight file (curriculum / re-anneal)")
	fs.Parse(args)

	initTables()
	initFarmTables()
	initPatterns()
	runtime.GOMAXPROCS(*workers)

	setObjective(*lambda > 0, *lambda, *cbase)

	var n *net
	if *initW != "" {
		// Warm start: load, then re-attach TC accumulators. loadNet overwrites the
		// objective from the file's trailer, so re-assert ours afterwards — a
		// warm start across objectives (mean -> exp) is exactly what we want to be
		// able to do.
		ln, err := loadNet(*initW)
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		setObjective(*lambda > 0, *lambda, *cbase)
		n = ln
		n.tc = *tc
		if *tc {
			for p := 0; p < nPat; p++ {
				n.e[p] = make([]float32, patSize)
				n.a[p] = make([]float32, patSize)
			}
		}
	} else {
		n = newNet(*tc)
	}
	var car *carousel
	if *carouselP > 0 {
		car = newCarousel()
	}
	fmt.Printf("# 8x6-tuple, %d weights (%.0f MB), tc=%v, carousel=%.2f, alpha=%.3f, workers=%d\n",
		nPat*patSize, float64(nPat*patSize*4)/1e6, *tc, *carouselP, *alpha, *workers)
	if obj.exp {
		fmt.Printf("# objective EXP(risk-seeking): lambda=%.4f base=%.0f  step=e^l=%.6f  W(death)=%.3g\n",
			obj.lambda, obj.cBase, obj.step, obj.term)
	} else {
		fmt.Printf("# objective MEAN: V = E[remaining moves]\n")
	}
	if *initW != "" {
		fmt.Println("# warm start from", *initW)
	}

	var done int64
	ch := make(chan trainStat, 4096)
	start := time.Now()
	deadline := start.Add(time.Duration(*minutes * float64(time.Minute)))

	var wg sync.WaitGroup
	for w := 0; w < *workers; w++ {
		wg.Add(1)
		go func(seed uint64) {
			defer wg.Done()
			r := &rng{s: seed*0x9E3779B97F4A7C15 + 12345}
			for {
				if atomic.AddInt64(&done, 1) > int64(*games) {
					return
				}
				if time.Now().After(deadline) {
					return
				}
				b := initBoard(r)
				fresh := true
				if car != nil && float64(r.next()%1000)/1000.0 < *carouselP {
					if s, ok := car.sample(r); ok {
						b, fresh = s, false
					}
				}
				m := playTrain(n, r, *alpha, car, b)
				ch <- trainStat{moves: m, fresh: fresh}
			}
		}(uint64(w) + 1)
	}
	go func() { wg.Wait(); close(ch) }()

	var win []int
	freshCount := 0
	total := 0
	fmt.Println("# fresh_games  elapsed_s  median_moves  mean_moves  carousel_pool[k=1..4]")
	for st := range ch {
		total++
		if !st.fresh {
			continue
		}
		freshCount++
		win = append(win, st.moves)
		if freshCount%*logEvery == 0 {
			sum := 0
			for _, m := range win {
				sum += m
			}
			pool := [carouselBuckets]int{}
			if car != nil {
				pool = car.counts()
			}
			fmt.Printf("%9d  %9.1f  %12.0f  %10.0f  %d/%d/%d/%d\n",
				freshCount, time.Since(start).Seconds(), median(win),
				float64(sum)/float64(len(win)), pool[1], pool[2], pool[3], pool[4])
			os.Stdout.Sync()
			win = win[:0]
			if err := n.save(*out); err != nil {
				fmt.Fprintln(os.Stderr, "save:", err)
			}
		}
	}
	el := time.Since(start).Seconds()
	fmt.Printf("\n== %d episodes (%d fresh) in %.1f s on %d workers = %.0f eps/s\n",
		total, freshCount, el, *workers, float64(total)/el)
	if err := n.save(*out); err != nil {
		fmt.Fprintln(os.Stderr, "save:", err)
		os.Exit(1)
	}
	fmt.Println("saved", *out)
}

// ---------------------------------------------------------------------------
// probe: does V know about the k=4 endgame?
// ---------------------------------------------------------------------------

// risk: measured P(death <= 100 moves | k 1024-reservoirs, e empty cells).
var risk = map[[2]int]float64{
	{2, 0}: 0.48, {2, 1}: 0.26, {2, 2}: 0.09, {2, 3}: 0.04,
	{3, 0}: 0.57, {3, 1}: 0.36, {3, 2}: 0.19, {3, 3}: 0.10,
	{4, 0}: 0.78, {4, 1}: 0.58, {4, 2}: 0.51, {4, 3}: 0.39,
}

type cellStat struct {
	sumV [2]float64
	n    int
}

// collectCells plays games with policy `pol` and averages the two value
// functions in g over the (k, e) cells of the risk table.
func collectCells(pol func(uint64, *rng) int, g [2]evalFn, games int, seed uint64,
	stats map[[2]int]*cellStat, mu *sync.Mutex) {
	r := &rng{s: seed}
	for gi := 0; gi < games; gi++ {
		b := initBoard(r)
		for {
			_, mask := legalAfterstates(b)
			if mask == 0 {
				break
			}
			k := countRank(b, 10)
			e := countEmpty(b)
			if k >= 2 && k <= 4 && e <= 3 {
				v0, v1 := g[0](b), g[1](b)
				mu.Lock()
				key := [2]int{k, e}
				st := stats[key]
				if st == nil {
					st = &cellStat{}
					stats[key] = st
				}
				st.sumV[0] += v0
				st.sumV[1] += v1
				st.n++
				mu.Unlock()
			}
			d := pol(b, r)
			if d < 0 {
				break
			}
			b = spawn(executeAllMoves(b)[d], r)
		}
	}
}

func spearman(x, y []float64) float64 {
	rank := func(v []float64) []float64 {
		type pr struct {
			val float64
			i   int
		}
		ps := make([]pr, len(v))
		for i, a := range v {
			ps[i] = pr{a, i}
		}
		sort.Slice(ps, func(a, b int) bool { return ps[a].val < ps[b].val })
		out := make([]float64, len(v))
		for i := 0; i < len(ps); {
			j := i
			for j+1 < len(ps) && ps[j+1].val == ps[i].val {
				j++
			}
			avg := float64(i+j)/2 + 1
			for k := i; k <= j; k++ {
				out[ps[k].i] = avg
			}
			i = j + 1
		}
		return out
	}
	rx, ry := rank(x), rank(y)
	var mx, my float64
	for i := range rx {
		mx += rx[i]
		my += ry[i]
	}
	mx /= float64(len(rx))
	my /= float64(len(ry))
	var num, dx, dy float64
	for i := range rx {
		a, b := rx[i]-mx, ry[i]-my
		num += a * b
		dx += a * a
		dy += b * b
	}
	return num / math.Sqrt(dx*dy)
}

func median(v []int) float64 {
	s := append([]int(nil), v...)
	sort.Ints(s)
	n := len(s)
	if n == 0 {
		return 0
	}
	if n%2 == 1 {
		return float64(s[n/2])
	}
	return float64(s[n/2-1]+s[n/2]) / 2
}

// greedyPolicy is 1-ply argmax over an evaluator.
func greedyPolicy(f evalFn) func(uint64, *rng) int {
	return func(b uint64, _ *rng) int {
		after, mask := legalAfterstates(b)
		best, bestV := -1, math.Inf(-1)
		for d := 0; d < 4; d++ {
			if mask&(1<<d) == 0 {
				continue
			}
			if v := f(after[d]); v > bestV {
				bestV, best = v, d
			}
		}
		return best
	}
}

func cmdProbe(args []string) {
	fs := flag.NewFlagSet("probe", flag.ExitOnError)
	wf := fs.String("weights", "weights.bin", "weight file")
	games := fs.Int("games", 200, "games per sampling policy")
	depth := fs.Int("depth", 3, "expectimax depth of the deep sampling policies")
	workers := fs.Int("workers", 8, "parallel sampling workers")
	fs.Parse(args)

	initTables()
	initFarmTables()
	initPatterns()
	runtime.GOMAXPROCS(*workers)

	n, err := loadNet(*wf)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	learned := func(b uint64) float64 { return n.eval(b) }
	farm := farmEvaluator{}.Eval

	// Three sampling policies. The learned 1-ply policy is the go/no-go's own
	// state distribution (and the one that never reached k=4); the deep learned
	// and deep FARM policies are added precisely to FORCE k=4 states into the
	// sample — the farm policy is the production one, which is known to build 4
	// reservoirs, so it supplies genuine, reachable k=4 boards that the learned
	// V has to be scored on whether it likes them or not.
	// searchPolicy keeps one searcher per sampling goroutine (keyed by its rng),
	// so the transposition table stays warm across a game.
	searchPolicy := func(mk func() *searcher) func(uint64, *rng) int {
		var mu sync.Mutex
		pool := map[*rng]*searcher{}
		return func(b uint64, r *rng) int {
			mu.Lock()
			s := pool[r]
			if s == nil {
				s = mk()
				pool[r] = s
			}
			mu.Unlock()
			dir, _ := s.bestMove(b)
			return dir
		}
	}

	type src struct {
		name string
		pol  func(uint64, *rng) int
	}
	srcs := []src{
		{"learned-1ply", greedyPolicy(learned)},
		{fmt.Sprintf("learned-d%d", *depth),
			searchPolicy(func() *searcher { return newSearcher(n.leafW, *depth, 0.1) })},
		{fmt.Sprintf("farm-d%d", *depth),
			searchPolicy(func() *searcher { return newFarmSearcher(*depth, 0.1) })},
	}

	stats := map[[2]int]*cellStat{}
	var mu sync.Mutex
	for _, s := range srcs {
		var wg sync.WaitGroup
		per := *games / *workers
		if per < 1 {
			per = 1
		}
		t0 := time.Now()
		for w := 0; w < *workers; w++ {
			wg.Add(1)
			go func(seed uint64) {
				defer wg.Done()
				collectCells(s.pol, [2]evalFn{learned, farm}, per, seed, stats, &mu)
			}(uint64(len(s.name))*1000 + uint64(w) + 7)
		}
		wg.Wait()
		fmt.Printf("# sampled %d games with %-14s (%.0f s)\n", per**workers, s.name, time.Since(t0).Seconds())
	}

	keys := make([][2]int, 0, len(risk))
	for k := range risk {
		keys = append(keys, k)
	}
	sort.Slice(keys, func(a, b int) bool {
		if keys[a][0] != keys[b][0] {
			return keys[a][0] < keys[b][0]
		}
		return keys[a][1] < keys[b][1]
	})
	var xs, yL, yF []float64
	fmt.Printf("\n k e     risk   mean_learnedV      mean_farm       n\n")
	for _, key := range keys {
		s := stats[key]
		cnt := 0
		if s != nil {
			cnt = s.n
		}
		if cnt < 30 {
			fmt.Printf(" %d %d  %6.2f    (n=%d too few)\n", key[0], key[1], risk[key], cnt)
			continue
		}
		m0 := s.sumV[0] / float64(s.n)
		m1 := s.sumV[1] / float64(s.n)
		fmt.Printf(" %d %d  %6.2f  %14.1f %14.0f  %6d\n", key[0], key[1], risk[key], m0, m1, s.n)
		xs = append(xs, risk[key])
		yL = append(yL, -m0)
		yF = append(yF, -m1)
	}
	if len(xs) >= 4 {
		fmt.Printf("\n SPEARMAN(risk, -learned V) over %d cells = %+.3f\n", len(xs), spearman(xs, yL))
		fmt.Printf(" SPEARMAN(risk, -farm eval) over %d cells = %+.3f  (control)\n", len(xs), spearman(xs, yF))
	} else {
		fmt.Printf("\n not enough cells (%d)\n", len(xs))
	}
}

// ---------------------------------------------------------------------------
// bench: how expensive is the learned leaf?
// ---------------------------------------------------------------------------

func cmdBench(args []string) {
	fs := flag.NewFlagSet("bench", flag.ExitOnError)
	wf := fs.String("weights", "weights.bin", "weight file")
	depths := fs.String("depths", "3,4,5,6", "search depths to time (comma list)")
	moves := fs.Int("moves", 60, "moves timed per depth")
	fs.Parse(args)

	initTables()
	initFarmTables()
	initPatterns()
	runtime.GOMAXPROCS(1)

	n, err := loadNet(*wf)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}

	// A realistic mid/late-game board set: walk a learned-greedy game and keep
	// every 20th board, so the leaf timing is measured on the boards the search
	// actually meets (cold cache lines in a 57 MB table), not on an empty one.
	r := &rng{s: 4242}
	var boards []uint64
	pol := greedyPolicy(func(b uint64) float64 { return n.eval(b) })
	b := initBoard(r)
	for i := 0; i < 4000; i++ {
		d := pol(b, r)
		if d < 0 {
			b = initBoard(r)
			continue
		}
		b = spawn(executeAllMoves(b)[d], r)
		if i%20 == 0 {
			boards = append(boards, b)
		}
	}
	fmt.Printf("# leaf timing over %d real boards\n", len(boards))

	timeLeaf := func(name string, f evalFn) {
		const reps = 300
		var sink float64
		t0 := time.Now()
		for i := 0; i < reps; i++ {
			for _, bb := range boards {
				sink += f(bb)
			}
		}
		el := time.Since(t0)
		ns := float64(el.Nanoseconds()) / float64(reps*len(boards))
		fmt.Printf("  %-16s %7.1f ns/eval   (sink %.0f)\n", name, ns, sink)
	}
	timeLeaf("farm (prod leaf)", farmEvaluator{}.Eval)
	timeLeaf("ntuple 8x6 (64 lookups)", n.evalNonNeg)

	fmt.Println("\n# search cost per move (single thread, cprob 1e-4)")
	fmt.Printf("  %-8s %-10s %10s %12s %10s\n", "depth", "leaf", "ms/move", "leaves/move", "ns/leaf")
	for _, ds := range splitInts(*depths) {
		for _, lf := range []struct {
			name string
			mk   func(int) *searcher
		}{
			{"farm", func(d int) *searcher { return newFarmSearcher(d, 0.1) }},
			{"ntuple", func(d int) *searcher { return newSearcher(n.leafW, d, 0.1) }},
		} {
			rr := &rng{s: 777}
			s := lf.mk(ds)
			bb := initBoard(rr)
			// warm up to a real mid-game position
			gp := greedyPolicy(func(x uint64) float64 { return n.eval(x) })
			for i := 0; i < 600; i++ {
				d := gp(bb, rr)
				if d < 0 {
					break
				}
				bb = spawn(executeAllMoves(bb)[d], rr)
			}
			s.leaves = 0
			t0 := time.Now()
			cnt := 0
			for i := 0; i < *moves; i++ {
				d, _ := s.bestMove(bb)
				if d < 0 {
					break
				}
				bb = spawn(executeAllMoves(bb)[d], rr)
				cnt++
			}
			el := time.Since(t0)
			if cnt == 0 {
				continue
			}
			msPer := float64(el.Milliseconds()) / float64(cnt)
			lvPer := float64(s.leaves) / float64(cnt)
			nsLeaf := float64(el.Nanoseconds()) / float64(s.leaves)
			fmt.Printf("  %-8d %-10s %10.2f %12.0f %10.1f\n", ds, lf.name, msPer, lvPer, nsLeaf)
		}
	}
}

func splitInts(s string) []int {
	var out []int
	cur, has := 0, false
	for i := 0; i <= len(s); i++ {
		if i == len(s) || s[i] == ',' {
			if has {
				out = append(out, cur)
			}
			cur, has = 0, false
			continue
		}
		if s[i] >= '0' && s[i] <= '9' {
			cur = cur*10 + int(s[i]-'0')
			has = true
		}
	}
	return out
}

// ---------------------------------------------------------------------------
// arena: play games, emit arena-compatible jsonl
// ---------------------------------------------------------------------------

type gameRec struct {
	Arm     string `json:"arm"`
	Seed    int    `json:"seed"`
	Moves   int    `json:"moves"`
	Score   int64  `json:"score"`
	Forced  bool   `json:"forced_2048"`
	MaxRank int    `json:"max_rank"`
	K1024   int    `json:"k1024"`
}

func cmdArena(args []string) {
	fs := flag.NewFlagSet("arena", flag.ExitOnError)
	wf := fs.String("weights", "weights.bin", "weight file")
	leafName := fs.String("leaf", "ntuple", "leaf evaluator: ntuple | farm")
	depth := fs.Int("depth", 3, "expectimax depth")
	games := fs.Int("games", 50, "games")
	seed0 := fs.Int("seed0", 1000, "first seed")
	workers := fs.Int("workers", 8, "parallel games")
	arm := fs.String("arm", "", "arm name (default: leaf-dN)")
	outDir := fs.String("out", ".", "output dir for arm-*.jsonl")
	kappa := fs.Float64("kappa", 0, "search-time risk seeking: chance nodes return E[X]+kappa*sd(X)")
	cashout := fs.Bool("cashout", false, "cash-out-aware player: deliberately execute the 2048 cascade (real final score) at the cash-out trigger (record.go)")
	maxcashout := fs.Bool("maxcashout", false, "endgame score-maximiser: bank every beneficial merge and fire the 2048 LAST (only with -cashout)")
	exact := fs.Bool("exact", false, "target-conditioned endgame solver: P(final>=T)-argmax moves + early-fire (only with -cashout). Levers: BOT2048_{TARGET,EXACT_EMPTIES,EXACT_NODES,TARGET_PCONF,EXACT_EARLYFIRE,EXACT_EARLY_EMPTIES,MAXCASH_DEPTH}")
	fs.Parse(args)

	initTables()
	initFarmTables()
	initPatterns()
	runtime.GOMAXPROCS(*workers)

	// newS builds a fresh searcher per game. `farm` runs the PRODUCTION objective
	// (S-sum cash-out); `ntuple` runs the learned objective carried in the weight
	// file's trailer (mean remaining moves, or the exponential utility).
	var newS func() *searcher
	switch *leafName {
	case "ntuple":
		n, err := loadNet(*wf)
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		if obj.exp {
			fmt.Printf("# leaf: EXP-utility net, lambda=%.4f base=%.0f\n", obj.lambda, obj.cBase)
		}
		// HEUR5 taboo-mobility leaf (mobility.go). Default 0 = OFF: the wrapper
		// is not installed, so the leaf is the exact champion function and
		// behaviour is bit-identical to the pre-HEUR5 engine.
		leaf := evalFn(n.leafW)
		if mobW := farmWeightFromEnv("BOT2048_NTUPLE_MOB_W", 0); mobW > 0 {
			fmt.Printf("# HEUR5 taboo-mobility leaf penalty ACTIVE: W=%.1f moves/hazard\n", mobW)
			leaf = newMobLeaf(n, mobW)
		}
		newS = func() *searcher {
			s := newSearcher(leaf, *depth, 0.1)
			s.kappa = *kappa
			return s
		}
	case "farm":
		newS = func() *searcher {
			s := newFarmSearcher(*depth, 0.1)
			s.kappa = *kappa
			return s
		}
	default:
		fmt.Fprintln(os.Stderr, "leaf must be ntuple or farm")
		os.Exit(1)
	}
	name := *arm
	if name == "" {
		name = fmt.Sprintf("%s-d%d", *leafName, *depth)
	}

	// Record engine (record.go) for the arena repro / batch validation. Same
	// levers and defaults as the serve path; the -exact flag here plays the role
	// of BOT2048_RECORD_ENGINE=1. ev/doom are not ported (the winning arm ran with
	// them OFF), so playCashout reproduces that arm byte-for-byte.
	maxcashDepth := farmIntFromEnv("BOT2048_MAXCASH_DEPTH", 8)
	var exactCfg exactConfig
	if *cashout && *exact {
		exactCfg = newExactConfig(true)
		name += "-exact"
		fmt.Printf("# record engine ON: %s\n",
			recordConfig{on: true, exact: exactCfg, maxcashDepth: maxcashDepth}.summary())
	}

	recs := make([]gameRec, *games)
	var idx int64 = -1
	var wg sync.WaitGroup
	t0 := time.Now()
	for w := 0; w < *workers; w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			// One score-maximiser + one score searcher per worker, reused across
			// games (epoch-invalidated per root). nil unless the record arm is on.
			var maxCash, exactScore *scoreSearcher
			if *cashout && *exact {
				if *maxcashout {
					maxCash = newScoreSearcher(maxcashDepth, 0.1)
				}
				exactScore = newScoreSearcher(exactCfg.scoreDepth, 0.1)
			}
			for {
				i := int(atomic.AddInt64(&idx, 1))
				if i >= *games {
					return
				}
				seed := *seed0 + i
				r := &rng{s: uint64(seed)*0x9E3779B97F4A7C15 + 1}
				var m int
				var forced bool
				var fb uint64
				var score int64
				if *cashout && *exact {
					res := playCashout(newS(), r, maxCash, exactCfg, exactScore)
					m, fb = res.moves, res.finalBoard
					forced = res.cashed
					score = int64(res.finalScore) // TRUE post-cascade score (+2048)
				} else {
					var f2048 bool
					m, f2048, fb = playSearch(newS(), r)
					forced = f2048
					score = int64(sSumBoard(fb))
				}
				mr := 0
				for c := 0; c < 16; c++ {
					if v := int((fb >> (4 * uint(c))) & 0xF); v > mr {
						mr = v
					}
				}
				recs[i] = gameRec{
					Arm: name, Seed: seed, Moves: m,
					Score: score, Forced: forced,
					MaxRank: mr, K1024: rank10Count(fb),
				}
			}
		}()
	}
	wg.Wait()

	if err := os.MkdirAll(*outDir, 0o755); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	path := filepath.Join(*outDir, "arm-"+name+".jsonl")
	f, err := os.Create(path)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	enc := json.NewEncoder(f)
	ms := make([]int, 0, len(recs))
	forced, mean := 0, 0.0
	for _, rc := range recs {
		enc.Encode(rc)
		ms = append(ms, rc.Moves)
		mean += float64(rc.Moves)
		if rc.Forced {
			forced++
		}
	}
	f.Close()
	mean /= float64(len(ms))
	sd := 0.0
	for _, m := range ms {
		sd += (float64(m) - mean) * (float64(m) - mean)
	}
	sd = math.Sqrt(sd / float64(len(ms)-1))
	mx := 0
	for _, m := range ms {
		if m > mx {
			mx = m
		}
	}
	// The objective is P(moves >= 2569) — a ~4-sigma event that n=100 games can
	// never observe directly. We therefore report the GAUSSIAN-FITTED tail, which
	// is what the project compares arms on; note it depends on sigma at least as
	// strongly as on the mean, so an arm that raises the mean while shrinking
	// sigma is a WORSE arm by this metric.
	z := (2569.0 - mean) / sd
	pTail := 0.5 * math.Erfc(z/math.Sqrt2)
	fmt.Printf("%-16s n=%d  median=%.0f  mean=%.0f  sigma=%.0f  max=%d  forced2048=%d  z=%.2f  P(>=2569)=%.4f%%  (%.0f s)\n",
		name, len(ms), median(ms), mean, sd, mx, forced, z, 100*pTail, time.Since(t0).Seconds())
	fmt.Println("wrote", path)
}

func main() {
	// A completely arg-less launch is the live runner's spawn (go_engine.py's
	// _ensure_proc runs `Popen([binary])` — no subcommand, no flags), so it
	// falls through to serve with the default weights (see defaultWeightsPath).
	if len(os.Args) < 2 {
		cmdServe(nil)
		return
	}
	switch os.Args[1] {
	case "train":
		cmdTrain(os.Args[2:])
	case "probe":
		cmdProbe(os.Args[2:])
	case "bench":
		cmdBench(os.Args[2:])
	case "perfbench":
		cmdPerfBench(os.Args[2:])
	case "arena":
		cmdArena(os.Args[2:])
	case "serve":
		cmdServe(os.Args[2:])
	default:
		fmt.Fprintln(os.Stderr, "unknown subcommand", os.Args[1])
		os.Exit(2)
	}
}
