package telegram

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sync"

	"github.com/gotd/td/session"
)

var (
	ErrSessionCorrupt = errors.New("telegram: session data corrupted or unreadable")
)

// AtomicFileStorage implements session.Storage using atomic temporary-file renames
// to guarantee that VPS power loss or hard process kills never leave half-written session files.
type AtomicFileStorage struct {
	mu   sync.Mutex
	path string
}

// NewAtomicFileStorage creates an AtomicFileStorage backed by path.
func NewAtomicFileStorage(path string) *AtomicFileStorage {
	return &AtomicFileStorage{
		path: filepath.Clean(path),
	}
}

// LoadSession reads and decodes the persisted session bytes.
func (s *AtomicFileStorage) LoadSession(ctx context.Context) ([]byte, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	data, err := os.ReadFile(s.path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, session.ErrNotFound
		}
		return nil, fmt.Errorf("reading session file: %w", err)
	}
	if len(data) == 0 {
		return nil, session.ErrNotFound
	}
	return data, nil
}

// StoreSession atomically writes data to path using a temporary sibling file and os.Rename.
func (s *AtomicFileStorage) StoreSession(ctx context.Context, data []byte) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	dir := filepath.Dir(s.path)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return fmt.Errorf("creating session directory: %w", err)
	}

	tmpFile := fmt.Sprintf("%s.tmp.%d", s.path, os.Getpid())
	if err := os.WriteFile(tmpFile, data, 0o600); err != nil {
		return fmt.Errorf("writing session temp file: %w", err)
	}

	if err := os.Rename(tmpFile, s.path); err != nil {
		_ = os.Remove(tmpFile)
		return fmt.Errorf("atomically replacing session file: %w", err)
	}
	return nil
}

// Purge removes the session file to force re-negotiation (e.g. after AUTH_KEY_UNREGISTERED).
func (s *AtomicFileStorage) Purge() error {
	s.mu.Lock()
	defer s.mu.Unlock()

	if err := os.Remove(s.path); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return nil
}
