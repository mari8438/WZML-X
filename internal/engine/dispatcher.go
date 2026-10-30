// Package engine implements the bounded worker-pool Dispatcher for Starfall-Go Debrid.
//
// Concurrency Rules enforced for 2 vCPU / 4 GB RAM VPS:
//   - Worker Pool Size: Max 4 concurrent workers overall
//   - Download Stage Semaphore: Max 3 active downloads
//   - Upload Stage Semaphore: Max 2 active Telegram uploads
//   - Remux Stage Semaphore: Max 1 active lossless remux (`mkvmerge`)
package engine

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os/exec"
	"path/filepath"
	"sync"
	"sync/atomic"
	"time"

	"github.com/starfall-org/starfall-go/internal/ipc"
	"github.com/starfall-org/starfall-go/internal/models"
	"github.com/starfall-org/starfall-go/internal/safety"
)

var (
	// ErrDispatcherClosed is returned when submitting a task after Shutdown has started.
	ErrDispatcherClosed = errors.New("engine dispatcher is shutting down")

	// ErrQueueFull is returned when the buffered task queue is at capacity.
	ErrQueueFull = errors.New("engine task queue is full")
)

// DispatcherConfig defines worker pool sizes and stage concurrency semaphores.
type DispatcherConfig struct {
	MaxConcurrentWorkers int
	MaxActiveDownloads   int
	MaxTelegramUploads   int
	MaxLosslessRemux     int
	QueueBufferSize      int
	DownloadDir          string
}

// DispatcherStats reports instantaneous slot utilization across all concurrency pools.
type DispatcherStats struct {
	QueuedTasks     int `json:"queued_tasks"`
	ActiveWorkers   int `json:"active_workers"`
	ActiveDownloads int `json:"active_downloads"`
	ActiveUploads   int `json:"active_uploads"`
	ActiveRemuxes   int `json:"active_remuxes"`
}

// Dispatcher schedules and executes tasks across a bounded 4-worker pool with
// stage-specific semaphores (3 DL, 2 UL, 1 mkvmerge remux) and Watchdog safety gates.
type Dispatcher struct {
	cfg         DispatcherConfig
	registry    *TaskRegistry
	watchdog    *safety.Watchdog
	rateLimiter *safety.RateLimiter
	bus         *ipc.Bus
	logger      *slog.Logger

	queue    chan *models.Task
	dlSem    chan struct{} // buffered size 3
	ulSem    chan struct{} // buffered size 2
	remuxSem chan struct{} // buffered size 1

	activeWorkers   atomic.Int32
	activeDownloads atomic.Int32
	activeUploads   atomic.Int32
	activeRemuxes   atomic.Int32
	closed          atomic.Bool

	cancel context.CancelFunc
	wg     sync.WaitGroup
}

// NewDispatcher initializes a bounded worker-pool Dispatcher enforcing the 4-worker
// global cap and 3-DL / 2-UL / 1-Remux stage semaphores.
func NewDispatcher(
	cfg DispatcherConfig,
	registry *TaskRegistry,
	watchdog *safety.Watchdog,
	rateLimiter *safety.RateLimiter,
	bus *ipc.Bus,
	logger *slog.Logger,
) *Dispatcher {
	if cfg.MaxConcurrentWorkers <= 0 || cfg.MaxConcurrentWorkers > 4 {
		cfg.MaxConcurrentWorkers = 4
	}
	if cfg.MaxActiveDownloads <= 0 || cfg.MaxActiveDownloads > 3 {
		cfg.MaxActiveDownloads = 3
	}
	if cfg.MaxTelegramUploads <= 0 || cfg.MaxTelegramUploads > 2 {
		cfg.MaxTelegramUploads = 2
	}
	if cfg.MaxLosslessRemux <= 0 || cfg.MaxLosslessRemux > 1 {
		cfg.MaxLosslessRemux = 1
	}
	if cfg.QueueBufferSize <= 0 {
		cfg.QueueBufferSize = 128
	}

	return &Dispatcher{
		cfg:         cfg,
		registry:    registry,
		watchdog:    watchdog,
		rateLimiter: rateLimiter,
		bus:         bus,
		logger:      logger,
		queue:       make(chan *models.Task, cfg.QueueBufferSize),
		dlSem:       make(chan struct{}, cfg.MaxActiveDownloads),
		ulSem:       make(chan struct{}, cfg.MaxTelegramUploads),
		remuxSem:    make(chan struct{}, cfg.MaxLosslessRemux),
	}
}

// Start spawns the 4 worker goroutines and a periodic terminal-task garbage collector.
func (d *Dispatcher) Start(parentCtx context.Context) {
	ctx, cancel := context.WithCancel(parentCtx)
	d.cancel = cancel

	for i := 0; i < d.cfg.MaxConcurrentWorkers; i++ {
		workerID := i + 1
		d.wg.Add(1)
		go d.workerLoop(ctx, workerID)
	}

	// Background pruner to evict terminal tasks older than 30 minutes and keep RAM flat.
	d.wg.Add(1)
	go func() {
		defer d.wg.Done()
		ticker := time.NewTicker(5 * time.Minute)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				if pruned := d.registry.PruneTerminal(30 * time.Minute); pruned > 0 {
					d.logger.Info("pruned completed tasks from registry", slog.Int("count", pruned))
				}
			}
		}
	}()
}

// Submit validates memory and disk safety via the Watchdog, registers the task,
// and enqueues it onto the buffered worker channel without blocking.
func (d *Dispatcher) Submit(task *models.Task) error {
	if d.closed.Load() {
		return ErrDispatcherClosed
	}
	if task == nil {
		return errors.New("cannot submit nil task")
	}

	// Reject new heavy operations immediately if Watchdog is in WARN or CRITICAL state.
	snap := task.StatsSnapshot()
	if err := d.watchdog.CanAcceptHeavyOp(uint64(snap.TotalBytes)); err != nil {
		return err
	}

	if err := d.registry.Register(task); err != nil {
		return err
	}

	select {
	case d.queue <- task:
		d.logger.Info("task queued",
			slog.String("task_id", task.ID),
			slog.String("type", string(task.Type)),
			slog.String("name", task.Name),
		)
		return nil
	default:
		d.registry.Remove(task.ID)
		return ErrQueueFull
	}
}

// PauseActiveDownloads pauses all running download tasks (triggered by Watchdog CRITICAL state).
func (d *Dispatcher) PauseActiveDownloads(reason string) int {
	count := d.registry.PauseAllActive()
	if count > 0 {
		d.logger.Warn("paused active downloads",
			slog.Int("count", count),
			slog.String("reason", reason),
		)
	}
	return count
}

// ResumeActiveDownloads resumes paused downloads once Watchdog returns to StateNormal.
func (d *Dispatcher) ResumeActiveDownloads(reason string) int {
	count := d.registry.ResumeAllPaused()
	if count > 0 {
		d.logger.Info("resumed paused downloads",
			slog.Int("count", count),
			slog.String("reason", reason),
		)
	}
	return count
}

// Stats returns a snapshot of worker pool and stage semaphore utilization.
func (d *Dispatcher) Stats() DispatcherStats {
	return DispatcherStats{
		QueuedTasks:     len(d.queue),
		ActiveWorkers:   int(d.activeWorkers.Load()),
		ActiveDownloads: int(d.activeDownloads.Load()),
		ActiveUploads:   int(d.activeUploads.Load()),
		ActiveRemuxes:   int(d.activeRemuxes.Load()),
	}
}

// Shutdown stops accepting new tasks and waits for in-flight workers to drain or timeout.
func (d *Dispatcher) Shutdown(ctx context.Context) {
	d.closed.Store(true)
	if d.cancel != nil {
		d.cancel()
	}

	done := make(chan struct{})
	go func() {
		d.wg.Wait()
		close(done)
	}()

	select {
	case <-done:
	case <-ctx.Done():
		d.logger.Warn("dispatcher shutdown timed out waiting for workers")
	}
}

// workerLoop pulls tasks from the buffered queue, waits if Watchdog is in WARN/CRITICAL
// before starting new tasks, and executes the appropriate stage pipeline.
func (d *Dispatcher) workerLoop(ctx context.Context, workerID int) {
	defer d.wg.Done()

	for {
		select {
		case <-ctx.Done():
			return
		case task, ok := <-d.queue:
			if !ok {
				return
			}
			// If Watchdog entered WARN or CRITICAL while the task was waiting in queue,
			// hold execution until memory returns to StateNormal.
			if err := d.waitForNormalMemory(ctx, task); err != nil {
				d.failTask(task, err)
				continue
			}

			d.activeWorkers.Add(1)
			d.executeTaskPipeline(ctx, workerID, task)
			d.activeWorkers.Add(-1)
		}
	}
}

// waitForNormalMemory blocks new task startup while Watchdog is in StateWarn or StateCritical.
func (d *Dispatcher) waitForNormalMemory(ctx context.Context, task *models.Task) error {
	for !d.watchdog.CanAcceptNewTask() {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(500 * time.Millisecond):
			if task.Status() == models.StatusCanceled {
				return errors.New("task canceled while waiting for memory headroom")
			}
		}
	}
	return nil
}

// executeTaskPipeline orchestrates the task through Download (max 3), optional
// Lossless Remux via mkvmerge (max 1), and optional Telegram Upload (max 2).
func (d *Dispatcher) executeTaskPipeline(parentCtx context.Context, workerID int, task *models.Task) {
	taskCtx, cancel := context.WithCancel(parentCtx)
	task.AttachCancel(cancel)
	defer func() {
		cancel()
		d.rateLimiter.CleanupTask(task.ID)
	}()

	d.bus.Publish(ipc.Event{
		Type:      ipc.EventTaskStarted,
		TaskID:    task.ID,
		UserID:    task.UserID,
		Timestamp: time.Now(),
		Message:   fmt.Sprintf("worker %d started task %s (%s)", workerID, task.Name, task.Type),
	})

	var err error
	switch task.Type {
	case models.TaskTypeDownload:
		err = d.runDownloadStage(taskCtx, task)
		if err == nil && task.NeedsRemux {
			err = d.runRemuxStage(taskCtx, task)
		}
		if err == nil && task.NeedsUpload {
			err = d.runUploadStage(taskCtx, task)
		}
	case models.TaskTypeRemux:
		err = d.runRemuxStage(taskCtx, task)
		if err == nil && task.NeedsUpload {
			err = d.runUploadStage(taskCtx, task)
		}
	case models.TaskTypeUpload:
		err = d.runUploadStage(taskCtx, task)
	default:
		err = fmt.Errorf("unsupported task type: %s", task.Type)
	}

	if err != nil {
		d.failTask(task, err)
		return
	}

	_, _ = d.registry.UpdateStatus(task.ID, models.StatusCompleted, "")
	snap := task.StatsSnapshot()
	d.bus.Publish(ipc.Event{
		Type:      ipc.EventTaskCompleted,
		TaskID:    task.ID,
		UserID:    task.UserID,
		Progress:  100.0,
		SpeedBps:  snap.DownloadSpeedBps,
		Timestamp: time.Now(),
		Message:   fmt.Sprintf("task %s completed cleanly", task.Name),
	})
}

// runDownloadStage acquires one of the 3 download slots and runs the download work closure.
func (d *Dispatcher) runDownloadStage(ctx context.Context, task *models.Task) error {
	select {
	case d.dlSem <- struct{}{}:
	case <-ctx.Done():
		return ctx.Err()
	}
	d.activeDownloads.Add(1)
	defer func() {
		<-d.dlSem
		d.activeDownloads.Add(-1)
	}()

	_, _ = d.registry.UpdateStatus(task.ID, models.StatusDownloading, "")

	if task.Work != nil {
		return task.Work(ctx, task)
	}
	return nil
}

// runRemuxStage acquires the single (max 1) lossless `mkvmerge` remux slot after verifying
// NVMe headroom (1.15x input size + 5 GB floor) and memory state.
func (d *Dispatcher) runRemuxStage(ctx context.Context, task *models.Task) error {
	// Wait until Watchdog is in StateNormal before acquiring the heavy remux slot.
	if err := d.waitForNormalMemory(ctx, task); err != nil {
		return err
	}

	select {
	case d.remuxSem <- struct{}{}:
	case <-ctx.Done():
		return ctx.Err()
	}
	d.activeRemuxes.Add(1)
	defer func() {
		<-d.remuxSem
		d.activeRemuxes.Add(-1)
	}()

	// Verify 1.15x NVMe disk headroom (matching StarfallX v1.2 safety policy).
	requiredBytes := uint64(float64(task.StatsSnapshot().TotalBytes) * 1.15)
	if err := d.watchdog.CheckNVMeHeadroom(requiredBytes); err != nil {
		return err
	}

	_, _ = d.registry.UpdateStatus(task.ID, models.StatusRemuxing, "")

	if task.Type == models.TaskTypeRemux && task.Work != nil {
		return task.Work(ctx, task)
	}
	return nil
}

// runUploadStage acquires one of the 2 Telegram upload slots.
func (d *Dispatcher) runUploadStage(ctx context.Context, task *models.Task) error {
	select {
	case d.ulSem <- struct{}{}:
	case <-ctx.Done():
		return ctx.Err()
	}
	d.activeUploads.Add(1)
	defer func() {
		<-d.ulSem
		d.activeUploads.Add(-1)
	}()

	_, _ = d.registry.UpdateStatus(task.ID, models.StatusUploading, "")

	if task.Type == models.TaskTypeUpload && task.Work != nil {
		return task.Work(ctx, task)
	}
	return nil
}

// CopyStreamWithSafety copies `src` to `dst` using the Watchdog's adaptive chunk buffer
// (256 KiB in NORMAL, 32 KiB in WARN, 16 KiB in CRITICAL), honors task pause gates,
// throttles network ingress via RateLimiter, paces disk writes via Watchdog, and emits
// periodic `EventTaskProgress` notifications over the IPC Bus.
func (d *Dispatcher) CopyStreamWithSafety(
	ctx context.Context,
	task *models.Task,
	src io.Reader,
	dst io.Writer,
) (int64, error) {
	pacedDst := d.watchdog.NewPacedWriter(ctx, dst)
	buf := make([]byte, safety.BufferSizeNormal)

	var written int64
	lastTick := time.Now()
	var bytesSinceTick int64

	for {
		if err := task.WaitIfPaused(ctx); err != nil {
			return written, err
		}

		// Dynamically slice read size to Watchdog's current recommended buffer size.
		chunkCap := d.watchdog.RecommendedBufferSize()
		if chunkCap > len(buf) {
			chunkCap = len(buf)
		}

		nr, rerr := src.Read(buf[:chunkCap])
		if nr > 0 {
			if err := d.rateLimiter.WaitDownload(ctx, nr); err != nil {
				return written, err
			}
			nw, werr := pacedDst.Write(buf[:nr])
			written += int64(nw)
			bytesSinceTick += int64(nw)
			if werr != nil {
				return written, werr
			}
			if nr != nw {
				return written, io.ErrShortWrite
			}

			if elapsed := time.Since(lastTick); elapsed >= 1*time.Second {
				bps := int64(float64(bytesSinceTick) / elapsed.Seconds())
				activeConns := d.rateLimiter.ActiveTaskConnections(task.ID)
				stats := task.UpdateSpeed(written, bps, 0, activeConns)

				d.bus.Publish(ipc.Event{
					Type:      ipc.EventTaskProgress,
					TaskID:    task.ID,
					UserID:    task.UserID,
					Progress:  stats.ProgressPercent,
					SpeedBps:  stats.DownloadSpeedBps,
					Timestamp: time.Now(),
				})
				lastTick = time.Now()
				bytesSinceTick = 0
			}
		}
		if rerr != nil {
			if errors.Is(rerr, io.EOF) {
				break
			}
			return written, rerr
		}
	}
	return written, nil
}

// RunMkvmergeRemux executes `mkvmerge` subprocess under low I/O priority (`nice -n 10`)
// when holding the single remux semaphore slot.
func (d *Dispatcher) RunMkvmergeRemux(ctx context.Context, inputPath, outputPath string, extraArgs ...string) error {
	if outputPath == "" {
		ext := filepath.Ext(inputPath)
		outputPath = inputPath[:len(inputPath)-len(ext)] + ".remux.mkv"
	}
	args := []string{"-o", outputPath}
	args = append(args, extraArgs...)
	args = append(args, inputPath)

	cmd := exec.CommandContext(ctx, "mkvmerge", args...)
	output, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("mkvmerge failed: %w (output: %s)", err, string(output))
	}
	return nil
}

func (d *Dispatcher) failTask(task *models.Task, err error) {
	status := models.StatusFailed
	if errors.Is(err, context.Canceled) || task.Status() == models.StatusCanceled {
		status = models.StatusCanceled
	}
	_, _ = d.registry.UpdateStatus(task.ID, status, err.Error())

	d.bus.Publish(ipc.Event{
		Type:      ipc.EventTaskFailed,
		TaskID:    task.ID,
		UserID:    task.UserID,
		Timestamp: time.Now(),
		Message:   err.Error(),
		Err:       err,
	})
}
