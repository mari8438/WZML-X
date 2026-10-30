// Package models defines core domain entities for Starfall-Go Debrid, including
// the Google OAuth 2.0 User schema, Telegram Dump Channel binding, and theme preferences.
package models

import (
	"errors"
	"regexp"
	"strings"
	"time"
)

const (
	// DefaultAnimeTheme is assigned to newly registered Google OAuth2 users.
	DefaultAnimeTheme = "solo-leveling"

	// DefaultPosterTemplate is the default flagship auto-poster template (1..11).
	DefaultPosterTemplate = 1

	// MinPosterTemplate is the minimum valid poster template ID.
	MinPosterTemplate = 1

	// MaxPosterTemplate is the maximum valid poster template ID.
	MaxPosterTemplate = 11
)

var (
	// telegramChannelRegex matches standard private/supergroup Telegram channel IDs
	// formatted as -100 followed by 7 to 15 digits (e.g. -1002345678901).
	telegramChannelRegex = regexp.MustCompile(`^-100[0-9]{7,15}$`)

	// allowedThemes enumerates supported Web UI anime themes.
	allowedThemes = map[string]struct{}{
		"solo-leveling":     {},
		"cyberpunk-edgerun": {},
		"jujutsu-kaisen":    {},
		"demon-slayer":      {},
		"attack-on-titan":   {},
		"chainsaw-man":      {},
		"one-piece-gear5":   {},
		"frieren":           {},
		"bleach-tybw":       {},
		"starfall-oled":     {},
	}

	// ErrInvalidTelegramChannelID is returned when `telegram_dump_chat` fails format validation.
	ErrInvalidTelegramChannelID = errors.New("telegram_dump_chat must be a valid channel ID starting with -100 (e.g. -1002345678901)")

	// ErrInvalidPosterTemplate is returned when `poster_template` is outside 1..11.
	ErrInvalidPosterTemplate = errors.New("poster_template must be between 1 and 11")

	// ErrInvalidAnimeTheme is returned when `anime_theme` is empty or unrecognized.
	ErrInvalidAnimeTheme = errors.New("unsupported anime_theme identifier")
)

// User represents an authenticated Google OAuth 2.0 user in Starfall-Go Debrid
// and their linked Telegram Cloud delivery channel and visual preferences.
type User struct {
	ID               string    `json:"id"`                 // Google Sub ID
	Email            string    `json:"email"`              // user@gmail.com
	Name             string    `json:"name"`               // Display name
	AvatarURL        string    `json:"avatar_url"`         // Google profile picture
	TelegramDumpChat string    `json:"telegram_dump_chat"` // -100xxxxxxxxxx
	AnimeTheme       string    `json:"anime_theme"`        // Default: "solo-leveling"
	PosterTemplate   int       `json:"poster_template"`    // Default: 1 (Templates 1-11)
	CreatedAt        time.Time `json:"created_at"`
}

// NewGoogleUser initializes a User record from verified Google OAuth2 claims
// with Starfall-Go default theme ("solo-leveling") and poster template (1).
func NewGoogleUser(googleSubID, email, name, avatarURL string) *User {
	return &User{
		ID:               strings.TrimSpace(googleSubID),
		Email:            strings.ToLower(strings.TrimSpace(email)),
		Name:             strings.TrimSpace(name),
		AvatarURL:        strings.TrimSpace(avatarURL),
		TelegramDumpChat: "",
		AnimeTheme:       DefaultAnimeTheme,
		PosterTemplate:   DefaultPosterTemplate,
		CreatedAt:        time.Now().UTC(),
	}
}

// HasTelegramDumpLinked reports whether the user has configured a valid Telegram dump channel.
func (u *User) HasTelegramDumpLinked() bool {
	return ValidateTelegramDumpChat(u.TelegramDumpChat) == nil && u.TelegramDumpChat != ""
}

// ValidateTelegramDumpChat checks that a Telegram channel ID is either empty (unlinked)
// or matches the `-100xxxxxxxxxx` supergroup/channel ID format.
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

// ValidateAnimeTheme checks whether the provided theme slug is valid.
func ValidateAnimeTheme(theme string) error {
	normalized := strings.ToLower(strings.TrimSpace(theme))
	if normalized == "" {
		return ErrInvalidAnimeTheme
	}
	if _, ok := allowedThemes[normalized]; !ok {
		return ErrInvalidAnimeTheme
	}
	return nil
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
