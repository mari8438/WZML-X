// Package user implements the pure-Go embedded SQLite persistence layer (`modernc.org/sqlite`)
// and user profile/Telegram Cloud linking service for Starfall-Go Debrid.
package user

import (
	"context"
	"database/sql"
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
	// ErrUserNotFound is returned when a user ID or email does not exist in SQLite.
	ErrUserNotFound = errors.New("user not found")
)

// Store manages persistent user records in an embedded WAL-mode SQLite database.
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
		"file:%s?_pragma=journal_mode(WAL)&_pragma=synchronous(NORMAL)&_pragma=busy_timeout(5000)&_pragma=foreign_keys(ON)&_pragma=cache_size(-16384)",
		dbPath,
	)
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("opening sqlite database: %w", err)
	}

	// Keep connection pool tight for embedded WAL mode on a 2 vCPU VPS.
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

// migrate initializes the `users` table and indexes idempotently.
func (s *Store) migrate(ctx context.Context) error {
	const schemaSQL = `
	CREATE TABLE IF NOT EXISTS users (
		id                 TEXT PRIMARY KEY,
		email              TEXT NOT NULL UNIQUE,
		name               TEXT NOT NULL,
		avatar_url         TEXT NOT NULL DEFAULT '',
		telegram_dump_chat TEXT NOT NULL DEFAULT '',
		anime_theme        TEXT NOT NULL DEFAULT 'solo-leveling',
		poster_template    INTEGER NOT NULL DEFAULT 1,
		created_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
		updated_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
	);
	CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
	`
	_, err := s.db.ExecContext(ctx, schemaSQL)
	return err
}

// UpsertGoogleUser inserts a newly authenticated Google OAuth2 user with default theme
// ("solo-leveling") and poster template (1), or updates their email, display name, and
// avatar URL on subsequent logins while preserving `telegram_dump_chat` and preferences.
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

	now := time.Now().UTC()
	const upsertSQL = `
	INSERT INTO users (
		id, email, name, avatar_url, telegram_dump_chat, anime_theme, poster_template, created_at, updated_at
	) VALUES (
		?, ?, ?, ?, '', ?, ?, ?, ?
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
		now,
		now,
	)
	if err != nil {
		return nil, fmt.Errorf("upserting google user %s: %w", id, err)
	}

	return s.GetByID(ctx, id)
}

// GetByID retrieves a user record by their Google Sub ID (`id`).
func (s *Store) GetByID(ctx context.Context, id string) (*models.User, error) {
	const querySQL = `
	SELECT id, email, name, avatar_url, telegram_dump_chat, anime_theme, poster_template, created_at
	FROM users
	WHERE id = ?
	LIMIT 1;
	`
	row := s.db.QueryRowContext(ctx, querySQL, strings.TrimSpace(id))
	return scanUserRow(row)
}

// GetByEmail retrieves a user record by their email address.
func (s *Store) GetByEmail(ctx context.Context, email string) (*models.User, error) {
	const querySQL = `
	SELECT id, email, name, avatar_url, telegram_dump_chat, anime_theme, poster_template, created_at
	FROM users
	WHERE email = ?
	LIMIT 1;
	`
	row := s.db.QueryRowContext(ctx, querySQL, strings.ToLower(strings.TrimSpace(email)))
	return scanUserRow(row)
}

// UpdateTelegramDump updates the user's private Telegram Dump Channel ID (`-100xxxxxxxxxx`).
func (s *Store) UpdateTelegramDump(ctx context.Context, id, telegramDumpChat string) (*models.User, error) {
	const updateSQL = `
	UPDATE users
	SET telegram_dump_chat = ?, updated_at = ?
	WHERE id = ?;
	`
	res, err := s.db.ExecContext(
		ctx,
		updateSQL,
		strings.TrimSpace(telegramDumpChat),
		time.Now().UTC(),
		strings.TrimSpace(id),
	)
	if err != nil {
		return nil, fmt.Errorf("updating telegram_dump_chat for user %s: %w", id, err)
	}
	rows, err := res.RowsAffected()
	if err == nil && rows == 0 {
		return nil, ErrUserNotFound
	}
	return s.GetByID(ctx, id)
}

// UpdateThemePreferences updates the user's `anime_theme` and `poster_template` (1..11).
func (s *Store) UpdateThemePreferences(
	ctx context.Context,
	id string,
	animeTheme string,
	posterTemplate int,
) (*models.User, error) {
	const updateSQL = `
	UPDATE users
	SET anime_theme = ?, poster_template = ?, updated_at = ?
	WHERE id = ?;
	`
	res, err := s.db.ExecContext(
		ctx,
		updateSQL,
		strings.ToLower(strings.TrimSpace(animeTheme)),
		posterTemplate,
		time.Now().UTC(),
		strings.TrimSpace(id),
	)
	if err != nil {
		return nil, fmt.Errorf("updating theme preferences for user %s: %w", id, err)
	}
	rows, err := res.RowsAffected()
	if err == nil && rows == 0 {
		return nil, ErrUserNotFound
	}
	return s.GetByID(ctx, id)
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
	err := row.Scan(
		&u.ID,
		&u.Email,
		&u.Name,
		&u.AvatarURL,
		&u.TelegramDumpChat,
		&u.AnimeTheme,
		&u.PosterTemplate,
		&u.CreatedAt,
	)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return nil, ErrUserNotFound
		}
		return nil, err
	}
	if u.AnimeTheme == "" {
		u.AnimeTheme = models.DefaultAnimeTheme
	}
	if u.PosterTemplate < models.MinPosterTemplate || u.PosterTemplate > models.MaxPosterTemplate {
		u.PosterTemplate = models.DefaultPosterTemplate
	}
	return &u, nil
}
