// Package telegram implements the Lossless MKV Auto-Splitter (`splitter.go`)
// for files exceeding the session's single-file upload limit (> 2000 MB on Free/Bot
// sessions, or > 4000 MB on Premium sessions) using `mkvmerge` stream-copy splitting
// with 0% CPU video transcoding.
package telegram

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"

	"github.com/starfall-org/starfall-go/internal/safety"
)

// SplitPart describes a single sequential file part produced by `mkvmerge --split`.
type SplitPart struct {
	PartNumber int    `json:"part_number"`
	TotalParts int    `json:"total_parts"`
	FilePath   string `json:"file_path"`
	FileName   string `json:"file_name"`
	SizeBytes  int64  `json:"size_bytes"`
}

// SplitResult holds the ordered list of split parts and a cleanup closure to delete
// temporary split files once all parts have been uploaded to Telegram.
type SplitResult struct {
	WasSplit bool
	Parts    []SplitPart
	Cleanup  func()
}

// Splitter executes lossless `mkvmerge` size-based splitting when a media file exceeds
// the active Telegram session's upload ceiling.
type Splitter struct {
	watchdog   *safety.Watchdog
	binaryPath string
	logger     *slog.Logger
}

// NewSplitter constructs a lossless MKV Auto-Splitter.
func NewSplitter(watchdog *safety.Watchdog, binaryPath string, logger *slog.Logger) *Splitter {
	if strings.TrimSpace(binaryPath) == "" {
		binaryPath = "mkvmerge"
	}
	return &Splitter{
		watchdog:   watchdog,
		binaryPath: binaryPath,
		logger:     logger,
	}
}

// EnsureWithinLimit inspects `inputPath` against `maxUploadBytes` (2,000 MB for Free,
// 4,000 MB for Premium). If the file fits within the ceiling, it returns a single-element
// `SplitResult` with a no-op Cleanup. If the file exceeds the ceiling, it runs:
//
//	mkvmerge -o "%d_<base>.mkv" --split size:1950M input.mkv
//
// and returns the ordered parts (`Part 1`, `Part 2`, ...) with 0% CPU video transcoding.
func (s *Splitter) EnsureWithinLimit(
	ctx context.Context,
	inputPath string,
	maxUploadBytes int64,
	splitSizeMB int,
) (*SplitResult, error) {
	info, err := os.Stat(inputPath)
	if err != nil {
		return nil, fmt.Errorf("statting input file %s: %w", inputPath, err)
	}

	if maxUploadBytes <= 0 {
		maxUploadBytes = FreeUploadCeilingBytes
	}
	if splitSizeMB <= 0 {
		splitSizeMB = FreeSplitTargetMB
	}

	// File fits inside a single Telegram upload: no splitting needed.
	if info.Size() <= maxUploadBytes {
		return &SplitResult{
			WasSplit: false,
			Parts: []SplitPart{
				{
					PartNumber: 1,
					TotalParts: 1,
					FilePath:   inputPath,
					FileName:   filepath.Base(inputPath),
					SizeBytes:  info.Size(),
				},
			},
			Cleanup: func() {},
		}, nil
	}

	// Verify NVMe headroom before creating split parts on disk.
	if s.watchdog != nil {
		if err := s.watchdog.CheckNVMeHeadroom(uint64(info.Size())); err != nil {
			return nil, err
		}
	}

	dir := filepath.Dir(inputPath)
	baseName := strings.TrimSuffix(filepath.Base(inputPath), filepath.Ext(inputPath))
	splitDir := filepath.Join(dir, fmt.Sprintf(".split_%s", baseName))
	if err := os.MkdirAll(splitDir, 0o755); err != nil {
		return nil, fmt.Errorf("creating temporary split directory %s: %w", splitDir, err)
	}

	// Exact pattern required: mkvmerge -o "%d_output.mkv" --split size:1950M input.mkv
	outputPattern := filepath.Join(splitDir, fmt.Sprintf("%%d_%s.mkv", baseName))
	splitArg := fmt.Sprintf("size:%dM", splitSizeMB)

	s.logger.Info("file exceeds session upload ceiling; running lossless mkvmerge auto-splitter",
		slog.String("input_path", inputPath),
		slog.Int64("file_size_mb", info.Size()/(1024*1024)),
		slog.Int64("ceiling_mb", maxUploadBytes/(1024*1024)),
		slog.String("split_arg", splitArg),
	)

	cmd := exec.CommandContext(
		ctx,
		s.binaryPath,
		"-o", outputPattern,
		"--split", splitArg,
		inputPath,
	)
	out, err := cmd.CombinedOutput()
	if err != nil {
		_ = os.RemoveAll(splitDir)
		return nil, fmt.Errorf("mkvmerge lossless split failed: %w (output: %s)", err, string(out))
	}

	entries, err := os.ReadDir(splitDir)
	if err != nil || len(entries) == 0 {
		_ = os.RemoveAll(splitDir)
		return nil, fmt.Errorf("locating split output files in %s: %w", splitDir, err)
	}

	var partPaths []string
	for _, entry := range entries {
		if !entry.IsDir() && strings.HasSuffix(strings.ToLower(entry.Name()), ".mkv") {
			partPaths = append(partPaths, filepath.Join(splitDir, entry.Name()))
		}
	}
	sort.Strings(partPaths)

	parts := make([]SplitPart, 0, len(partPaths))
	totalParts := len(partPaths)
	for i, p := range partPaths {
		pInfo, sErr := os.Stat(p)
		if sErr != nil {
			_ = os.RemoveAll(splitDir)
			return nil, sErr
		}
		parts = append(parts, SplitPart{
			PartNumber: i + 1,
			TotalParts: totalParts,
			FilePath:   p,
			FileName:   filepath.Base(p),
			SizeBytes:  pInfo.Size(),
		})
	}

	return &SplitResult{
		WasSplit: true,
		Parts:    parts,
		Cleanup: func() {
			_ = os.RemoveAll(splitDir)
		},
	}, nil
}
