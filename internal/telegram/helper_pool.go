// Package telegram implements the Multi-Bot Helper Pool (`HELPER_TOKENS` & `USER_UPLOAD_BOT_TOKEN`)
// with round-robin load balancing and automatic `FLOOD_WAIT` cooldown rotation for <2000 MB uploads.
package telegram

import (
	"context"
	"fmt"
	"log/slog"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

type helperNode struct {
	token         string
	client        *Client
	cooldownUntil atomic.Int64 // Unix timestamp
}

// HelperPool manages optional secondary Telegram Bot MTProto sessions for high-throughput dump uploads.
type HelperPool struct {
	appID      int
	appHash    string
	sessionDir string
	logger     *slog.Logger

	mu      sync.RWMutex
	helpers []*helperNode
	rrIdx   atomic.Uint64
}

// NewHelperPool creates a HelperPool for `tokens`. Call `Start(ctx)` to connect helper bots in background.
func NewHelperPool(appID int, appHash, sessionDir string, tokens []string, logger *slog.Logger) *HelperPool {
	if logger == nil {
		logger = slog.Default()
	}
	if strings.TrimSpace(sessionDir) == "" {
		sessionDir = "data"
	}
	p := &HelperPool{
		appID:      appID,
		appHash:    appHash,
		sessionDir: sessionDir,
		logger:     logger,
	}
	for i, tok := range tokens {
		tok = strings.TrimSpace(tok)
		if tok == "" {
			continue
		}
		cli, err := NewClient(ClientConfig{
			AppID:       appID,
			AppHash:     appHash,
			BotToken:    tok,
			SessionFile: filepath.Join(sessionDir, fmt.Sprintf("helper_bot_%d.json", i+1)),
			DeviceModel: fmt.Sprintf("Starfall-Helper-%d", i+1),
		}, logger)
		if err == nil {
			p.helpers = append(p.helpers, &helperNode{
				token:  tok,
				client: cli,
			})
		}
	}
	return p
}

// Start launches background MTProto sessions for all configured helper bots.
func (p *HelperPool) Start(ctx context.Context) {
	if p == nil {
		return
	}
	p.mu.RLock()
	nodes := append([]*helperNode(nil), p.helpers...)
	p.mu.RUnlock()

	for i, node := range nodes {
		idx := i + 1
		n := node
		go func() {
			for {
				if ctx.Err() != nil {
					return
				}
				err := n.client.Run(ctx, func(caps SessionCapabilities) {
					p.logger.Info("helper bot online",
						slog.Int("helper_index", idx),
						slog.String("username", caps.Username),
					)
				})
				if ctx.Err() != nil {
					return
				}
				p.logger.Warn("helper bot disconnected; retrying in 10s",
					slog.Int("helper_index", idx),
					slog.Any("error", err),
				)
				select {
				case <-ctx.Done():
					return
				case <-time.After(10 * time.Second):
				}
			}
		}()
	}
}

// PickClient selects the next connected, non-cooldown helper bot via round-robin,
// falling back to `fallback` (the primary bot client) when none are available.
func (p *HelperPool) PickClient(fallback *Client) *Client {
	if p == nil {
		return fallback
	}
	p.mu.RLock()
	n := len(p.helpers)
	if n == 0 {
		p.mu.RUnlock()
		return fallback
	}
	now := time.Now().Unix()
	start := int(p.rrIdx.Add(1) % uint64(n))
	for i := 0; i < n; i++ {
		cand := p.helpers[(start+i)%n]
		if cand.client.IsConnected() && cand.cooldownUntil.Load() <= now {
			p.mu.RUnlock()
			return cand.client
		}
	}
	p.mu.RUnlock()
	return fallback
}

// MarkFloodWait puts a helper client on temporary cooldown so subsequent chunks/tasks rotate away.
func (p *HelperPool) MarkFloodWait(cli *Client, waitSec int) {
	if p == nil || cli == nil || waitSec <= 0 {
		return
	}
	until := time.Now().Add(time.Duration(waitSec+2) * time.Second).Unix()
	p.mu.RLock()
	defer p.mu.RUnlock()
	for _, h := range p.helpers {
		if h.client == cli {
			h.cooldownUntil.Store(until)
			break
		}
	}
}

// ConnectedCount returns the number of currently online helper bots.
func (p *HelperPool) ConnectedCount() int {
	if p == nil {
		return 0
	}
	p.mu.RLock()
	defer p.mu.RUnlock()
	cnt := 0
	for _, h := range p.helpers {
		if h.client.IsConnected() {
			cnt++
		}
	}
	return cnt
}
