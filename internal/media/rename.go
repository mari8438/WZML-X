// Package media implements regex swap rules, StarfallX `lremname_auto` template
// placeholder substitution, filename sanitization, and `LEECH_FONT` HTML caption styling.
package media

import (
	"fmt"
	"html"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"

	"github.com/starfall-org/starfall-go/internal/models"
)

var (
	reEmptyBrackets  = regexp.MustCompile(`\[\s*[-~]*\s*\]|\(\s*[-~]*\s*\)`)
	reTrailingHyphen = regexp.MustCompile(`\[([^\[\]]*?)\s*-\s*\]`)
	reLeadingHyphen  = regexp.MustCompile(`\[\s*-\s*([^\[\]]*?)\]`)
	reIllegalFSChars = regexp.MustCompile(`[<>:"/\\|?*\x00-\x1f]`)
)

// ApplyRegexSwaps applies `NAME_SWAP` and `lremname_regex` rules onto `input`.
// Supports:
//  1. Bare regex strip patterns such as `^\[Judas\]\s*`
//  2. `pattern:replacement` or `pattern=>replacement` pairs
//  3. Multiple rules separated by `;` or `|` (when `:` or `=>` is used)
func ApplyRegexSwaps(input string, ruleSets ...string) string {
	result := input
	for _, rawSet := range ruleSets {
		rawSet = strings.TrimSpace(rawSet)
		if rawSet == "" {
			continue
		}

		rules := splitSwapRules(rawSet)
		for _, rule := range rules {
			rule = strings.TrimSpace(rule)
			if rule == "" {
				continue
			}
			pat, repl := parseSingleSwapRule(rule)
			if pat == "" {
				continue
			}
			re, err := regexp.Compile(pat)
			if err != nil {
				continue
			}
			result = re.ReplaceAllString(result, repl)
		}
	}
	return strings.TrimSpace(collapseSpaces(result))
}

// FormatAutoRename produces the final sanitized filename (including extension) for a media file
// using `NAME_SWAP`, `lremname_regex`, `lremname_auto`, `LEECH_PREFIX`, and `LEECH_SUFFIX`.
func FormatAutoRename(originalFilename string, md models.MediaMetadata, settings models.UserSettings) string {
	base := filepath.Base(originalFilename)
	ext := filepath.Ext(base)
	stem := strings.TrimSuffix(base, ext)

	// 1. Apply regex swaps on both raw stem and extracted title.
	swappedStem := ApplyRegexSwaps(stem, settings.NameSwap, settings.LRemnameRegex)
	cleanName := strings.TrimSpace(md.Title)
	if cleanName != "" {
		cleanName = ApplyRegexSwaps(cleanName, settings.NameSwap, settings.LRemnameRegex)
	} else {
		cleanName = swappedStem
	}

	finalStem := swappedStem
	if settings.AutoRename && strings.TrimSpace(settings.LRemnameAuto) != "" {
		tpl := settings.LRemnameAuto

		// Format season & episode placeholders
		seasonStr := ""
		if md.Season > 0 {
			seasonStr = fmt.Sprintf("%02d", md.Season)
		}
		epStr := ""
		if md.Episode > 0 {
			if md.EndEpisode > md.Episode {
				epStr = fmt.Sprintf("%02d-%02d", md.Episode, md.EndEpisode)
			} else {
				epStr = fmt.Sprintf("%02d", md.Episode)
			}
		}

		// Handle movies or season packs gracefully when `[S{season}E{episode}]` is in template
		if seasonStr == "" && epStr == "" {
			tpl = strings.ReplaceAll(tpl, "[S{season}E{episode}]", "")
			tpl = strings.ReplaceAll(tpl, "S{season}E{episode}", "")
		} else if seasonStr != "" && epStr == "" {
			tpl = strings.ReplaceAll(tpl, "[S{season}E{episode}]", "[S"+seasonStr+"]")
			tpl = strings.ReplaceAll(tpl, "S{season}E{episode}", "S"+seasonStr)
		} else if seasonStr == "" && epStr != "" {
			tpl = strings.ReplaceAll(tpl, "[S{season}E{episode}]", "[E"+epStr+"]")
			tpl = strings.ReplaceAll(tpl, "S{season}E{episode}", "E"+epStr)
		}

		yearStr := ""
		if md.Year > 0 {
			yearStr = strconv.Itoa(md.Year)
		}

		langStr := strings.Join(md.AudioLanguages, "+")
		audioCombined := strings.TrimSpace(md.AudioCodec + " " + md.AudioChannels)

		replacer := strings.NewReplacer(
			"{season}", seasonStr,
			"{episode}", epStr,
			"{name}", cleanName,
			"{title}", cleanName,
			"{year}", yearStr,
			"{resolution}", md.Resolution,
			"{bit}", md.BitDepth,
			"{ott}", md.OTT,
			"{quality}", md.Quality,
			"{codec}", md.VideoCodec,
			"{languages}", langStr,
			"{audio}", audioCombined,
			"{audio_codec}", md.AudioCodec,
			"{audio_channels}", md.AudioChannels,
			"{shortsub}", md.ShortSub,
		)

		rendered := replacer.Replace(tpl)
		rendered = reTrailingHyphen.ReplaceAllString(rendered, "[$1]")
		rendered = reLeadingHyphen.ReplaceAllString(rendered, "[$1]")
		rendered = reEmptyBrackets.ReplaceAllString(rendered, "")
		rendered = collapseSpaces(rendered)
		if strings.TrimSpace(rendered) != "" {
			finalStem = strings.TrimSpace(rendered)
		}
	}

	if prefix := strings.TrimSpace(settings.LeechPrefix); prefix != "" {
		if !strings.HasPrefix(finalStem, prefix) {
			finalStem = prefix + " " + finalStem
		}
	}
	if suffix := strings.TrimSpace(settings.LeechSuffix); suffix != "" {
		if !strings.HasSuffix(finalStem, suffix) {
			finalStem = finalStem + " " + suffix
		}
	}

	finalStem = SanitizeFilename(finalStem)
	if finalStem == "" {
		finalStem = "Starfall_Media"
	}
	return finalStem + ext
}

// SanitizeFilename strips OS-illegal path characters while preserving emojis and Unicode symbols.
func SanitizeFilename(name string) string {
	clean := reIllegalFSChars.ReplaceAllString(name, " ")
	clean = collapseSpaces(clean)
	clean = strings.Trim(clean, ". ")
	// Clamp stem byte length below 235 bytes for Linux ext4/xfs 255-byte filename limits.
	for len(clean) > 235 {
		runes := []rune(clean)
		if len(runes) <= 10 {
			break
		}
		clean = strings.TrimSpace(string(runes[:len(runes)-2]))
	}
	return clean
}

// FormatUploadCaption wraps `text` in Telegram HTML font tags (`<b>`, `<i>`, `<code>`, `<u>`, `<s>`, `<tg-spoiler>`).
func FormatUploadCaption(text string, font string) string {
	escaped := html.EscapeString(strings.TrimSpace(text))
	if escaped == "" {
		return ""
	}
	switch strings.ToLower(strings.TrimSpace(font)) {
	case "b", "bold":
		return "<b>" + escaped + "</b>"
	case "i", "italic":
		return "<i>" + escaped + "</i>"
	case "code", "mono", "monospace":
		return "<code>" + escaped + "</code>"
	case "u", "underline":
		return "<u>" + escaped + "</u>"
	case "s", "strike":
		return "<s>" + escaped + "</s>"
	case "spoiler":
		return "<tg-spoiler>" + escaped + "</tg-spoiler>"
	default:
		return escaped
	}
}

func splitSwapRules(raw string) []string {
	if strings.Contains(raw, ";") {
		return strings.Split(raw, ";")
	}
	if strings.Contains(raw, "\n") {
		return strings.Split(raw, "\n")
	}
	// Only split on `|` if the rule list uses `:` or `=>` key-value pairs;
	// otherwise preserve `|` for regex alternation inside a single pattern.
	if strings.Contains(raw, "|") && (strings.Contains(raw, ":") || strings.Contains(raw, "=>")) {
		return strings.Split(raw, "|")
	}
	return []string{raw}
}

func parseSingleSwapRule(rule string) (pattern, replacement string) {
	if idx := strings.Index(rule, "=>"); idx != -1 {
		return strings.TrimSpace(rule[:idx]), strings.TrimSpace(rule[idx+2:])
	}
	if idx := strings.Index(rule, ":::"); idx != -1 {
		return strings.TrimSpace(rule[:idx]), strings.TrimSpace(rule[idx+3:])
	}
	// Look for unescaped `:` outside `[...]` and not part of `(?:...)` regex syntax.
	inBracket := false
	escaped := false
	for i := 0; i < len(rule); i++ {
		ch := rule[i]
		if escaped {
			escaped = false
			continue
		}
		if ch == '\\' {
			escaped = true
			continue
		}
		if ch == '[' {
			inBracket = true
			continue
		}
		if ch == ']' {
			inBracket = false
			continue
		}
		if ch == ':' && !inBracket {
			if i >= 2 && rule[i-2:i] == "(?" {
				continue
			}
			return rule[:i], rule[i+1:]
		}
	}
	return rule, ""
}
