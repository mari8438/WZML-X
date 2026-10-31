package safety

import (
	"context"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestBufferPools(t *testing.T) {
	cBuf := GetCopyBuf()
	if cBuf == nil || len(*cBuf) != 64*1024 {
		t.Fatalf("expected 64 KiB slice, got %v", cBuf)
	}
	PutCopyBuf(cBuf)

	hBuf := GetHashBuf()
	if hBuf == nil || len(*hBuf) != 256*1024 {
		t.Fatalf("expected 256 KiB slice, got %v", hBuf)
	}
	PutHashBuf(hBuf)
}

func TestBoundedRingBuffer(t *testing.T) {
	rb := NewBoundedRingBuffer(10)
	n, err := rb.Write([]byte("hello"))
	if err != nil || n != 5 {
		t.Fatalf("write failed: %v, n=%d", err, n)
	}
	if rb.String() != "hello" {
		t.Fatalf("expected 'hello', got '%s'", rb.String())
	}

	// Write more to overflow capacity
	_, _ = rb.Write([]byte("world123")) // total 13 bytes into 10-byte buffer
	str := rb.String()
	if len(str) != 10 {
		t.Fatalf("expected length 10, got %d ('%s')", len(str), str)
	}
	if str != "loworld123" {
		t.Fatalf("expected 'loworld123', got '%s'", str)
	}

	rb.Reset()
	if len(rb.Bytes()) != 0 {
		t.Fatalf("expected 0 after reset, got %d", len(rb.Bytes()))
	}
}

func TestBoundedPool(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	pool := NewBoundedPool(ctx, 2, 5)
	var executed atomic.Int32

	for i := 0; i < 5; i++ {
		err := pool.TrySubmit(func(ctx context.Context) error {
			executed.Add(1)
			return nil
		})
		if err != nil {
			t.Fatalf("unexpected submit error: %v", err)
		}
	}

	// Let jobs finish
	time.Sleep(50 * time.Millisecond)
	if executed.Load() != 5 {
		t.Fatalf("expected 5 executed jobs, got %d", executed.Load())
	}

	pool.Close()
	err := pool.TrySubmit(func(ctx context.Context) error { return nil })
	if err != ErrPoolClosed {
		t.Fatalf("expected ErrPoolClosed, got %v", err)
	}
}

func TestSupervisedCommand(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	cmd := NewSupervisedCommand(ctx, "echo", "starfall-proc-check")
	out, err := cmd.Output()
	if err != nil {
		t.Fatalf("failed to run supervised echo command: %v", err)
	}
	if !strings.Contains(string(out), "starfall-proc-check") {
		t.Fatalf("unexpected output: %s", string(out))
	}
}
