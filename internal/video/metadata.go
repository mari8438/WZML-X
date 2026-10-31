// Package video injects global container and per-stream (`VIDEO_METADATA`, `AUDIO_METADATA`,
// `SUBTITLE_METADATA`) branding tags losslessly via `ffmpeg -map 0 -c copy` (`0%` transcode).
package video

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/starfall-org/starfall-go/internal/media"
	"github.com/starfall-org/starfall-go/internal/models"
	"github.com/starfall-org/starfall-go/internal/safety"
)

// BuildFFmpegMetadataArgs constructs lossless `ffmpeg -map 0 -c copy` CLI arguments
// for global container tags and per-stream video/audio/subtitle tags (`{audiolang}`, `{sublang}`).
func BuildFFmpegMetadataArgs(
	inputPath, outputPath string,
	settings models.UserSettings,
	probe *MediaProbe,
	md models.MediaMetadata,
) []string {
	args := []string{
		"-hide_banner",
		"-loglevel", "error",
		"-y",
		"-i", inputPath,
		"-map", "0",
		"-c", "copy",
	}

	yearStr := ""
	if md.Year > 0 {
		yearStr = strconv.Itoa(md.Year)
	}
	baseFilename := strings.TrimSuffix(filepath.Base(inputPath), filepath.Ext(inputPath))

	expandCommon := func(val, audioLang, subLang string) string {
		r := strings.NewReplacer(
			"{audiolang}", audioLang,
			"{sublang}", subLang,
			"{filename}", baseFilename,
			"{title}", md.Title,
			"{year}", yearStr,
		)
		return strings.TrimSpace(r.Replace(val))
	}

	// 1. Global container metadata (`METADATA` + `METADATA_TITLE`)
	globalMap := make(map[string]string)
	for k, v := range settings.Metadata {
		globalMap[k] = v
	}
	if settings.MetadataTitle != "" && globalMap["title"] == "" {
		globalMap["title"] = settings.MetadataTitle
	}
	for k, v := range globalMap {
		if strings.TrimSpace(k) == "" || strings.TrimSpace(v) == "" {
			continue
		}
		args = append(args, "-metadata", fmt.Sprintf("%s=%s", k, expandCommon(v, "", "")))
	}

	if probe != nil {
		// 2. Per-video stream metadata (`VIDEO_METADATA`)
		videoMap := make(map[string]string)
		for k, v := range settings.VideoMetadata {
			videoMap[k] = v
		}
		if settings.MetadataTitle != "" && videoMap["title"] == "" {
			videoMap["title"] = settings.MetadataTitle
		}
		for _, vs := range probe.VideoStreams() {
			for k, v := range videoMap {
				if strings.TrimSpace(k) == "" || strings.TrimSpace(v) == "" {
					continue
				}
				args = append(args,
					fmt.Sprintf("-metadata:s:v:%d", vs.TypeIndex),
					fmt.Sprintf("%s=%s", k, expandCommon(v, "", "")),
				)
			}
		}

		// 3. Per-audio stream metadata (`AUDIO_METADATA` + `METADATA_AUDIO_TITLE`)
		audioMap := make(map[string]string)
		for k, v := range settings.AudioMetadata {
			audioMap[k] = v
		}
		if settings.MetadataAudioTitle != "" && audioMap["title"] == "" {
			audioMap["title"] = settings.MetadataAudioTitle
		}
		for _, as := range probe.AudioStreams() {
			fullLang := media.FullLanguageName(as.Language, as.Title)
			isoLang := CanonicalLangKey(as.Language)
			if isoLang == "" {
				isoLang = CanonicalLangKey(fullLang)
			}
			for k, v := range audioMap {
				if strings.TrimSpace(k) == "" || strings.TrimSpace(v) == "" {
					continue
				}
				// ISO-639-2 code is required by Matroska/MP4 for the `language` tag specifically
				if strings.EqualFold(k, "language") {
					if isoLang != "" && len(isoLang) == 3 {
						args = append(args,
							fmt.Sprintf("-metadata:s:a:%d", as.TypeIndex),
							fmt.Sprintf("language=%s", isoLang),
						)
					}
					continue
				}
				args = append(args,
					fmt.Sprintf("-metadata:s:a:%d", as.TypeIndex),
					fmt.Sprintf("%s=%s", k, expandCommon(v, fullLang, "")),
				)
			}
		}

		// 4. Per-subtitle stream metadata (`SUBTITLE_METADATA` + `METADATA_SUBTITLE_TITLE`)
		subMap := make(map[string]string)
		for k, v := range settings.SubtitleMetadata {
			subMap[k] = v
		}
		if settings.MetadataSubtitleTitle != "" && subMap["title"] == "" {
			subMap["title"] = settings.MetadataSubtitleTitle
		}
		for _, ss := range probe.SubtitleStreams() {
			fullLang := media.FullLanguageName(ss.Language, ss.Title)
			isoLang := CanonicalLangKey(ss.Language)
			if isoLang == "" {
				isoLang = CanonicalLangKey(fullLang)
			}
			for k, v := range subMap {
				if strings.TrimSpace(k) == "" || strings.TrimSpace(v) == "" {
					continue
				}
				if strings.EqualFold(k, "language") {
					if isoLang != "" && len(isoLang) == 3 {
						args = append(args,
							fmt.Sprintf("-metadata:s:s:%d", ss.TypeIndex),
							fmt.Sprintf("language=%s", isoLang),
						)
					}
					continue
				}
				args = append(args,
					fmt.Sprintf("-metadata:s:s:%d", ss.TypeIndex),
					fmt.Sprintf("%s=%s", k, expandCommon(v, "", fullLang)),
				)
			}
		}
	}

	args = append(args, outputPath)
	return args
}

// ApplyLosslessMetadata injects global and stream-level metadata tags in-place using `-c copy`.
func ApplyLosslessMetadata(
	ctx context.Context,
	inputPath string,
	settings models.UserSettings,
	probe *MediaProbe,
	md models.MediaMetadata,
) (string, error) {
	ext := filepath.Ext(inputPath)
	if ext == "" {
		ext = ".mkv"
	}
	tmpOut := strings.TrimSuffix(inputPath, ext) + ".meta_tmp" + ext
	args := BuildFFmpegMetadataArgs(inputPath, tmpOut, settings, probe, md)

	cmd := safety.NewSupervisedCommand(ctx, "ffmpeg", args...)
	ringBuf := safety.NewBoundedRingBuffer(64 * 1024)
	cmd.Stderr = ringBuf
	if err := cmd.Run(); err != nil {
		_ = os.Remove(tmpOut)
		return inputPath, fmt.Errorf("ffmpeg lossless metadata failed: %w (%s)", err, strings.TrimSpace(ringBuf.String()))
	}

	if err := os.Rename(tmpOut, inputPath); err != nil {
		_ = os.Remove(tmpOut)
		return inputPath, fmt.Errorf("replacing tagged file: %w", err)
	}
	return inputPath, nil
}
