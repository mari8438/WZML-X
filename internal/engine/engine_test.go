package engine

import (
	"context"
	"sync/atomic"
	"testing"
	"time"

	"github.com/starfall-org/starfall-go/internal/ipc"
	"github.com/starfall-org/starfall-go/internal/models"
	"github.com/starfall-org/starfall-go/internal/safety"
)

func TestDispatcherConcurrencyLimits(t *testing.T) {
	bus := ipc.NewBus()
	registry := NewTaskRegistry(bus)
	limiter := safety.NewRateLimiter(150, 30)

	cfg := DispatcherConfig{
		MaxConcurrentWorkers: 4,
		MaxActiveDownloads:   3,
		MaxTelegramUploads:   2,
		MaxLosslessRemux:     1,
		QueueBufferSize:      10,
	}

	dispatcher := NewDispatcher(cfg, registry, nil, limiter, bus, nil)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	dispatcher.Start(ctx)
	defer dispatcher.Shutdown(1 * time.Second)

	var concurrentWork atomic.Int32
	var maxConcurrent atomic.Int32

	// Submit 6 lightweight tasks
	for i := 0; i < 6; i++ {
		task := models.NewTask("test_user", models.TaskTypeDownload, "Direct", models.DefaultUserSettings())
		task.Work = func(taskCtx context.Context) error {
			current := concurrentWork.Add(1)
			for {
				oldMax := maxConcurrent.Load()
				if current <= oldMax || maxConcurrent.CompareAndSwap(oldMax, current) {
					break
				}
			}
			time.Sleep(30 * time.Millisecond)
			concurrentWork.Add(-1)
			return nil
		}

		if err := dispatcher.Submit(task); err != nil {
			t.Fatalf("submitting task %d failed: %v", i, err)
		}
	}

	// Wait for queue to drain
	time.Sleep(200 * time.Millisecond)

	// Max concurrent should never exceed MaxActiveDownloads (3)
	if maxConcurrent.Load() > 3 {
		t.Fatalf("expected max concurrent active downloads <= 3, got %d", maxConcurrent.Load())
	}
}
