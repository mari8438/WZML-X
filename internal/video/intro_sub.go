// Package video generates lossless UTF-8 SubRip (`.srt`) intro branding subtitle tracks
// (`AUTO_INTRO_SUBTITLE`, `INTRO_SUBTITLE_TEXT`, `INTRO_SUBTITLE_DURATION`, `INTRO_SUBTITLE_RANGES`).
package video

import (
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

type timeRange struct {
	startSec int
	endSec   int
}

// WriteIntroSRT writes a temporary UTF-8 `.srt` subtitle file inside `workDir` displaying
// `text` (e.g. `[@Anime_Starfall🥰]`) from `00:00:00,000` to `durationSec` (or custom `ranges`).
func WriteIntroSRT(workDir, text string, durationSec int, ranges string) (string, error) {
	text = strings.TrimSpace(text)
	if text == "" {
		text = "[@Anime_Starfall🥰]"
	}
	if durationSec <= 0 {
		durationSec = 10
	}
	if err := os.MkdirAll(workDir, 0o755); err != nil {
		return "", fmt.Errorf("creating intro srt dir: %w", err)
	}

	parsedRanges := parseIntroRanges(ranges)
	if len(parsedRanges) == 0 {
		parsedRanges = []timeRange{{startSec: 0, endSec: durationSec}}
	}

	var sb strings.Builder
	for i, rg := range parsedRanges {
		sb.WriteString(strconv.Itoa(i + 1))
		sb.WriteString("\n")
		sb.WriteString(formatSRTTimestamp(rg.startSec))
		sb.WriteString(" --> ")
		sb.WriteString(formatSRTTimestamp(rg.endSec))
		sb.WriteString("\n")
		sb.WriteString(text)
		sb.WriteString("\n\n")
	}

	srtPath := filepath.Join(workDir, "starfall_intro_brand.srt")
	if err := os.WriteFile(srtPath, []byte(sb.String()), 0o644); err != nil {
		return "", fmt.Errorf("writing intro srt %s: %w", srtPath, err)
	}
	return srtPath, nil
}

func parseIntroRanges(raw string) []timeRange {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return nil
	}
	parts := strings.FieldsFunc(raw, func(r rune) bool {
		return r == ',' || r == ';'
	})
	var out []timeRange
	for _, p := range parts {
		dash := strings.Split(strings.TrimSpace(p), "-")
		if len(dash) != 2 {
			continue
		}
		s, err1 := strconv.Atoi(strings.TrimSpace(dash[0]))
		e, err2 := strconv.Atoi(strings.TrimSpace(dash[1]))
		if err1 == nil && err2 == nil && s >= 0 && e > s {
			out = append(out, timeRange{startSec: s, endSec: e})
		}
	}
	return out
}

func formatSRTTimestamp(totalSec int) string {
	h := totalSec / 3600
	m := (totalSec % 3600) / 60
	s := totalSec % 60
	return fmt.Sprintf("%02d:%02d:%02d,000", h, m, s)
}
