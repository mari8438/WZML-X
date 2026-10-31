package downloader

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/starfall-org/starfall-go/internal/downloader/qbit"
)

func TestQbitClientMockServer(t *testing.T) {
	// Mock qBittorrent WebAPI server
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/v2/auth/login":
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte("Ok."))
		case "/api/v2/torrents/add":
			w.WriteHeader(http.StatusOK)
		case "/api/v2/torrents/info":
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`[{"hash":"testhash123","name":"Test Torrent","progress":0.5,"state":"downloading"}]`))
		case "/api/v2/torrents/delete":
			w.WriteHeader(http.StatusOK)
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	client, err := qbit.NewClient(qbit.Config{
		BaseURL:  server.URL,
		Username: "admin",
		Password: "adminadmin",
		Timeout:  2 * time.Second,
	})
	if err != nil {
		t.Fatalf("failed to create qbit client: %v", err)
	}

	ctx := context.Background()

	// Test Login
	if err := client.Login(ctx); err != nil {
		t.Fatalf("qbit login failed: %v", err)
	}

	// Test AddTorrent
	if err := client.AddTorrent(ctx, "magnet:?xt=urn:btih:testhash123", "/downloads"); err != nil {
		t.Fatalf("qbit add torrent failed: %v", err)
	}

	// Test GetTorrent
	info, err := client.GetTorrent(ctx, "testhash123")
	if err != nil {
		t.Fatalf("qbit get torrent failed: %v", err)
	}
	if info.Hash != "testhash123" || info.Name != "Test Torrent" {
		t.Fatalf("unexpected torrent info: %+v", info)
	}

	// Test DeleteTorrent
	if err := client.DeleteTorrent(ctx, "testhash123", false); err != nil {
		t.Fatalf("qbit delete torrent failed: %v", err)
	}
}
