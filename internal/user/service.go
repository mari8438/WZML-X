// Package user provides domain methods (`GetUser`, `GetOrCreateByTelegramID`, `UpdateUserSettings`,
// `UpdateTelegramDump`, `UpdateThemePreferences`, `GetBotSettings`) and HTTP API handlers
// for linking a user's Google or Telegram account to their private Telegram delivery channel.
package user

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"strings"

	"github.com/starfall-org/starfall-go/internal/auth"
	"github.com/starfall-org/starfall-go/internal/models"
)

// Service encapsulates business validation and persistence for Starfall-Go users.
type Service struct {
	store  *Store
	logger *slog.Logger
}

// NewService constructs a User Service backed by the embedded SQLite Store.
func NewService(store *Store, logger *slog.Logger) *Service {
	if logger == nil {
		logger = slog.Default()
	}
	return &Service{
		store:  store,
		logger: logger,
	}
}

// SeedFromJSONDir imports `configs/user_settings_*.json` snapshots into SQLite on boot.
func (s *Service) SeedFromJSONDir(ctx context.Context, dir string) (int, error) {
	n, err := s.store.SeedFromJSONDir(ctx, dir)
	if err != nil {
		return n, err
	}
	if n > 0 {
		s.logger.Info("seeded starfall user settings snapshots", slog.Int("count", n), slog.String("dir", dir))
	}
	return n, nil
}

// UpsertGoogleUser creates or updates a user upon Google OAuth 2.0 callback completion.
func (s *Service) UpsertGoogleUser(
	ctx context.Context,
	id, email, name, avatarURL string,
) (*models.User, error) {
	return s.store.UpsertGoogleUser(ctx, id, email, name, avatarURL)
}

// GetOrCreateByTelegramID fetches or creates a Telegram-linked user (`tg:<telegramID>`).
func (s *Service) GetOrCreateByTelegramID(ctx context.Context, telegramID int64, displayName string) (*models.User, error) {
	return s.store.GetOrCreateByTelegramID(ctx, telegramID, displayName)
}

// GetByTelegramID retrieves a user record by their numeric Telegram ID.
func (s *Service) GetByTelegramID(ctx context.Context, telegramID int64) (*models.User, error) {
	return s.store.GetByTelegramID(ctx, telegramID)
}

// GetUser retrieves a user's profile and settings by user ID (`id`).
func (s *Service) GetUser(ctx context.Context, userID string) (*models.User, error) {
	if strings.TrimSpace(userID) == "" {
		return nil, ErrUserNotFound
	}
	return s.store.GetByID(ctx, userID)
}

// UpdateUserSettings validates and persists the full 40+ StarfallX settings struct for `userID`.
func (s *Service) UpdateUserSettings(
	ctx context.Context,
	userID string,
	settings models.UserSettings,
) (*models.User, error) {
	cleanedDump := strings.TrimSpace(settings.LeechDumpChat)
	if err := models.ValidateTelegramDumpChat(cleanedDump); err != nil {
		return nil, err
	}
	settings.LeechDumpChat = cleanedDump
	if settings.PostTemplateID != 0 {
		if err := models.ValidatePosterTemplate(settings.PostTemplateID); err != nil {
			return nil, err
		}
	}
	if settings.AnimeTheme != "" {
		canonical, err := models.NormalizeAnimeTheme(settings.AnimeTheme)
		if err != nil {
			return nil, err
		}
		settings.AnimeTheme = canonical
	}
	updated, err := s.store.UpdateUserSettings(ctx, userID, settings)
	if err != nil {
		return nil, err
	}
	s.logger.Info("updated user settings",
		slog.String("user_id", updated.ID),
		slog.String("dump_chat", updated.TelegramDumpChat),
		slog.Int("poster_template", updated.PosterTemplate),
	)
	return updated, nil
}

// UpdateTelegramDump validates and saves the user's private Telegram Dump Channel ID
// (e.g., `-1002345678901` or `-1002345678901|15`).
func (s *Service) UpdateTelegramDump(
	ctx context.Context,
	userID string,
	telegramDumpChat string,
) (*models.User, error) {
	cleaned := strings.TrimSpace(telegramDumpChat)
	if err := models.ValidateTelegramDumpChat(cleaned); err != nil {
		return nil, err
	}
	user, err := s.store.UpdateTelegramDump(ctx, userID, cleaned)
	if err != nil {
		return nil, err
	}

	s.logger.Info("updated user telegram dump channel",
		slog.String("user_id", user.ID),
		slog.String("telegram_dump_chat", user.TelegramDumpChat),
	)
	return user, nil
}

// UpdateThemePreferences validates and updates the user's `anime_theme` and `poster_template` (1..11).
func (s *Service) UpdateThemePreferences(
	ctx context.Context,
	userID string,
	animeTheme string,
	posterTemplate int,
) (*models.User, error) {
	theme := strings.ToLower(strings.TrimSpace(animeTheme))
	if theme == "" {
		theme = models.DefaultAnimeTheme
	}
	canonicalTheme, err := models.NormalizeAnimeTheme(theme)
	if err != nil {
		return nil, err
	}
	if posterTemplate == 0 {
		posterTemplate = models.DefaultPosterTemplate
	}
	if err := models.ValidatePosterTemplate(posterTemplate); err != nil {
		return nil, err
	}

	user, err := s.store.UpdateThemePreferences(ctx, userID, canonicalTheme, posterTemplate)
	if err != nil {
		return nil, err
	}

	s.logger.Info("updated user theme preferences",
		slog.String("user_id", user.ID),
		slog.String("anime_theme", user.AnimeTheme),
		slog.Int("poster_template", user.PosterTemplate),
	)
	return user, nil
}

// GetBotSettings returns all persisted global bot overrides (`bot_settings` table).
func (s *Service) GetBotSettings(ctx context.Context) (map[string]string, error) {
	return s.store.GetBotSettings(ctx)
}

// UpdateBotSettings persists global bot setting overrides (`bot_settings` table).
func (s *Service) UpdateBotSettings(ctx context.Context, kv map[string]string) error {
	return s.store.UpdateBotSettings(ctx, kv)
}

// RegisterRoutes mounts the protected `/api/user/*` endpoints onto `mux` behind `authMiddleware`.
func (s *Service) RegisterRoutes(mux *http.ServeMux, authMiddleware *auth.Middleware) {
	mux.Handle("GET /api/user/profile", authMiddleware.RequireAuthFunc(s.HandleGetProfile))
	mux.Handle("POST /api/user/telegram-dump", authMiddleware.RequireAuthFunc(s.HandleUpdateTelegramDump))
	mux.Handle("POST /api/user/preferences", authMiddleware.RequireAuthFunc(s.HandleUpdateThemePreferences))
}

// HandleGetProfile (`GET /api/user/profile`) returns the authenticated user's profile,
// linked `telegram_dump_chat` channel ID, and web-leech readiness state.
func (s *Service) HandleGetProfile(w http.ResponseWriter, r *http.Request) {
	u, ok := auth.UserFromContext(r.Context())
	if !ok {
		userID, hasID := auth.UserIDFromContext(r.Context())
		if !hasID {
			writeJSON(w, http.StatusUnauthorized, map[string]any{"ok": false, "error": "unauthorized"})
			return
		}
		var err error
		u, err = s.GetUser(r.Context(), userID)
		if err != nil {
			writeJSON(w, http.StatusNotFound, map[string]any{"ok": false, "error": "user profile not found"})
			return
		}
	}

	writeJSON(w, http.StatusOK, models.UserProfileResponse{
		User:                 u,
		TelegramLinked:       u.HasTelegramDumpLinked(),
		ReadyForWebLeechTask: u.HasTelegramDumpLinked(),
	})
}

// HandleUpdateTelegramDump (`POST /api/user/telegram-dump`) validates and persists the
// authenticated user's `telegram_dump_chat` channel ID (e.g., `-1002345678901`).
func (s *Service) HandleUpdateTelegramDump(w http.ResponseWriter, r *http.Request) {
	userID, ok := auth.UserIDFromContext(r.Context())
	if !ok {
		writeJSON(w, http.StatusUnauthorized, map[string]any{"ok": false, "error": "unauthorized"})
		return
	}

	var req models.UpdateTelegramDumpRequest
	if strings.Contains(strings.ToLower(r.Header.Get("Content-Type")), "application/x-www-form-urlencoded") {
		if err := r.ParseForm(); err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]any{"ok": false, "error": "invalid form payload"})
			return
		}
		req.TelegramDumpChat = r.FormValue("telegram_dump_chat")
	} else {
		dec := json.NewDecoder(io.LimitReader(r.Body, 8192))
		dec.DisallowUnknownFields()
		if err := dec.Decode(&req); err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]any{"ok": false, "error": "invalid JSON body"})
			return
		}
	}

	updatedUser, err := s.UpdateTelegramDump(r.Context(), userID, req.TelegramDumpChat)
	if err != nil {
		status := http.StatusInternalServerError
		if errors.Is(err, models.ErrInvalidTelegramChannelID) {
			status = http.StatusUnprocessableEntity
		} else if errors.Is(err, ErrUserNotFound) {
			status = http.StatusNotFound
		}
		writeJSON(w, status, map[string]any{"ok": false, "error": err.Error()})
		return
	}

	writeJSON(w, http.StatusOK, map[string]any{
		"ok":                      true,
		"message":                 "telegram dump channel updated",
		"user":                    updatedUser,
		"telegram_linked":         updatedUser.HasTelegramDumpLinked(),
		"ready_for_web_leech_task": updatedUser.HasTelegramDumpLinked(),
	})
}

// HandleUpdateThemePreferences (`POST /api/user/preferences`) updates the user's
// `anime_theme` and `poster_template` (1..11) settings.
func (s *Service) HandleUpdateThemePreferences(w http.ResponseWriter, r *http.Request) {
	userID, ok := auth.UserIDFromContext(r.Context())
	if !ok {
		writeJSON(w, http.StatusUnauthorized, map[string]any{"ok": false, "error": "unauthorized"})
		return
	}

	var req models.UpdateThemePreferencesRequest
	dec := json.NewDecoder(io.LimitReader(r.Body, 8192))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"ok": false, "error": "invalid JSON body"})
		return
	}

	updatedUser, err := s.UpdateThemePreferences(r.Context(), userID, req.AnimeTheme, req.PosterTemplate)
	if err != nil {
		status := http.StatusInternalServerError
		if errors.Is(err, models.ErrInvalidAnimeTheme) || errors.Is(err, models.ErrInvalidPosterTemplate) {
			status = http.StatusUnprocessableEntity
		} else if errors.Is(err, ErrUserNotFound) {
			status = http.StatusNotFound
		}
		writeJSON(w, status, map[string]any{"ok": false, "error": err.Error()})
		return
	}

	writeJSON(w, http.StatusOK, map[string]any{
		"ok":   true,
		"user": updatedUser,
	})
}

func writeJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(payload)
}
