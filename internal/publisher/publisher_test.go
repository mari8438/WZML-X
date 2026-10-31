package publisher

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestBuildTorrentFile(t *testing.T) {
	tempDir, err := os.MkdirTemp("", "starfall_torrent_test_*")
	if err != nil {
		t.Fatalf("failed to create temp dir: %v", err)
	}
	defer os.RemoveAll(tempDir)

	srcFile := filepath.Join(tempDir, "sample_video.mkv")
	// Write 600 KiB data (spans > 2 pieces of 256 KiB)
	data := make([]byte, 600*1024)
	for i := range data {
		data[i] = byte(i % 256)
	}
	if err := os.WriteFile(srcFile, data, 0o644); err != nil {
		t.Fatalf("failed to write test source: %v", err)
	}

	outTorrent := filepath.Join(tempDir, "sample_video.torrent")
	announceURL := "udp://tracker.opentrackr.org:1337/announce"

	infoHash, err := BuildTorrentFile(context.Background(), srcFile, announceURL, outTorrent)
	if err != nil {
		t.Fatalf("BuildTorrentFile failed: %v", err)
	}

	if len(infoHash) != 40 {
		t.Fatalf("expected 40-character hex infoHash, got %d (%s)", len(infoHash), infoHash)
	}

	// Verify torrent file on disk
	torrentBytes, err := os.ReadFile(outTorrent)
	if err != nil {
		t.Fatalf("reading generated torrent: %v", err)
	}

	if !strings.HasPrefix(string(torrentBytes), "d") || !strings.HasSuffix(string(torrentBytes), "e") {
		t.Fatalf("torrent does not start with 'd' and end with 'e'")
	}
	if !strings.Contains(string(torrentBytes), "sample_video.mkv") {
		t.Fatalf("torrent does not contain file name")
	}
}

func TestHappyFappyPublisher(t *testing.T) {
	tempDir, err := os.MkdirTemp("", "starfall_hf_test_*")
	if err != nil {
		t.Fatalf("failed to create temp dir: %v", err)
	}
	defer os.RemoveAll(tempDir)

	srcFile := filepath.Join(tempDir, "release.mp4")
	_ = os.WriteFile(srcFile, []byte("release payload"), 0o644)

	pub := NewHappyFappyPublisher(nil, "http://tracker.example.com/announce")
	state, err := pub.StartPublish(context.Background(), "tx_999", srcFile, "Test Release 2026")
	if err != nil {
		t.Fatalf("publishing failed: %v", err)
	}

	if state.Stage != StageCompleted {
		t.Fatalf("expected StageCompleted, got %s", state.Stage)
	}
	if len(state.InfoHash) != 40 {
		t.Fatalf("expected 40-char infoHash, got %s", state.InfoHash)
	}
}
