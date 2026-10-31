// Package safety implements the Memory/CPU Watchdog, OOM Circuit Breaker,
// Disk I/O Pacer, and Global/Per-Task Network Connection Rate Limiter for Starfall-Go Debrid.
package safety

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"runtime"
	"runtime/debug"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"golang.org/x/time/rate"
)

// WatchdogState represents the current memory/resource pressure tier.
type WatchdogState int32

const (
	// StateNormal (< 70% RAM and < 70% heap ceiling): normal queue operations and full I/O buffers.
	StateNormal WatchdogState = iota

	// StateWarn (70% - 82% RAM): pauses new task intake, shrinks chunk buffers to 32 KiB,
	// triggers proactive GC, and tightens disk write pacing.
	StateWarn

	// StateCritical (> 82% RAM or >= 80% of safety heap limit): pauses active downloads,
	// triggers debug.FreeOSMemory(), rejects all heavy operations, and throttles disk I/O.
	StateCritical
)

// String returns the human-readable state identifier.
func (s WatchdogState) String() string {
	switch s {
	case StateNormal:
		return "NORMAL"
	case StateWarn:
		return "WARN"
	case StateCritical:
		return "CRITICAL"
	default:
		return "UNKNOWN"
	}
}

const (
	// BufferSizeNormal is the default stream copy buffer in StateNormal (256 KiB).
	BufferSizeNormal = 256 * 1024

	// BufferSizeWarn is the minimized stream copy buffer in StateWarn (32 KiB).
	BufferSizeWarn = 32 * 1024

	// BufferSizeCritical is the emergency minimal buffer in StateCritical (16 KiB).
	BufferSizeCritical = 16 * 1024
)

var (
	// ErrCircuitBreakerOpen is returned when new tasks or heavy operations are rejected
	// due to WARN or CRITICAL memory pressure.
	ErrCircuitBreakerOpen = errors.New("safety watchdog circuit breaker open: insufficient memory headroom")

	// ErrInsufficientDiskSpace is returned when NVMe free space drops below the safety floor.
	ErrInsufficientDiskSpace = errors.New("insufficient NVMe disk headroom for safe operation")
)

// MemorySnapshot captures a point-in-time reading of Go runtime and Linux host memory/CPU.
type MemorySnapshot struct {
	Timestamp          time.Time
	State              WatchdogState
	HeapAllocBytes     uint64
	HeapInuseBytes     uint64
	SysBytes           uint64
	NumGC              uint32
	HostMemTotalBytes  uint64
	HostMemUsedBytes   uint64
	UsagePercent       float64
	HeapSafetyPercent  float64
	CPULoadPercent     float64
	ChunkBufferBytes   int
	DiskWriteLimitBps  int64
}

// WatchdogConfig holds configuration parameters for the Memory & I/O Watchdog.
type WatchdogConfig struct {
	SoftLimitBytes    uint64
	HardCeilingBytes  uint64
	TotalSystemRAM    uint64
	WarnThresholdPct  float64
	CritThresholdPct  float64
	HeapSafetyRatio   float64
	TickInterval      time.Duration
	BaseDiskWriteBps  int64
	MinNVMeFreeBytes  uint64
	DownloadDirectory string
}

// WatchdogCallbacks allows engine subsystems to react immediately on state transitions.
type WatchdogCallbacks struct {
	OnEnterNormal   func(MemorySnapshot)
	OnEnterWarn     func(MemorySnapshot)
	OnEnterCritical func(MemorySnapshot)
}

// Watchdog monitors Go runtime heap and Linux system RAM/CPU every 1 second,
// operating as an OOM circuit breaker and adaptive disk I/O pacer.
type Watchdog struct {
	cfg       WatchdogConfig
	logger    *slog.Logger
	state     atomic.Int32
	chunkSize atomic.Int32

	mu           sync.RWMutex
	lastSnapshot MemorySnapshot
	callbacks    WatchdogCallbacks
	prevCPUIdle  uint64
	prevCPUTotal uint64

	diskLimiter *rate.Limiter
	cancel      context.CancelFunc
	wg          sync.WaitGroup
}

// NewWatchdog creates a configured Memory & Disk I/O Watchdog.
func NewWatchdog(cfg WatchdogConfig, logger *slog.Logger) *Watchdog {
	if cfg.SoftLimitBytes == 0 {
		cfg.SoftLimitBytes = 3000 * 1024 * 1024
	}
	if cfg.HardCeilingBytes == 0 {
		cfg.HardCeilingBytes = 3200 * 1024 * 1024
	}
	if cfg.TotalSystemRAM == 0 {
		cfg.TotalSystemRAM = 4096 * 1024 * 1024
	}
	if cfg.WarnThresholdPct <= 0 {
		cfg.WarnThresholdPct = 70.0
	}
	if cfg.CritThresholdPct <= 0 {
		cfg.CritThresholdPct = 82.0
	}
	if cfg.HeapSafetyRatio <= 0 {
		cfg.HeapSafetyRatio = 0.80
	}
	if cfg.TickInterval <= 0 {
		cfg.TickInterval = 1 * time.Second
	}
	if cfg.BaseDiskWriteBps <= 0 {
		cfg.BaseDiskWriteBps = 120 * 1024 * 1024 // 120 MiB/s baseline NVMe write cap
	}
	if cfg.MinNVMeFreeBytes == 0 {
		cfg.MinNVMeFreeBytes = 5 * 1024 * 1024 * 1024 // 5 GiB floor
	}

	burst := int(cfg.BaseDiskWriteBps / 4)
	if burst < BufferSizeNormal*2 {
		burst = BufferSizeNormal * 2
	}

	w := &Watchdog{
		cfg:         cfg,
		logger:      logger,
		diskLimiter: rate.NewLimiter(rate.Limit(cfg.BaseDiskWriteBps), burst),
	}
	w.state.Store(int32(StateNormal))
	w.chunkSize.Store(BufferSizeNormal)
	return w
}

// SetCallbacks registers state-transition hooks for the Dispatcher and RateLimiter.
func (w *Watchdog) SetCallbacks(cb WatchdogCallbacks) {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.callbacks = cb
}

// Start launches the 1-second watchdog monitoring loop.
func (w *Watchdog) Start(parentCtx context.Context) {
	ctx, cancel := context.WithCancel(parentCtx)
	w.cancel = cancel

	// Take an immediate initial sample before starting ticker.
	w.sampleAndEvaluate()

	w.wg.Add(1)
	go func() {
		defer w.wg.Done()
		ticker := time.NewTicker(w.cfg.TickInterval)
		defer ticker.Stop()

		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				w.sampleAndEvaluate()
			}
		}
	}()
}

// Stop halts the watchdog ticker loop cleanly.
func (w *Watchdog) Stop() {
	if w.cancel != nil {
		w.cancel()
	}
	w.wg.Wait()
}

// State returns the current WatchdogState atomically.
func (w *Watchdog) State() WatchdogState {
	return WatchdogState(w.state.Load())
}

// RecommendedBufferSize returns the adaptive I/O chunk size for the current memory state
// (256 KiB in NORMAL, 32 KiB in WARN, 16 KiB in CRITICAL).
func (w *Watchdog) RecommendedBufferSize() int {
	return int(w.chunkSize.Load())
}

// CanAcceptNewTask returns true only when the watchdog is in StateNormal.
// In StateWarn or StateCritical, new tasks are paused/rejected.
func (w *Watchdog) CanAcceptNewTask() bool {
	if w == nil {
		return true
	}
	return w.State() == StateNormal
}

// CanAcceptHeavyOp checks whether heavy operations (such as lossless mkvmerge remuxing
// or multi-part archive extraction) are permitted under current memory and disk headroom.
func (w *Watchdog) CanAcceptHeavyOp(requiredDiskBytes uint64) error {
	if w == nil {
		return nil
	}
	if w.State() != StateNormal {
		return fmt.Errorf("%w (state=%s)", ErrCircuitBreakerOpen, w.State().String())
	}
	if err := w.CheckNVMeHeadroom(requiredDiskBytes); err != nil {
		return err
	}
	return nil
}

// Snapshot returns the most recent MemorySnapshot safely.
func (w *Watchdog) Snapshot() MemorySnapshot {
	w.mu.RLock()
	defer w.mu.RUnlock()
	return w.lastSnapshot
}

// sampleAndEvaluate reads runtime.MemStats, Linux /proc/meminfo, and /proc/stat,
// computes memory pressure, transitions state if needed, and adjusts disk I/O pacing.
func (w *Watchdog) sampleAndEvaluate() {
	var m runtime.MemStats
	runtime.ReadMemStats(&m)

	hostTotal, hostUsed := readLinuxMemInfo(w.cfg.TotalSystemRAM)
	cpuLoad := w.readLinuxCPULoad()

	// Effective process footprint combines Go heap in-use + runtime Sys relative to ceiling.
	processFootprint := m.HeapInuse
	if m.HeapAlloc > processFootprint {
		processFootprint = m.HeapAlloc
	}

	// Calculate RAM usage percentage using the higher of:
	// 1) Host memory usage percentage (from /proc/meminfo)
	// 2) Go process footprint percentage against the 3200 MiB hard ceiling.
	ceilingPct := (float64(processFootprint) / float64(w.cfg.HardCeilingBytes)) * 100.0
	hostPct := 0.0
	if hostTotal > 0 {
		hostPct = (float64(hostUsed) / float64(hostTotal)) * 100.0
	}

	effectivePct := ceilingPct
	if hostPct > effectivePct {
		effectivePct = hostPct
	}

	// Rule 2 check: If heap allocation reaches 80% of safety limit (SoftLimitBytes),
	// force CRITICAL throttling immediately.
	heapSafetyPct := (float64(m.HeapAlloc) / float64(w.cfg.SoftLimitBytes)) * 100.0
	heapExceeded80Pct := float64(m.HeapAlloc) >= float64(w.cfg.SoftLimitBytes)*w.cfg.HeapSafetyRatio

	var targetState WatchdogState
	switch {
	case effectivePct >= w.cfg.CritThresholdPct || heapExceeded80Pct || m.Sys >= w.cfg.HardCeilingBytes:
		targetState = StateCritical
	case effectivePct >= w.cfg.WarnThresholdPct:
		targetState = StateWarn
	default:
		targetState = StateNormal
	}

	// Apply buffer size and disk write rate pacing based on targetState and CPU load.
	var bufSize int
	var writeRateBps int64

	switch targetState {
	case StateNormal:
		bufSize = BufferSizeNormal
		writeRateBps = w.cfg.BaseDiskWriteBps
	case StateWarn:
		bufSize = BufferSizeWarn
		writeRateBps = w.cfg.BaseDiskWriteBps / 2 // 50% disk I/O pacing in WARN
	case StateCritical:
		bufSize = BufferSizeCritical
		writeRateBps = w.cfg.BaseDiskWriteBps / 6 // ~16% emergency disk I/O pacing in CRITICAL
	}

	// Additional CPU-aware I/O pacing: if 2-vCPU load exceeds 88%, reduce disk write rate by 40%
	// to prevent kernel iowait lockup on shared hypervisors.
	if cpuLoad >= 88.0 {
		writeRateBps = (writeRateBps * 60) / 100
	}
	if writeRateBps < 4*1024*1024 {
		writeRateBps = 4 * 1024 * 1024 // Minimum 4 MiB/s floor to prevent deadlocks
	}

	w.chunkSize.Store(int32(bufSize))
	w.diskLimiter.SetLimit(rate.Limit(writeRateBps))

	snap := MemorySnapshot{
		Timestamp:         time.Now(),
		State:             targetState,
		HeapAllocBytes:    m.HeapAlloc,
		HeapInuseBytes:    m.HeapInuse,
		SysBytes:          m.Sys,
		NumGC:             m.NumGC,
		HostMemTotalBytes: hostTotal,
		HostMemUsedBytes:  hostUsed,
		UsagePercent:      effectivePct,
		HeapSafetyPercent: heapSafetyPct,
		CPULoadPercent:    cpuLoad,
		ChunkBufferBytes:  bufSize,
		DiskWriteLimitBps: writeRateBps,
	}

	prevState := WatchdogState(w.state.Swap(int32(targetState)))

	w.mu.Lock()
	w.lastSnapshot = snap
	cb := w.callbacks
	w.mu.Unlock()

	// Execute memory reclamation actions and invoke transition hooks.
	if targetState == StateWarn && prevState != StateWarn {
		runtime.GC()
		if cb.OnEnterWarn != nil {
			cb.OnEnterWarn(snap)
		}
	} else if targetState == StateCritical {
		// Always trigger debug.FreeOSMemory() when entering CRITICAL (and periodically if stuck).
		debug.FreeOSMemory()
		if prevState != StateCritical && cb.OnEnterCritical != nil {
			cb.OnEnterCritical(snap)
		}
	} else if targetState == StateNormal && prevState != StateNormal {
		if cb.OnEnterNormal != nil {
			cb.OnEnterNormal(snap)
		}
	}
}

// WaitDiskWrite blocks until the disk I/O token bucket permits writing `n` bytes,
// preventing hypervisor I/O lockup on budget NVMe/virtio-blk storage.
func (w *Watchdog) WaitDiskWrite(ctx context.Context, n int) error {
	if n <= 0 {
		return nil
	}
	burst := w.diskLimiter.Burst()
	for n > 0 {
		step := n
		if step > burst {
			step = burst
		}
		if err := w.diskLimiter.WaitN(ctx, step); err != nil {
			return err
		}
		n -= step
	}
	return nil
}

// PacedWriter wraps an io.Writer and paces all write calls through the Watchdog's
// CPU & I/O token-bucket limiter using the Watchdog's current chunk size.
type PacedWriter struct {
	ctx      context.Context
	watchdog *Watchdog
	dst      io.Writer
}

// NewPacedWriter creates a disk-write-throttled io.Writer bound to `ctx`.
func (w *Watchdog) NewPacedWriter(ctx context.Context, dst io.Writer) *PacedWriter {
	return &PacedWriter{
		ctx:      ctx,
		watchdog: w,
		dst:      dst,
	}
}

// Write slices large buffers into adaptive chunks (`RecommendedBufferSize`) and paces
// each write against the Watchdog's disk I/O rate limiter.
func (pw *PacedWriter) Write(p []byte) (int, error) {
	total := 0
	for len(p) > 0 {
		if err := pw.ctx.Err(); err != nil {
			return total, err
		}
		chunkMax := pw.watchdog.RecommendedBufferSize()
		if chunkMax <= 0 {
			chunkMax = BufferSizeWarn
		}
		chunk := p
		if len(chunk) > chunkMax {
			chunk = p[:chunkMax]
		}
		if err := pw.watchdog.WaitDiskWrite(pw.ctx, len(chunk)); err != nil {
			return total, err
		}
		nw, err := pw.dst.Write(chunk)
		total += nw
		if err != nil {
			return total, err
		}
		p = p[nw:]
	}
	return total, nil
}

// CheckNVMeHeadroom inspects filesystem free space on the download directory using
// syscall.Statfs and verifies at least `MinNVMeFreeBytes + requiredBytes` are available.
func (w *Watchdog) CheckNVMeHeadroom(requiredBytes uint64) error {
	targetDir := w.cfg.DownloadDirectory
	if targetDir == "" {
		targetDir = "."
	}
	var stat syscall.Statfs_t
	if err := syscall.Statfs(targetDir, &stat); err != nil {
		// Fall back to current working directory if download directory is not yet created.
		if err2 := syscall.Statfs(".", &stat); err2 != nil {
			return nil
		}
	}
	availBytes := stat.Bavail * uint64(stat.Bsize)
	needed := w.cfg.MinNVMeFreeBytes + requiredBytes
	if availBytes < needed {
		return fmt.Errorf("%w: available=%d MiB, required=%d MiB",
			ErrInsufficientDiskSpace, availBytes/(1024*1024), needed/(1024*1024))
	}
	return nil
}

// readLinuxMemInfo parses /proc/meminfo on Linux to obtain accurate host memory usage.
func readLinuxMemInfo(fallbackTotal uint64) (totalBytes uint64, usedBytes uint64) {
	f, err := os.Open("/proc/meminfo")
	if err != nil {
		return fallbackTotal, 0
	}
	defer f.Close()

	var memTotalKB, memAvailableKB uint64
	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		line := scanner.Text()
		if strings.HasPrefix(line, "MemTotal:") {
			memTotalKB = parseMemInfoKB(line)
		} else if strings.HasPrefix(line, "MemAvailable:") {
			memAvailableKB = parseMemInfoKB(line)
		}
		if memTotalKB > 0 && memAvailableKB > 0 {
			break
		}
	}

	if memTotalKB == 0 {
		return fallbackTotal, 0
	}
	totalBytes = memTotalKB * 1024
	if memAvailableKB < memTotalKB {
		usedBytes = (memTotalKB - memAvailableKB) * 1024
	}
	return totalBytes, usedBytes
}

func parseMemInfoKB(line string) uint64 {
	fields := strings.Fields(line)
	if len(fields) < 2 {
		return 0
	}
	val, err := strconv.ParseUint(fields[1], 10, 64)
	if err != nil {
		return 0
	}
	return val
}

// readLinuxCPULoad computes aggregate CPU busy percentage between ticks from /proc/stat.
func (w *Watchdog) readLinuxCPULoad() float64 {
	f, err := os.Open("/proc/stat")
	if err != nil {
		return 0.0
	}
	defer f.Close()

	scanner := bufio.NewScanner(f)
	if !scanner.Scan() {
		return 0.0
	}
	fields := strings.Fields(scanner.Text())
	if len(fields) < 5 || fields[0] != "cpu" {
		return 0.0
	}

	var total, idle uint64
	for i, raw := range fields[1:] {
		val, err := strconv.ParseUint(raw, 10, 64)
		if err != nil {
			continue
		}
		total += val
		// field index 3 is idle, index 4 is iowait (Wait I/O counts as pressure on VPS!).
		if i == 3 {
			idle = val
		}
	}

	w.mu.Lock()
	prevIdle := w.prevCPUIdle
	prevTotal := w.prevCPUTotal
	w.prevCPUIdle = idle
	w.prevCPUTotal = total
	w.mu.Unlock()

	if prevTotal == 0 || total <= prevTotal {
		return 0.0
	}
	deltaTotal := total - prevTotal
	deltaIdle := idle - prevIdle
	if deltaIdle >= deltaTotal {
		return 0.0
	}
	return (float64(deltaTotal-deltaIdle) / float64(deltaTotal)) * 100.0
}
