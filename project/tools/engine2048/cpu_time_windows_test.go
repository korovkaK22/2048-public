//go:build windows

package main

import (
	"syscall"
	"time"
)

// cpuTime is the process's total user+system CPU across all threads, via
// GetProcessTimes — the Windows equivalent of getrusage(RUSAGE_SELF).
func cpuTime() time.Duration {
	h, err := syscall.GetCurrentProcess()
	if err != nil {
		return 0
	}
	var creation, exit, kernel, user syscall.Filetime
	if err := syscall.GetProcessTimes(h, &creation, &exit, &kernel, &user); err != nil {
		return 0
	}
	return time.Duration(user.Nanoseconds()) + time.Duration(kernel.Nanoseconds())
}
