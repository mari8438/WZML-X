package database

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/starfall-org/starfall-go/internal/models"
	_ "modernc.org/sqlite" // Pure-Go SQLite driver (CGO_ENABLED=0 compatible)
)

var (
	ErrNotFound       = errors.New("record not found")
	ErrStaleWrite     = errors.New("stale write rejected by monotonic version guard")
	ErrOutboxFull     = errors.New("sync outbox queue capacity exceeded")
	ErrDatabaseClosed = errors.New("database connection closed")
)

// TaskCheckpoint stores resumable download/task state across reboots.
type TaskCheckpoint struct {
	GID             string          `json:"gid"`
	Engine          string          `json:"engine"`
	Status          string          `json:"status"`
	Progress        float64         `json:"progress"`
	TotalBytes      int64           `json:"total_bytes"`
	DownloadedBytes int64           `json:"downloaded_bytes"`
	MetadataJSON    json.RawMessage `json:"metadata_json"`
	CreatedAt       time.Time       `json:"created_at"`
	UpdatedAt       time.Time       `json:"updated_at"`
	UpdatedAtNs     int64           `json:"updated_at_ns"`
}

// OutboxItem represents a mutation queued for replication to MongoDB Atlas.
type OutboxItem struct {
	ID             int64  `json:"id"`
	CollectionName string `json:"collection_name"`
	DocID          string `json:"doc_id"`
	Action         string `json:"action"` // "upsert" or "delete"
	PayloadJSON    string `json:"payload_json"`
	UpdatedAtNs    int64  `json:"updated_at_ns"`
}

// PYQRecord represents a TNPSC question row in SQLite FTS5.
type PYQRecord struct {
	QuestionID  string   `json:"question_id"`
	Year        string   `json:"year"`
	ExamName    string   `json:"exam_name"`
	Subject     string   `json:"subject"`
	Question    string   `json:"question"`
	OptionsJSON []string `json:"options_json"`
	Answer      string   `json:"answer"`
	Explanation string   `json:"explanation"`
}

// DBManager manages dual SQLite connection handles (single-writer + multi-reader)
// guaranteeing zero SQLITE_BUSY deadlocks and low-latency reads.
type DBManager struct {
	writeDB      *sql.DB
	readDB       *sql.DB
	path         string
	notifyOutbox chan struct{}
	closeOnce    sync.Once
	closed       atomic.Bool
}

// NewDBManager opens the SQLite database with separate write and read connection pools.
func NewDBManager(dbPath string) (*DBManager, error) {
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

	// 1. writeDB: MaxOpenConns = 1, IMMEDIATE transaction lock
	writeDSN := fmt.Sprintf(
		"file:%s?_txlock=immediate&_pragma=journal_mode(WAL)&_pragma=synchronous(NORMAL)&_pragma=busy_timeout(10000)&_pragma=foreign_keys(ON)&_pragma=cache_size(-8192)",
		dbPath,
	)
	writeDB, err := sql.Open("sqlite", writeDSN)
	if err != nil {
		return nil, fmt.Errorf("opening sqlite writeDB: %w", err)
	}
	writeDB.SetMaxOpenConns(1)
	writeDB.SetMaxIdleConns(1)
	writeDB.SetConnMaxLifetime(0)

	// 2. readDB: MaxOpenConns = 4, query_only=ON for non-blocking concurrent reads
	readDSN := fmt.Sprintf(
		"file:%s?_pragma=query_only(ON)&_pragma=journal_mode(WAL)&_pragma=synchronous(NORMAL)&_pragma=busy_timeout(10000)&_pragma=foreign_keys(ON)&_pragma=cache_size(-8192)",
		dbPath,
	)
	readDB, err := sql.Open("sqlite", readDSN)
	if err != nil {
		_ = writeDB.Close()
		return nil, fmt.Errorf("opening sqlite readDB: %w", err)
	}
	readDB.SetMaxOpenConns(4)
	readDB.SetMaxIdleConns(2)
	readDB.SetConnMaxLifetime(30 * time.Minute)

	mgr := &DBManager{
		writeDB:      writeDB,
		readDB:       readDB,
		path:         dbPath,
		notifyOutbox: make(chan struct{}, 100),
	}

	if err := mgr.migrate(); err != nil {
		_ = mgr.Close()
		return nil, fmt.Errorf("running sqlite migrations: %w", err)
	}

	return mgr, nil
}

func (m *DBManager) migrate() error {
	schema := `
	CREATE TABLE IF NOT EXISTS users (
		id TEXT PRIMARY KEY,
		telegram_id INTEGER UNIQUE,
		role TEXT NOT NULL DEFAULT 'user',
		name TEXT NOT NULL,
		email TEXT DEFAULT '',
		avatar_url TEXT DEFAULT '',
		telegram_dump_chat TEXT DEFAULT '',
		anime_theme TEXT DEFAULT 'solo-leveling',
		poster_template INTEGER DEFAULT 11,
		settings_json TEXT DEFAULT '{}',
		created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
		updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
		updated_at_ns INTEGER NOT NULL DEFAULT 0
	);

	CREATE INDEX IF NOT EXISTS idx_users_telegram_id ON users(telegram_id);

	CREATE TABLE IF NOT EXISTS bot_settings (
		key TEXT PRIMARY KEY,
		value_json TEXT NOT NULL DEFAULT '{}',
		updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
		updated_at_ns INTEGER NOT NULL DEFAULT 0
	);

	CREATE TABLE IF NOT EXISTS task_checkpoints (
		gid TEXT PRIMARY KEY,
		engine TEXT NOT NULL,
		status TEXT NOT NULL,
		progress REAL NOT NULL DEFAULT 0.0,
		total_bytes INTEGER NOT NULL DEFAULT 0,
		downloaded_bytes INTEGER NOT NULL DEFAULT 0,
		metadata_json TEXT DEFAULT '{}',
		created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
		updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
		updated_at_ns INTEGER NOT NULL DEFAULT 0
	);

	CREATE INDEX IF NOT EXISTS idx_task_checkpoints_status ON task_checkpoints(status);

	CREATE TABLE IF NOT EXISTS seen_items (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		source TEXT NOT NULL,
		item_key TEXT NOT NULL,
		title TEXT NOT NULL,
		url TEXT NOT NULL,
		seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
		UNIQUE(source, item_key)
	);

	CREATE INDEX IF NOT EXISTS idx_seen_items_source_key ON seen_items(source, item_key);

	CREATE TABLE IF NOT EXISTS happyfappy_transactions (
		tx_id TEXT PRIMARY KEY,
		amount INTEGER NOT NULL DEFAULT 0,
		status TEXT NOT NULL,
		details_json TEXT DEFAULT '{}',
		created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
		updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
		updated_at_ns INTEGER NOT NULL DEFAULT 0
	);

	CREATE TABLE IF NOT EXISTS sync_outbox (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		collection_name TEXT NOT NULL,
		doc_id TEXT NOT NULL,
		action TEXT NOT NULL,
		payload_json TEXT NOT NULL,
		updated_at_ns INTEGER NOT NULL,
		created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
	);

	CREATE INDEX IF NOT EXISTS idx_sync_outbox_id ON sync_outbox(id);

	CREATE VIRTUAL TABLE IF NOT EXISTS tnpsc_pyq_fts USING fts5(
		question_id UNINDEXED,
		year,
		exam_name,
		subject,
		question,
		options_json UNINDEXED,
		answer UNINDEXED,
		explanation
	);
	`
	_, err := m.writeDB.Exec(schema)
	return err
}

// StartWALCheckpointer periodically checkpoints the SQLite WAL file to bound disk growth.
func (m *DBManager) StartWALCheckpointer(ctx context.Context, interval time.Duration) {
	if interval <= 0 {
		interval = 5 * time.Minute
	}
	go func() {
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				if m.closed.Load() {
					return
				}
				// PASSIVE checkpoint checkpoints as many frames as possible without blocking readers or writers
				if _, err := m.writeDB.ExecContext(ctx, "PRAGMA wal_checkpoint(PASSIVE)"); err != nil {
					slog.Debug("sqlite wal_checkpoint failed", "error", err)
				}
			}
		}
	}()
}

// NotifyOutboxChannel returns the receive-only channel triggered on new outbox mutations.
func (m *DBManager) NotifyOutboxChannel() <-chan struct{} {
	return m.notifyOutbox
}

func (m *DBManager) triggerOutboxNotify() {
	select {
	case m.notifyOutbox <- struct{}{}:
	default:
	}
}

// Close gracefully closes read and write database handles.
func (m *DBManager) Close() error {
	var firstErr error
	m.closeOnce.Do(func() {
		m.closed.Store(true)
		if err := m.writeDB.Close(); err != nil && firstErr == nil {
			firstErr = err
		}
		if err := m.readDB.Close(); err != nil && firstErr == nil {
			firstErr = err
		}
	})
	return firstErr
}

// -----------------------------------------------------------------------------
// User Operations
// -----------------------------------------------------------------------------

// UpsertUser persists a User in SQLite writeDB and enqueues an outbox replication event.
func (m *DBManager) UpsertUser(ctx context.Context, u *models.User) error {
	if m.closed.Load() {
		return ErrDatabaseClosed
	}
	if u == nil || strings.TrimSpace(u.ID) == "" {
		return errors.New("user ID cannot be empty")
	}

	settingsJSON, err := json.Marshal(u.Settings)
	if err != nil {
		return fmt.Errorf("marshaling user settings: %w", err)
	}

	now := time.Now().UTC()
	nowNs := now.UnixNano()

	tx, err := m.writeDB.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("starting tx: %w", err)
	}
	defer tx.Rollback()

	query := `
	INSERT INTO users (
		id, telegram_id, role, name, email, avatar_url,
		telegram_dump_chat, anime_theme, poster_template,
		settings_json, created_at, updated_at, updated_at_ns
	) VALUES (?, ?, 'user', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
	ON CONFLICT(id) DO UPDATE SET
		telegram_id = excluded.telegram_id,
		name = excluded.name,
		email = excluded.email,
		avatar_url = excluded.avatar_url,
		telegram_dump_chat = excluded.telegram_dump_chat,
		anime_theme = excluded.anime_theme,
		poster_template = excluded.poster_template,
		settings_json = excluded.settings_json,
		updated_at = excluded.updated_at,
		updated_at_ns = excluded.updated_at_ns
	WHERE excluded.updated_at_ns >= users.updated_at_ns;
	`
	var tgID any
	if u.TelegramID > 0 {
		tgID = u.TelegramID
	} else {
		tgID = nil
	}

	theme := u.AnimeTheme
	if theme == "" {
		theme = models.DefaultAnimeTheme
	}
	posterID := u.PosterTemplate
	if posterID <= 0 {
		posterID = models.DefaultPosterTemplate
	}

	if _, err := tx.ExecContext(ctx, query,
		u.ID, tgID, u.Name, u.Email, u.AvatarURL,
		u.TelegramDumpChat, theme, posterID,
		string(settingsJSON), now, now, nowNs,
	); err != nil {
		return fmt.Errorf("upserting user: %w", err)
	}

	// Insert into sync_outbox
	userPayload, _ := json.Marshal(u)
	outboxQuery := `
	INSERT INTO sync_outbox (collection_name, doc_id, action, payload_json, updated_at_ns)
	VALUES ('users', ?, 'upsert', ?, ?);
	`
	if _, err := tx.ExecContext(ctx, outboxQuery, u.ID, string(userPayload), nowNs); err != nil {
		return fmt.Errorf("enqueueing user outbox: %w", err)
	}

	if err := tx.Commit(); err != nil {
		return fmt.Errorf("committing upsert user tx: %w", err)
	}

	m.triggerOutboxNotify()
	return nil
}

// GetUser fetches a User by ID from readDB.
func (m *DBManager) GetUser(ctx context.Context, id string) (*models.User, error) {
	if m.closed.Load() {
		return nil, ErrDatabaseClosed
	}

	query := `
	SELECT id, telegram_id, name, email, avatar_url,
	       telegram_dump_chat, anime_theme, poster_template,
	       settings_json, created_at
	FROM users WHERE id = ?;
	`
	row := m.readDB.QueryRowContext(ctx, query, id)

	var u models.User
	var tgID sql.NullInt64
	var settingsStr string
	if err := row.Scan(
		&u.ID, &tgID, &u.Name, &u.Email, &u.AvatarURL,
		&u.TelegramDumpChat, &u.AnimeTheme, &u.PosterTemplate,
		&settingsStr, &u.CreatedAt,
	); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return nil, ErrNotFound
		}
		return nil, fmt.Errorf("querying user by id: %w", err)
	}
	if tgID.Valid {
		u.TelegramID = tgID.Int64
	}
	if settingsStr != "" {
		_ = json.Unmarshal([]byte(settingsStr), &u.Settings)
	}
	return &u, nil
}

// GetUserByTelegramID fetches a User by Telegram ID from readDB.
func (m *DBManager) GetUserByTelegramID(ctx context.Context, telegramID int64) (*models.User, error) {
	if m.closed.Load() {
		return nil, ErrDatabaseClosed
	}

	query := `
	SELECT id, telegram_id, name, email, avatar_url,
	       telegram_dump_chat, anime_theme, poster_template,
	       settings_json, created_at
	FROM users WHERE telegram_id = ?;
	`
	row := m.readDB.QueryRowContext(ctx, query, telegramID)

	var u models.User
	var tgID sql.NullInt64
	var settingsStr string
	if err := row.Scan(
		&u.ID, &tgID, &u.Name, &u.Email, &u.AvatarURL,
		&u.TelegramDumpChat, &u.AnimeTheme, &u.PosterTemplate,
		&settingsStr, &u.CreatedAt,
	); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return nil, ErrNotFound
		}
		return nil, fmt.Errorf("querying user by telegram_id: %w", err)
	}
	if tgID.Valid {
		u.TelegramID = tgID.Int64
	}
	if settingsStr != "" {
		_ = json.Unmarshal([]byte(settingsStr), &u.Settings)
	}
	return &u, nil
}

// ListUsers returns all users from readDB.
func (m *DBManager) ListUsers(ctx context.Context) ([]*models.User, error) {
	if m.closed.Load() {
		return nil, ErrDatabaseClosed
	}

	query := `
	SELECT id, telegram_id, name, email, avatar_url,
	       telegram_dump_chat, anime_theme, poster_template,
	       settings_json, created_at
	FROM users ORDER BY created_at DESC;
	`
	rows, err := m.readDB.QueryContext(ctx, query)
	if err != nil {
		return nil, fmt.Errorf("listing users: %w", err)
	}
	defer rows.Close()

	var users []*models.User
	for rows.Next() {
		var u models.User
		var tgID sql.NullInt64
		var settingsStr string
		if err := rows.Scan(
			&u.ID, &tgID, &u.Name, &u.Email, &u.AvatarURL,
			&u.TelegramDumpChat, &u.AnimeTheme, &u.PosterTemplate,
			&settingsStr, &u.CreatedAt,
		); err != nil {
			return nil, err
		}
		if tgID.Valid {
			u.TelegramID = tgID.Int64
		}
		if settingsStr != "" {
			_ = json.Unmarshal([]byte(settingsStr), &u.Settings)
		}
		users = append(users, &u)
	}
	return users, rows.Err()
}

// -----------------------------------------------------------------------------
// Bot Settings
// -----------------------------------------------------------------------------

// SetBotSetting upserts a key-value pair into bot_settings and sync_outbox.
func (m *DBManager) SetBotSetting(ctx context.Context, key string, value any) error {
	if m.closed.Load() {
		return ErrDatabaseClosed
	}
	valBytes, err := json.Marshal(value)
	if err != nil {
		return fmt.Errorf("marshaling bot setting %s: %w", key, err)
	}

	now := time.Now().UTC()
	nowNs := now.UnixNano()

	tx, err := m.writeDB.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("starting tx: %w", err)
	}
	defer tx.Rollback()

	query := `
	INSERT INTO bot_settings (key, value_json, updated_at, updated_at_ns)
	VALUES (?, ?, ?, ?)
	ON CONFLICT(key) DO UPDATE SET
		value_json = excluded.value_json,
		updated_at = excluded.updated_at,
		updated_at_ns = excluded.updated_at_ns
	WHERE excluded.updated_at_ns >= bot_settings.updated_at_ns;
	`
	if _, err := tx.ExecContext(ctx, query, key, string(valBytes), now, nowNs); err != nil {
		return fmt.Errorf("upserting bot setting: %w", err)
	}

	outboxQuery := `
	INSERT INTO sync_outbox (collection_name, doc_id, action, payload_json, updated_at_ns)
	VALUES ('bot_settings', ?, 'upsert', ?, ?);
	`
	if _, err := tx.ExecContext(ctx, outboxQuery, key, string(valBytes), nowNs); err != nil {
		return fmt.Errorf("enqueueing bot setting outbox: %w", err)
	}

	if err := tx.Commit(); err != nil {
		return fmt.Errorf("committing bot setting tx: %w", err)
	}

	m.triggerOutboxNotify()
	return nil
}

// GetBotSetting reads a setting from readDB and unmarshals it into dest.
func (m *DBManager) GetBotSetting(ctx context.Context, key string, dest any) error {
	if m.closed.Load() {
		return ErrDatabaseClosed
	}

	var valStr string
	query := `SELECT value_json FROM bot_settings WHERE key = ?;`
	err := m.readDB.QueryRowContext(ctx, query, key).Scan(&valStr)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return ErrNotFound
		}
		return fmt.Errorf("getting bot setting %s: %w", key, err)
	}
	return json.Unmarshal([]byte(valStr), dest)
}

// -----------------------------------------------------------------------------
// Seen Items (Auto-Leech Deduplication)
// -----------------------------------------------------------------------------

// IsItemSeen checks if an item from a crawler source has already been processed.
func (m *DBManager) IsItemSeen(ctx context.Context, source, itemKey string) (bool, error) {
	if m.closed.Load() {
		return false, ErrDatabaseClosed
	}

	query := `SELECT 1 FROM seen_items WHERE source = ? AND item_key = ? LIMIT 1;`
	var found int
	err := m.readDB.QueryRowContext(ctx, query, source, itemKey).Scan(&found)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return false, nil
		}
		return false, err
	}
	return true, nil
}

// MarkItemSeen records a seen item.
func (m *DBManager) MarkItemSeen(ctx context.Context, source, itemKey, title, url string) error {
	if m.closed.Load() {
		return ErrDatabaseClosed
	}

	query := `
	INSERT OR IGNORE INTO seen_items (source, item_key, title, url)
	VALUES (?, ?, ?, ?);
	`
	_, err := m.writeDB.ExecContext(ctx, query, source, itemKey, title, url)
	return err
}

// -----------------------------------------------------------------------------
// Task Checkpoints
// -----------------------------------------------------------------------------

// SaveCheckpoint saves or updates a task checkpoint.
func (m *DBManager) SaveCheckpoint(ctx context.Context, cp *TaskCheckpoint) error {
	if m.closed.Load() {
		return ErrDatabaseClosed
	}
	if cp == nil || cp.GID == "" {
		return errors.New("checkpoint GID required")
	}

	now := time.Now().UTC()
	nowNs := now.UnixNano()

	query := `
	INSERT INTO task_checkpoints (
		gid, engine, status, progress, total_bytes, downloaded_bytes,
		metadata_json, created_at, updated_at, updated_at_ns
	) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
	ON CONFLICT(gid) DO UPDATE SET
		status = excluded.status,
		progress = excluded.progress,
		downloaded_bytes = excluded.downloaded_bytes,
		total_bytes = excluded.total_bytes,
		metadata_json = excluded.metadata_json,
		updated_at = excluded.updated_at,
		updated_at_ns = excluded.updated_at_ns;
	`
	metaStr := "{}"
	if len(cp.MetadataJSON) > 0 {
		metaStr = string(cp.MetadataJSON)
	}

	_, err := m.writeDB.ExecContext(ctx, query,
		cp.GID, cp.Engine, cp.Status, cp.Progress,
		cp.TotalBytes, cp.DownloadedBytes, metaStr,
		now, now, nowNs,
	)
	return err
}

// DeleteCheckpoint removes a task checkpoint.
func (m *DBManager) DeleteCheckpoint(ctx context.Context, gid string) error {
	if m.closed.Load() {
		return ErrDatabaseClosed
	}
	_, err := m.writeDB.ExecContext(ctx, "DELETE FROM task_checkpoints WHERE gid = ?;", gid)
	return err
}

// -----------------------------------------------------------------------------
// Outbox Replication Helpers
// -----------------------------------------------------------------------------

// FetchOutboxBatch retrieves up to limit pending outbox entries.
func (m *DBManager) FetchOutboxBatch(ctx context.Context, limit int) ([]OutboxItem, error) {
	if m.closed.Load() {
		return nil, ErrDatabaseClosed
	}
	if limit <= 0 {
		limit = 50
	}

	query := `
	SELECT id, collection_name, doc_id, action, payload_json, updated_at_ns
	FROM sync_outbox
	ORDER BY id ASC
	LIMIT ?;
	`
	rows, err := m.readDB.QueryContext(ctx, query, limit)
	if err != nil {
		return nil, fmt.Errorf("fetching outbox batch: %w", err)
	}
	defer rows.Close()

	var items []OutboxItem
	for rows.Next() {
		var item OutboxItem
		if err := rows.Scan(&item.ID, &item.CollectionName, &item.DocID, &item.Action, &item.PayloadJSON, &item.UpdatedAtNs); err != nil {
			return nil, err
		}
		items = append(items, item)
	}
	return items, rows.Err()
}

// DeleteOutboxBatch deletes successfully acknowledged items from sync_outbox.
func (m *DBManager) DeleteOutboxBatch(ctx context.Context, ids []int64) error {
	if m.closed.Load() {
		return ErrDatabaseClosed
	}
	if len(ids) == 0 {
		return nil
	}

	args := make([]any, len(ids))
	placeholders := make([]string, len(ids))
	for i, id := range ids {
		placeholders[i] = "?"
		args[i] = id
	}

	query := fmt.Sprintf("DELETE FROM sync_outbox WHERE id IN (%s);", strings.Join(placeholders, ","))
	_, err := m.writeDB.ExecContext(ctx, query, args...)
	return err
}

// -----------------------------------------------------------------------------
// TNPSC FTS5 Virtual Table Indexing & Search
// -----------------------------------------------------------------------------

// IndexPYQ inserts a TNPSC question record into the FTS5 search index.
func (m *DBManager) IndexPYQ(ctx context.Context, pyq *PYQRecord) error {
	if m.closed.Load() {
		return ErrDatabaseClosed
	}
	optsJSON, _ := json.Marshal(pyq.OptionsJSON)
	query := `
	INSERT INTO tnpsc_pyq_fts (
		question_id, year, exam_name, subject, question, options_json, answer, explanation
	) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
	`
	_, err := m.writeDB.ExecContext(ctx, query,
		pyq.QuestionID, pyq.Year, pyq.ExamName, pyq.Subject,
		pyq.Question, string(optsJSON), pyq.Answer, pyq.Explanation,
	)
	return err
}

// SearchPYQ queries the FTS5 index using BM25 relevance ranking.
func (m *DBManager) SearchPYQ(ctx context.Context, queryStr string, limit int) ([]PYQRecord, error) {
	if m.closed.Load() {
		return nil, ErrDatabaseClosed
	}
	if limit <= 0 {
		limit = 10
	}
	queryStr = strings.TrimSpace(queryStr)
	if queryStr == "" {
		return nil, nil
	}

	// Escape double quotes to prevent syntax errors in SQLite FTS5 MATCH expressions
	cleanQuery := strings.ReplaceAll(queryStr, `"`, `""`)
	sqlQuery := `
	SELECT question_id, year, exam_name, subject, question, options_json, answer, explanation
	FROM tnpsc_pyq_fts
	WHERE tnpsc_pyq_fts MATCH ?
	ORDER BY rank
	LIMIT ?;
	`
	rows, err := m.readDB.QueryContext(ctx, sqlQuery, `"`+cleanQuery+`"`, limit)
	if err != nil {
		// Fallback to unquoted if match fails
		rows, err = m.readDB.QueryContext(ctx, sqlQuery, cleanQuery, limit)
		if err != nil {
			return nil, fmt.Errorf("searching pyq fts: %w", err)
		}
	}
	defer rows.Close()

	var results []PYQRecord
	for rows.Next() {
		var r PYQRecord
		var optsStr string
		if err := rows.Scan(
			&r.QuestionID, &r.Year, &r.ExamName, &r.Subject,
			&r.Question, &optsStr, &r.Answer, &r.Explanation,
		); err != nil {
			return nil, err
		}
		if optsStr != "" {
			_ = json.Unmarshal([]byte(optsStr), &r.OptionsJSON)
		}
		results = append(results, r)
	}
	return results, rows.Err()
}
