// Package video implements Starfall-Go's zero-transcode lossless video pipeline:
// ffprobe stream inspection, language alias matching, archive extraction (`7z`),
// companion track discovery, single-pass `mkvmerge` stream filtering/reordering/intro-sub muxing,
// and lossless `ffmpeg -map 0 -c copy` global & per-stream metadata branding.
package video

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/starfall-org/starfall-go/internal/media"
)

// StreamInfo describes a single elementary track inside a media container.
type StreamInfo struct {
	Index      int    `json:"index"`       // Global container track ID (used by mkvmerge)
	TypeIndex  int    `json:"type_index"`  // 0-based index within its codec_type (v:0, a:0, s:0)
	CodecType  string `json:"codec_type"`  // "video", "audio", "subtitle"
	CodecName  string `json:"codec_name"`  // "hevc", "h264", "eac3", "aac", "subrip", "ass"
	PixFmt     string `json:"pix_fmt"`
	BitDepth   int    `json:"bit_depth"`
	Width      int    `json:"width"`
	Height     int    `json:"height"`
	Channels   int    `json:"channels"`
	Language   string `json:"language"`
	Title      string `json:"title"`
	IsDefault  bool   `json:"is_default"`
	IsForced   bool   `json:"is_forced"`
}

// MediaProbe aggregates container format and elementary stream metadata from `ffprobe`.
type MediaProbe struct {
	FilePath    string       `json:"file_path"`
	FormatName  string       `json:"format_name"`
	DurationSec float64      `json:"duration_sec"`
	SizeBytes   int64        `json:"size_bytes"`
	Streams     []StreamInfo `json:"streams"`
}

// VideoStreams returns all video streams (excluding attached cover art images).
func (p *MediaProbe) VideoStreams() []StreamInfo {
	var out []StreamInfo
	for _, s := range p.Streams {
		if s.CodecType == "video" && s.CodecName != "mjpeg" && s.CodecName != "png" && s.CodecName != "webp" {
			out = append(out, s)
		}
	}
	return out
}

// AudioStreams returns all audio streams.
func (p *MediaProbe) AudioStreams() []StreamInfo {
	var out []StreamInfo
	for _, s := range p.Streams {
		if s.CodecType == "audio" {
			out = append(out, s)
		}
	}
	return out
}

// SubtitleStreams returns all subtitle streams.
func (p *MediaProbe) SubtitleStreams() []StreamInfo {
	var out []StreamInfo
	for _, s := range p.Streams {
		if s.CodecType == "subtitle" {
			out = append(out, s)
		}
	}
	return out
}

// ToMediaTracks converts StreamInfo records into `media.ProbeTrackInfo` for `media.MergeProbeTracks`.
func (p *MediaProbe) ToMediaTracks() []media.ProbeTrackInfo {
	if p == nil {
		return nil
	}
	out := make([]media.ProbeTrackInfo, 0, len(p.Streams))
	for _, s := range p.Streams {
		if s.CodecType == "video" && (s.CodecName == "mjpeg" || s.CodecName == "png" || s.CodecName == "webp") {
			continue
		}
		out = append(out, media.ProbeTrackInfo{
			CodecType: s.CodecType,
			CodecName: s.CodecName,
			Width:     s.Width,
			Height:    s.Height,
			BitDepth:  s.BitDepth,
			Channels:  s.Channels,
			Language:  s.Language,
			Title:     s.Title,
		})
	}
	return out
}

type rawFFProbeOutput struct {
	Streams []struct {
		Index            int               `json:"index"`
		CodecType        string            `json:"codec_type"`
		CodecName        string            `json:"codec_name"`
		PixFmt           string            `json:"pix_fmt"`
		BitsPerRawSample string            `json:"bits_per_raw_sample"`
		Width            int               `json:"width"`
		Height           int               `json:"height"`
		Channels         int               `json:"channels"`
		Disposition      map[string]int    `json:"disposition"`
		Tags             map[string]string `json:"tags"`
	} `json:"streams"`
	Format struct {
		FormatName string `json:"format_name"`
		Duration   string `json:"duration"`
		Size       string `json:"size"`
	} `json:"format"`
}

// ProbeMedia executes `ffprobe` on `filePath` and parses stream metadata without loading media bytes.
func ProbeMedia(ctx context.Context, filePath string) (*MediaProbe, error) {
	cmd := exec.CommandContext(
		ctx,
		"ffprobe",
		"-v", "error",
		"-print_format", "json",
		"-show_format",
		"-show_streams",
		filePath,
	)
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		return nil, fmt.Errorf("ffprobe %s failed: %w (%s)", filepath.Base(filePath), err, strings.TrimSpace(stderr.String()))
	}

	var parsed rawFFProbeOutput
	if err := json.Unmarshal(stdout.Bytes(), &parsed); err != nil {
		return nil, fmt.Errorf("decoding ffprobe json: %w", err)
	}

	dur, _ := strconv.ParseFloat(parsed.Format.Duration, 64)
	sz, _ := strconv.ParseInt(parsed.Format.Size, 10, 64)

	typeCounters := map[string]int{}
	streams := make([]StreamInfo, 0, len(parsed.Streams))
	for _, st := range parsed.Streams {
		cType := strings.ToLower(strings.TrimSpace(st.CodecType))
		tIdx := typeCounters[cType]
		typeCounters[cType]++

		bitDepth, _ := strconv.Atoi(st.BitsPerRawSample)
		if bitDepth == 0 {
			pf := strings.ToLower(st.PixFmt)
			switch {
			case strings.Contains(pf, "12le") || strings.Contains(pf, "12be"):
				bitDepth = 12
			case strings.Contains(pf, "10le") || strings.Contains(pf, "10be") || strings.Contains(pf, "p010"):
				bitDepth = 10
			case pf != "":
				bitDepth = 8
			}
		}

		lang := tagInsensitive(st.Tags, "language")
		title := tagInsensitive(st.Tags, "title")
		if title == "" {
			title = tagInsensitive(st.Tags, "handler_name")
		}

		streams = append(streams, StreamInfo{
			Index:     st.Index,
			TypeIndex: tIdx,
			CodecType: cType,
			CodecName: strings.ToLower(strings.TrimSpace(st.CodecName)),
			PixFmt:    st.PixFmt,
			BitDepth:  bitDepth,
			Width:     st.Width,
			Height:    st.Height,
			Channels:  st.Channels,
			Language:  lang,
			Title:     title,
			IsDefault: st.Disposition["default"] == 1,
			IsForced:  st.Disposition["forced"] == 1,
		})
	}

	return &MediaProbe{
		FilePath:    filePath,
		FormatName:  parsed.Format.FormatName,
		DurationSec: dur,
		SizeBytes:   sz,
		Streams:     streams,
	}, nil
}

// IsVideoFile reports whether `path` has a video container file extension.
func IsVideoFile(path string) bool {
	switch strings.ToLower(filepath.Ext(path)) {
	case ".mkv", ".mp4", ".webm", ".avi", ".mov", ".m4v", ".ts", ".m2ts", ".flv":
		return true
	default:
		return false
	}
}

// LanguageAliases maps canonical language group keys to all accepted ISO codes and names.
var languageAliasGroups = [][]string{
	{"ta", "tam", "tamil"},
	{"te", "tel", "telugu"},
	{"hi", "hin", "hindi"},
	{"ml", "mal", "malayalam"},
	{"kn", "kan", "kannada"},
	{"en", "eng", "english"},
	{"ja", "jpn", "jap", "japanese"},
	{"ko", "kor", "korean"},
	{"zh", "zho", "chi", "cmn", "chinese", "mandarin"},
	{"es", "spa", "spanish"},
	{"fr", "fra", "fre", "french"},
	{"de", "deu", "ger", "german"},
	{"pt", "por", "portuguese"},
	{"ru", "rus", "russian"},
	{"ar", "ara", "arabic"},
	{"id", "ind", "indonesian"},
	{"th", "tha", "thai"},
}

// CanonicalLangKey normalizes a language token (`Tamil`, `tam`, `ta`) into a canonical 3-letter ISO-639-2 code.
func CanonicalLangKey(raw string) string {
	low := strings.ToLower(strings.TrimSpace(raw))
	if low == "" {
		return ""
	}
	for _, grp := range languageAliasGroups {
		for _, alias := range grp {
			if low == alias {
				return grp[1] // 3-letter ISO-639-2 code (e.g., "tam", "eng", "jpn")
			}
		}
	}
	return low
}

// ParseLanguageList parses comma/space/plus separated language preferences (e.g., `"Tamil, Japanese"`).
func ParseLanguageList(raw string) []string {
	parts := strings.FieldsFunc(raw, func(r rune) bool {
		return r == ',' || r == '+' || r == ';' || r == '|' || r == ' '
	})
	var out []string
	seen := make(map[string]struct{})
	for _, p := range parts {
		canon := CanonicalLangKey(p)
		if canon == "" {
			continue
		}
		if _, exists := seen[canon]; !exists {
			seen[canon] = struct{}{}
			out = append(out, canon)
		}
	}
	return out
}

// StreamMatchesLang checks if a stream's `Language` tag or `Title` matches `canonicalTarget` (`tam`, `eng`, etc.).
func StreamMatchesLang(st StreamInfo, canonicalTarget string) bool {
	if canonicalTarget == "" {
		return false
	}
	if CanonicalLangKey(st.Language) == canonicalTarget {
		return true
	}
	titleLow := strings.ToLower(st.Title)
	if titleLow == "" {
		return false
	}
	for _, grp := range languageAliasGroups {
		if grp[1] == canonicalTarget {
			for _, alias := range grp {
				if len(alias) >= 3 && strings.Contains(titleLow, alias) {
					return true
				}
			}
		}
	}
	return false
}

func tagInsensitive(tags map[string]string, key string) string {
	if len(tags) == 0 {
		return ""
	}
	if v, ok := tags[key]; ok {
		return strings.TrimSpace(v)
	}
	for k, v := range tags {
		if strings.EqualFold(k, key) {
			return strings.TrimSpace(v)
		}
	}
	return ""
}
