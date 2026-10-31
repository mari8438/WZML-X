// Package user implements the pure-Go embedded SQLite persistence layer (`modernc.org/sqlite`),
// automatic `configs/user_settings_*.json` seeding, unified 40+ StarfallX UserSettings storage,
// and global BotSettings persistence for Starfall-Go Debrid.
package user

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/starfall-org/starfall-go/internal/models"
	_ "modernc.org/sqlite" // Pure-Go SQLite driver (CGO_ENABLED=0 compatible)
)

var (
	// ErrUserNotFound is returned when a user ID, Telegram ID, or email does not exist in SQLite.
	ErrUserNotFound = errors.New("user not found")
)

// Store manages persistent user records and global bot overrides in an embedded WAL-mode SQLite database.
type Store struct {
	db *sql.DB
}

// NewStore opens (or creates) a pure-Go SQLite database at `dbPath`, applies memory-safe
// WAL and page-cache PRAGMAs for the 2 vCPU / 4 GB RAM VPS, and runs schema migrations.
func NewStore(dbPath string) (*Store, error) {
	dbPath = strings.TrimSpace(dbPath)
	if dbPath == "" {
		dbPath = "data/starfall_users.db"
	}
	if dbPath != ":memory:" {
		if dir := filepath.Dir(dbPath); dir != "" && dir != "." {
			if err := os.MkdirAll(dir, 0o750); err != nil {
				return nil, fmt.Errorf("creating sqlite directory %s: %w", dir, err)
			}
		}
	}

	dsn := fmt.Sprintf(
		"file:%s?_pragma=journal_mode(WAL)&_pragma=synchronous(NORMAL)&_pragma=busy_timeout(5000)&_pragma=foreign_keys(ON)&_pragma=cache_size(-8192)",
		dbPath,
	)
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("opening sqlite database: %w", err)
	}

	// Keep connection pool tight for embedded WAL mode on a 2 vCPU / 4 GB RAM VPS.
	db.SetMaxOpenConns(4)
	db.SetMaxIdleConns(2)
	db.SetConnMaxLifetime(30 * time.Minute)

	store := &Store{db: db}
	if err := store.migrate(context.Background()); err != nil {
		_ = db.Close()
		return nil, fmt.Errorf("migrating sqlite schema: %w", err)
	}

	return store, nil
}

// migrate initializes the `users` and `bot_settings` tables and upgrades older schemas idempotently.
func (s *Store) migrate(ctx context.Context) error {
	const schemaSQL = `
	CREATE TABLE IF NOT EXISTS users (
		id                 TEXT PRIMARY KEY,
		telegram_id        INTEGER NOT NULL DEFAULT 0,
		email              TEXT NOT NULL UNIQUE,
		name               TEXT NOT NULL,
		avatar_url         TEXT NOT NULL DEFAULT '',
		telegram_dump_chat TEXT NOT NULL DEFAULT '',
		anime_theme        TEXT NOT NULL DEFAULT 'solo-leveling',
		poster_template    INTEGER NOT NULL DEFAULT 11,
		settings_json      TEXT NOT NULL DEFAULT '{}',
		created_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
		updated_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
	);
	CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);

	CREATE TABLE IF NOT EXISTS bot_settings (
		key        TEXT PRIMARY KEY,
		value      TEXT NOT NULL DEFAULT '',
		updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
	);
	`
	if _, err := s.db.ExecContext(ctx, schemaSQL); err != nil {
		return err
	}

	// Upgrade pre-existing baseline databases seamlessly if columns were added after initial deploy.
	_, _ = s.db.ExecContext(ctx, `ALTER TABLE users ADD COLUMN telegram_id INTEGER NOT NULL DEFAULT 0;`)
	_, _ = s.db.ExecContext(ctx, `ALTER TABLE users ADD COLUMN settings_json TEXT NOT NULL DEFAULT '{}';`)
	_, _ = s.db.ExecContext(ctx, `CREATE INDEX IF NOT EXISTS idx_users_telegram_id ON users(telegram_id);`)
	return nil
}

type seedFilePayload struct {
	UserID   int64           `json:"user_id"`
	Settings json.RawMessage `json:"settings"`
}

// SeedFromJSONDir imports `user_settings_*.json` snapshots from `dir` (e.g. `configs/`) into SQLite
// on first boot without overwriting settings modified later by the user.
func (s *Store) SeedFromJSONDir(ctx context.Context, dir string) (int, error) {
	dir = strings.TrimSpace(dir)
	if dir == "" {
		return 0, nil
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		if os.IsNotExist(err) {
			return 0, nil
		}
		return 0, fmt.Errorf("reading seed directory %s: %w", dir, err)
	}

	imported := 0
	for _, entry := range entries {
		if entry.IsDir() {
			continue
		}
		name := entry.Name()
		if !strings.HasPrefix(name, "user_settings_") || !strings.HasSuffix(name, ".json") {
			continue
		}
		rawBytes, err := os.ReadFile(filepath.Join(dir, name))
		if err != nil {
			continue
		}

		var payload seedFilePayload
		if err := json.Unmarshal(rawBytes, &payload); err != nil || payload.UserID <= 0 {
			continue
		}

		settings := models.DefaultUserSettings()
		if len(payload.Settings) > 0 {
			if err := json.Unmarshal(payload.Settings, &settings); err != nil {
				continue
			}
		}
		// Sync derived convenience fields if present in METADATA maps.
		if settings.AudioMetadata != nil && settings.MetadataAudioTitle == "" {
			settings.MetadataAudioTitle = settings.AudioMetadata["title"]
		}
		if settings.SubtitleMetadata != nil && settings.MetadataSubtitleTitle == "" {
			settings.MetadataSubtitleTitle = settings.SubtitleMetadata["title"]
		}
		if settings.Metadata != nil && settings.MetadataTitle == "" {
			settings.MetadataTitle = settings.Metadata["title"]
		}
		if settings.PostTemplateID < models.MinPosterTemplate || settings.PostTemplateID > models.MaxPosterTemplate {
			settings.PostTemplateID = models.DefaultPosterTemplate
		}
		if canonicalTheme, err := models.NormalizeAnimeTheme(settings.AnimeTheme); err == nil {
			settings.AnimeTheme = canonicalTheme
		} else {
			settings.AnimeTheme = models.DefaultAnimeTheme
		}

		userID := fmt.Sprintf("tg:%d", payload.UserID)
		existing, err := s.GetByTelegramID(ctx, payload.UserID)
		if err == nil && existing != nil {
			// Already seeded; only hydrate if settings_json was empty defaults.
			if existing.TelegramDumpChat == "" && settings.LeechDumpChat != "" {
				_, _ = s.UpdateUserSettings(ctx, existing.ID, settings)
				imported++
			}
			continue
		}

		settingsBytes, err := json.Marshal(settings)
		if err != nil {
			continue
		}
		email := fmt.Sprintf("tg_%d@starfall.local", payload.UserID)
		displayName := settings.PostBrandName
		if strings.TrimSpace(displayName) == "" {
			displayName = fmt.Sprintf("Telegram User %d", payload.UserID)
		}
		now := time.Now().UTC()

		const insertSeedSQL = `
		INSERT INTO users (
			id, telegram_id, email, name, avatar_url, telegram_dump_chat,
			anime_theme, poster_template, settings_json, created_at, updated_at
		) VALUES (
			?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?
		)
		ON CONFLICT(id) DO NOTHING;
		`
		res, err := s.db.ExecContext(
			ctx,
			insertSeedSQL,
			userID,
			payload.UserID,
			email,
			displayName,
			strings.TrimSpace(settings.LeechDumpChat),
			settings.AnimeTheme,
			settings.PostTemplateID,
			string(settingsBytes),
			now,
			now,
		)
		if err == nil {
			if n, _ := res.RowsAffected(); n > 0 {
				imported++
			}
		}
	}
	return imported, nil
}

// UpsertGoogleUser inserts a newly authenticated Google OAuth2 user or updates their
// email, display name, and avatar URL while preserving existing settings.
func (s *Store) UpsertGoogleUser(
	ctx context.Context,
	id, email, name, avatarURL string,
) (*models.User, error) {
	id = strings.TrimSpace(id)
	email = strings.ToLower(strings.TrimSpace(email))
	name = strings.TrimSpace(name)
	avatarURL = strings.TrimSpace(avatarURL)

	if id == "" || email == "" {
		return nil, errors.New("google sub id and email cannot be empty")
	}
	if name == "" {
		name = email
	}

	defaults := models.DefaultUserSettings()
	defaultJSON, _ := json.Marshal(defaults)

	now := time.Now().UTC()
	const upsertSQL = `
	INSERT INTO users (
		id, telegram_id, email, name, avatar_url, telegram_dump_chat,
		anime_theme, poster_template, settings_json, created_at, updated_at
	) VALUES (
		?, 0, ?, ?, ?, '', ?, ?, ?, ?, ?
	)
	ON CONFLICT(id) DO UPDATE SET
		email      = excluded.email,
		name       = excluded.name,
		avatar_url = excluded.avatar_url,
		updated_at = excluded.updated_at;
	`

	_, err := s.db.ExecContext(
		ctx,
		upsertSQL,
		id,
		email,
		name,
		avatarURL,
		models.DefaultAnimeTheme,
		models.DefaultPosterTemplate,
		string(defaultJSON),
		now,
		now,
	)
	if err != nil {
		return nil, fmt.Errorf("upserting google user %s: %w", id, err)
	}

	return s.GetByID(ctx, id)
}

// GetOrCreateByTelegramID retrieves a user linked to `telegramID` or creates `tg:<telegramID>`.
func (s *Store) GetOrCreateByTelegramID(ctx context.Context, telegramID int64, displayName string) (*models.User, error) {
	if telegramID <= 0 {
		return nil, errors.New("invalid telegram user id")
	}
	if u, err := s.GetByTelegramID(ctx, telegramID); err == nil {
		return u, nil
	}

	userID := fmt.Sprintf("tg:%d", telegramID)
	email := fmt.Sprintf("tg_%d@starfall.local", telegramID)
	name := strings.TrimSpace(displayName)
	if name == "" {
		name = fmt.Sprintf("Starfall User %d", telegramID)
	}
	defaults := models.DefaultUserSettings()
	settingsBytes, _ := json.Marshal(defaults)
	now := time.Now().UTC()

	const insertSQL = `
	INSERT INTO users (
		id, telegram_id, email, name, avatar_url, telegram_dump_chat,
		anime_theme, poster_template, settings_json, created_at, updated_at
	) VALUES (
		?, ?, ?, ?, '', '', ?, ?, ?, ?, ?
	)
	ON CONFLICT(id) DO UPDATE SET
		telegram_id = excluded.telegram_id,
		updated_at  = excluded.updated_at;
	`
	if _, err := s.db.ExecContext(
		ctx,
		insertSQL,
		userID,
		telegramID,
		email,
		name,
		models.DefaultAnimeTheme,
		models.DefaultPosterTemplate,
		string(settingsBytes),
		now,
		now,
	); err != nil {
		return nil, fmt.Errorf("creating telegram user %d: %w", telegramID, err)
	}

	return s.GetByID(ctx, userID)
}

// GetByID retrieves a user record by their ID (`id`).
func (s *Store) GetByID(ctx context.Context, id string) (*models.User, error) {
	const querySQL = `
	SELECT id, telegram_id, email, name, avatar_url, telegram_dump_chat, anime_theme, poster_template, settings_json, created_at
	FROM users
	WHERE id = ?
	LIMIT 1;
	`
	row := s.db.QueryRowContext(ctx, querySQL, strings.TrimSpace(id))
	return scanUserRow(row)
}

// GetByTelegramID retrieves a user record by their numeric Telegram user ID.
func (s *Store) GetByTelegramID(ctx context.Context, telegramID int64) (*models.User, error) {
	if telegramID <= 0 {
		return nil, ErrUserNotFound
	}
	const querySQL = `
	SELECT id, telegram_id, email, name, avatar_url, telegram_dump_chat, anime_theme, poster_template, settings_json, created_at
	FROM users
	WHERE telegram_id = ? OR id = ?
	LIMIT 1;
	`
	row := s.db.QueryRowContext(ctx, querySQL, telegramID, fmt.Sprintf("tg:%d", telegramID))
	return scanUserRow(row)
}

// GetByEmail retrieves a user record by their email address.
func (s *Store) GetByEmail(ctx context.Context, email string) (*models.User, error) {
	const querySQL = `
	SELECT id, telegram_id, email, name, avatar_url, telegram_dump_chat, anime_theme, poster_template, settings_json, created_at
	FROM users
	WHERE email = ?
	LIMIT 1;
	`
	row := s.db.QueryRowContext(ctx, querySQL, strings.ToLower(strings.TrimSpace(email)))
	return scanUserRow(row)
}

// UpdateUserSettings persists the full `models.UserSettings` struct and syncs indexed columns.
func (s *Store) UpdateUserSettings(ctx context.Context, id string, settings models.UserSettings) (*models.User, error) {
	id = strings.TrimSpace(id)
	if id == "" {
		return nil, ErrUserNotFound
	}

	dumpChat := strings.TrimSpace(settings.LeechDumpChat)
	if canonicalTheme, err := models.NormalizeAnimeTheme(settings.AnimeTheme); err == nil {
		settings.AnimeTheme = canonicalTheme
	} else {
		settings.AnimeTheme = models.DefaultAnimeTheme
	}
	if settings.PostTemplateID < models.MinPosterTemplate || settings.PostTemplateID > models.MaxPosterTemplate {
		settings.PostTemplateID = models.DefaultPosterTemplate
	}

	// Keep METADATA sub-maps synchronized when top-level convenience strings are edited in Web UI.
	if settings.MetadataTitle != "" {
		if settings.Metadata == nil {
			settings.Metadata = make(map[string]string)
		}
		settings.Metadata["title"] = settings.MetadataTitle
	}
	if settings.MetadataAudioTitle != "" {
		if settings.AudioMetadata == nil {
			settings.AudioMetadata = make(map[string]string)
		}
		settings.AudioMetadata["title"] = settings.MetadataAudioTitle
	}
	if settings.MetadataSubtitleTitle != "" {
		if settings.SubtitleMetadata == nil {
			settings.SubtitleMetadata = make(map[string]string)
		}
		settings.SubtitleMetadata["title"] = settings.MetadataSubtitleTitle
	}

	rawJSON, err := json.Marshal(settings)
	if err != nil {
		return nil, fmt.Errorf("marshaling user settings: %w", err)
	}

	const updateSQL = `
	UPDATE users
	SET telegram_dump_chat = ?,
	    anime_theme        = ?,
	    poster_template    = ?,
	    settings_json      = ?,
	    updated_at         = ?
	WHERE id = ?;
	`
	res, err := s.db.ExecContext(
		ctx,
		updateSQL,
		dumpChat,
		settings.AnimeTheme,
		settings.PostTemplateID,
		string(rawJSON),
		time.Now().UTC(),
		id,
	)
	if err != nil {
		return nil, fmt.Errorf("updating settings for user %s: %w", id, err)
	}
	rows, err := res.RowsAffected()
	if err == nil && rows == 0 {
		return nil, ErrUserNotFound
	}
	return s.GetByID(ctx, id)
}

// UpdateTelegramDump updates the user's private Telegram Dump Channel ID (`-100xxxxxxxxxx` or `-100xxxx|topic`).
func (s *Store) UpdateTelegramDump(ctx context.Context, id, telegramDumpChat string) (*models.User, error) {
	u, err := s.GetByID(ctx, id)
	if err != nil {
		return nil, err
	}
	u.Settings.LeechDumpChat = strings.TrimSpace(telegramDumpChat)
	return s.UpdateUserSettings(ctx, u.ID, u.Settings)
}

// UpdateThemePreferences updates the user's `anime_theme` and `poster_template` (1..11).
func (s *Store) UpdateThemePreferences(
	ctx context.Context,
	id string,
	animeTheme string,
	posterTemplate int,
) (*models.User, error) {
	u, err := s.GetByID(ctx, id)
	if err != nil {
		return nil, err
	}
	if canonical, err := models.NormalizeAnimeTheme(animeTheme); err == nil {
		u.Settings.AnimeTheme = canonical
	}
	if posterTemplate >= models.MinPosterTemplate && posterTemplate <= models.MaxPosterTemplate {
		u.Settings.PostTemplateID = posterTemplate
	}
	return s.UpdateUserSettings(ctx, u.ID, u.Settings)
}

// GetBotSettings returns all persisted global bot overrides (`bot_settings` table).
func (s *Store) GetBotSettings(ctx context.Context) (map[string]string, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT key, value FROM bot_settings;`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	out := make(map[string]string)
	for rows.Next() {
		var k, v string
		if err := rows.Scan(&k, &v); err == nil {
			out[k] = v
		}
	}
	return out, rows.Err()
}

// UpdateBotSettings upserts multiple global bot setting key-value pairs atomically.
func (s *Store) UpdateBotSettings(ctx context.Context, kv map[string]string) error {
	if len(kv) == 0 {
		return nil
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }()

	stmt, err := tx.PrepareContext(ctx, `
		INSERT INTO bot_settings (key, value, updated_at)
		VALUES (?, ?, ?)
		ON CONFLICT(key) DO UPDATE SET
			value      = excluded.value,
			updated_at = excluded.updated_at;
	`)
	if err != nil {
		return err
	}
	defer stmt.Close()

	now := time.Now().UTC()
	for k, v := range kv {
		k = strings.TrimSpace(k)
		if k == "" {
			continue
		}
		if _, err := stmt.ExecContext(ctx, k, v, now); err != nil {
			return err
		}
	}
	return tx.Commit()
}

// Close cleanly closes the underlying SQLite connection pool.
func (s *Store) Close() error {
	if s.db == nil {
		return nil
	}
	return s.db.Close()
}

func scanUserRow(row *sql.Row) (*models.User, error) {
	var u models.User
	var rawSettings string
	err := row.Scan(
		&u.ID,
		&u.TelegramID,
		&u.Email,
		&u.Name,
		&u.AvatarURL,
		&u.TelegramDumpChat,
		&u.AnimeTheme,
		&u.PosterTemplate,
		&rawSettings,
		&u.CreatedAt,
	)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return nil, ErrUserNotFound
		}
		return nil, err
	}

	settings := models.DefaultUserSettings()
	if strings.TrimSpace(rawSettings) != "" && rawSettings != "{}" {
		_ = json.Unmarshal([]byte(rawSettings), &settings)
	}

	if u.AnimeTheme == "" {
		u.AnimeTheme = models.DefaultAnimeTheme
	} else if canonical, err := models.NormalizeAnimeTheme(u.AnimeTheme); err == nil {
		u.AnimeTheme = canonical
	}
	if u.PosterTemplate < models.MinPosterTemplate || u.PosterTemplate > models.MaxPosterTemplate {
		u.PosterTemplate = models.DefaultPosterTemplate
	}

	if settings.LeechDumpChat == "" && u.TelegramDumpChat != "" {
		settings.LeechDumpChat = u.TelegramDumpChat
	} else if u.TelegramDumpChat == "" && settings.LeechDumpChat != "" {
		u.TelegramDumpChat = settings.LeechDumpChat
	}
	if settings.PostTemplateID < models.MinPosterTemplate || settings.PostTemplateID > models.MaxPosterTemplate {
		settings.PostTemplateID = u.PosterTemplate
	} else {
		u.PosterTemplate = settings.PostTemplateID
	}
	if settings.AnimeTheme == "" {
		settings.AnimeTheme = u.AnimeTheme
	}
	if settings.AudioMetadata != nil && settings.MetadataAudioTitle == "" {
		settings.MetadataAudioTitle = settings.AudioMetadata["title"]
	}
	if settings.SubtitleMetadata != nil && settings.MetadataSubtitleTitle == "" {
		settings.MetadataSubtitleTitle = settings.SubtitleMetadata["title"]
	}
	if settings.Metadata != nil && settings.MetadataTitle == "" {
		settings.MetadataTitle = settings.Metadata["title"]
	}

	u.Settings = settings
	return &u, nil
}
