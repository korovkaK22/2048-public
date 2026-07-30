//go:build !unix && !windows

package main

import "time"

// cpuTime has no implementation on this platform; return 0 so the measurement
// tests still build and run (their CPU columns just read 0).
func cpuTime() time.Duration { return 0 }
