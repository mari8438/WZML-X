// Package models defines core domain entities for Starfall-Go Debrid, including
// Task lifecycles, transfer speed telemetry, and Google OAuth2 user delivery targets.
package models

import (
	"context"
	"sync"
	"sync/atomic"
	"time"
)

// TaskType classifies the workload pipeline stage required by a task.
type TaskType string

const (
	// TaskTypeDownload represents an HTTP/Debrid/BitTorrent/Aria2 ingress transfer.
	TaskTypeDownload TaskType = "DOWNLOAD"

	// TaskTypeRemux represents a lossless container remux operation via mkvmerge/FFmpeg.
	TaskTypeRemux TaskType = "REMUX"

	// TaskTypeUpload represents an egress upload to the user's linked Telegram dump channel.
	TaskTypeUpload TaskType = "UPLOAD"
)

// TaskStatus represents the lifecycle state of a Task.
type TaskStatus string

const (
	StatusQueued      TaskStatus = "QUEUED"
	StatusDownloading TaskStatus = "DOWNLOADING"
	StatusPaused      TaskStatus = "PAUSED"
	StatusRemuxing    TaskStatus = "REMUXING"
	StatusUploading   TaskStatus = "UPLOADING"
	StatusCompleted   TaskStatus = "COMPLETED"
	StatusFailed      TaskStatus = "FAILED"
	StatusCanceled    TaskStatus = "CANCELED"
)

// IsTerminal returns true if the task has finished execution and will not transition further.
func (s TaskStatus) IsTerminal() bool {
	return s == StatusCompleted || s == StatusFailed || s == StatusCanceled
}

// SpeedStats holds real-time throughput, progress, and socket telemetry for a Task.
type SpeedStats struct {
	TotalBytes        int64         `json:"total_bytes"`
	CompletedBytes    int64         `json:"completed_bytes"`
	DownloadSpeedBps  int64         `json:"download_speed_bps"`
	UploadSpeedBps    int64         `json:"upload_speed_bps"`
	ProgressPercent   float64       `json:"progress_percent"`
	ETASeconds        int64         `json:"eta_seconds"`
	ActiveConnections int           `json:"active_connections"`
	LastUpdated       time.Time     `json:"last_updated"`
	Elapsed           time.Duration `json:"elapsed"`
}

// MediaMetadata holds optional video/anime stream properties used during lossless remux
// and Starfall auto-poster / caption generation.
type MediaMetadata struct {
	Title          string   `json:"title,omitempty"`
	Year           int      `json:"year,omitempty"`
	Season         int      `json:"season,omitempty"`
	Episode        int      `json:"episode,omitempty"`
	Resolution     string   `json:"resolution,omitempty"`
	VideoCodec     string   `json:"video_codec,omitempty"`
	AudioLanguages []string `json:"audio_languages,omitempty"`
	SubLanguages   []string `json:"sub_languages,omitempty"`
}

// TaskWorkFunc is the execution closure invoked by the Dispatcher once concurrency
// semaphores and Watchdog memory checks pass.
type TaskWorkFunc func(ctx context.Context, task *Task) error

// Task represents a unit of work scheduled and tracked by the Starfall-Go Debrid engine.
type Task struct {
	ID               string        `json:"id"`
	UserID           string        `json:"user_id"`            // Google Sub ID of task owner
	TelegramDumpChat string        `json:"telegram_dump_chat"` // Destination -100xxxxxxxxxx channel
	PosterTemplate   int           `json:"poster_template"`    // User's selected template (1-11)
	Name             string        `json:"name"`
	SourceURI        string        `json:"source_uri"`
	OutputPath       string        `json:"output_path"`
	Type             TaskType      `json:"type"`
	NeedsRemux       bool          `json:"needs_remux"`
	NeedsUpload      bool          `json:"needs_upload"`
	Metadata         MediaMetadata `json:"metadata"`
	CreatedAt        time.Time     `json:"created_at"`
	StartedAt        time.Time     `json:"started_at,omitempty"`
	FinishedAt       time.Time     `json:"finished_at,omitempty"`

	mu         sync.RWMutex
	status     TaskStatus
	stats      SpeedStats
	errMessage string
	cancelFunc context.CancelFunc
	pausedFlag atomic.Bool
	resumeCh   chan struct{}
	Work       TaskWorkFunc `json:"-"`
}

// NewTask constructs a thread-safe Task instance initialized in StatusQueued and bound
// to the authenticated Google user's `TelegramDumpChat` destination channel.
func NewTask(id string, owner *User, name, sourceURI string, taskType TaskType, totalBytes int64) *Task {
	now := time.Now().UTC()
	var userID, dumpChat string
	posterTpl := DefaultPosterTemplate
	if owner != nil {
		userID = owner.ID
		dumpChat = owner.TelegramDumpChat
		if owner.PosterTemplate >= MinPosterTemplate && owner.PosterTemplate <= MaxPosterTemplate {
			posterTpl = owner.PosterTemplate
		}
	}
	return &Task{
		ID:               id,
		UserID:           userID,
		TelegramDumpChat: dumpChat,
		PosterTemplate:   posterTpl,
		Name:             name,
		SourceURI:        sourceURI,
		Type:             taskType,
		NeedsUpload:      dumpChat != "",
		CreatedAt:        now,
		status:           StatusQueued,
		stats: SpeedStats{
			TotalBytes:  totalBytes,
			LastUpdated: now,
		},
		resumeCh: make(chan struct{}),
	}
}

// Status returns the current TaskStatus safely.
func (t *Task) Status() TaskStatus {
	t.mu.RLock()
	defer t.mu.RUnlock()
	return t.status
}

// SetStatus updates the task's lifecycle status and timestamps safely.
func (t *Task) SetStatus(next TaskStatus, errMsg string) {
	t.mu.Lock()
	defer t.mu.Unlock()

	now := time.Now().UTC()
	if (next == StatusDownloading || next == StatusRemuxing || next == StatusUploading) && t.StartedAt.IsZero() {
		t.StartedAt = now
	}
	if next.IsTerminal() {
		t.FinishedAt = now
	}
	t.status = next
	if errMsg != "" {
		t.errMessage = errMsg
	}
}

// ErrorMessage returns the failure reason if the task failed or was canceled.
func (t *Task) ErrorMessage() string {
	t.mu.RLock()
	defer t.mu.RUnlock()
	return t.errMessage
}

// AttachCancel binds a context.CancelFunc so the task can be aborted at any time.
func (t *Task) AttachCancel(cancel context.CancelFunc) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.cancelFunc = cancel
}

// Cancel aborts the task's execution context and marks it StatusCanceled.
func (t *Task) Cancel(reason string) {
	t.mu.Lock()
	cancel := t.cancelFunc
	if !t.status.IsTerminal() {
		t.status = StatusCanceled
		t.errMessage = reason
		t.FinishedAt = time.Now().UTC()
	}
	t.mu.Unlock()

	if cancel != nil {
		cancel()
	}
	t.Resume()
}

// Pause sets the task's pause gate so transfer loops suspend until Resume() is called.
func (t *Task) Pause() bool {
	if t.pausedFlag.CompareAndSwap(false, true) {
		t.mu.Lock()
		if t.status == StatusDownloading {
			t.status = StatusPaused
		}
		t.resumeCh = make(chan struct{})
		t.mu.Unlock()
		return true
	}
	return false
}

// Resume releases the pause gate and restores StatusDownloading if paused.
func (t *Task) Resume() bool {
	if t.pausedFlag.CompareAndSwap(true, false) {
		t.mu.Lock()
		if t.status == StatusPaused {
			t.status = StatusDownloading
		}
		ch := t.resumeCh
		t.mu.Unlock()
		close(ch)
		return true
	}
	return false
}

// IsPaused reports whether the task is currently paused by the user or Watchdog.
func (t *Task) IsPaused() bool {
	return t.pausedFlag.Load()
}

// WaitIfPaused blocks the calling worker if the task has been paused.
func (t *Task) WaitIfPaused(ctx context.Context) error {
	if !t.pausedFlag.Load() {
		return nil
	}
	t.mu.RLock()
	ch := t.resumeCh
	t.mu.RUnlock()

	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-ch:
		return nil
	}
}

// UpdateSpeed recalculates EMA-smoothed transfer speed, completion percentage, and ETA.
func (t *Task) UpdateSpeed(completedBytes int64, instantDlBps, instantUlBps int64, activeConns int) SpeedStats {
	t.mu.Lock()
	defer t.mu.Unlock()

	now := time.Now().UTC()
	const alpha = 0.35
	if t.stats.DownloadSpeedBps == 0 {
		t.stats.DownloadSpeedBps = instantDlBps
	} else {
		t.stats.DownloadSpeedBps = int64(alpha*float64(instantDlBps) + (1.0-alpha)*float64(t.stats.DownloadSpeedBps))
	}

	if t.stats.UploadSpeedBps == 0 {
		t.stats.UploadSpeedBps = instantUlBps
	} else {
		t.stats.UploadSpeedBps = int64(alpha*float64(instantUlBps) + (1.0-alpha)*float64(t.stats.UploadSpeedBps))
	}

	t.stats.CompletedBytes = completedBytes
	t.stats.ActiveConnections = activeConns
	t.stats.LastUpdated = now
	if !t.StartedAt.IsZero() {
		t.stats.Elapsed = now.Sub(t.StartedAt)
	}

	if t.stats.TotalBytes > 0 {
		pct := (float64(completedBytes) / float64(t.stats.TotalBytes)) * 100.0
		if pct > 100.0 {
			pct = 100.0
		}
		t.stats.ProgressPercent = pct

		remaining := t.stats.TotalBytes - completedBytes
		activeRate := t.stats.DownloadSpeedBps
		if t.status == StatusUploading && t.stats.UploadSpeedBps > 0 {
			activeRate = t.stats.UploadSpeedBps
		}
		if remaining > 0 && activeRate > 0 {
			t.stats.ETASeconds = remaining / activeRate
		} else {
			t.stats.ETASeconds = 0
		}
	}

	return t.stats
}

// StatsSnapshot returns a value copy of the task's current SpeedStats.
func (t *Task) StatsSnapshot() SpeedStats {
	t.mu.RLock()
	defer t.mu.RUnlock()
	return t.stats
}

// TaskSnapshot is an immutable, lock-free view of a Task suitable for JSON serialization.
type TaskSnapshot struct {
	ID               string        `json:"id"`
	UserID           string        `json:"user_id"`
	TelegramDumpChat string        `json:"telegram_dump_chat"`
	PosterTemplate   int           `json:"poster_template"`
	Name             string        `json:"name"`
	SourceURI        string        `json:"source_uri"`
	OutputPath       string        `json:"output_path"`
	Type             TaskType      `json:"type"`
	Status           TaskStatus    `json:"status"`
	Stats            SpeedStats    `json:"stats"`
	ErrorMessage     string        `json:"error_message,omitempty"`
	CreatedAt        time.Time     `json:"created_at"`
	StartedAt        time.Time     `json:"started_at,omitempty"`
	FinishedAt       time.Time     `json:"finished_at,omitempty"`
	Metadata         MediaMetadata `json:"metadata"`
}

// Snapshot produces an immutable TaskSnapshot under read lock.
func (t *Task) Snapshot() TaskSnapshot {
	t.mu.RLock()
	defer t.mu.RUnlock()
	return TaskSnapshot{
		ID:               t.ID,
		UserID:           t.UserID,
		TelegramDumpChat: t.TelegramDumpChat,
		PosterTemplate:   t.PosterTemplate,
		Name:             t.Name,
		SourceURI:        t.SourceURI,
		OutputPath:       t.OutputPath,
		Type:             t.Type,
		Status:           t.status,
		Stats:            t.stats,
		ErrorMessage:     t.errMessage,
		CreatedAt:        t.CreatedAt,
		StartedAt:        t.StartedAt,
		FinishedAt:       t.FinishedAt,
		Metadata:         t.Metadata,
	}
}
