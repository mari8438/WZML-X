package telegram

import (
	"context"
	"fmt"
	"math/rand"
	"time"

	"github.com/gotd/td/tgerr"
)

// ErrHelperFloodEject signals that an operation received a FLOOD_WAIT > 30s
// requiring this node to be quarantined and the task re-routed to another helper.
type ErrHelperFloodEject struct {
	Wait time.Duration
}

func (e ErrHelperFloodEject) Error() string {
	return fmt.Sprintf("telegram: flood wait %v exceeds 30s inline threshold; ejecting node", e.Wait)
}

// FloodWaitMiddleware intercepts MTProto flood wait errors.
// Small waits (<= 30s) are slept inline with 10% random jitter to avoid losing single message edits.
// Large waits (> 30s) are converted to ErrHelperFloodEject to immediately rotate to another helper node.
type FloodWaitMiddleware struct {
	InlineMaxWait time.Duration
}

// NewFloodWaitMiddleware returns a FloodWaitMiddleware with default 30s inline threshold.
func NewFloodWaitMiddleware() *FloodWaitMiddleware {
	return &FloodWaitMiddleware{
		InlineMaxWait: 30 * time.Second,
	}
}

// Execute wraps an MTProto operation, applying inline backoff or node ejection.
func (m *FloodWaitMiddleware) Execute(ctx context.Context, op func(ctx context.Context) error) error {
	for {
		err := op(ctx)
		if err == nil {
			return nil
		}

		if d, ok := tgerr.AsFloodWait(err); ok {
			if d <= m.InlineMaxWait {
				// Inline backoff with 1.1x jitter
				jitterMs := rand.Intn(int(d.Milliseconds()/10) + 100)
				sleepDur := d + time.Duration(jitterMs)*time.Millisecond
				select {
				case <-ctx.Done():
					return ctx.Err()
				case <-time.After(sleepDur):
					continue // retry operation after sleep
				}
			}
			// Exceeds inline threshold; reject immediately with ErrHelperFloodEject
			return ErrHelperFloodEject{Wait: d}
		}

		return err
	}
}
