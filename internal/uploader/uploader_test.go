package uploader

import (
	"context"
	"io"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"
)

func TestDeterministicMultipartRequest(t *testing.T) {
	tempDir, err := os.MkdirTemp("", "starfall_uploader_test_*")
	if err != nil {
		t.Fatalf("failed to create temp dir: %v", err)
	}
	defer os.RemoveAll(tempDir)

	testFilePath := filepath.Join(tempDir, "sample_payload.bin")
	payload := []byte("Starfall Deterministic Streaming Payload 1234567890 ABCDEFGHIJKLMNOPQRSTUVWXYZ")
	if err := os.WriteFile(testFilePath, payload, 0o644); err != nil {
		t.Fatalf("failed to write test file: %v", err)
	}

	var progressReported atomic.Int64
	onProgress := func(uploaded, total int64) {
		progressReported.Store(uploaded)
	}

	extra := map[string]string{
		"token": "test-secret-token",
		"desc":  "anime video release",
	}

	req, err := NewDeterministicMultipartRequest(
		context.Background(),
		"http://localhost/upload",
		"file",
		testFilePath,
		extra,
		onProgress,
	)
	if err != nil {
		t.Fatalf("failed to create multipart request: %v", err)
	}

	if req.ContentLength <= 0 {
		t.Fatalf("expected deterministic ContentLength > 0, got %d", req.ContentLength)
	}

	// Read entire body and verify byte count matches ContentLength exactly
	bodyBytes, err := io.ReadAll(req.Body)
	if err != nil {
		t.Fatalf("failed to read body: %v", err)
	}

	if int64(len(bodyBytes)) != req.ContentLength {
		t.Fatalf("ContentLength mismatch: header=%d, readBytes=%d", req.ContentLength, len(bodyBytes))
	}

	if progressReported.Load() != req.ContentLength {
		t.Fatalf("Progress mismatch: reported=%d, header=%d", progressReported.Load(), req.ContentLength)
	}
}
