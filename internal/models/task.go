// Package models defines core domain entities for Starfall-Go Debrid, including
// Task lifecycles, transfer speed telemetry, media metadata, and video mux options.
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
	// TaskTypeDownload represents an HTTP/Debrid/BitTorrent/Aria2/yt-dlp ingress transfer.
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
	Seeders           int           `json:"seeders,omitempty"`
	Leechers          int           `json:"leechers,omitempty"`
	LastUpdated       time.Time     `json:"last_updated"`
	Elapsed           time.Duration `json:"elapsed"`
}

// MediaMetadata holds video/anime stream properties extracted from filenames, ffprobe,
// and AniList/TMDb for smart auto-renaming, lossless remuxing, and poster generation.
type MediaMetadata struct {
	Title          string   `json:"title,omitempty"`
	Year           int      `json:"year,omitempty"`
	Season         int      `json:"season,omitempty"`
	Episode        int      `json:"episode,omitempty"`
	EndEpisode     int      `json:"end_episode,omitempty"`
	TotalEpisodes  int      `json:"total_episodes,omitempty"`
	Resolution     string   `json:"resolution,omitempty"`
	BitDepth       string   `json:"bit_depth,omitempty"`
	VideoCodec     string   `json:"video_codec,omitempty"`
	AudioCodec     string   `json:"audio_codec,omitempty"`
	AudioChannels  string   `json:"audio_channels,omitempty"`
	OTT            string   `json:"ott,omitempty"`
	Quality        string   `json:"quality,omitempty"`
	ShortSub       string   `json:"short_sub,omitempty"`
	AudioLanguages []string `json:"audio_languages,omitempty"`
	SubLanguages   []string `json:"sub_languages,omitempty"`
	Rating         string   `json:"rating,omitempty"`
	Genres         []string `json:"genres,omitempty"`
	Synopsis       string   `json:"synopsis,omitempty"`
	CoverURL       string   `json:"cover_url,omitempty"`
	BannerURL      string   `json:"banner_url,omitempty"`
	Category       string   `json:"category,omitempty"` // "ANIME", "TV", or "MOVIE"
	DurationSec    float64  `json:"duration_sec,omitempty"`
}

// TaskVideoOptions holds per-task interactive Video Tools (`/vt` or Web Video Studio) mux overrides.
type TaskVideoOptions struct {
	KeepAudioTrackIDs []int    `json:"keep_audio_track_ids,omitempty"`
	KeepSubTrackIDs   []int    `json:"keep_sub_track_ids,omitempty"`
	AudioSyncDelayMs  int      `json:"audio_sync_delay_ms,omitempty"`
	SubSyncDelayMs    int      `json:"sub_sync_delay_ms,omitempty"`
	IntroSubEnabled   bool     `json:"intro_sub_enabled,omitempty"`
	IntroSubText      string   `json:"intro_sub_text,omitempty"`
	IntroSubDuration  int      `json:"intro_sub_duration,omitempty"`
	ExternalTracks    []string `json:"external_tracks,omitempty"`
}

// TaskWorkFunc is the execution closure invoked by the Dispatcher once concurrency
// semaphores and Watchdog memory checks pass.
type TaskWorkFunc func(ctx context.Context, task *Task) error

// Task represents a unit of work scheduled and tracked by the Starfall-Go Debrid engine.
type Task struct {
	ID                  string            `json:"id"`
	UserID              string            `json:"user_id"`            // User ID ("tg:8814651149" or Google Sub ID)
	TelegramDumpChat    string            `json:"telegram_dump_chat"` // Destination -100xxxxxxxxxx or -100xxxx|topic
	PosterTemplate      int               `json:"poster_template"`    // User's selected template (1-11)
	Name                string            `json:"name"`
	CustomName          string            `json:"custom_name,omitempty"`
	SourceURI           string            `json:"source_uri"`
	OutputPath          string            `json:"output_path"`
	Type                TaskType          `json:"type"`
	NeedsRemux          bool              `json:"needs_remux"`
	NeedsUpload         bool              `json:"needs_upload"`
	SelectFiles         bool              `json:"select_files,omitempty"`
	SelectedFileIndices []int             `json:"selected_file_indices,omitempty"`
	ExtractArchive      bool              `json:"extract_archive,omitempty"`
	MergeDirectory      bool              `json:"merge_directory,omitempty"`
	OriginChatID        int64             `json:"origin_chat_id,omitempty"`
	OriginThreadID      int               `json:"origin_thread_id,omitempty"`
	OriginMsgID         int               `json:"origin_msg_id,omitempty"`
	BatchKey            string            `json:"batch_key,omitempty"`
	CustomThumbPath     string            `json:"custom_thumb_path,omitempty"`
	EngineName          string            `json:"engine_name,omitempty"`
	StageDetail         string            `json:"stage_detail,omitempty"`
	Aria2GID            string            `json:"aria2_gid,omitempty"`
	ResultFiles         []string          `json:"result_files,omitempty"`
	TelegramDeepLink    string            `json:"telegram_deep_link,omitempty"`
	VideoOpts           *TaskVideoOptions `json:"video_opts,omitempty"`
	Metadata            MediaMetadata     `json:"metadata"`
	CreatedAt           time.Time         `json:"created_at"`
	StartedAt           time.Time         `json:"started_at,omitempty"`
	FinishedAt          time.Time         `json:"finished_at,omitempty"`

	mu          sync.RWMutex
	status      TaskStatus
	stats       SpeedStats
	errMessage  string
	cancelFunc  context.CancelFunc
	pausedFlag  atomic.Bool
	resumeCh    chan struct{}
	selectReady chan struct{}
	Work        TaskWorkFunc `json:"-"`
}

// NewTask constructs a thread-safe Task instance initialized in StatusQueued and bound
// to the authenticated user's effective Telegram dump channel and settings.
func NewTask(id string, owner *User, name, sourceURI string, taskType TaskType, totalBytes int64) *Task {
	now := time.Now().UTC()
	var userID, dumpChat, customThumb string
	posterTpl := DefaultPosterTemplate
	needsRemux := false
	extractArchive := true
	mergeDir := true

	if owner != nil {
		userID = owner.ID
		dumpChat = owner.EffectiveDumpChat()
		if owner.Settings.PostTemplateID >= MinPosterTemplate && owner.Settings.PostTemplateID <= MaxPosterTemplate {
			posterTpl = owner.Settings.PostTemplateID
		} else if owner.PosterTemplate >= MinPosterTemplate && owner.PosterTemplate <= MaxPosterTemplate {
			posterTpl = owner.PosterTemplate
		}
		needsRemux = owner.Settings.AutoProcess || owner.Settings.AutoMetadata || owner.Settings.AutoIntroSubtitle
		extractArchive = owner.Settings.AutoUnzip
		mergeDir = owner.Settings.AutoMerge
		customThumb = owner.Settings.CustomThumbnailFilePath
	}

	return &Task{
		ID:               id,
		UserID:           userID,
		TelegramDumpChat: dumpChat,
		PosterTemplate:   posterTpl,
		Name:             name,
		SourceURI:        sourceURI,
		Type:             taskType,
		NeedsRemux:       needsRemux,
		NeedsUpload:      dumpChat != "",
		ExtractArchive:   extractArchive,
		MergeDirectory:   mergeDir,
		CustomThumbPath:  customThumb,
		CreatedAt:        now,
		status:           StatusQueued,
		stats: SpeedStats{
			TotalBytes:  totalBytes,
			LastUpdated: now,
		},
		resumeCh:    make(chan struct{}),
		selectReady: make(chan struct{}),
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

// SetName updates the display/file name safely.
func (t *Task) SetName(name string) {
	t.mu.Lock()
	defer t.mu.Unlock()
	if name != "" {
		t.Name = name
	}
}

// SetOutputPath updates the output path safely.
func (t *Task) SetOutputPath(p string) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.OutputPath = p
}

// SetTotalBytes updates total expected bytes safely.
func (t *Task) SetTotalBytes(total int64) {
	t.mu.Lock()
	defer t.mu.Unlock()
	if total >= 0 {
		t.stats.TotalBytes = total
	}
}

// SetMetadata updates the task's MediaMetadata safely.
func (t *Task) SetMetadata(md MediaMetadata) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.Metadata = md
}

// GetMetadata returns a snapshot of the task's MediaMetadata.
func (t *Task) GetMetadata() MediaMetadata {
	t.mu.RLock()
	defer t.mu.RUnlock()
	return t.Metadata
}

// SetEngineName updates the engine label (e.g. "Aria2", "DirectHTTP", "yt-dlp", "GDrive", "Mega").
func (t *Task) SetEngineName(eng string) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.EngineName = eng
}

// SetStageDetail updates the human-readable sub-stage (e.g., "Extracting archive", "Lossless mkvmerge").
func (t *Task) SetStageDetail(detail string) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.StageDetail = detail
}

// SetAria2GID records the active Aria2 GID for torrent/magnet tasks.
func (t *Task) SetAria2GID(gid string) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.Aria2GID = gid
}

// GetAria2GID returns the active Aria2 GID if any.
func (t *Task) GetAria2GID() string {
	t.mu.RLock()
	defer t.mu.RUnlock()
	return t.Aria2GID
}

// SetTelegramDeepLink stores the deep link to the uploaded Telegram post.
func (t *Task) SetTelegramDeepLink(link string) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.TelegramDeepLink = link
}

// SetResultFiles stores the final processed local file paths before/during upload.
func (t *Task) SetResultFiles(files []string) {
	t.mu.Lock()
	defer t.mu.Unlock()
	cp := make([]string, len(files))
	copy(cp, files)
	t.ResultFiles = cp
}

// SetSelectedFiles commits the user's chosen torrent file indices and unblocks WaitForFileSelection.
func (t *Task) SetSelectedFiles(indices []int) {
	t.mu.Lock()
	cp := make([]int, len(indices))
	copy(cp, indices)
	t.SelectedFileIndices = cp
	ch := t.selectReady
	t.selectReady = nil
	t.mu.Unlock()
	if ch != nil {
		close(ch)
	}
}

// GetSelectedFiles returns the selected 1-based Aria2 file indices, if any.
func (t *Task) GetSelectedFiles() []int {
	t.mu.RLock()
	defer t.mu.RUnlock()
	cp := make([]int, len(t.SelectedFileIndices))
	copy(cp, t.SelectedFileIndices)
	return cp
}

// WaitForFileSelection blocks until SetSelectedFiles is invoked, timeout elapses, or ctx cancels.
func (t *Task) WaitForFileSelection(ctx context.Context, timeout time.Duration) ([]int, error) {
	t.mu.RLock()
	ch := t.selectReady
	existing := t.SelectedFileIndices
	t.mu.RUnlock()
	if ch == nil || len(existing) > 0 {
		return t.GetSelectedFiles(), nil
	}
	timer := time.NewTimer(timeout)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return nil, ctx.Err()
	case <-ch:
		return t.GetSelectedFiles(), nil
	case <-timer.C:
		return nil, nil
	}
}

// SetVideoOpts stores per-task Video Tools overrides safely.
func (t *Task) SetVideoOpts(opts *TaskVideoOptions) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.VideoOpts = opts
}

// GetVideoOpts returns the per-task Video Tools overrides safely.
func (t *Task) GetVideoOpts() *TaskVideoOptions {
	t.mu.RLock()
	defer t.mu.RUnlock()
	return t.VideoOpts
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
	selectCh := t.selectReady
	t.selectReady = nil
	t.mu.Unlock()

	if selectCh != nil {
		close(selectCh)
	}
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
	if t.stats.DownloadSpeedBps == 0 || instantDlBps == 0 {
		t.stats.DownloadSpeedBps = instantDlBps
	} else {
		t.stats.DownloadSpeedBps = int64(alpha*float64(instantDlBps) + (1.0-alpha)*float64(t.stats.DownloadSpeedBps))
	}

	if t.stats.UploadSpeedBps == 0 || instantUlBps == 0 {
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

// UpdatePeerCounts updates BitTorrent/Aria2 seeder and leecher counts safely.
func (t *Task) UpdatePeerCounts(seeders, leechers int) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.stats.Seeders = seeders
	t.stats.Leechers = leechers
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
	CustomName       string        `json:"custom_name,omitempty"`
	SourceURI        string        `json:"source_uri"`
	OutputPath       string        `json:"output_path"`
	Type             TaskType      `json:"type"`
	EngineName       string        `json:"engine_name,omitempty"`
	StageDetail      string        `json:"stage_detail,omitempty"`
	Aria2GID         string        `json:"aria2_gid,omitempty"`
	SelectFiles      bool          `json:"select_files,omitempty"`
	TelegramDeepLink string        `json:"telegram_deep_link,omitempty"`
	ResultFiles      []string      `json:"result_files,omitempty"`
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
	var resFiles []string
	if len(t.ResultFiles) > 0 {
		resFiles = make([]string, len(t.ResultFiles))
		copy(resFiles, t.ResultFiles)
	}
	return TaskSnapshot{
		ID:               t.ID,
		UserID:           t.UserID,
		TelegramDumpChat: t.TelegramDumpChat,
		PosterTemplate:   t.PosterTemplate,
		Name:             t.Name,
		CustomName:       t.CustomName,
		SourceURI:        t.SourceURI,
		OutputPath:       t.OutputPath,
		Type:             t.Type,
		EngineName:       t.EngineName,
		StageDetail:      t.StageDetail,
		Aria2GID:         t.Aria2GID,
		SelectFiles:      t.SelectFiles,
		TelegramDeepLink: t.TelegramDeepLink,
		ResultFiles:      resFiles,
		Status:           t.status,
		Stats:            t.stats,
		ErrorMessage:     t.errMessage,
		CreatedAt:        t.CreatedAt,
		StartedAt:        t.StartedAt,
		FinishedAt:       t.FinishedAt,
		Metadata:         t.Metadata,
	}
}
