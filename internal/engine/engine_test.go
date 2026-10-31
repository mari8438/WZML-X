package engine

import (
	"context"
	"fmt"
	"log/slog"
	"sync/atomic"
	"testing"
	"time"

	"github.com/starfall-org/starfall-go/internal/ipc"
	"github.com/starfall-org/starfall-go/internal/models"
	"github.com/starfall-org/starfall-go/internal/safety"
)

func TestDispatcherConcurrencyLimits(t *testing.T) {
	logger := slog.Default()
	bus := ipc.NewBus(logger, 64)
	defer bus.Close()

	registry := NewTaskRegistry()
	limiter := safety.NewRateLimiter(safety.RateLimitConfig{
		MaxGlobalConnections:  150,
		MaxPerTaskConnections: 30,
	}, logger)

	cfg := DispatcherConfig{
		MaxConcurrentWorkers: 4,
		MaxActiveDownloads:   3,
		MaxTelegramUploads:   2,
		MaxLosslessRemux:     1,
		QueueBufferSize:      10,
	}

	dispatcher := NewDispatcher(cfg, registry, nil, limiter, bus, logger)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	dispatcher.Start(ctx)
	defer func() {
		shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer shutdownCancel()
		_ = dispatcher.Shutdown(shutdownCtx)
	}()

	var concurrentWork atomic.Int32
	var maxConcurrent atomic.Int32

	user := models.NewGoogleUser("sub_123", "user@test.com", "Tester", "")

	// Submit 6 lightweight tasks
	for i := 0; i < 6; i++ {
		taskID := fmt.Sprintf("task_%d", i)
		task := models.NewTask(taskID, user, "Test Task", "http://example.com/file", models.TaskTypeDownload, 1024)
		task.Work = func(taskCtx context.Context, t *models.Task) error {
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
	time.Sleep(250 * time.Millisecond)

	// Max concurrent should never exceed MaxActiveDownloads (3)
	if maxConcurrent.Load() > 3 {
		t.Fatalf("expected max concurrent active downloads <= 3, got %d", maxConcurrent.Load())
	}
}
