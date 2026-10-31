// Package downloader provides the Unified Leech & Pipeline Manager (`Start`, `StartWithOptions`,
// `Pause`, `Resume`, `Abort`, `ListTorrentFiles`, `SelectTorrentFiles`) orchestrating
// Aria2 RPC (Magnets/Torrents), Direct HTTP/GDrive/Mega/Terabox/yt-dlp ingress,
// Lossless Video Auto-Processing, Smart Auto-Renaming, Auto-Posters, and Telegram Upload.
package downloader

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strings"

	"github.com/starfall-org/starfall-go/internal/downloader/aria2"
	"github.com/starfall-org/starfall-go/internal/downloader/buffer"
	"github.com/starfall-org/starfall-go/internal/downloader/direct"
	"github.com/starfall-org/starfall-go/internal/engine"
	"github.com/starfall-org/starfall-go/internal/ipc"
	"github.com/starfall-org/starfall-go/internal/media"
	"github.com/starfall-org/starfall-go/internal/models"
	"github.com/starfall-org/starfall-go/internal/safety"
	"github.com/starfall-org/starfall-go/internal/telegram"
	"github.com/starfall-org/starfall-go/internal/video"
)

var (
	// ErrTelegramDumpNotLinked is returned when a user starts a web leech task
	// before linking their `telegram_dump_chat` channel ID in Web Profile settings.
	ErrTelegramDumpNotLinked = errors.New("telegram_dump_chat is not linked: configure your private Telegram channel ID first")
)

// UserSettingsResolver fetches fresh user settings before remuxing and uploading.
type UserSettingsResolver interface {
	GetUser(ctx context.Context, userID string) (*models.User, error)
}

// StartOptions specifies optional StarfallX `/leech` flags and Web Studio overrides.
type StartOptions struct {
	RawURI         string
	CustomName     string
	SelectFiles    bool
	NeedsRemux     bool
	ExtractArchive *bool
	MergeDirectory *bool
	OriginChatID   int64
	OriginThreadID int
	OriginMsgID    int
	VideoOpts      *models.TaskVideoOptions
	SkipUpload     bool // Used for local dry-run / pipeline benchmark tests when MTProto is offline
}

// ManagerConfig configures the Unified Leech & Pipeline Manager.
type ManagerConfig struct {
	DownloadDir         string
	RequireTelegramDump bool
	GDriveAPIKey        string
	TeraboxCookie       string
	YTDLPBinaryPath     string
	Aria2Enabled        bool
	Aria2RPCPort        int
	Aria2RPCSecret      string
	NVMeMinFreeGB       float64
}

// Manager coordinates lifecycle operations (`Start`, `Pause`, `Resume`, `Abort`)
// across Aria2 RPC, Direct Downloaders, Lossless Video Pipeline, and Telegram Uploader.
type Manager struct {
	cfg          ManagerConfig
	bufPool      *buffer.Pool
	resolver     *direct.Resolver
	aria2Client  *aria2.Client
	dispatcher   *engine.Dispatcher
	registry     *engine.TaskRegistry
	watchdog     *safety.Watchdog
	limiter      *safety.RateLimiter
	uploader     *telegram.Uploader
	userResolver UserSettingsResolver
	bus          *ipc.Bus
	logger       *slog.Logger
}

// NewManager constructs a Unified Leech Manager and attaches end-to-end stage hooks onto `dispatcher`.
func NewManager(
	cfg ManagerConfig,
	dispatcher *engine.Dispatcher,
	registry *engine.TaskRegistry,
	watchdog *safety.Watchdog,
	limiter *safety.RateLimiter,
	bus *ipc.Bus,
	logger *slog.Logger,
) *Manager {
	if logger == nil {
		logger = slog.Default()
	}
	if strings.TrimSpace(cfg.DownloadDir) == "" {
		cfg.DownloadDir = "/app/downloads"
	}
	if cfg.NVMeMinFreeGB <= 0 {
		cfg.NVMeMinFreeGB = 5.0
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

	aria2Cli := aria2.NewClient(aria2.Config{
		Enabled:     cfg.Aria2Enabled,
		RPCPort:     cfg.Aria2RPCPort,
		RPCSecret:   cfg.Aria2RPCSecret,
		DownloadDir: cfg.DownloadDir,
	}, bus, logger)

	m := &Manager{
		cfg:         cfg,
		bufPool:     bufPool,
		resolver:    resolver,
		aria2Client: aria2Cli,
		dispatcher:  dispatcher,
		registry:    registry,
		watchdog:    watchdog,
		limiter:     limiter,
		bus:         bus,
		logger:      logger,
	}

	if dispatcher != nil {
		dispatcher.SetStageHooks(m.runPostProcessStage, m.runUploadStage)
	}
	return m
}

// AttachPipelineServices wires the Telegram Uploader and UserSettingsResolver for end-to-end delivery.
func (m *Manager) AttachPipelineServices(uploader *telegram.Uploader, userResolver UserSettingsResolver) {
	m.uploader = uploader
	m.userResolver = userResolver
}

// StartAria2 starts the supervised local `aria2c` RPC daemon in the background.
func (m *Manager) StartAria2(ctx context.Context) error {
	if m.aria2Client == nil {
		return nil
	}
	return m.aria2Client.Start(ctx)
}

// Aria2 returns the underlying Aria2 JSON-RPC client.
func (m *Manager) Aria2() *aria2.Client {
	return m.aria2Client
}

// Start validates the source URL and user's linked `telegram_dump_chat`, creates a `models.Task`,
// and submits it to the Dispatcher.
func (m *Manager) Start(owner *models.User, rawURL string, needsRemux bool) (*models.Task, error) {
	return m.StartWithOptions(owner, StartOptions{
		RawURI:     rawURL,
		NeedsRemux: needsRemux,
	})
}

// StartFromBot implements `telegram.BotTaskController` to launch `/leech` & `/ytdl` tasks from Telegram.
func (m *Manager) StartFromBot(owner *models.User, req telegram.BotLeechRequest) (*models.Task, error) {
	return m.StartWithOptions(owner, StartOptions{
		RawURI:         req.RawURI,
		CustomName:     req.CustomName,
		SelectFiles:    req.SelectFiles,
		ExtractArchive: req.ExtractArchive,
		MergeDirectory: req.MergeDirectory,
		OriginChatID:   req.OriginChatID,
		OriginMsgID:    req.OriginMsgID,
	})
}

// StartWithOptions submits a task supporting magnets, `.torrent` files/URLs, direct cloud links,
// `yt-dlp` streams, `-n` custom naming, and `-s` selective torrent file picking.
func (m *Manager) StartWithOptions(owner *models.User, opts StartOptions) (*models.Task, error) {
	if owner == nil || strings.TrimSpace(owner.ID) == "" {
		return nil, errors.New("authenticated user is required to start a leech task")
	}
	if m.cfg.RequireTelegramDump && !opts.SkipUpload && !owner.HasTelegramDumpLinked() {
		return nil, ErrTelegramDumpNotLinked
	}

	rawURI := strings.TrimSpace(opts.RawURI)
	if rawURI == "" {
		return nil, errors.New("source link or magnet URI cannot be empty")
	}

	taskID := generateTaskID()
	taskWorkDir := filepath.Join(m.cfg.DownloadDir, taskID)

	isTorrent := aria2.IsMagnetOrTorrent(rawURI)
	isLocalFile := false
	if !isTorrent && !strings.Contains(rawURI, "://") {
		if _, err := os.Stat(rawURI); err == nil {
			isLocalFile = true
		}
	}

	var initialName, engineName string
	if isTorrent {
		initialName = "[Torrent] Resolving metadata..."
		engineName = "Aria2"
	} else if isLocalFile {
		initialName = filepath.Base(rawURI)
		engineName = "LocalMedia"
	} else {
		kind, parsedURL, err := m.resolver.Classify(rawURI)
		if err != nil {
			return nil, err
		}
		initialName = fmt.Sprintf("[%s] %s", kind, parsedURL.Host)
		engineName = string(kind)
	}

	task := models.NewTask(taskID, owner, initialName, rawURI, models.TaskTypeDownload, 0)
	task.SetEngineName(engineName)
	task.CustomName = strings.TrimSpace(opts.CustomName)
	task.SelectFiles = opts.SelectFiles
	task.OriginChatID = opts.OriginChatID
	task.OriginThreadID = opts.OriginThreadID
	task.OriginMsgID = opts.OriginMsgID
	if opts.NeedsRemux {
		task.NeedsRemux = true
	}
	if opts.ExtractArchive != nil {
		task.ExtractArchive = *opts.ExtractArchive
	}
	if opts.MergeDirectory != nil {
		task.MergeDirectory = *opts.MergeDirectory
	}
	if opts.VideoOpts != nil {
		task.SetVideoOpts(opts.VideoOpts)
		task.NeedsRemux = true
	}
	if opts.SkipUpload {
		task.NeedsUpload = false
	} else if m.uploader != nil && !m.uploader.IsReady() {
		// Gracefully complete locally if MTProto credentials aren't connected yet
		task.NeedsUpload = false
	}

	task.Work = func(ctx context.Context, t *models.Task) error {
		if err := os.MkdirAll(taskWorkDir, 0o755); err != nil {
			return err
		}
		if isTorrent {
			outPath, bytesSaved, dlErr := m.aria2Client.Download(ctx, t, rawURI, taskWorkDir)
			if dlErr != nil {
				_ = os.RemoveAll(taskWorkDir)
				return dlErr
			}
			t.SetOutputPath(outPath)
			m.logger.Info("aria2 torrent/magnet download completed",
				slog.String("task_id", t.ID),
				slog.String("output_path", outPath),
				slog.Int64("bytes_saved", bytesSaved),
			)
			return nil
		}

		if isLocalFile {
			t.SetOutputPath(rawURI)
			if fi, serr := os.Stat(rawURI); serr == nil {
				t.SetTotalBytes(fi.Size())
				t.UpdateSpeed(fi.Size(), 0, 0, 0)
			}
			return nil
		}

		res, dlErr := m.resolver.ResolveAndDownload(ctx, t, rawURI, taskWorkDir)
		if dlErr != nil {
			_ = os.RemoveAll(taskWorkDir)
			return dlErr
		}
		t.SetName(res.FileName)
		t.SetOutputPath(res.FilePath)
		t.SetEngineName(string(res.Source))
		m.logger.Info("direct leech download stage completed",
			slog.String("task_id", t.ID),
			slog.String("source", string(res.Source)),
			slog.String("file_path", res.FilePath),
			slog.Int64("bytes_saved", res.BytesSaved),
		)
		return nil
	}

	if err := m.dispatcher.Submit(task); err != nil {
		return nil, err
	}
	return task, nil
}

// runPostProcessStage runs under `remuxSem` (max 1 concurrent):
//  1. Archive extraction (`AUTO_UNZIP` via `7z`)
//  2. Deliverable & companion track bundle discovery (`AUTO_MERGE`)
//  3. Single-pass lossless `mkvmerge` stream filtering, intro `.srt` muxing, and `ffmpeg -map 0 -c copy` metadata
//  4. Smart template auto-renaming (`lremname_auto`, `NAME_SWAP`, `LEECH_PREFIX`, `LEECH_SUFFIX`, or `-n` CustomName)
func (m *Manager) runPostProcessStage(ctx context.Context, task *models.Task) error {
	snap := task.Snapshot()
	if strings.TrimSpace(snap.OutputPath) == "" {
		return nil
	}

	settings := m.resolveUserSettings(ctx, snap.UserID)
	targetPath := snap.OutputPath
	taskWorkDir := filepath.Join(m.cfg.DownloadDir, task.ID)
	_ = os.MkdirAll(taskWorkDir, 0o755)

	// 1. Archive Extraction (`AUTO_UNZIP`)
	if settings.AutoUnzip || task.ExtractArchive {
		task.SetStageDetail("Extracting archive (7z)")
		extractedPath, err := video.ExtractArchivesIfNeeded(ctx, targetPath, m.cfg.NVMeMinFreeGB)
		if err != nil {
			m.logger.Warn("archive extraction warning; proceeding with raw files",
				slog.String("task_id", task.ID),
				slog.String("error", err.Error()),
			)
		} else {
			targetPath = extractedPath
		}
	}

	// 2. Discover deliverable files & companion tracks (`AUTO_MERGE`)
	bundles, err := video.CollectDeliverableBundles(targetPath, settings.AutoMerge || task.MergeDirectory)
	if err != nil {
		return fmt.Errorf("scanning downloaded deliverables: %w", err)
	}
	if len(bundles) == 0 {
		return errors.New("no deliverable media files found after download")
	}

	var finalDeliverables []string
	var primaryMD models.MediaMetadata

	for idx, bundle := range bundles {
		curPath := bundle.PrimaryPath
		md := media.ParseFilename(filepath.Base(curPath))

		if bundle.IsVideo {
			task.SetStageDetail(fmt.Sprintf("Lossless video auto-process (%d/%d)", idx+1, len(bundles)))
			processedPath, _, enrichedMD, perr := video.ProcessVideoBundle(
				ctx,
				bundle,
				settings,
				task.GetVideoOpts(),
				taskWorkDir,
			)
			if perr != nil {
				m.logger.Warn("lossless video auto-process fallback to original stream",
					slog.String("task_id", task.ID),
					slog.String("file", filepath.Base(curPath)),
					slog.String("error", perr.Error()),
				)
			} else {
				curPath = processedPath
				md = enrichedMD
			}
		}

		// 3. Smart Auto-Rename or `-n` CustomName
		var desiredName string
		if len(bundles) == 1 && strings.TrimSpace(task.CustomName) != "" {
			ext := filepath.Ext(curPath)
			custom := media.SanitizeFilename(strings.TrimSuffix(task.CustomName, filepath.Ext(task.CustomName)))
			desiredName = custom + ext
		} else {
			desiredName = media.FormatAutoRename(filepath.Base(curPath), md, settings)
		}

		if desiredName != "" && desiredName != filepath.Base(curPath) {
			renamedPath := filepath.Join(filepath.Dir(curPath), desiredName)
			if rerr := os.Rename(curPath, renamedPath); rerr == nil {
				curPath = renamedPath
			}
		}

		if idx == 0 {
			primaryMD = md
			task.SetName(filepath.Base(curPath))
			task.SetMetadata(md)
		}
		finalDeliverables = append(finalDeliverables, curPath)
	}

	task.SetStageDetail("")
	if len(finalDeliverables) > 0 {
		task.SetOutputPath(finalDeliverables[0])
		task.SetResultFiles(finalDeliverables)
		_ = primaryMD
	}
	return nil
}

// runUploadStage runs under `ulSem` (max 2 concurrent) to deliver all processed files
// to the user's `TelegramDumpChat` and clean temporary NVMe files immediately afterward.
func (m *Manager) runUploadStage(ctx context.Context, task *models.Task) error {
	if m.uploader == nil || !m.uploader.IsReady() {
		m.logger.Info("skipping telegram upload (MTProto session not connected); keeping processed files on disk",
			slog.String("task_id", task.ID),
		)
		return nil
	}

	snap := task.Snapshot()
	settings := m.resolveUserSettings(ctx, snap.UserID)
	deliverables := snap.ResultFiles
	if len(deliverables) == 0 && snap.OutputPath != "" {
		deliverables = []string{snap.OutputPath}
	}
	if len(deliverables) == 0 {
		return errors.New("no deliverable files available for upload")
	}

	if len(deliverables) > 1 {
		task.BatchKey = task.ID
	}

	for i, filePath := range deliverables {
		isLast := i == len(deliverables)-1
		md := media.ParseFilename(filepath.Base(filePath))
		if i == 0 && snap.Metadata.Title != "" {
			md = snap.Metadata
		}
		if probe, perr := video.ProbeMedia(ctx, filePath); perr == nil && probe != nil {
			md = media.MergeProbeTracks(md, probe.ToMediaTracks())
			md.DurationSec = probe.DurationSec
		}

		task.SetStageDetail(fmt.Sprintf("Uploading %d/%d to Telegram", i+1, len(deliverables)))
		_, err := m.uploader.UploadDeliverable(
			ctx,
			task,
			filePath,
			md,
			settings,
			task.CustomThumbPath,
			isLast,
		)
		if err != nil {
			return err
		}
		// Immediately free NVMe space as soon as each episode/part completes uploading
		_ = os.Remove(filePath)
	}

	task.SetStageDetail("")
	// Clean isolated task workspace directory
	taskWorkDir := filepath.Join(m.cfg.DownloadDir, task.ID)
	_ = os.RemoveAll(taskWorkDir)
	return nil
}

func (m *Manager) resolveUserSettings(ctx context.Context, userID string) models.UserSettings {
	if m.userResolver != nil && strings.TrimSpace(userID) != "" {
		if u, err := m.userResolver.GetUser(ctx, userID); err == nil && u != nil {
			return u.Settings
		}
	}
	return models.DefaultUserSettings()
}

// ListTorrentFiles queries active Aria2 metadata for the Web Torrent File Selector UI.
func (m *Manager) ListTorrentFiles(ctx context.Context, taskID string) ([]aria2.TorrentFileEntry, error) {
	task, err := m.registry.Get(taskID)
	if err != nil {
		return nil, err
	}
	gid := task.GetAria2GID()
	if gid == "" || m.aria2Client == nil {
		return nil, errors.New("task does not have an active Aria2 torrent GID")
	}
	return m.aria2Client.ListFiles(ctx, gid)
}

// SelectTorrentFiles commits 1-based Aria2 file indices chosen in the Web Torrent File Selector UI.
func (m *Manager) SelectTorrentFiles(ctx context.Context, taskID string, indices []int) error {
	task, err := m.registry.Get(taskID)
	if err != nil {
		return err
	}
	gid := task.GetAria2GID()
	if gid != "" && m.aria2Client != nil && len(indices) > 0 {
		_ = m.aria2Client.SelectFiles(ctx, gid, indices)
	}
	task.SetSelectedFiles(indices)
	return nil
}

// Pause suspends an active download task.
func (m *Manager) Pause(taskID string) error {
	task, err := m.registry.Get(taskID)
	if err != nil {
		return err
	}
	if !task.Pause() {
		return fmt.Errorf("task %s is not in a pausable state (current=%s)", taskID, task.Status())
	}
	m.logger.Info("paused leech task", slog.String("task_id", taskID))
	return nil
}

// Resume unblocks a paused download task.
func (m *Manager) Resume(taskID string) error {
	task, err := m.registry.Get(taskID)
	if err != nil {
		return err
	}
	if !task.Resume() {
		return fmt.Errorf("task %s is not currently paused", taskID)
	}
	m.logger.Info("resumed leech task", slog.String("task_id", taskID))
	return nil
}

// Abort cancels an in-flight or queued task immediately and cleans its workspace directory.
func (m *Manager) Abort(taskID string, reason string) error {
	task, err := m.registry.Get(taskID)
	if err != nil {
		return err
	}
	if strings.TrimSpace(reason) == "" {
		reason = "aborted by user"
	}

	gid := task.GetAria2GID()
	task.Cancel(reason)
	_, _ = m.registry.UpdateStatus(taskID, models.StatusCanceled, reason)

	if gid != "" && m.aria2Client != nil {
		_ = m.aria2Client.RemoveDownload(context.Background(), gid)
	}

	snap := task.Snapshot()
	if snap.OutputPath != "" {
		_ = os.Remove(snap.OutputPath)
	}
	_ = os.RemoveAll(filepath.Join(m.cfg.DownloadDir, taskID))

	m.logger.Info("aborted leech task and cleaned workspace",
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
