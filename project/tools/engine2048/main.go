package main

import (
	"bufio"
	"fmt"
	"os"
	"runtime"
	"strconv"
	"strings"
	"time"
)

// Stdio protocol (persistent server, one reply line per request line):
//
//	search <16hex> <depth> <p4> <adaptive 0|1> [mode 0|1] [budget_ms]
//	    -> <dir> <value> <ms> <depth_limit>
//	    (mode 0 = classic default, 1 = farm/monobank stop-rule objective;
//	     omitting it keeps the classic behaviour, so old callers are unchanged.
//
//	     budget_ms > 0 is LIVE MODE: depth and adaptive are ignored and the
//	     search climbs the (depth, cprob) ladder for as long as the budget
//	     allows, on ALL cores (BOT2048_SEARCH_WORKERS goroutines, default
//	     GOMAXPROCS). <depth_limit> reports the deepest rung it completed.
//	     Live mode is nondeterministic BY DESIGN — the answer depends on how
//	     much the clock allowed — so simulations must NOT use it. With
//	     budget_ms absent or 0 the search is the fixed-depth, single-core,
//	     bit-for-bit reproducible one that simulations rely on.)
//
//	search <16hex> <depth> <p4> <adaptive 0|1> <mode 0|1> <budget_ms> [maxdepth=D|0] [nodes=N|0] [cprob=C]
//	    -> <dir> <value> <ms> <depth_reached> stop=<reason> nodes=<N> cprob=<C>
//	    (PROTOCOL 2, additive: a request that carries any `k=v` tail selects
//	     the limited search (search.go searchLimitedExt) — two INDEPENDENT
//	     optional limits, maxdepth and budget_ms (0 = none each), stopping at
//	     whichever hits first, plus the nodes guard; cprob pins the base
//	     horizon. maxdepth=D with budget_ms=0 is the fixed-depth path; with a
//	     budget it is direct-first: the fixed-depth-D answer when it fits
//	     (stop=depth, bit-identical to fixed depth), else the ladder capped at
//	     D. budget_ms=0 is allowed here (it is "none"). Without a tail the
//	     positional <depth> is the cap for a 0/absent budget and ignored for a
//	     positive one, exactly as before; with a tail that lacks maxdepth= the
//	     same rule applies. The reply tail is emitted ONLY for tailed requests,
//	     so the old wire is byte-identical. stop is one of depth | budget |
//	     nodes | ladder_end.)
//	move <16hex> <dir>
//	    -> <16hex-newboard> <score> <moved 0|1>
//	prefetch <16hex> <depth> <p4> <adaptive 0|1> <mode 0|1> <budget_ms> [maxdepth=…] [nodes=…] [cprob=…]
//	    -> ok <n_states>
//	    (speculative: enumerates every spawn state of the given post-swipe
//	     board — each empty cell x {2, 4}, 2-spawns first — and computes the
//	     decision a `search` request with the same parameters would return,
//	     in ONE background goroutine; budget_ms 0 means fixed depth. Replies
//	     immediately. Any later non-lookup command aborts the speculation
//	     within a sub-millisecond poll window before being handled, so
//	     speculation never delays a real request.)
//	lookup <16hex>
//	    -> hit <dir> <value> <depth> [stop=… nodes=… cprob=…] | miss
//	    (served from the prefetch cache; never triggers a search and does
//	     not interrupt an in-flight prefetch; the tail only for entries a
//	     tailed — protocol-2 — prefetch computed)
//	version
//	    -> version protocol=<n> go=<ver> <KEY>=<value> ... (the 18 farm
//	     weights + BOT2048_CPROB_THRESH the engine ACTUALLY applied at
//	     startup, after the env overrides — an experiment's provenance record:
//	     a config that omits a weight still gets the compiled-in default,
//	     and only this reply says which one)
//	ping -> pong
//	anything else -> err <message>
//
// <16hex>: 16 hex digits, digit j (leftmost = j=0) = exponent of cell j
// (row-major). EOF on stdin -> exit 0.

// parseBoardHex decodes 16 hex digits into a bitboard (digit j -> bits
// 4*j..4*j+3, matching the row-major cell order).
func parseBoardHex(s string) (uint64, error) {
	if len(s) != 16 {
		return 0, fmt.Errorf("board must be 16 hex digits, got %d", len(s))
	}
	var bb uint64
	for j := 0; j < 16; j++ {
		c := s[j]
		var v uint64
		switch {
		case c >= '0' && c <= '9':
			v = uint64(c - '0')
		case c >= 'a' && c <= 'f':
			v = uint64(c-'a') + 10
		case c >= 'A' && c <= 'F':
			v = uint64(c-'A') + 10
		default:
			return 0, fmt.Errorf("invalid hex digit %q", c)
		}
		bb |= v << (4 * j)
	}
	return bb, nil
}

func formatBoardHex(bb uint64) string {
	const digits = "0123456789abcdef"
	var buf [16]byte
	for j := 0; j < 16; j++ {
		buf[j] = digits[(bb>>(4*j))&0xF]
	}
	return string(buf[:])
}

// splitLimitTail separates a request's positional fields from its protocol-2
// `k=v` tail (everything from the first field containing '='). An old-wire
// request has an empty tail.
func splitLimitTail(fields []string) (pos, tail []string) {
	for i, f := range fields {
		if strings.Contains(f, "=") {
			return fields[:i], fields[i:]
		}
	}
	return fields, nil
}

// parseLimitTail decodes the protocol-2 tail: maxdepth=D|0, nodes=N|0,
// cprob=C. hasMaxDepth says whether maxdepth= was present at all (absent means
// "the positional depth rule applies", 0 means None).
func parseLimitTail(tail []string) (lim searchLimits, hasMaxDepth bool, err error) {
	for _, kv := range tail {
		key, val, _ := strings.Cut(kv, "=")
		switch key {
		case "maxdepth":
			d, perr := strconv.Atoi(val)
			if perr != nil || d < 0 {
				return lim, false, fmt.Errorf("maxdepth must be an integer >= 0 (0 = none)")
			}
			lim.maxDepth, hasMaxDepth = d, true
		case "nodes":
			n, perr := strconv.ParseUint(val, 10, 64)
			if perr != nil {
				return lim, false, fmt.Errorf("nodes must be an integer >= 0 (0 = none)")
			}
			lim.maxNodes = n
		case "cprob":
			c, perr := strconv.ParseFloat(val, 64)
			if perr != nil || c <= 0 || c > 1 {
				return lim, false, fmt.Errorf("cprob must be a float in (0, 1]")
			}
			lim.cprob = c
		default:
			return lim, false, fmt.Errorf("unknown limit %q (want maxdepth= nodes= cprob=)", key)
		}
	}
	return lim, hasMaxDepth, nil
}

// resolveLimits applies the positional-depth rule to a tailed request: without
// maxdepth= the positional depth caps a search with no budget (the old
// fixed-depth semantics) and nothing caps a budgeted one (the old ladder).
func resolveLimits(lim searchLimits, hasMaxDepth bool, depth int, budgetMs float64) searchLimits {
	lim.budgetMs = budgetMs
	if !hasMaxDepth && budgetMs <= 0 {
		lim.maxDepth = depth
	}
	return lim
}

func handleSearch(fields []string, eval Evaluator) string {
	fields, tail := splitLimitTail(fields)
	if len(fields) < 5 || len(fields) > 7 {
		return "err usage: search <16hex> <depth> <p4> <adaptive 0|1> [mode 0|1] [budget_ms]"
	}
	board, err := parseBoardHex(fields[1])
	if err != nil {
		return "err " + err.Error()
	}
	depth, err := strconv.Atoi(fields[2])
	if err != nil || depth < 1 {
		return "err depth must be an integer >= 1"
	}
	p4, err := strconv.ParseFloat(fields[3], 64)
	if err != nil || p4 < 0.0 || p4 > 1.0 {
		return "err p4 must be a float in [0, 1]"
	}
	var adaptive bool
	switch fields[4] {
	case "0":
		adaptive = false
	case "1":
		adaptive = true
	default:
		return "err adaptive must be 0 or 1"
	}
	farm := false
	if len(fields) >= 6 {
		switch fields[5] {
		case "0":
			farm = false
		case "1":
			farm = true
		default:
			return "err mode must be 0 (classic) or 1 (farm)"
		}
	}
	budgetMs := 0.0
	if len(fields) == 7 {
		budgetMs, err = strconv.ParseFloat(fields[6], 64)
		if tail != nil {
			// Protocol 2: budget_ms 0 is "no clock" (the depth cap / node cap stop
			// the search instead).
			if err != nil || budgetMs < 0.0 || budgetMs > 600000.0 {
				return "err budget_ms must be a float in [0, 600000] (0 = none)"
			}
		} else if err != nil || budgetMs <= 0.0 || budgetMs > 600000.0 {
			return "err budget_ms must be a float in (0, 600000]"
		}
	}
	if tail != nil {
		lim, hasMaxDepth, err := parseLimitTail(tail)
		if err != nil {
			return "err " + err.Error()
		}
		lim = resolveLimits(lim, hasMaxDepth, depth, budgetMs)
		start := time.Now()
		res, completed := searchLimitedExt(board, p4, adaptive, eval, farm, lim, realTables, nil)
		if !completed {
			// Unreachable without an ext flag (see `search`); fail loudly.
			panic("engine2048: limited search aborted with no abort source armed")
		}
		ms := float64(time.Since(start).Nanoseconds()) / 1e6
		return fmt.Sprintf("%d %.6f %.3f %d %s", res.bestDir, res.bestValue, ms, res.depthLimit,
			formatSearchTail(res.stop, res.nodes, res.cprob))
	}
	start := time.Now()
	var res searchResult
	if budgetMs > 0.0 {
		res = searchBudget(board, p4, eval, farm, budgetMs)
	} else {
		res = search(board, depth, p4, adaptive, eval, farm)
	}
	ms := float64(time.Since(start).Nanoseconds()) / 1e6
	return fmt.Sprintf("%d %.6f %.3f %d", res.bestDir, res.bestValue, ms, res.depthLimit)
}

func handleMove(fields []string) string {
	if len(fields) != 3 {
		return "err usage: move <16hex> <dir>"
	}
	board, err := parseBoardHex(fields[1])
	if err != nil {
		return "err " + err.Error()
	}
	dir, err := strconv.Atoi(fields[2])
	if err != nil || dir < 0 || dir > 3 {
		return "err direction must be 0 (UP), 1 (RIGHT), 2 (DOWN) or 3 (LEFT)"
	}
	newboard := executeMove(board, dir)
	moved := 0
	score := int64(0)
	if newboard != board {
		moved = 1
		score = int64(moveScore(board, dir))
	}
	return fmt.Sprintf("%s %d %d", formatBoardHex(newboard), score, moved)
}

func handlePrefetch(fields []string, eval Evaluator) string {
	fields, tail := splitLimitTail(fields)
	if len(fields) != 7 {
		return "err usage: prefetch <16hex> <depth> <p4> <adaptive 0|1> <mode 0|1> <budget_ms>"
	}
	board, err := parseBoardHex(fields[1])
	if err != nil {
		return "err " + err.Error()
	}
	depth, err := strconv.Atoi(fields[2])
	if err != nil || depth < 1 {
		return "err depth must be an integer >= 1"
	}
	p4, err := strconv.ParseFloat(fields[3], 64)
	if err != nil || p4 < 0.0 || p4 > 1.0 {
		return "err p4 must be a float in [0, 1]"
	}
	var adaptive bool
	switch fields[4] {
	case "0":
		adaptive = false
	case "1":
		adaptive = true
	default:
		return "err adaptive must be 0 or 1"
	}
	var farm bool
	switch fields[5] {
	case "0":
		farm = false
	case "1":
		farm = true
	default:
		return "err mode must be 0 (classic) or 1 (farm)"
	}
	budgetMs, err := strconv.ParseFloat(fields[6], 64)
	if err != nil || budgetMs < 0.0 || budgetMs > 600000.0 {
		return "err budget_ms must be a float in [0, 600000] (0 = fixed depth)"
	}
	if tail != nil {
		lim, hasMaxDepth, err := parseLimitTail(tail)
		if err != nil {
			return "err " + err.Error()
		}
		lim = resolveLimits(lim, hasMaxDepth, depth, budgetMs)
		n := startPrefetchLimited(board, depth, p4, adaptive, eval, farm, budgetMs, &lim)
		return fmt.Sprintf("ok %d", n)
	}
	n := startPrefetch(board, depth, p4, adaptive, eval, farm, budgetMs)
	return fmt.Sprintf("ok %d", n)
}

func handleLookup(fields []string) string {
	if len(fields) != 2 {
		return "err usage: lookup <16hex>"
	}
	board, err := parseBoardHex(fields[1])
	if err != nil {
		return "err " + err.Error()
	}
	if e, ok := lookupPrefetch(board); ok {
		return fmt.Sprintf("hit %d %.6f %d", e.dir, e.value, e.depth) + e.lookupTail()
	}
	return "miss"
}

// engineProtocolVersion is bumped whenever the stdio protocol changes shape.
// 2 = the additive `k=v` limits tail (maxdepth= nodes= cprob=) on search and
// prefetch, with the stop=/nodes=/cprob= reply tail; the version-1 wire is a
// byte-identical subset.
const engineProtocolVersion = 2

// handleVersion reports what this engine IS and what it actually DOES: the
// protocol/toolchain version plus the effective value of every farm weight,
// read once at startup in initFarmTables (compiled-in default, possibly
// overridden by a BOT2048_FARM_* env var). Callers record this with each run,
// so two runs with byte-identical configs but different compiled-in defaults
// (e.g. HOARD_TRIGGER 2 -> 99) can still be told apart after the fact.
func handleVersion() string {
	num := func(v float64) string { return strconv.FormatFloat(v, 'g', -1, 64) }
	return strings.Join([]string{
		"version",
		"protocol=" + strconv.Itoa(engineProtocolVersion),
		"go=" + runtime.Version(),
		// Which engine this is: the client picks the source tree to hash (and
		// the semantics to expect) from this key, not from the binary's path.
		"engine=engine2048",
		// The live search's pool width. It affects ONLY budget_ms searches (the
		// fixed-depth path simulations use is serial whatever this says), but a
		// live run's achieved depth depends on it, so it belongs in the record.
		"BOT2048_SEARCH_WORKERS=" + strconv.Itoa(searchWorkers),
		"GOMAXPROCS=" + strconv.Itoa(runtime.GOMAXPROCS(0)),
		"BOT2048_FARM_EMPTY_W=" + num(farmEmptyWeight),
		"BOT2048_FARM_MERGE_W=" + num(farmMergeWeight),
		"BOT2048_FARM_CORNER_W=" + num(farmCornerWeight),
		"BOT2048_FARM_EDGE_W=" + num(farmEdgeWeight),
		"BOT2048_FARM_STAIR_W=" + num(farmStairWeight),
		"BOT2048_FARM_SNAKE_W=" + num(farmSnakeWeight),
		"BOT2048_FARM_SNAKE_RATIO=" + num(farmSnakeRatio),
		"BOT2048_FARM_CORNER_RANK_W=" + num(farmCornerRankWeight),
		"BOT2048_FARM_CONVEX_W=" + num(farmConvexWeight),
		"BOT2048_FARM_LATE_EMPTY_W=" + num(farmLateEmptyWeight),
		"BOT2048_FARM_LATE_GRAD_MULT=" + num(farmLateGradMult),
		"BOT2048_FARM_LATE_CORNER_RANK_W=" + num(farmLateCornerRankWeight),
		"BOT2048_FARM_HOARD_TRIGGER=" + strconv.Itoa(farmHoardTrigger),
		"BOT2048_FARM_MOBILITY_W=" + num(farmMobilityWeight),
		"BOT2048_FARM_MOB_PAIR_PEN=" + num(farmMobPairPenalty),
		"BOT2048_FARM_QUADCORNER_W=" + num(farmQuadCornerWeight),
		"BOT2048_FARM_QUAD_RATIO=" + num(farmQuadRatio),
		"BOT2048_FARM_EMPTY_CURVE_W=" + num(farmEmptyCurveWeight),
		"BOT2048_CPROB_THRESH=" + num(cprobThreshBase),
	}, " ")
}

func handleLine(line string, eval Evaluator) string {
	fields := strings.Fields(line)
	if len(fields) == 0 {
		return "err empty request"
	}
	switch fields[0] {
	case "ping":
		return "pong"
	case "version":
		return handleVersion()
	case "search":
		return handleSearch(fields, eval)
	case "move":
		return handleMove(fields)
	case "prefetch":
		return handlePrefetch(fields, eval)
	case "lookup":
		return handleLookup(fields)
	default:
		return "err unknown command " + strconv.Quote(fields[0])
	}
}

// safeHandle turns any panic into an err reply — the server never crashes
// on a bad request.
func safeHandle(line string, eval Evaluator) (reply string) {
	defer func() {
		if r := recover(); r != nil {
			reply = fmt.Sprintf("err internal panic: %v", r)
		}
	}()
	return handleLine(line, eval)
}

func main() {
	initTables()
	var eval Evaluator = tableEvaluator{}

	in := bufio.NewScanner(os.Stdin)
	in.Buffer(make([]byte, 0, 64*1024), 64*1024)
	out := bufio.NewWriter(os.Stdout)
	for in.Scan() {
		line := in.Text()
		// A real request preempts speculation: abort + join the prefetch
		// worker (sub-ms) before handling anything that searches or mutates.
		// `lookup` and `ping` are passive reads; `prefetch` does its own
		// stop-then-restart inside startPrefetch.
		if f := strings.Fields(line); len(f) > 0 {
			switch f[0] {
			case "ping", "lookup", "prefetch":
			default:
				stopPrefetch()
			}
		}
		fmt.Fprintln(out, safeHandle(line, eval))
		out.Flush()
	}
	stopPrefetch()
	os.Exit(0)
}
