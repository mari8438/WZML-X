package telegram

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/gotd/td/tgerr"
)

func TestAtomicFileStorage(t *testing.T) {
	tempDir, err := os.MkdirTemp("", "starfall_session_test_*")
	if err != nil {
		t.Fatalf("failed to create temp dir: %v", err)
	}
	defer os.RemoveAll(tempDir)

	sessPath := filepath.Join(tempDir, "session.json")
	storage := NewAtomicFileStorage(sessPath)
	ctx := context.Background()

	// Initial load should fail with ErrNotFound
	_, err = storage.LoadSession(ctx)
	if err == nil {
		t.Fatalf("expected error on empty session, got nil")
	}

	// Write session
	data := []byte(`{"version": 1, "data": "secret-session-bytes"}`)
	if err := storage.StoreSession(ctx, data); err != nil {
		t.Fatalf("failed to store session: %v", err)
	}

	// Load session back
	loaded, err := storage.LoadSession(ctx)
	if err != nil {
		t.Fatalf("failed to load session: %v", err)
	}
	if string(loaded) != string(data) {
		t.Fatalf("data mismatch: got %s, want %s", string(loaded), string(data))
	}

	// Purge session
	if err := storage.Purge(); err != nil {
		t.Fatalf("failed to purge session: %v", err)
	}
	_, err = storage.LoadSession(ctx)
	if err == nil {
		t.Fatalf("expected error after purge, got nil")
	}
}

func TestFloodWaitMiddleware(t *testing.T) {
	mw := NewFloodWaitMiddleware()
	mw.InlineMaxWait = 100 * time.Millisecond

	// 1. Large FloodWait (> inline threshold) should return ErrHelperFloodEject immediately
	largeFloodErr := tgerr.New(420, "FLOOD_WAIT_60")
	opLarge := func(ctx context.Context) error {
		return largeFloodErr
	}

	err := mw.Execute(context.Background(), opLarge)
	var ejectErr ErrHelperFloodEject
	if !errors.As(err, &ejectErr) {
		t.Fatalf("expected ErrHelperFloodEject, got: %v", err)
	}
	if ejectErr.Wait != 60*time.Second {
		t.Fatalf("expected wait 60s, got: %v", ejectErr.Wait)
	}

	// 2. Small FloodWait (<= inline threshold) should sleep and then succeed
	attempts := 0
	opSmall := func(ctx context.Context) error {
		attempts++
		if attempts == 1 {
			return tgerr.New(420, "FLOOD_WAIT_0")
		}
		return nil
	}

	err = mw.Execute(context.Background(), opSmall)
	if err != nil {
		t.Fatalf("expected success on small wait, got: %v", err)
	}
	if attempts != 2 {
		t.Fatalf("expected 2 attempts, got: %d", attempts)
	}
}

func TestInlineMarkupBuilder(t *testing.T) {
	builder := NewInlineMarkupBuilder()
	builder.Row(
		ButtonData("▶️ Resume", "task_resume_123"),
		ButtonData("⏸ Pause", "task_pause_123"),
	).Row(
		ButtonURL("🌐 Web Studio", "https://starfall.local/app"),
	)

	markup := builder.Build()
	if len(markup.Rows) != 2 {
		t.Fatalf("expected 2 rows, got %d", len(markup.Rows))
	}
	if len(markup.Rows[0].Buttons) != 2 {
		t.Fatalf("expected 2 buttons in row 0, got %d", len(markup.Rows[0].Buttons))
	}
	if len(markup.Rows[1].Buttons) != 1 {
		t.Fatalf("expected 1 button in row 1, got %d", len(markup.Rows[1].Buttons))
	}
}

func TestCommandFiltersAndRateLimiter(t *testing.T) {
	ownerID := int64(8814651149)
	sudoUsers := []int64{1234567, 7654321}
	authChats := []int64{-1001234567890}

	// Permission checks
	if !IsAuthorized(ownerID, 0, ownerID, sudoUsers, authChats) {
		t.Fatalf("owner should be authorized")
	}
	if !IsAuthorized(1234567, 0, ownerID, sudoUsers, authChats) {
		t.Fatalf("sudo user should be authorized")
	}
	if !IsAuthorized(9999999, -1001234567890, ownerID, sudoUsers, authChats) {
		t.Fatalf("auth chat should be authorized")
	}
	if IsAuthorized(9999999, 0, ownerID, sudoUsers, authChats) {
		t.Fatalf("stranger should not be authorized")
	}

	// Rate limiter checks (max 2 per second)
	limiter := NewCommandRateLimiter(2, 500*time.Millisecond)

	// Owner is never limited
	for i := 0; i < 10; i++ {
		if !limiter.Allow(ownerID, ownerID) {
			t.Fatalf("owner was rate limited at iteration %d", i)
		}
	}

	regularUser := int64(55555)
	if !limiter.Allow(regularUser, ownerID) {
		t.Fatalf("first request for regular user should be allowed")
	}
	if !limiter.Allow(regularUser, ownerID) {
		t.Fatalf("second request for regular user should be allowed")
	}
	if limiter.Allow(regularUser, ownerID) {
		t.Fatalf("third request within window should be rejected")
	}
}
