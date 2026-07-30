package main

import (
	"math"
	"sync/atomic"
)

// ---------------------------------------------------------------------------
// Leaf-value cache (perf #1): a lock-free direct-mapped board -> leaf-value
// memo, probed at the cprob/depth cutoff of the search (chanceNode reaches it
// via s.leaf(board)). The leaf is a PURE function of the board given the fixed
// weight file, so a cached value never needs invalidation: it survives ladder
// rungs AND real moves. At empty<=3 the tree is leaf-dominated and the leaf is
// DRAM-latency bound (64 gathers into a ~57 MB table), so avoiding a recompute
// on a transposed cut node is the single largest lever.
//
// BIT-EXACT: each entry stores the FULL float64 leaf value. A hit therefore
// returns the identical float64 a recompute would, so search decisions on the
// serial path are bit-identical with the cache on or off. Any two writers write
// the same payload for the same board, so the concurrent (parallel-driver)
// writes race benignly.
//
// Structure mirrors sharedTT (parallel.go): two atomic words per slot, Hyatt's
// lockless XOR. A reader that lands mid-update sees a val from one write and a
// key from another, xorKey^val then does not equal the board it wants, and the
// torn entry reads as a miss (recompute). Pointer-free (noscan), so it is
// invisible to the GC.
//
//	2^22 slots x 16 B = 64 MB.
const (
	leafCacheLogSize = 22
	leafCacheSize    = 1 << leafCacheLogSize
	leafCacheMask    = leafCacheSize - 1
)

// countLeafCache turns on the (atomic) probe/hit counters. OFF in production so
// the live parallel pool never contends on a shared counter cache line; the
// perfbench harness flips it on to report hit rate.
var countLeafCache = false

type leafCacheSlot struct {
	xorKey atomic.Uint64 // board ^ Float64bits(val)
	val    atomic.Uint64 // Float64bits(val)
}

type leafCache struct {
	slots  []leafCacheSlot
	probes atomic.Uint64
	hits   atomic.Uint64
}

func newLeafCache() *leafCache {
	return &leafCache{slots: make([]leafCacheSlot, leafCacheSize)}
}

func leafCacheIndex(board uint64) uint64 {
	return (board * 0x9E3779B97F4A7C15) >> (64 - leafCacheLogSize)
}

// get returns the cached full-float64 value for board, or ok=false on a miss
// (empty slot, evicted board, or a torn concurrent write).
func (c *leafCache) get(board uint64) (float64, bool) {
	s := &c.slots[leafCacheIndex(board)&leafCacheMask]
	v := s.val.Load()
	x := s.xorKey.Load()
	// Validate the payload against the key it was written with: a slot caught
	// mid-update fails this and reads as a miss.
	if x^v == board {
		return math.Float64frombits(v), true
	}
	return 0, false
}

// put stores val for board, evicting whatever direct-mapped slot it hashes to.
func (c *leafCache) put(board uint64, val float64) {
	v := math.Float64bits(val)
	s := &c.slots[leafCacheIndex(board)&leafCacheMask]
	// Publish the payload first, then the key, so a reader that lands between
	// the two sees the OLD key against the NEW payload — a detected miss, never
	// a wrong value.
	s.val.Store(v)
	s.xorKey.Store(board ^ v)
}

// newCachedLeaf wraps inner with a fresh leaf cache and returns the wrapped
// evaluator plus the cache (for hit-rate reporting). Every searcher built with
// the wrapped leaf — persistent, speculative, and each parallel-rung worker via
// s.leaf — shares the one cache, which is exactly what makes a subtree computed
// by one worker reusable by another (and across moves).
func newCachedLeaf(inner evalFn) (evalFn, *leafCache) {
	c := newLeafCache()
	return func(b uint64) float64 {
		if countLeafCache {
			c.probes.Add(1)
		}
		if v, ok := c.get(b); ok {
			if countLeafCache {
				c.hits.Add(1)
			}
			return v
		}
		v := inner(b)
		c.put(b, v)
		return v
	}, c
}
