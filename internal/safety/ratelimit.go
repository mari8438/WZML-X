// Package safety implements P2P Connection Throttling (150 global / 30 per-task TCP/UDP cap)
// and adaptive token-bucket bandwidth rate limiting.
package safety

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"sync"
	"sync/atomic"
	"time"

	"golang.org/x/time/rate"
)

const (
	// DefaultMaxGlobalConnections is the hard ceiling on simultaneous TCP/UDP sockets.
	DefaultMaxGlobalConnections = 150

	// DefaultMaxPerTaskConnections is the hard ceiling on sockets for a single task.
	DefaultMaxPerTaskConnections = 30
)

var (
	// ErrGlobalConnLimitExceeded indicates all 150 global socket slots are occupied.
	ErrGlobalConnLimitExceeded = errors.New("global P2P connection limit (150) reached")

	// ErrTaskConnLimitExceeded indicates a task has reached its 30-connection cap.
	ErrTaskConnLimitExceeded = errors.New("per-task connection limit (30) reached")
)

// RateLimitConfig configures the global and per-task connection and bandwidth limiters.
type RateLimitConfig struct {
	MaxGlobalConnections  int
	MaxPerTaskConnections int
	GlobalDownloadBps     int64
	GlobalUploadBps       int64
	DialTimeout           time.Duration
}

// RateLimiter enforces strict P2P TCP/UDP socket caps (150 global, 30 per task)
// to prevent VPS hypervisor network abuse bans, alongside adaptive bandwidth shaping.
type RateLimiter struct {
	cfg        RateLimitConfig
	logger     *slog.Logger
	globalSem  chan struct{}
	activeConn atomic.Int32

	mu          sync.Mutex
	taskSemMap  map[string]chan struct{}
	taskConnCnt map[string]*atomic.Int32

	dlLimiter      *rate.Limiter
	ulLimiter      *rate.Limiter
	baseDlBps      int64
	baseUlBps      int64
	throttleFactor atomic.Uint64 // stores float64 bits or scaled percentage (1000 = 1.0x)
}

// NewRateLimiter constructs a P2P connection and bandwidth rate limiter.
func NewRateLimiter(cfg RateLimitConfig, logger *slog.Logger) *RateLimiter {
	if cfg.MaxGlobalConnections <= 0 || cfg.MaxGlobalConnections > DefaultMaxGlobalConnections {
		cfg.MaxGlobalConnections = DefaultMaxGlobalConnections
	}
	if cfg.MaxPerTaskConnections <= 0 || cfg.MaxPerTaskConnections > DefaultMaxPerTaskConnections {
		cfg.MaxPerTaskConnections = DefaultMaxPerTaskConnections
	}
	if cfg.DialTimeout <= 0 {
		cfg.DialTimeout = 15 * time.Second
	}

	// Default baseline bandwidth when unthrottled (0 = 500 MiB/s ceiling).
	baseDl := cfg.GlobalDownloadBps
	if baseDl <= 0 {
		baseDl = 500 * 1024 * 1024
	}
	baseUl := cfg.GlobalUploadBps
	if baseUl <= 0 {
		baseUl = 500 * 1024 * 1024
	}

	rl := &RateLimiter{
		cfg:         cfg,
		logger:      logger,
		globalSem:   make(chan struct{}, cfg.MaxGlobalConnections),
		taskSemMap:  make(map[string]chan struct{}),
		taskConnCnt: make(map[string]*atomic.Int32),
		dlLimiter:   rate.NewLimiter(rate.Limit(baseDl), int(minInt64(baseDl, 4*1024*1024))),
		ulLimiter:   rate.NewLimiter(rate.Limit(baseUl), int(minInt64(baseUl, 4*1024*1024))),
		baseDlBps:   baseDl,
		baseUlBps:   baseUl,
	}
	rl.throttleFactor.Store(1000) // 100.0%
	return rl
}

// AcquireConnection blocks until both a per-task slot (<= 30) and a global slot (<= 150)
// are available, or returns an error if `ctx` expires.
// The returned release function MUST be called exactly once when the connection closes.
func (rl *RateLimiter) AcquireConnection(ctx context.Context, taskID string) (func(), error) {
	taskSem, taskCounter := rl.getOrCreateTaskSlot(taskID)

	// 1. Acquire per-task slot (max 30).
	select {
	case taskSem <- struct{}{}:
	case <-ctx.Done():
		return nil, fmt.Errorf("%w for task %s: %v", ErrTaskConnLimitExceeded, taskID, ctx.Err())
	}

	// 2. Acquire global slot (max 150).
	select {
	case rl.globalSem <- struct{}{}:
	case <-ctx.Done():
		<-taskSem
		return nil, fmt.Errorf("%w: %v", ErrGlobalConnLimitExceeded, ctx.Err())
	}

	rl.activeConn.Add(1)
	taskCounter.Add(1)

	var once sync.Once
	release := func() {
		once.Do(func() {
			<-rl.globalSem
			<-taskSem
			rl.activeConn.Add(-1)
			taskCounter.Add(-1)
		})
	}
	return release, nil
}

// TryAcquireConnection attempts non-blocking acquisition of a per-task and global slot.
func (rl *RateLimiter) TryAcquireConnection(taskID string) (func(), error) {
	taskSem, taskCounter := rl.getOrCreateTaskSlot(taskID)

	select {
	case taskSem <- struct{}{}:
	default:
		return nil, ErrTaskConnLimitExceeded
	}

	select {
	case rl.globalSem <- struct{}{}:
	default:
		<-taskSem
		return nil, ErrGlobalConnLimitExceeded
	}

	rl.activeConn.Add(1)
	taskCounter.Add(1)

	var once sync.Once
	release := func() {
		once.Do(func() {
			<-rl.globalSem
			<-taskSem
			rl.activeConn.Add(-1)
			taskCounter.Add(-1)
		})
	}
	return release, nil
}

// DialContext establishes a TCP or UDP connection governed by the 150 global / 30 per-task
// semaphore limits and wraps the resulting net.Conn so closing the socket automatically
// frees its reservation slots.
func (rl *RateLimiter) DialContext(ctx context.Context, taskID, network, address string) (net.Conn, error) {
	release, err := rl.AcquireConnection(ctx, taskID)
	if err != nil {
		return nil, err
	}

	dialer := &net.Dialer{
		Timeout:   rl.cfg.DialTimeout,
		KeepAlive: 30 * time.Second,
	}
	conn, err := dialer.DialContext(ctx, network, address)
	if err != nil {
		release()
		return nil, err
	}

	return &TrackedConn{
		Conn:    conn,
		release: release,
	}, nil
}

// CleanupTask removes per-task semaphore tracking once a task reaches a terminal state.
func (rl *RateLimiter) CleanupTask(taskID string) {
	rl.mu.Lock()
	defer rl.mu.Unlock()
	delete(rl.taskSemMap, taskID)
	delete(rl.taskConnCnt, taskID)
}

// ActiveGlobalConnections returns the instantaneous count of active TCP/UDP sockets (0..150).
func (rl *RateLimiter) ActiveGlobalConnections() int {
	return int(rl.activeConn.Load())
}

// ActiveTaskConnections returns the instantaneous count of active sockets for `taskID` (0..30).
func (rl *RateLimiter) ActiveTaskConnections(taskID string) int {
	rl.mu.Lock()
	cnt, ok := rl.taskConnCnt[taskID]
	rl.mu.Unlock()
	if !ok {
		return 0
	}
	return int(cnt.Load())
}

// SetThrottleFactor dynamically scales global download/upload bandwidth limits
// (e.g., 1.0 in NORMAL, 0.60 in WARN, 0.15 in CRITICAL).
func (rl *RateLimiter) SetThrottleFactor(factor float64) {
	if factor <= 0.05 {
		factor = 0.05
	}
	if factor > 1.0 {
		factor = 1.0
	}
	scaled := uint64(factor * 1000)
	rl.throttleFactor.Store(scaled)

	newDl := float64(rl.baseDlBps) * factor
	newUl := float64(rl.baseUlBps) * factor
	rl.dlLimiter.SetLimit(rate.Limit(newDl))
	rl.ulLimiter.SetLimit(rate.Limit(newUl))
}

// WaitDownload blocks until `n` bytes of ingress bandwidth are permitted.
func (rl *RateLimiter) WaitDownload(ctx context.Context, n int) error {
	return waitLimiterChunked(ctx, rl.dlLimiter, n)
}

// WaitUpload blocks until `n` bytes of egress bandwidth are permitted.
func (rl *RateLimiter) WaitUpload(ctx context.Context, n int) error {
	return waitLimiterChunked(ctx, rl.ulLimiter, n)
}

func (rl *RateLimiter) getOrCreateTaskSlot(taskID string) (chan struct{}, *atomic.Int32) {
	if taskID == "" {
		taskID = "_default"
	}
	rl.mu.Lock()
	defer rl.mu.Unlock()

	sem, ok := rl.taskSemMap[taskID]
	if !ok {
		sem = make(chan struct{}, rl.cfg.MaxPerTaskConnections)
		rl.taskSemMap[taskID] = sem
		rl.taskConnCnt[taskID] = &atomic.Int32{}
	}
	return sem, rl.taskConnCnt[taskID]
}

// TrackedConn wraps a standard net.Conn to guarantee connection slot release upon Close().
type TrackedConn struct {
	net.Conn
	release func()
}

// Close closes the underlying socket and releases the global and per-task connection slots.
func (tc *TrackedConn) Close() error {
	defer tc.release()
	return tc.Conn.Close()
}

func waitLimiterChunked(ctx context.Context, lim *rate.Limiter, n int) error {
	if n <= 0 {
		return nil
	}
	burst := lim.Burst()
	for n > 0 {
		step := n
		if step > burst {
			step = burst
		}
		if err := lim.WaitN(ctx, step); err != nil {
			return err
		}
		n -= step
	}
	return nil
}

func minInt64(a, b int64) int64 {
	if a < b {
		return a
	}
	return b
}
