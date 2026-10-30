// Package ipc provides a high-throughput, non-blocking, channel-based internal
// Publish/Subscribe event bus for Starfall-Go Debrid.
package ipc

import (
	"log/slog"
	"sync"
	"sync/atomic"
	"time"
)

// EventType enumerates lifecycle notifications emitted across the engine.
type EventType string

const (
	EventTaskStarted   EventType = "EventTaskStarted"
	EventTaskProgress  EventType = "EventTaskProgress"
	EventTaskCompleted EventType = "EventTaskCompleted"
	EventTaskFailed    EventType = "EventTaskFailed"
)

// Event is the immutable payload broadcast over the internal IPC Bus.
type Event struct {
	Type      EventType      `json:"type"`
	TaskID    string         `json:"task_id"`
	UserID    string         `json:"user_id"` // Google Sub ID
	Progress  float64        `json:"progress"`
	SpeedBps  int64          `json:"speed_bps"`
	Message   string         `json:"message,omitempty"`
	Metadata  map[string]any `json:"metadata,omitempty"`
	Timestamp time.Time      `json:"timestamp"`
	Err       error          `json:"-"`
}

type subscriber struct {
	id     uint64
	filter map[EventType]struct{} // empty map = wildcard (all events)
	ch     chan Event
}

// Bus manages concurrent event publishing and topic-filtered channel subscriptions
// with zero blocking on hot worker paths.
type Bus struct {
	logger        *slog.Logger
	defaultBuffer int
	nextSubID     atomic.Uint64
	closed        atomic.Bool
	droppedEvents atomic.Uint64

	mu   sync.RWMutex
	subs map[uint64]*subscriber
}

// NewBus constructs an internal channel-based Pub/Sub event bus.
func NewBus(logger *slog.Logger, defaultBuffer int) *Bus {
	if defaultBuffer <= 0 {
		defaultBuffer = 128
	}
	return &Bus{
		logger:        logger,
		defaultBuffer: defaultBuffer,
		subs:          make(map[uint64]*subscriber),
	}
}

// Subscribe registers a listener channel for specific EventTypes (e.g., EventTaskStarted,
// EventTaskFailed). It returns a unique subscription ID and a receive-only `<-chan Event`.
func (b *Bus) Subscribe(bufferSize int, types ...EventType) (uint64, <-chan Event) {
	if bufferSize <= 0 {
		bufferSize = b.defaultBuffer
	}
	id := b.nextSubID.Add(1)
	filter := make(map[EventType]struct{}, len(types))
	for _, t := range types {
		filter[t] = struct{}{}
	}

	sub := &subscriber{
		id:     id,
		filter: filter,
		ch:     make(chan Event, bufferSize),
	}

	b.mu.Lock()
	if b.closed.Load() {
		close(sub.ch)
		b.mu.Unlock()
		return id, sub.ch
	}
	b.subs[id] = sub
	b.mu.Unlock()

	return id, sub.ch
}

// SubscribeAll registers a wildcard subscriber that receives all EventTypes.
func (b *Bus) SubscribeAll(bufferSize int) (uint64, <-chan Event) {
	return b.Subscribe(bufferSize)
}

// Unsubscribe removes a subscriber and closes its event channel cleanly.
func (b *Bus) Unsubscribe(subID uint64) {
	b.mu.Lock()
	sub, ok := b.subs[subID]
	if ok {
		delete(b.subs, subID)
		close(sub.ch)
	}
	b.mu.Unlock()
}

// Publish broadcasts `evt` to all matching subscribers without blocking the caller.
func (b *Bus) Publish(evt Event) {
	if b.closed.Load() {
		return
	}
	if evt.Timestamp.IsZero() {
		evt.Timestamp = time.Now()
	}

	b.mu.RLock()
	defer b.mu.RUnlock()

	for _, sub := range b.subs {
		if len(sub.filter) > 0 {
			if _, match := sub.filter[evt.Type]; !match {
				continue
			}
		}

		select {
		case sub.ch <- evt:
		default:
			select {
			case <-sub.ch:
				b.droppedEvents.Add(1)
			default:
			}
			select {
			case sub.ch <- evt:
			default:
				b.droppedEvents.Add(1)
			}
		}
	}
}

// DroppedCount returns the total number of stale events evicted due to slow consumers.
func (b *Bus) DroppedCount() uint64 {
	return b.droppedEvents.Load()
}

// Close shuts down the event bus and closes all active subscriber channels.
func (b *Bus) Close() {
	if !b.closed.CompareAndSwap(false, true) {
		return
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	for id, sub := range b.subs {
		close(sub.ch)
		delete(b.subs, id)
	}
}
