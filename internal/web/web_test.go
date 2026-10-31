package web

import (
	"context"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/starfall-org/starfall-go/internal/engine"
	"github.com/starfall-org/starfall-go/internal/ipc"
)

func TestBuildTorrentFileTree(t *testing.T) {
	files := []TorrentFileEntry{
		{Index: 0, Path: "Solo Leveling S02/Episodes/S02E01.mkv", Size: 1000, Selected: true},
		{Index: 1, Path: "Solo Leveling S02/Episodes/S02E02.mkv", Size: 1200, Selected: true},
		{Index: 2, Path: "Solo Leveling S02/Subtitles/S02E01.srt", Size: 50, Selected: false},
		{Index: 3, Path: "Solo Leveling S02/Readme.txt", Size: 20, Selected: true},
	}

	root := BuildTorrentFileTree(files)
	if root == nil || !root.IsDir {
		t.Fatalf("expected root directory node, got %+v", root)
	}

	totalExpectedSize := int64(1000 + 1200 + 50 + 20)
	if root.Size != totalExpectedSize {
		t.Fatalf("expected root total size %d, got %d", totalExpectedSize, root.Size)
	}

	// Root should have 1 child directory: "Solo Leveling S02"
	if len(root.Children) != 1 || root.Children[0].Name != "Solo Leveling S02" {
		t.Fatalf("expected child 'Solo Leveling S02', got %+v", root.Children)
	}

	slDir := root.Children[0]
	if len(slDir.Children) != 3 {
		t.Fatalf("expected 3 items inside Solo Leveling S02 (Episodes, Subtitles, Readme.txt), got %d", len(slDir.Children))
	}
}

func TestSSEBroker(t *testing.T) {
	bus := ipc.NewBus(nil, 32)
	defer bus.Close()

	registry := engine.NewTaskRegistry()
	broker := NewSSEBroker(registry, nil, nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	broker.Start(ctx)

	// Simulate client subscription
	req := httptest.NewRequest("GET", "/api/sse", nil)
	w := httptest.NewRecorder()

	clientCtx, clientCancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer clientCancel()
	req = req.WithContext(clientCtx)

	broker.ServeHTTP(w, req)

	// Header checks
	if w.Header().Get("Content-Type") != "text/event-stream" {
		t.Fatalf("expected Content-Type text/event-stream, got %s", w.Header().Get("Content-Type"))
	}
}
