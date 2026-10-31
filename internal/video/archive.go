// Package video implements `AUTO_UNZIP` (`7z` archive extraction with NVMe headroom guards)
// and `AUTO_MERGE` (smart folder pairing of video files with companion audio/subtitle tracks).
package video

import (
	"bytes"
	"context"
	"fmt"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"

	"github.com/starfall-org/starfall-go/internal/safety"
)

// MediaBundle represents a deliverable file (or a primary video paired with companion external tracks).
type MediaBundle struct {
	PrimaryPath    string   // Video file or standalone deliverable file
	ExternalTracks []string // Companion .aac/.eac3/.mka/.srt/.ass tracks to mux losslessly
	IsVideo        bool
}

// IsArchiveFile reports whether `path` is a supported archive container (`.zip`, `.rar`, `.7z`, `.tar`, etc.).
func IsArchiveFile(path string) bool {
	low := strings.ToLower(filepath.Base(path))
	for _, suffix := range []string{".zip", ".rar", ".7z", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".001"} {
		if strings.HasSuffix(low, suffix) {
			return true
		}
	}
	return false
}

// IsExternalTrackFile reports whether `path` is an elementary/companion audio or subtitle file.
func IsExternalTrackFile(path string) bool {
	switch strings.ToLower(filepath.Ext(path)) {
	case ".aac", ".eac3", ".ec3", ".ac3", ".mka", ".opus", ".flac", ".dts", ".mp3", ".m4a",
		".srt", ".ass", ".ssa", ".vtt", ".sup", ".sub":
		return true
	default:
		return false
	}
}

// ExtractArchivesIfNeeded extracts `.zip` / `.rar` / `.7z` / `.tar` archives inside `targetPath`
// when `autoUnzip` is enabled, checking NVMe headroom first and deleting each archive right after extraction.
func ExtractArchivesIfNeeded(ctx context.Context, targetPath string, minFreeGB float64) (string, error) {
	info, err := os.Stat(targetPath)
	if err != nil {
		return targetPath, err
	}

	if !info.IsDir() {
		if !IsArchiveFile(targetPath) {
			return targetPath, nil
		}
		destDir := strings.TrimSuffix(targetPath, filepath.Ext(targetPath)) + "_extracted"
		if err := extractSingleArchive(ctx, targetPath, destDir, minFreeGB); err != nil {
			return targetPath, err
		}
		_ = os.Remove(targetPath)
		return destDir, nil
	}

	// Directory scan (up to 2 passes for nested archives)
	for pass := 0; pass < 2; pass++ {
		var archives []string
		_ = filepath.WalkDir(targetPath, func(p string, d fs.DirEntry, werr error) error {
			if werr != nil || d.IsDir() {
				return nil
			}
			// Skip multi-volume `.r00`, `.part2.rar` secondary parts when `.part1.rar` exists
			low := strings.ToLower(d.Name())
			if strings.Contains(low, ".part") && strings.HasSuffix(low, ".rar") && !strings.Contains(low, ".part1.rar") && !strings.Contains(low, ".part01.rar") {
				return nil
			}
			if IsArchiveFile(p) {
				archives = append(archives, p)
			}
			return nil
		})
		if len(archives) == 0 {
			break
		}
		for _, arc := range archives {
			dest := filepath.Dir(arc)
			if err := extractSingleArchive(ctx, arc, dest, minFreeGB); err != nil {
				return targetPath, err
			}
			_ = os.Remove(arc)
		}
	}
	return targetPath, nil
}

func extractSingleArchive(ctx context.Context, archivePath, destDir string, minFreeGB float64) error {
	if minFreeGB <= 0 {
		minFreeGB = 5.0
	}
	st, err := os.Stat(archivePath)
	if err != nil {
		return err
	}
	if _, freeBytes, derr := safety.GetDiskSpace(filepath.Dir(archivePath)); derr == nil {
		required := uint64(st.Size()) + uint64(minFreeGB*1024*1024*1024)
		if freeBytes < required {
			return fmt.Errorf("insufficient NVMe disk space to extract %s (free %.2f GB)", filepath.Base(archivePath), float64(freeBytes)/(1024*1024*1024))
		}
	}

	if err := os.MkdirAll(destDir, 0o755); err != nil {
		return err
	}

	bin := "7z"
	if _, err := exec.LookPath(bin); err != nil {
		bin = "7zz"
	}
	cmd := exec.CommandContext(ctx, bin, "x", "-y", "-bd", "-o"+destDir, archivePath)
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("7z extraction failed for %s: %w (%s)", filepath.Base(archivePath), err, strings.TrimSpace(stderr.String()))
	}
	return nil
}

// CollectDeliverableBundles discovers all deliverable files inside `rootPath` (file or directory),
// sorting episodes naturally and pairing companion external audio/subtitle files when `autoMerge` is true.
func CollectDeliverableBundles(rootPath string, autoMerge bool) ([]MediaBundle, error) {
	info, err := os.Stat(rootPath)
	if err != nil {
		return nil, err
	}
	if !info.IsDir() {
		return []MediaBundle{{
			PrimaryPath: rootPath,
			IsVideo:     IsVideoFile(rootPath),
		}}, nil
	}

	var videos []string
	var extTracks []string
	var otherFiles []string

	err = filepath.WalkDir(rootPath, func(p string, d fs.DirEntry, werr error) error {
		if werr != nil || d.IsDir() {
			return nil
		}
		name := d.Name()
		low := strings.ToLower(name)
		if strings.HasPrefix(name, ".") || strings.HasSuffix(low, ".aria2") || strings.HasSuffix(low, ".part") ||
			strings.HasSuffix(low, ".nfo") || strings.HasSuffix(low, ".url") || strings.HasSuffix(low, ".torrent") {
			return nil
		}
		if fi, serr := d.Info(); serr == nil {
			if fi.Size() == 0 {
				return nil
			}
			// Skip tiny sample video clips (< 10 MiB with "sample" in name)
			if IsVideoFile(p) && strings.Contains(low, "sample") && fi.Size() < 10*1024*1024 {
				return nil
			}
		}

		switch {
		case IsVideoFile(p):
			videos = append(videos, p)
		case IsExternalTrackFile(p):
			extTracks = append(extTracks, p)
		default:
			otherFiles = append(otherFiles, p)
		}
		return nil
	})
	if err != nil {
		return nil, err
	}

	sort.Strings(videos)
	sort.Strings(extTracks)
	sort.Strings(otherFiles)

	if len(videos) == 0 {
		var out []MediaBundle
		for _, f := range append(extTracks, otherFiles...) {
			out = append(out, MediaBundle{PrimaryPath: f, IsVideo: false})
		}
		return out, nil
	}

	usedTracks := make(map[string]bool)
	bundles := make([]MediaBundle, 0, len(videos)+len(otherFiles))

	for _, vid := range videos {
		b := MediaBundle{
			PrimaryPath: vid,
			IsVideo:     true,
		}
		if autoMerge && len(extTracks) > 0 {
			vidDir := filepath.Dir(vid)
			vidStem := strings.ToLower(strings.TrimSuffix(filepath.Base(vid), filepath.Ext(vid)))
			for _, tr := range extTracks {
				if usedTracks[tr] {
					continue
				}
				trStem := strings.ToLower(strings.TrimSuffix(filepath.Base(tr), filepath.Ext(tr)))
				// Pair if only 1 video exists in the whole download OR if track is in same folder & shares stem prefix
				if len(videos) == 1 || (filepath.Dir(tr) == vidDir && (strings.HasPrefix(trStem, vidStem) || strings.HasPrefix(vidStem, trStem))) {
					b.ExternalTracks = append(b.ExternalTracks, tr)
					usedTracks[tr] = true
				}
			}
		}
		bundles = append(bundles, b)
	}

	if !autoMerge {
		for _, tr := range extTracks {
			bundles = append(bundles, MediaBundle{PrimaryPath: tr, IsVideo: false})
		}
	} else {
		for _, tr := range extTracks {
			if !usedTracks[tr] {
				bundles = append(bundles, MediaBundle{PrimaryPath: tr, IsVideo: false})
			}
		}
	}
	for _, oth := range otherFiles {
		bundles = append(bundles, MediaBundle{PrimaryPath: oth, IsVideo: false})
	}

	return bundles, nil
}
