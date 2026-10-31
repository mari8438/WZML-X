// Package video orchestrates the single-pass lossless `mkvmerge` auto-processor
// (`AUTO_PROCESS`, `AUTO_KEEP_AUDIO_LANGS`, `AUTO_KEEP_SUBTITLE_LANGS`, `AUTO_ORDER`,
// `AUTO_INTRO_SUBTITLE`, companion track muxing, and `AUTO_METADATA`).
package video

import (
	"bytes"
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/starfall-org/starfall-go/internal/media"
	"github.com/starfall-org/starfall-go/internal/models"
)

// BuildMkvmergeArgs constructs the exact single-pass `mkvmerge` CLI arguments for `probe`,
// `settings`, optional `taskOpts`, external companion tracks, and optional `introSRTPath`.
func BuildMkvmergeArgs(
	inputPath, outputPath string,
	probe *MediaProbe,
	settings models.UserSettings,
	taskOpts *models.TaskVideoOptions,
	externalTracks []string,
	introSRTPath string,
) []string {
	args := []string{"-q", "-o", outputPath}

	hasExternalAudio := false
	for _, ext := range externalTracks {
		low := strings.ToLower(filepath.Ext(ext))
		if low == ".aac" || low == ".eac3" || low == ".ec3" || low == ".ac3" || low == ".mka" || low == ".opus" || low == ".flac" || low == ".mp3" || low == ".m4a" {
			hasExternalAudio = true
			break
		}
	}

	if probe != nil {
		// 1. Audio track selection + zero-match safety guard
		audioStreams := probe.AudioStreams()
		if taskOpts != nil && len(taskOpts.KeepAudioTrackIDs) > 0 {
			args = append(args, "--audio-tracks", joinInts(taskOpts.KeepAudioTrackIDs))
		} else if (settings.AutoProcess || settings.AutoRemoveStreams) && strings.TrimSpace(settings.AutoKeepAudioLangs) != "" && len(audioStreams) > 0 {
			keepLangs := ParseLanguageList(settings.AutoKeepAudioLangs)
			keptIDs := matchStreamsOrdered(audioStreams, keepLangs)
			if len(keptIDs) > 0 {
				args = append(args, "--audio-tracks", joinInts(keptIDs))
			} else if hasExternalAudio {
				args = append(args, "--no-audio")
			}
			// Zero-match safety guard: when 0 embedded tracks match and no external audio exists,
			// omit `--no-audio` so all original audio tracks remain intact rather than muting the video!
		}

		// 2. Subtitle track selection
		subStreams := probe.SubtitleStreams()
		if taskOpts != nil && len(taskOpts.KeepSubTrackIDs) > 0 {
			args = append(args, "--subtitle-tracks", joinInts(taskOpts.KeepSubTrackIDs))
		} else if (settings.AutoProcess || settings.AutoRemoveStreams) && strings.TrimSpace(settings.AutoKeepSubtitleLangs) != "" && len(subStreams) > 0 {
			keepSubLangs := ParseLanguageList(settings.AutoKeepSubtitleLangs)
			keptSubIDs := matchStreamsOrdered(subStreams, keepSubLangs)
			if len(keptSubIDs) > 0 {
				args = append(args, "--subtitle-tracks", joinInts(keptSubIDs))
			}
		}

		// 3. Audio / Subtitle Sync Delay (`--sync <tid>:<ms>`)
		if taskOpts != nil {
			if taskOpts.AudioSyncDelayMs != 0 {
				for _, as := range audioStreams {
					args = append(args, "--sync", fmt.Sprintf("%d:%d", as.Index, taskOpts.AudioSyncDelayMs))
				}
			}
			if taskOpts.SubSyncDelayMs != 0 {
				for _, ss := range subStreams {
					args = append(args, "--sync", fmt.Sprintf("%d:%d", ss.Index, taskOpts.SubSyncDelayMs))
				}
			}
		}

		// 4. Optional Track Reordering (`AUTO_ORDER`, `AUTO_AUDIO_ORDER`, `AUTO_SUBTITLE_ORDER`)
		if settings.AutoOrder && (strings.TrimSpace(settings.AutoAudioOrder) != "" || strings.TrimSpace(settings.AutoSubtitleOrder) != "") {
			orderSpec := buildTrackOrderSpec(probe, settings.AutoAudioOrder, settings.AutoSubtitleOrder)
			if orderSpec != "" {
				args = append(args, "--track-order", orderSpec)
			}
		}
	}

	// Primary container input
	args = append(args, inputPath)

	// 5. External companion audio/subtitle files (`AUTO_MERGE` / `-i N -m`)
	for _, extTrack := range externalTracks {
		if strings.TrimSpace(extTrack) == "" {
			continue
		}
		guessedLang := guessLangFromTrackFilename(extTrack)
		if guessedLang != "" {
			args = append(args, "--language", "0:"+guessedLang)
		}
		args = append(args, extTrack)
	}

	// 6. Intro Branding `.srt` Subtitle Track (`AUTO_INTRO_SUBTITLE`)
	if strings.TrimSpace(introSRTPath) != "" {
		brandTitle := strings.TrimSpace(settings.IntroSubtitleText)
		if brandTitle == "" {
			brandTitle = "[@Anime_Starfall🥰]"
		}
		args = append(args,
			"--language", "0:eng",
			"--track-name", "0:"+brandTitle,
			"--default-track-flag", "0:yes",
			introSRTPath,
		)
	}

	return args
}

// ProcessVideoBundle runs the complete zero-transcode StarfallX video pipeline on `bundle`:
//  1. `ffprobe` inspection
//  2. Single-pass `mkvmerge` stream keep/reorder + companion mux + intro `.srt` (when enabled)
//  3. Lossless `ffmpeg -map 0 -c copy` global & per-stream metadata branding (`AUTO_METADATA`)
//
// Returns the resulting file path, final `MediaProbe`, and enriched `models.MediaMetadata`.
func ProcessVideoBundle(
	ctx context.Context,
	bundle MediaBundle,
	settings models.UserSettings,
	taskOpts *models.TaskVideoOptions,
	workDir string,
) (string, *MediaProbe, models.MediaMetadata, error) {
	currentPath := bundle.PrimaryPath
	md := media.ParseFilename(filepath.Base(currentPath))

	probe, probeErr := ProbeMedia(ctx, currentPath)
	if probeErr == nil && probe != nil {
		md = media.MergeProbeTracks(md, probe.ToMediaTracks())
		md.DurationSec = probe.DurationSec
	}

	extTracks := append([]string(nil), bundle.ExternalTracks...)
	if taskOpts != nil && len(taskOpts.ExternalTracks) > 0 {
		extTracks = append(extTracks, taskOpts.ExternalTracks...)
	}

	wantIntro := settings.AutoIntroSubtitle || (taskOpts != nil && taskOpts.IntroSubEnabled)
	wantRemux := settings.AutoProcess || settings.AutoRemoveStreams || wantIntro || len(extTracks) > 0 || taskOpts != nil

	if wantRemux {
		var introSRT string
		if wantIntro {
			introText := settings.IntroSubtitleText
			introDur := settings.IntroSubtitleDuration
			if taskOpts != nil {
				if taskOpts.IntroSubText != "" {
					introText = taskOpts.IntroSubText
				}
				if taskOpts.IntroSubDuration > 0 {
					introDur = taskOpts.IntroSubDuration
				}
			}
			srtPath, err := WriteIntroSRT(workDir, introText, introDur, settings.IntroSubtitleRanges)
			if err == nil {
				introSRT = srtPath
				defer func() { _ = os.Remove(srtPath) }()
			}
		}

		stem := strings.TrimSuffix(filepath.Base(currentPath), filepath.Ext(currentPath))
		remuxOut := filepath.Join(filepath.Dir(currentPath), stem+".starfall_mux.mkv")
		args := BuildMkvmergeArgs(currentPath, remuxOut, probe, settings, taskOpts, extTracks, introSRT)

		cmd := exec.CommandContext(ctx, "mkvmerge", args...)
		var stderr, stdout bytes.Buffer
		cmd.Stdout = &stdout
		cmd.Stderr = &stderr
		err := cmd.Run()
		// mkvmerge exit code 1 means "warnings generated" (mux succeeded); exit code 2 means fatal error!
		exitCode := 0
		if err != nil {
			if ee, ok := err.(*exec.ExitError); ok {
				exitCode = ee.ExitCode()
			} else {
				exitCode = 2
			}
		}

		if exitCode <= 1 {
			if fi, serr := os.Stat(remuxOut); serr == nil && fi.Size() > 0 {
				finalMkv := filepath.Join(filepath.Dir(currentPath), stem+".mkv")
				_ = os.Remove(currentPath)
				for _, et := range bundle.ExternalTracks {
					_ = os.Remove(et)
				}
				if err := os.Rename(remuxOut, finalMkv); err == nil {
					currentPath = finalMkv
				} else {
					currentPath = remuxOut
				}
				if newProbe, perr := ProbeMedia(ctx, currentPath); perr == nil {
					probe = newProbe
					md = media.MergeProbeTracks(md, probe.ToMediaTracks())
					md.DurationSec = probe.DurationSec
				}
			} else {
				_ = os.Remove(remuxOut)
			}
		} else {
			_ = os.Remove(remuxOut)
			return currentPath, probe, md, fmt.Errorf("mkvmerge failed (exit %d): %s %s", exitCode, strings.TrimSpace(stderr.String()), strings.TrimSpace(stdout.String()))
		}
	}

	// Apply lossless container & per-stream metadata branding (`AUTO_METADATA`)
	if settings.AutoMetadata {
		if taggedPath, merr := ApplyLosslessMetadata(ctx, currentPath, settings, probe, md); merr == nil {
			currentPath = taggedPath
		}
	}

	return currentPath, probe, md, nil
}

func matchStreamsOrdered(streams []StreamInfo, canonicalLangs []string) []int {
	var out []int
	seen := make(map[int]bool)
	for _, lang := range canonicalLangs {
		for _, st := range streams {
			if !seen[st.Index] && StreamMatchesLang(st, lang) {
				seen[st.Index] = true
				out = append(out, st.Index)
			}
		}
	}
	return out
}

func buildTrackOrderSpec(probe *MediaProbe, audioOrderRaw, subOrderRaw string) string {
	var specs []string
	for _, vs := range probe.VideoStreams() {
		specs = append(specs, fmt.Sprintf("0:%d", vs.Index))
	}
	audioPref := ParseLanguageList(audioOrderRaw)
	seenAudio := make(map[int]bool)
	for _, lang := range audioPref {
		for _, as := range probe.AudioStreams() {
			if !seenAudio[as.Index] && StreamMatchesLang(as, lang) {
				seenAudio[as.Index] = true
				specs = append(specs, fmt.Sprintf("0:%d", as.Index))
			}
		}
	}
	for _, as := range probe.AudioStreams() {
		if !seenAudio[as.Index] {
			specs = append(specs, fmt.Sprintf("0:%d", as.Index))
		}
	}

	subPref := ParseLanguageList(subOrderRaw)
	seenSub := make(map[int]bool)
	for _, lang := range subPref {
		for _, ss := range probe.SubtitleStreams() {
			if !seenSub[ss.Index] && StreamMatchesLang(ss, lang) {
				seenSub[ss.Index] = true
				specs = append(specs, fmt.Sprintf("0:%d", ss.Index))
			}
		}
	}
	for _, ss := range probe.SubtitleStreams() {
		if !seenSub[ss.Index] {
			specs = append(specs, fmt.Sprintf("0:%d", ss.Index))
		}
	}
	return strings.Join(specs, ",")
}

func guessLangFromTrackFilename(path string) string {
	base := strings.ToLower(filepath.Base(path))
	for _, grp := range languageAliasGroups {
		for _, alias := range grp {
			if len(alias) >= 3 && strings.Contains(base, alias) {
				return grp[1]
			}
		}
	}
	return ""
}

func joinInts(ids []int) string {
	parts := make([]string, len(ids))
	for i, id := range ids {
		parts[i] = strconv.Itoa(id)
	}
	return strings.Join(parts, ",")
}
