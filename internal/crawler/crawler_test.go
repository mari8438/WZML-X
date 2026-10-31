package crawler

import (
	"context"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"
	"time"

	"github.com/starfall-org/starfall-go/internal/database"
)

type mockCrawler struct {
	name  string
	items []DiscoveredRelease
}

func (m *mockCrawler) Name() string { return m.name }
func (m *mockCrawler) CrawlOnce(ctx context.Context) ([]DiscoveredRelease, error) {
	return m.items, nil
}

func TestHasTamilToken(t *testing.T) {
	cases := []struct {
		title    string
		hasTamil bool
	}{
		{"Leo (2023) [Tamil + Telugu + Hindi] 1080p WEB-DL", true},
		{"Dandadan - S01E09 [Multi Audio: Tam, Jap] 1080p", true},
		{"Jailer (2023) Tamil HQ HDRip 700MB", true},
		{"Breaking Bad S05 1080p Bluray x264 English", false},
		{"Solo Leveling S02E05 1080p CR WEB-DL JAP ESub", false},
	}

	for _, tc := range cases {
		got := HasTamilToken(tc.title)
		if got != tc.hasTamil {
			t.Errorf("HasTamilToken(%q) = %v; want %v", tc.title, got, tc.hasTamil)
		}
	}
}

func TestCrawlerSchedulerDeduplication(t *testing.T) {
	tempDir, err := os.MkdirTemp("", "starfall_crawler_test_*")
	if err != nil {
		t.Fatalf("failed to create temp dir: %v", err)
	}
	defer os.RemoveAll(tempDir)

	dbPath := filepath.Join(tempDir, "crawler_test.db")
	dbMgr, err := database.NewDBManager(dbPath)
	if err != nil {
		t.Fatalf("failed to init DBManager: %v", err)
	}
	defer dbMgr.Close()

	var hookDispatched atomic.Int32
	leechHook := func(ctx context.Context, rel DiscoveredRelease) error {
		hookDispatched.Add(1)
		return nil
	}

	scheduler := NewScheduler(dbMgr, leechHook, nil)

	mock := &mockCrawler{
		name: "test_source",
		items: []DiscoveredRelease{
			{Source: "test_source", ItemKey: "item_001", Title: "Movie 1 Tamil", HasTamil: true},
			{Source: "test_source", ItemKey: "item_002", Title: "Movie 2 English", HasTamil: false},
		},
	}
	scheduler.Register(mock)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// First crawl run: 2 items discovered and marked seen
	scheduler.dispatchAll(ctx)
	time.Sleep(100 * time.Millisecond)

	if hookDispatched.Load() != 2 {
		t.Fatalf("expected 2 items dispatched on first run, got %d", hookDispatched.Load())
	}

	// Second crawl run: both items are already seen, so 0 additional dispatches!
	scheduler.dispatchAll(ctx)
	time.Sleep(100 * time.Millisecond)

	if hookDispatched.Load() != 2 {
		t.Fatalf("expected 0 additional dispatches on duplicate crawl, total got %d", hookDispatched.Load())
	}
}
