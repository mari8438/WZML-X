// Package media provides smart anime/TV/movie filename parsing, ffprobe stream enrichment,
// regex swap rules, and template-driven auto-renaming (`lremname_auto`) for Starfall-Go.
package media

import (
	"path/filepath"
	"regexp"
	"strconv"
	"strings"

	"github.com/starfall-org/starfall-go/internal/models"
)

var (
	reLeadingBracketTag = regexp.MustCompile(`^(?:\[[^\]]+\]\s*|\([^\)]+\)\s*)+`)
	reLeadingSitePrefix = regexp.MustCompile(`(?i)^(?:www\.[a-z0-9\-]+\.[a-z]{2,6}\s*[-–~]\s*)+`)
	reSeasonEpisode     = regexp.MustCompile(`(?i)\bS(\d{1,2})[\s._-]*E(\d{1,4})(?:[\s._-]*(?:E|-|to)[\s._-]*(\d{1,4}))?\b`)
	reAltNxM            = regexp.MustCompile(`(?i)\b(\d{1,2})x(\d{1,3})\b`)
	reSeasonOnly        = regexp.MustCompile(`(?i)\b(?:Season[\s._-]*(\d{1,2})|S(\d{1,2}))\b`)
	reAnimeDashEp       = regexp.MustCompile(`(?i)(?:[\s._-]+-\s*|\bEP?[\s._-]*)(\d{1,4})(?:v\d)?(?:\s*[\[\(]|\s+1080p|\s+720p|\s+2160p|\s+480p|\.|$)`)
	reYear              = regexp.MustCompile(`\b(19[89]\d|20[0-3]\d)\b`)
	reResolution        = regexp.MustCompile(`(?i)\b(2160p|4k|1080p|720p|576p|480p|360p)\b`)
	reBitDepth          = regexp.MustCompile(`(?i)\b(10[\s._-]?bit|8[\s._-]?bit|12[\s._-]?bit|hi10p)\b`)
	reOTT               = regexp.MustCompile(`(?i)\b(CR|Crunchyroll|NF|Netflix|AMZN|Amazon|DSNP|Disney\+?|HMAX|BILI|B-Global|HIDIVE|ATVP|HULU|SONYLIV|ZEE5|JIO|AHA)\b`)
	reQuality           = regexp.MustCompile(`(?i)\b(WEB[\s._-]?DL|WEBRip|Blu[\s._-]?Ray|BDRip|BRRip|REMUX|HDTV|HDRip|DVDRip)\b`)
	reVideoCodec        = regexp.MustCompile(`(?i)\b(HEVC|x265|H\.?265|AVC|x264|H\.?264|AV1|VP9)\b`)
	reAudioCombined     = regexp.MustCompile(`(?i)\b(DDP|EAC3|E-AC-3|AC3|DD|AAC|FLAC|OPUS|TRUEHD|DTS(?:-HD)?)[\s._-]*(7\.1|5\.1|2\.0|2\.1|1\.0)?\b`)
	reChannelsStandalone = regexp.MustCompile(`\b(7\.1|5\.1|2\.0|1\.0)\b`)
	reShortSub          = regexp.MustCompile(`(?i)\b(ESubs?|MSubs?|HSubs?|Multi[\s._-]?Subs?)\b`)
	reAnimeGroup        = regexp.MustCompile(`(?i)\[(SubsPlease|Erai-raws|Judas|EMBER|ASW|ToonsHub|AnimeRG|DeadFish|HorribleSubs|DKB|psa|Golumpa|Yameii| Varyg)\]`)
)

// ProbeTrackInfo summarizes an audio, video, or subtitle stream from ffprobe for metadata enrichment.
type ProbeTrackInfo struct {
	CodecType string // "video", "audio", "subtitle"
	CodecName string // "hevc", "h264", "eac3", "aac", "subrip", "ass"
	Width     int
	Height    int
	BitDepth  int
	Channels  int
	Language  string
	Title     string
}

// ParseFilename extracts structured MediaMetadata (`{season}`, `{episode}`, `{name}`, `{year}`,
// `{resolution}`, `{bit}`, `{ott}`, `{quality}`, `{codec}`, `{languages}`, `{audio_codec}`,
// `{audio_channels}`, `{shortsub}`) from a raw release filename.
func ParseFilename(filename string) models.MediaMetadata {
	base := filepath.Base(filename)
	ext := filepath.Ext(base)
	if len(ext) <= 6 {
		base = strings.TrimSuffix(base, ext)
	}

	var md models.MediaMetadata
	isAnimeHint := reAnimeGroup.MatchString(base)

	working := reLeadingSitePrefix.ReplaceAllString(base, "")
	working = reLeadingBracketTag.ReplaceAllString(working, "")

	// Normalize separators into spaces while keeping hyphens for anime episode detection.
	spaced := strings.NewReplacer("_", " ", ".", " ").Replace(working)

	// 1. Season & Episode extraction
	titleCutIdx := len(spaced)
	if m := reSeasonEpisode.FindStringSubmatchIndex(spaced); m != nil {
		md.Season, _ = strconv.Atoi(spaced[m[2]:m[3]])
		md.Episode, _ = strconv.Atoi(spaced[m[4]:m[5]])
		if m[6] != -1 && m[7] != -1 {
			md.EndEpisode, _ = strconv.Atoi(spaced[m[6]:m[7]])
		}
		if m[0] < titleCutIdx {
			titleCutIdx = m[0]
		}
	} else if m := reAltNxM.FindStringSubmatchIndex(spaced); m != nil {
		md.Season, _ = strconv.Atoi(spaced[m[2]:m[3]])
		md.Episode, _ = strconv.Atoi(spaced[m[4]:m[5]])
		if m[0] < titleCutIdx {
			titleCutIdx = m[0]
		}
	} else {
		if sm := reSeasonOnly.FindStringSubmatchIndex(spaced); sm != nil {
			sStr := ""
			if sm[2] != -1 {
				sStr = spaced[sm[2]:sm[3]]
			} else if sm[4] != -1 {
				sStr = spaced[sm[4]:sm[5]]
			}
			md.Season, _ = strconv.Atoi(sStr)
			if sm[0] < titleCutIdx {
				titleCutIdx = sm[0]
			}
		}
		if em := reAnimeDashEp.FindStringSubmatchIndex(spaced); em != nil {
			epVal, _ := strconv.Atoi(spaced[em[2]:em[3]])
			if epVal > 0 && epVal != 1080 && epVal != 720 && epVal != 2160 && epVal != 480 && epVal < 1980 {
				md.Episode = epVal
				if md.Season == 0 {
					md.Season = 1
				}
				isAnimeHint = true
				if em[0] < titleCutIdx {
					titleCutIdx = em[0]
				}
			}
		}
	}

	// 2. Year extraction
	for _, ym := range reYear.FindAllStringSubmatchIndex(spaced, -1) {
		yr, _ := strconv.Atoi(spaced[ym[2]:ym[3]])
		if yr >= 1980 && yr <= 2035 {
			md.Year = yr
			if ym[0] < titleCutIdx && ym[0] > 2 {
				titleCutIdx = ym[0]
			}
			break
		}
	}

	// 3. Resolution
	if m := reResolution.FindStringSubmatchIndex(spaced); m != nil {
		res := strings.ToLower(spaced[m[2]:m[3]])
		if res == "4k" {
			md.Resolution = "2160p"
		} else {
			md.Resolution = res
		}
		if m[0] < titleCutIdx && m[0] > 2 {
			titleCutIdx = m[0]
		}
	}

	// 4. Clean Title
	rawTitle := strings.TrimSpace(spaced[:titleCutIdx])
	rawTitle = strings.Trim(rawTitle, "-–~[]() ")
	rawTitle = collapseSpaces(rawTitle)
	if rawTitle == "" {
		rawTitle = collapseSpaces(strings.Trim(spaced, "-–~[]() "))
	}
	md.Title = rawTitle

	// 5. Bit Depth
	if m := reBitDepth.FindStringSubmatch(spaced); len(m) > 1 {
		b := strings.ToLower(strings.ReplaceAll(strings.ReplaceAll(m[1], "-", ""), " ", ""))
		if b == "hi10p" || strings.HasPrefix(b, "10") {
			md.BitDepth = "10bit"
		} else if strings.HasPrefix(b, "12") {
			md.BitDepth = "12bit"
		} else if strings.HasPrefix(b, "8") {
			md.BitDepth = "8bit"
		}
	}

	// 6. OTT Platform
	if m := reOTT.FindStringSubmatch(spaced); len(m) > 1 {
		switch strings.ToUpper(m[1]) {
		case "CR", "CRUNCHYROLL":
			md.OTT = "CR"
			isAnimeHint = true
		case "NF", "NETFLIX":
			md.OTT = "NF"
		case "AMZN", "AMAZON":
			md.OTT = "AMZN"
		case "DSNP", "DISNEY", "DISNEY+":
			md.OTT = "DSNP"
		case "BILI", "B-GLOBAL":
			md.OTT = "BILI"
			isAnimeHint = true
		case "HIDIVE":
			md.OTT = "HIDIVE"
			isAnimeHint = true
		default:
			md.OTT = strings.ToUpper(m[1])
		}
	}

	// 7. Quality / Source
	if m := reQuality.FindStringSubmatch(spaced); len(m) > 1 {
		q := strings.ToUpper(strings.ReplaceAll(strings.ReplaceAll(m[1], ".", ""), " ", "-"))
		switch {
		case strings.Contains(q, "WEB") && strings.Contains(q, "DL"):
			md.Quality = "WEB-DL"
		case strings.Contains(q, "WEBRIP"):
			md.Quality = "WEBRip"
		case strings.Contains(q, "BLURAY"):
			md.Quality = "BluRay"
		case strings.Contains(q, "BDRIP"):
			md.Quality = "BDRip"
		case strings.Contains(q, "HDRIP"):
			md.Quality = "HDRip"
		default:
			md.Quality = m[1]
		}
	}

	// 8. Video Codec
	if m := reVideoCodec.FindStringSubmatch(spaced); len(m) > 1 {
		vc := strings.ToUpper(strings.ReplaceAll(m[1], ".", ""))
		switch vc {
		case "HEVC", "X265", "H265":
			md.VideoCodec = "HEVC"
		case "AVC", "X264", "H264":
			md.VideoCodec = "AVC"
		case "AV1":
			md.VideoCodec = "AV1"
		case "VP9":
			md.VideoCodec = "VP9"
		}
	}

	// 9. Audio Codec & Channels
	if m := reAudioCombined.FindStringSubmatch(base); len(m) > 1 {
		ac := strings.ToUpper(strings.ReplaceAll(m[1], "-", ""))
		switch ac {
		case "DDP", "EAC3":
			md.AudioCodec = "EAC3"
		case "DD", "AC3":
			md.AudioCodec = "AC3"
		case "TRUEHD":
			md.AudioCodec = "TrueHD"
		default:
			md.AudioCodec = ac
		}
		if len(m) > 2 && m[2] != "" {
			md.AudioChannels = m[2]
		}
	}
	if md.AudioChannels == "" {
		if cm := reChannelsStandalone.FindStringSubmatch(base); len(cm) > 1 {
			md.AudioChannels = cm[1]
		}
	}

	// 10. Languages from filename
	md.AudioLanguages = detectLanguagesInText(spaced)

	// 11. ShortSub
	if m := reShortSub.FindStringSubmatch(spaced); len(m) > 1 {
		low := strings.ToLower(m[1])
		switch {
		case strings.HasPrefix(low, "msub") || strings.HasPrefix(low, "multi"):
			md.ShortSub = "MSub"
		case strings.HasPrefix(low, "hsub"):
			md.ShortSub = "HSub"
		default:
			md.ShortSub = "ESub"
		}
	}

	// 12. Category
	if isAnimeHint {
		md.Category = "ANIME"
	} else if md.Season > 0 || md.Episode > 0 {
		md.Category = "TV"
	} else {
		md.Category = "MOVIE"
	}

	return md
}

// MergeProbeTracks enriches filename-parsed metadata with ground-truth stream codecs,
// resolution, bit-depth, channels, and languages read via `ffprobe`.
func MergeProbeTracks(md models.MediaMetadata, tracks []ProbeTrackInfo) models.MediaMetadata {
	var audioLangs []string
	var subLangs []string
	seenAudio := make(map[string]struct{})
	seenSub := make(map[string]struct{})

	for _, tr := range tracks {
		switch tr.CodecType {
		case "video":
			if md.Resolution == "" && tr.Height > 0 {
				switch {
				case tr.Height >= 2000 || tr.Width >= 3800:
					md.Resolution = "2160p"
				case tr.Height >= 1000 || tr.Width >= 1900:
					md.Resolution = "1080p"
				case tr.Height >= 700 || tr.Width >= 1260:
					md.Resolution = "720p"
				case tr.Height >= 460:
					md.Resolution = "480p"
				default:
					md.Resolution = strconv.Itoa(tr.Height) + "p"
				}
			}
			if tr.BitDepth >= 10 {
				md.BitDepth = strconv.Itoa(tr.BitDepth) + "bit"
			} else if md.BitDepth == "" && tr.BitDepth == 8 {
				md.BitDepth = "8bit"
			}
			switch strings.ToLower(tr.CodecName) {
			case "hevc":
				md.VideoCodec = "HEVC"
			case "h264":
				md.VideoCodec = "AVC"
			case "av1":
				md.VideoCodec = "AV1"
			case "vp9":
				md.VideoCodec = "VP9"
			}

		case "audio":
			if md.AudioCodec == "" {
				switch strings.ToLower(tr.CodecName) {
				case "eac3":
					md.AudioCodec = "EAC3"
				case "ac3":
					md.AudioCodec = "AC3"
				case "aac":
					md.AudioCodec = "AAC"
				case "opus":
					md.AudioCodec = "OPUS"
				case "flac":
					md.AudioCodec = "FLAC"
				case "truehd":
					md.AudioCodec = "TrueHD"
				case "dts":
					md.AudioCodec = "DTS"
				case "mp3":
					md.AudioCodec = "MP3"
				}
			}
			if md.AudioChannels == "" && tr.Channels > 0 {
				switch tr.Channels {
				case 1:
					md.AudioChannels = "1.0"
				case 2:
					md.AudioChannels = "2.0"
				case 6:
					md.AudioChannels = "5.1"
				case 8:
					md.AudioChannels = "7.1"
				}
			}
			langCode := ShortLanguageLabel(tr.Language, tr.Title)
			if langCode != "" {
				if _, ok := seenAudio[langCode]; !ok {
					seenAudio[langCode] = struct{}{}
					audioLangs = append(audioLangs, langCode)
				}
			}

		case "subtitle":
			langCode := ShortLanguageLabel(tr.Language, tr.Title)
			if langCode == "" {
				langCode = "Eng"
			}
			if _, ok := seenSub[langCode]; !ok {
				seenSub[langCode] = struct{}{}
				subLangs = append(subLangs, langCode)
			}
		}
	}

	if len(audioLangs) > 0 {
		md.AudioLanguages = audioLangs
	}
	if len(subLangs) > 0 {
		md.SubLanguages = subLangs
		if len(subLangs) > 1 {
			md.ShortSub = "MSub"
		} else {
			md.ShortSub = "ESub"
		}
	}
	return md
}

// ShortLanguageLabel converts ISO-639 codes or track titles into compact Starfall 3-letter labels
// (`Tam`, `Tel`, `Hin`, `Mal`, `Kan`, `Eng`, `Jap`, `Kor`, `Chi`) for `[Tam+Jap-AAC 2.0]`.
func ShortLanguageLabel(isoOrName, fallbackTitle string) string {
	for _, candidate := range []string{isoOrName, fallbackTitle} {
		low := strings.ToLower(strings.TrimSpace(candidate))
		if low == "" || low == "und" || low == "unknown" {
			continue
		}
		switch {
		case low == "ta" || low == "tam" || strings.Contains(low, "tamil"):
			return "Tam"
		case low == "te" || low == "tel" || strings.Contains(low, "telugu"):
			return "Tel"
		case low == "hi" || low == "hin" || strings.Contains(low, "hindi"):
			return "Hin"
		case low == "ml" || low == "mal" || strings.Contains(low, "malayalam"):
			return "Mal"
		case low == "kn" || low == "kan" || strings.Contains(low, "kannada"):
			return "Kan"
		case low == "en" || low == "eng" || strings.Contains(low, "english"):
			return "Eng"
		case low == "ja" || low == "jpn" || low == "jap" || strings.Contains(low, "japanese"):
			return "Jap"
		case low == "ko" || low == "kor" || strings.Contains(low, "korean"):
			return "Kor"
		case low == "zh" || low == "zho" || low == "chi" || strings.Contains(low, "chinese"):
			return "Chi"
		}
	}
	return ""
}

// FullLanguageName converts ISO-639 or compact codes into full readable names (`Tamil`, `English`, `Japanese`)
// for `{audiolang}` and `{sublang}` stream metadata placeholders.
func FullLanguageName(isoOrName, fallbackTitle string) string {
	short := ShortLanguageLabel(isoOrName, fallbackTitle)
	switch short {
	case "Tam":
		return "Tamil"
	case "Tel":
		return "Telugu"
	case "Hin":
		return "Hindi"
	case "Mal":
		return "Malayalam"
	case "Kan":
		return "Kannada"
	case "Eng":
		return "English"
	case "Jap":
		return "Japanese"
	case "Kor":
		return "Korean"
	case "Chi":
		return "Chinese"
	default:
		trimmed := strings.TrimSpace(isoOrName)
		if trimmed == "" || strings.EqualFold(trimmed, "und") {
			return "Unknown"
		}
		return strings.ToUpper(trimmed[:1]) + strings.ToLower(trimmed[1:])
	}
}

func detectLanguagesInText(s string) []string {
	type langPair struct {
		re    *regexp.Regexp
		label string
	}
	checks := []langPair{
		{regexp.MustCompile(`(?i)\b(tamil|tam)\b`), "Tam"},
		{regexp.MustCompile(`(?i)\b(telugu|tel)\b`), "Tel"},
		{regexp.MustCompile(`(?i)\b(hindi|hin)\b`), "Hin"},
		{regexp.MustCompile(`(?i)\b(malayalam|mal)\b`), "Mal"},
		{regexp.MustCompile(`(?i)\b(kannada|kan)\b`), "Kan"},
		{regexp.MustCompile(`(?i)\b(english|eng)\b`), "Eng"},
		{regexp.MustCompile(`(?i)\b(japanese|jpn|jap)\b`), "Jap"},
		{regexp.MustCompile(`(?i)\bdual[\s._-]?audio\b`), "Dual"},
		{regexp.MustCompile(`(?i)\bmulti[\s._-]?audio\b`), "Multi"},
	}
	var out []string
	for _, c := range checks {
		if c.re.MatchString(s) {
			out = append(out, c.label)
		}
	}
	return out
}

func collapseSpaces(s string) string {
	return strings.Join(strings.Fields(s), " ")
}
