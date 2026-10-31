package web

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"sync"
	"time"

	"github.com/starfall-org/starfall-go/internal/engine"
	"github.com/starfall-org/starfall-go/internal/safety"
)

// TelemetryPayload packages real-time cluster metrics fanned out to Web Studio SSE clients.
type TelemetryPayload struct {
	Tasks      []any                  `json:"tasks"`
	TaskCount  int                    `json:"task_count"`
	Memory     safety.MemorySnapshot  `json:"memory"`
	Dispatcher engine.DispatcherStats `json:"dispatcher"`
	Timestamp  time.Time              `json:"timestamp"`
}

// SSEBroker manages fan-out of real-time server-sent events to browser clients.
type SSEBroker struct {
	mu          sync.RWMutex
	subscribers map[chan []byte]bool
	registry    *engine.TaskRegistry
	watchdog    *safety.Watchdog
	dispatcher  *engine.Dispatcher
}

// NewSSEBroker constructs an SSE telemetry broker.
func NewSSEBroker(
	registry *engine.TaskRegistry,
	watchdog *safety.Watchdog,
	dispatcher *engine.Dispatcher,
) *SSEBroker {
	return &SSEBroker{
		subscribers: make(map[chan []byte]bool),
		registry:    registry,
		watchdog:    watchdog,
		dispatcher:  dispatcher,
	}
}

// Start begins the coalesced poller fanning out telemetry to all connected clients.
func (b *SSEBroker) Start(ctx context.Context) {
	go func() {
		ticker := time.NewTicker(1 * time.Second)
		defer ticker.Stop()

		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				b.broadcastSnapshot()
			}
		}
	}()
}

func (b *SSEBroker) broadcastSnapshot() {
	b.mu.RLock()
	if len(b.subscribers) == 0 {
		b.mu.RUnlock()
		return
	}
	b.mu.RUnlock()

	var memSnap safety.MemorySnapshot
	if b.watchdog != nil {
		memSnap = b.watchdog.Snapshot()
	}

	var dStats engine.DispatcherStats
	if b.dispatcher != nil {
		dStats = b.dispatcher.Stats()
	}

	var taskSnap []any
	if b.registry != nil {
		for _, t := range b.registry.Snapshot() {
			taskSnap = append(taskSnap, t)
		}
	}

	payload := TelemetryPayload{
		Tasks:      taskSnap,
		TaskCount:  len(taskSnap),
		Memory:     memSnap,
		Dispatcher: dStats,
		Timestamp:  time.Now().UTC(),
	}

	rawJSON, err := json.Marshal(payload)
	if err != nil {
		return
	}

	msg := []byte(fmt.Sprintf("data: %s\n\n", rawJSON))

	b.mu.RLock()
	defer b.mu.RUnlock()
	for ch := range b.subscribers {
		select {
		case ch <- msg:
		default:
			// Non-blocking drop if client buffer is congested
		}
	}
}

// ServeHTTP handles incoming SSE browser subscriptions with a 5s write deadline.
func (b *SSEBroker) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	flusher, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "streaming unsupported", http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	w.Header().Set("Access-Control-Allow-Origin", "*")

	ch := make(chan []byte, 4)
	b.mu.Lock()
	b.subscribers[ch] = true
	b.mu.Unlock()

	defer func() {
		b.mu.Lock()
		delete(b.subscribers, ch)
		close(ch)
		b.mu.Unlock()
	}()

	// Send immediate initial ping
	_, _ = w.Write([]byte(": ping\n\n"))
	flusher.Flush()

	rc := http.NewResponseController(w)

	for {
		select {
		case <-r.Context().Done():
			return
		case msg, ok := <-ch:
			if !ok {
				return
			}
			_ = rc.SetWriteDeadline(time.Now().Add(5 * time.Second))
			if _, err := w.Write(msg); err != nil {
				return
			}
			flusher.Flush()
		}
	}
}
