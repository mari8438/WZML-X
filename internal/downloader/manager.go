// Package downloader provides the Unified Leech Manager (`Start`, `Pause`, `Resume`, `Abort`)
// orchestrating direct cloud drive, cyberlocker, and video stream downloads through
// the bounded Starfall-Go Dispatcher and 512 KiB buffer pool.
package downloader

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"strings"

	"github.com/starfall-org/starfall-go/internal/downloader/buffer"
	"github.com/starfall-org/starfall-go/internal/downloader/direct"
	"github.com/starfall-org/starfall-go/internal/engine"
	"github.com/starfall-org/starfall-go/internal/ipc"
	"github.com/starfall-org/starfall-go/internal/models"
	"github.com/starfall-org/starfall-go/internal/safety"
)

var (
	// ErrTelegramDumpNotLinked is returned when a user starts a web leech task
	// before linking their `telegram_dump_chat` channel ID in Web Profile settings.
	ErrTelegramDumpNotLinked = errors.New("telegram_dump_chat is not linked: configure your private Telegram channel ID first")
)

// ManagerConfig configures the Unified Direct Leech Manager.
type ManagerConfig struct {
	DownloadDir         string
	RequireTelegramDump bool
	GDriveAPIKey        string
	TeraboxCookie       string
	YTDLPBinaryPath     string
}

// Manager coordinates lifecycle operations (`Start`, `Pause`, `Resume`, `Abort`)
// for all direct leech tasks.
type Manager struct {
	cfg        ManagerConfig
	bufPool    *buffer.Pool
	resolver   *direct.Resolver
	dispatcher *engine.Dispatcher
	registry   *engine.TaskRegistry
	watchdog   *safety.Watchdog
	limiter    *safety.RateLimiter
	bus        *ipc.Bus
	logger     *slog.Logger
}

// NewManager constructs a Unified Leech Manager backed by the 512 KiB `buffer.Pool`
// and Universal Direct Link Resolver.
func NewManager(
	cfg ManagerConfig,
	dispatcher *engine.Dispatcher,
	registry *engine.TaskRegistry,
	watchdog *safety.Watchdog,
	limiter *safety.RateLimiter,
	bus *ipc.Bus,
	logger *slog.Logger,
) *Manager {
	if strings.TrimSpace(cfg.DownloadDir) == "" {
		cfg.DownloadDir = "/app/downloads"
	}

	bufPool := buffer.NewPool()
	resolver := direct.NewResolver(
		direct.ResolverConfig{
			DefaultDownloadDir: cfg.DownloadDir,
			GDriveAPIKey:       cfg.GDriveAPIKey,
			TeraboxCookie:      cfg.TeraboxCookie,
			YTDLPBinaryPath:    cfg.YTDLPBinaryPath,
		},
		bufPool,
		watchdog,
		limiter,
		bus,
		logger,
	)

	return &Manager{
		cfg:        cfg,
		bufPool:    bufPool,
		resolver:   resolver,
		dispatcher: dispatcher,
		registry:   registry,
		watchdog:   watchdog,
		limiter:    limiter,
		bus:        bus,
		logger:     logger,
	}
}

// Start validates the source URL and user's linked `telegram_dump_chat`, creates a
// `models.Task` bound to the Universal Link Resolver, and submits it to the Dispatcher.
func (m *Manager) Start(owner *models.User, rawURL string, needsRemux bool) (*models.Task, error) {
	if owner == nil || strings.TrimSpace(owner.ID) == "" {
		return nil, errors.New("authenticated user is required to start a leech task")
	}
	if m.cfg.RequireTelegramDump && !owner.HasTelegramDumpLinked() {
		return nil, ErrTelegramDumpNotLinked
	}

	kind, parsedURL, err := m.resolver.Classify(rawURL)
	if err != nil {
		return nil, err
	}

	taskID := generateTaskID()
	initialName := fmt.Sprintf("[%s] %s", kind, parsedURL.Host)
	task := models.NewTask(taskID, owner, initialName, rawURL, models.TaskTypeDownload, 0)
	task.NeedsRemux = needsRemux
	task.NeedsUpload = owner.HasTelegramDumpLinked()

	task.Work = func(ctx context.Context, t *models.Task) error {
		res, dlErr := m.resolver.ResolveAndDownload(ctx, t, rawURL, m.cfg.DownloadDir)
		if dlErr != nil {
			return dlErr
		}
		t.Name = res.FileName
		t.OutputPath = res.FilePath
		m.logger.Info("direct leech download stage completed",
			slog.String("task_id", t.ID),
			slog.String("source", string(res.Source)),
			slog.String("file_path", res.FilePath),
			slog.Int64("bytes_saved", res.BytesSaved),
			slog.String("target_telegram_dump", t.TelegramDumpChat),
		)
		return nil
	}

	if err := m.dispatcher.Submit(task); err != nil {
		return nil, err
	}
	return task, nil
}

// Pause suspends an active download task so its chunk workers pause at the next 512 KiB boundary.
func (m *Manager) Pause(taskID string) error {
	task, err := m.registry.Get(taskID)
	if err != nil {
		return err
	}
	if !task.Pause() {
		return fmt.Errorf("task %s is not in a pausable state (current=%s)", taskID, task.Status())
	}
	m.logger.Info("paused direct leech task", slog.String("task_id", taskID))
	return nil
}

// Resume unblocks a paused download task so its workers continue from their exact byte offsets.
func (m *Manager) Resume(taskID string) error {
	task, err := m.registry.Get(taskID)
	if err != nil {
		return err
	}
	if !task.Resume() {
		return fmt.Errorf("task %s is not currently paused", taskID)
	}
	m.logger.Info("resumed direct leech task", slog.String("task_id", taskID))
	return nil
}

// Abort cancels an in-flight or queued task immediately, removes any partial file on disk,
// and publishes `EventTaskFailed` (canceled) to the internal event bus.
func (m *Manager) Abort(taskID string, reason string) error {
	task, err := m.registry.Get(taskID)
	if err != nil {
		return err
	}
	if strings.TrimSpace(reason) == "" {
		reason = "aborted by user"
	}

	task.Cancel(reason)
	_, _ = m.registry.UpdateStatus(taskID, models.StatusCanceled, reason)

	snap := task.Snapshot()
	if snap.OutputPath != "" {
		_ = os.Remove(snap.OutputPath)
	}

	m.logger.Info("aborted direct leech task and cleaned partial file",
		slog.String("task_id", taskID),
		slog.String("reason", reason),
	)
	return nil
}

// BufferPool returns the underlying 512 KiB buffer pool for telemetry inspection.
func (m *Manager) BufferPool() *buffer.Pool {
	return m.bufPool
}

func generateTaskID() string {
	b := make([]byte, 8)
	_, _ = rand.Read(b)
	return "sf_" + hex.EncodeToString(b)
}
