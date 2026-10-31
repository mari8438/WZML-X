// Package direct implements a high-speed, zero-RAM-buffering `yt-dlp` subprocess
// wrapper for YouTube, anime streaming portals, HLS `.m3u8` / DASH `.mpd` manifests,
// and direct MP4/MKV streams.
package direct

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/starfall-org/starfall-go/internal/ipc"
	"github.com/starfall-org/starfall-go/internal/models"
	"github.com/starfall-org/starfall-go/internal/safety"
)

// YTDLPDownloader wraps the `yt-dlp` CLI to stream video fragments directly to
// `/app/downloads/` while parsing real-time stdout progress into the IPC Event Bus.
type YTDLPDownloader struct {
	binaryPath string
	watchdog   *safety.Watchdog
	bus        *ipc.Bus
	logger     *slog.Logger
}

// NewYTDLPDownloader constructs a resource-bounded yt-dlp streaming wrapper.
func NewYTDLPDownloader(
	binaryPath string,
	watchdog *safety.Watchdog,
	bus *ipc.Bus,
	logger *slog.Logger,
) *YTDLPDownloader {
	if strings.TrimSpace(binaryPath) == "" {
		binaryPath = "yt-dlp"
	}
	return &YTDLPDownloader{
		binaryPath: binaryPath,
		watchdog:   watchdog,
		bus:        bus,
		logger:     logger,
	}
}

// Download invokes `yt-dlp` with 4 concurrent fragment workers and a 512 KiB stream buffer,
// writing directly to `destDir` and emitting 500ms progress events onto `ipc.Bus`.
func (y *YTDLPDownloader) Download(
	ctx context.Context,
	task *models.Task,
	rawURL string,
	destDir string,
) (*DownloadResult, error) {
	if strings.TrimSpace(destDir) == "" {
		destDir = "/app/downloads"
	}
	if err := os.MkdirAll(destDir, 0o755); err != nil {
		return nil, err
	}

	if y.watchdog != nil {
		if err := y.watchdog.CheckNVMeHeadroom(0); err != nil {
			return nil, err
		}
	}

	outTemplate := filepath.Join(destDir, fmt.Sprintf("%s_%%(title).150B.%%(ext)s", task.ID))

	args := []string{
		"--no-playlist",
		"--newline",
		"--no-warnings",
		"--no-part",
		"--concurrent-fragments", "4",
		"--buffer-size", "512K",
		"--progress-template", "download:STARFALL_PROG|%(progress.downloaded_bytes)s|%(progress.total_bytes)s|%(progress.total_bytes_estimate)s|%(progress.speed)s|%(progress.eta)s",
		"--print", "after_move:STARFALL_FILE|%(filepath)s",
		"-o", outTemplate,
		rawURL,
	}

	cmd := safety.NewSupervisedCommand(ctx, y.binaryPath, args...)
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return nil, fmt.Errorf("attaching yt-dlp stdout pipe: %w", err)
	}
	cmd.Stderr = cmd.Stdout

	if err := cmd.Start(); err != nil {
		return nil, fmt.Errorf("starting yt-dlp process: %w", err)
	}

	var resolvedFilePath string
	var lastEmit time.Time
	scanner := bufio.NewScanner(stdout)

	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if strings.HasPrefix(line, "STARFALL_FILE|") {
			resolvedFilePath = strings.TrimSpace(strings.TrimPrefix(line, "STARFALL_FILE|"))
			continue
		}
		if strings.HasPrefix(line, "STARFALL_PROG|") {
			if time.Since(lastEmit) < ProgressBroadcastInterval {
				continue
			}
			lastEmit = time.Now()
			y.parseAndPublishProgress(task, strings.TrimPrefix(line, "STARFALL_PROG|"))
		}
	}

	if err := cmd.Wait(); err != nil {
		return nil, fmt.Errorf("yt-dlp execution failed: %w", err)
	}

	if resolvedFilePath == "" {
		matches, _ := filepath.Glob(filepath.Join(destDir, task.ID+"_*"))
		if len(matches) == 0 {
			return nil, errors.New("yt-dlp finished but output file could not be located")
		}
		resolvedFilePath = matches[0]
	}

	info, err := os.Stat(resolvedFilePath)
	if err != nil {
		return nil, fmt.Errorf("statting yt-dlp output file %s: %w", resolvedFilePath, err)
	}

	fileName := filepath.Base(resolvedFilePath)
	task.Name = fileName
	task.OutputPath = resolvedFilePath

	return &DownloadResult{
		FilePath:   resolvedFilePath,
		FileName:   fileName,
		BytesSaved: info.Size(),
		Source:     SourceYTDLP,
	}, nil
}

func (y *YTDLPDownloader) parseAndPublishProgress(task *models.Task, rawFields string) {
	// Format: downloaded_bytes|total_bytes|total_bytes_estimate|speed|eta
	parts := strings.Split(rawFields, "|")
	if len(parts) < 5 {
		return
	}

	downloaded, _ := strconv.ParseInt(strings.TrimSpace(parts[0]), 10, 64)
	total, _ := strconv.ParseInt(strings.TrimSpace(parts[1]), 10, 64)
	if total <= 0 {
		total, _ = strconv.ParseInt(strings.TrimSpace(parts[2]), 10, 64)
	}
	speedFloat, _ := strconv.ParseFloat(strings.TrimSpace(parts[3]), 64)
	speedBps := int64(speedFloat)

	stats := task.UpdateSpeed(downloaded, speedBps, 0, 4)
	var pct float64
	if total > 0 {
		pct = (float64(downloaded) / float64(total)) * 100.0
		if pct > 100.0 {
			pct = 100.0
		}
	} else {
		pct = stats.ProgressPercent
	}

	if y.bus != nil {
		y.bus.Publish(ipc.Event{
			Type:      ipc.EventTaskProgress,
			TaskID:    task.ID,
			UserID:    task.UserID,
			Progress:  pct,
			SpeedBps:  stats.DownloadSpeedBps,
			Timestamp: time.Now().UTC(),
			Metadata: map[string]any{
				"bytes_downloaded": downloaded,
				"total_bytes":      total,
				"speed_mbps":       float64(stats.DownloadSpeedBps) / (1024.0 * 1024.0),
				"eta_seconds":      stats.ETASeconds,
				"percent":          pct,
			},
		})
	}
}
