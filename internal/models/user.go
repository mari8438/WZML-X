// Package models defines core domain entities for Starfall-Go Debrid, including
// the Google OAuth 2.0 & Telegram User schema, unified 40+ StarfallX UserSettings,
// Telegram Dump Channel + Forum Topic binding, and anime theme preferences.
package models

import (
	"errors"
	"regexp"
	"strconv"
	"strings"
	"time"
)

const (
	// DefaultAnimeTheme is assigned to newly registered users.
	DefaultAnimeTheme = "solo-leveling"

	// DefaultPosterTemplate is the default flagship auto-poster template (1..11).
	DefaultPosterTemplate = 11

	// MinPosterTemplate is the minimum valid poster template ID.
	MinPosterTemplate = 1

	// MaxPosterTemplate is the maximum valid poster template ID.
	MaxPosterTemplate = 11
)

var (
	// telegramChannelRegex matches standard private/supergroup Telegram channel IDs
	// formatted as -100 followed by 7 to 15 digits, with optional "|topic_id" suffix.
	telegramChannelRegex = regexp.MustCompile(`^-100[0-9]{7,15}(\|[0-9]{1,10})?$`)

	// allowedThemes enumerates supported Web UI anime themes (canonical hyphenated slugs).
	allowedThemes = map[string]string{
		"solo-leveling":          "solo-leveling",
		"solo_leveling":          "solo-leveling",
		"cyberpunk-edgerun":      "cyberpunk-edgerun",
		"cyberpunk-edgerunners":  "cyberpunk-edgerun",
		"cyberpunk_edgerunners":  "cyberpunk-edgerun",
		"jujutsu-kaisen":         "jujutsu-kaisen",
		"jujutsu_kaisen":         "jujutsu-kaisen",
		"demon-slayer":           "demon-slayer",
		"demon_slayer":           "demon-slayer",
		"attack-on-titan":        "attack-on-titan",
		"attack_on_titan":        "attack-on-titan",
		"chainsaw-man":           "chainsaw-man",
		"chainsaw_man":           "chainsaw-man",
		"one-piece-gear5":        "one-piece-gear5",
		"one_piece_gear5":        "one-piece-gear5",
		"frieren":                "frieren",
		"bleach-tybw":            "bleach-tybw",
		"bleach_tybw":            "bleach-tybw",
		"starfall-oled":          "starfall-oled",
		"starfall_oled":          "starfall-oled",
	}

	// ErrInvalidTelegramChannelID is returned when `telegram_dump_chat` fails format validation.
	ErrInvalidTelegramChannelID = errors.New("telegram_dump_chat must be a valid channel ID starting with -100 (e.g. -1002345678901 or -1002345678901|12)")

	// ErrInvalidPosterTemplate is returned when `poster_template` is outside 1..11.
	ErrInvalidPosterTemplate = errors.New("poster_template must be between 1 and 11")

	// ErrInvalidAnimeTheme is returned when `anime_theme` is empty or unrecognized.
	ErrInvalidAnimeTheme = errors.New("unsupported anime_theme identifier")
)

// UserSettings captures the full 40+ StarfallX per-user workflow settings shared identically
// between Telegram (`/usetting`) and Web Studio (`/app/settings`).
type UserSettings struct {
	LeechDumpChat           string            `json:"LEECH_DUMP_CHAT"`
	AutoRename              bool              `json:"AUTORENAME"`
	LRemnameAuto            string            `json:"lremname_auto"`
	NameSwap                string            `json:"NAME_SWAP"`
	LRemnameRegex           string            `json:"lremname_regex"`
	LeechPrefix             string            `json:"LEECH_PREFIX"`
	LeechSuffix             string            `json:"LEECH_SUFFIX"`
	LeechFont               string            `json:"LEECH_FONT"`
	AsDocument              bool              `json:"AS_DOCUMENT"`
	MediaGroup              bool              `json:"MEDIA_GROUP"`
	SequentialLeech         bool              `json:"SEQUENTIAL_LEECH"`
	StopDuplicate           bool              `json:"STOP_DUPLICATE"`
	LeechCompleteMsg        bool              `json:"LEECH_COMPLETE_MSG"`
	AutoLeech               bool              `json:"AUTO_LEECH"`
	LeechSplitSize          int64             `json:"LEECH_SPLIT_SIZE"`
	ThumbnailMode           string            `json:"THUMBNAIL_MODE"`
	AutoThumbnail           bool              `json:"AUTO_THUMBNAIL"`
	ForceAnimeThumbnail     bool              `json:"FORCE_ANIME_THUMBNAIL"`
	AutoProcess             bool              `json:"AUTO_PROCESS"`
	AutoUnzip               bool              `json:"AUTO_UNZIP"`
	AutoMerge               bool              `json:"AUTO_MERGE"`
	AutoVT                  bool              `json:"AUTO_VT"`
	AutoOrder               bool              `json:"AUTO_ORDER"`
	AutoRemoveStreams       bool              `json:"AUTO_REMOVE_STREAMS"`
	AutoKeepAudioLangs      string            `json:"AUTO_KEEP_AUDIO_LANGS"`
	AutoKeepSubtitleLangs   string            `json:"AUTO_KEEP_SUBTITLE_LANGS"`
	AutoAudioOrder          string            `json:"AUTO_AUDIO_ORDER"`
	AutoSubtitleOrder       string            `json:"AUTO_SUBTITLE_ORDER"`
	AutoIntroSubtitle       bool              `json:"AUTO_INTRO_SUBTITLE"`
	IntroSubtitleText       string            `json:"INTRO_SUBTITLE_TEXT"`
	IntroSubtitleDuration   int               `json:"INTRO_SUBTITLE_DURATION"`
	IntroSubtitleRanges     string            `json:"INTRO_SUBTITLE_RANGES"`
	AutoMetadata            bool              `json:"AUTO_METADATA"`
	MetadataTitle           string            `json:"METADATA_TITLE"`
	MetadataAudioTitle      string            `json:"METADATA_AUDIO_TITLE"`
	MetadataSubtitleTitle   string            `json:"METADATA_SUBTITLE_TITLE"`
	Metadata                map[string]string `json:"METADATA,omitempty"`
	VideoMetadata           map[string]string `json:"VIDEO_METADATA,omitempty"`
	AudioMetadata           map[string]string `json:"AUDIO_METADATA,omitempty"`
	SubtitleMetadata        map[string]string `json:"SUBTITLE_METADATA,omitempty"`
	AutoPosterEnabled       bool              `json:"AUTO_POSTER_ENABLED"`
	AutoPosterUseAsThumb    bool              `json:"AUTO_POSTER_USE_AS_THUMBNAIL"`
	AutoPosterSendMessage   bool              `json:"AUTO_POSTER_SEND_MESSAGE"`
	PostTemplateID          int               `json:"POST_TEMPLATE_ID"`
	PostBrandName           string            `json:"POST_BRAND_NAME"`
	PostLogo                string            `json:"POST_LOGO"`
	PostAnimeCaption        string            `json:"POST_ANIME_CAPTION"`
	PostTVCaption           string            `json:"POST_TV_CAPTION"`
	PostMovieCaption        string            `json:"POST_MOVIE_CAPTION"`
	UserUploadBotToken      string            `json:"USER_UPLOAD_BOT_TOKEN"`
	UserCookieFile          string            `json:"USER_COOKIE_FILE"`
	TokenPickle             string            `json:"TOKEN_PICKLE"`
	DefaultUpload           string            `json:"DEFAULT_UPLOAD"`
	UphosterService         string            `json:"UPHOSTER_SERVICE"`
	IndexURL                string            `json:"INDEX_URL"`
	UserTokens              bool              `json:"USER_TOKENS"`
	UserTransmission        bool              `json:"USER_TRANSMISSION"`
	HybridLeech             bool              `json:"HYBRID_LEECH"`
	TaskMode                string            `json:"TASK_MODE"`
	AnimeTheme              string            `json:"ANIME_THEME"`
	CustomThumbnailFilePath string            `json:"CUSTOM_THUMBNAIL_PATH,omitempty"`
}

// DefaultUserSettings returns the standard Anime Starfall workflow defaults.
func DefaultUserSettings() UserSettings {
	return UserSettings{
		AutoRename:            false,
		LRemnameAuto:          "🄰🅂- [S{season}E{episode}] {name} {year} {resolution} {bit} {ott} {quality} {codec} [{languages}-{audio_codec} {audio_channels}] {shortsub} ~ [@Anime_Starfall🥰]",
		LeechFont:             "b",
		AsDocument:            false,
		SequentialLeech:       true,
		StopDuplicate:         true,
		LeechSplitSize:        2097152000,
		ThumbnailMode:         "automatic",
		AutoThumbnail:         true,
		AutoProcess:           false,
		AutoUnzip:             true,
		AutoMerge:             true,
		IntroSubtitleText:     "[@Anime_Starfall🥰]",
		IntroSubtitleDuration: 10,
		MetadataTitle:         "[@Anime_Starfall🥰]",
		MetadataAudioTitle:    "[@Anime_Starfall🥰] - {audiolang}",
		MetadataSubtitleTitle: "[@Anime_Starfall🥰] - {sublang}",
		AutoPosterEnabled:     true,
		AutoPosterUseAsThumb:  true,
		AutoPosterSendMessage: true,
		PostTemplateID:        11,
		PostBrandName:         "Anime Starfall",
		PostAnimeCaption:      "<b>「 {title} - {year} 」</b>\n━━━━━━━━━━━━━━━━━━\n╔════◇═══════════◇════\n║ Season ➤ {season} \n║ AniList ➤ {rating} Rating \n║ Genres ➤ {genres} \n║ Quality ➤ {resolution} {bit} {codec}\n║ Audio ➤ {languages} {audio_codec} {audio_channels} ~ {shortsub} \n╚════◇═══════════◇════\n <blockquote expandable>Synopsis :\n   {plot}</blockquote>\n\nDropped By ➤ [@Anime_Starfall🥰]",
		PostTVCaption:         "<b>「 {title} - {year} 」</b>\n━━━━━━━━━━━━━━━━━━\n╔════◇═══════════◇════\n║ Season ➤ {season} ( {episodes} Episodes )\n║ IMDb ➤ {rating} Rating \n║ Genres ➤ {genres} \n║ Quality ➤ {resolution} {bit} {codec}\n║ Audio ➤ {languages} {audio_codec} {audio_channels} ~ {shortsub} \n╚════◇═══════════◇════\n\n <blockquote expandable>Synopsis :\n   {plot}</blockquote>\n\nDropped By ➤ [@Anime_Starfall🥰]",
		PostMovieCaption:      "<b>「 {title} - {year} 」</b>\n━━━━━━━━━━━━━━━━━━\n╔════◇═══════════◇════\n║ IMDb ➤ {rating} Rating \n║ Genres ➤ {genres} \n║ Quality ➤ {resolution} {bit} {codec}\n║ Audio ➤ {languages} {audio_codec} {audio_channels} ~ {shortsub} \n╚════◇═══════════◇════\n <blockquote expandable>Synopsis :\n   {plot}</blockquote>\n\nDropped By ➤ [@Anime_Starfall🥰]",
		TaskMode:              "web",
		AnimeTheme:            DefaultAnimeTheme,
	}
}

// User represents an authenticated Google OAuth 2.0 or Telegram user in Starfall-Go Debrid
// along with their linked Telegram Cloud delivery channel, visual theme, and full UserSettings.
type User struct {
	ID               string       `json:"id"`                 // Google Sub ID or "tg:<telegram_id>"
	TelegramID       int64        `json:"telegram_id"`        // Linked Telegram numeric user ID (0 if unlinked)
	Email            string       `json:"email"`              // user@gmail.com or synthetic tg email
	Name             string       `json:"name"`               // Display name
	AvatarURL        string       `json:"avatar_url"`         // Profile picture URL
	TelegramDumpChat string       `json:"telegram_dump_chat"` // -100xxxxxxxxxx or -100xxxxxxxxxx|topic_id
	AnimeTheme       string       `json:"anime_theme"`        // Default: "solo-leveling"
	PosterTemplate   int          `json:"poster_template"`    // Default: 11 (Templates 1-11)
	Settings         UserSettings `json:"settings"`           // Full 40+ StarfallX settings
	CreatedAt        time.Time    `json:"created_at"`
}

// NewGoogleUser initializes a User record from verified Google OAuth2 claims.
func NewGoogleUser(googleSubID, email, name, avatarURL string) *User {
	settings := DefaultUserSettings()
	return &User{
		ID:               strings.TrimSpace(googleSubID),
		Email:            strings.ToLower(strings.TrimSpace(email)),
		Name:             strings.TrimSpace(name),
		AvatarURL:        strings.TrimSpace(avatarURL),
		TelegramDumpChat: "",
		AnimeTheme:       DefaultAnimeTheme,
		PosterTemplate:   DefaultPosterTemplate,
		Settings:         settings,
		CreatedAt:        time.Now().UTC(),
	}
}

// HasTelegramDumpLinked reports whether the user has configured a valid Telegram dump channel.
func (u *User) HasTelegramDumpLinked() bool {
	if u == nil {
		return false
	}
	dump := strings.TrimSpace(u.TelegramDumpChat)
	if dump == "" {
		dump = strings.TrimSpace(u.Settings.LeechDumpChat)
	}
	return dump != "" && ValidateTelegramDumpChat(dump) == nil
}

// EffectiveDumpChat returns the active dump channel (with optional "|topic_id" suffix).
func (u *User) EffectiveDumpChat() string {
	if u == nil {
		return ""
	}
	if strings.TrimSpace(u.Settings.LeechDumpChat) != "" {
		return strings.TrimSpace(u.Settings.LeechDumpChat)
	}
	return strings.TrimSpace(u.TelegramDumpChat)
}

// ParseDumpChatTarget parses "-1003887117402" or "-1003887117402|15" into numeric chatID and optional forum threadID.
func ParseDumpChatTarget(raw string) (chatID int64, threadID int, ok bool) {
	trimmed := strings.TrimSpace(raw)
	if trimmed == "" {
		return 0, 0, false
	}
	chatPart := trimmed
	if idx := strings.Index(trimmed, "|"); idx != -1 {
		chatPart = strings.TrimSpace(trimmed[:idx])
		topicPart := strings.TrimSpace(trimmed[idx+1:])
		if topicPart != "" {
			t, err := strconv.Atoi(topicPart)
			if err != nil || t <= 0 {
				return 0, 0, false
			}
			threadID = t
		}
	}
	cid, err := strconv.ParseInt(chatPart, 10, 64)
	if err != nil || cid == 0 {
		return 0, 0, false
	}
	return cid, threadID, true
}

// ValidateTelegramDumpChat checks that a Telegram channel ID is either empty (unlinked)
// or matches the `-100xxxxxxxxxx` (or `-100xxxxxxxxxx|topic_id`) format.
func ValidateTelegramDumpChat(chatID string) error {
	trimmed := strings.TrimSpace(chatID)
	if trimmed == "" {
		return nil
	}
	if !telegramChannelRegex.MatchString(trimmed) {
		return ErrInvalidTelegramChannelID
	}
	return nil
}

// ValidatePosterTemplate ensures the template ID falls within the supported 1..11 range.
func ValidatePosterTemplate(templateID int) error {
	if templateID < MinPosterTemplate || templateID > MaxPosterTemplate {
		return ErrInvalidPosterTemplate
	}
	return nil
}

// NormalizeAnimeTheme maps underscore or hyphenated theme names to canonical slugs.
func NormalizeAnimeTheme(theme string) (string, error) {
	normalized := strings.ToLower(strings.TrimSpace(theme))
	if normalized == "" {
		return "", ErrInvalidAnimeTheme
	}
	canonical, ok := allowedThemes[normalized]
	if !ok {
		return "", ErrInvalidAnimeTheme
	}
	return canonical, nil
}

// ValidateAnimeTheme checks whether the provided theme slug is valid.
func ValidateAnimeTheme(theme string) error {
	_, err := NormalizeAnimeTheme(theme)
	return err
}

// UpdateTelegramDumpRequest is the JSON payload for `POST /api/user/telegram-dump`.
type UpdateTelegramDumpRequest struct {
	TelegramDumpChat string `json:"telegram_dump_chat"`
}

// UpdateThemePreferencesRequest is the JSON payload for updating UI theme and poster template.
type UpdateThemePreferencesRequest struct {
	AnimeTheme     string `json:"anime_theme"`
	PosterTemplate int    `json:"poster_template"`
}

// UserProfileResponse wraps the authenticated user's profile and linking status for `GET /api/user/profile`.
type UserProfileResponse struct {
	User                 *User `json:"user"`
	TelegramLinked       bool  `json:"telegram_linked"`
	ReadyForWebLeechTask bool  `json:"ready_for_web_leech_task"`
}
