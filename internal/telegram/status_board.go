// Package telegram implements the throttled Live Status Board (`status_board.go`)
// editing active progress messages at most once every 4 seconds to prevent `FLOOD_WAIT`
// while displaying task progress bars, speeds, ETAs, seeders, RAM, and free NVMe disk.
package telegram

import (
	"context"
	"fmt"
	"html"
	"strings"
	"sync"
	"time"

	"github.com/gotd/td/tg"
	"github.com/starfall-org/starfall-go/internal/engine"
	"github.com/starfall-org/starfall-go/internal/models"
	"github.com/starfall-org/starfall-go/internal/safety"
)

type trackedBoard struct {
	chatID     int64
	threadID   int
	msgID      int
	lastEdited time.Time
	lastText   string
}

// StatusBoard renders and throttles live Telegram `/status` messages across active chats.
type StatusBoard struct {
	client      *Client
	registry    *engine.TaskRegistry
	watchdog    *safety.Watchdog
	downloadDir string

	mu     sync.Mutex
	boards map[int64]*trackedBoard
}

// NewStatusBoard creates a throttled Telegram status board manager.
func NewStatusBoard(
	client *Client,
	registry *engine.TaskRegistry,
	watchdog *safety.Watchdog,
	downloadDir string,
) *StatusBoard {
	return &StatusBoard{
		client:      client,
		registry:    registry,
		watchdog:    watchdog,
		downloadDir: downloadDir,
		boards:      make(map[int64]*trackedBoard),
	}
}

// Start Loop refreshes tracked status boards every 4.5 seconds while tasks are active,
// automatically deleting or finalizing the board when all tasks finish.
func (sb *StatusBoard) Start(ctx context.Context) {
	go func() {
		ticker := time.NewTicker(4500 * time.Millisecond)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				sb.refreshAll(ctx)
			}
		}
	}()
}

// SendOrReplace sends a fresh `/status` board to `chatID` and tracks it for auto-updates.
func (sb *StatusBoard) SendOrReplace(ctx context.Context, chatID int64, threadID int) error {
	textHTML, hasActive := sb.RenderHTML()
	msgID, err := sendBotHTML(ctx, sb.client, chatID, threadID, textHTML, nil)
	if err != nil {
		return err
	}
	if !hasActive {
		return nil
	}

	sb.mu.Lock()
	sb.boards[chatID] = &trackedBoard{
		chatID:     chatID,
		threadID:   threadID,
		msgID:      msgID,
		lastEdited: time.Now(),
		lastText:   textHTML,
	}
	sb.mu.Unlock()
	return nil
}

func (sb *StatusBoard) refreshAll(ctx context.Context) {
	if sb.client == nil || !sb.client.IsConnected() {
		return
	}
	sb.mu.Lock()
	if len(sb.boards) == 0 {
		sb.mu.Unlock()
		return
	}
	targets := make([]*trackedBoard, 0, len(sb.boards))
	for _, b := range sb.boards {
		targets = append(targets, b)
	}
	sb.mu.Unlock()

	textHTML, hasActive := sb.RenderHTML()
	for _, b := range targets {
		if time.Since(b.lastEdited) < 4*time.Second && hasActive {
			continue
		}
		if b.lastText != textHTML {
			_ = editBotHTML(ctx, sb.client, b.chatID, b.msgID, textHTML, nil)
			b.lastEdited = time.Now()
			b.lastText = textHTML
		}
		if !hasActive {
			sb.mu.Lock()
			delete(sb.boards, b.chatID)
			sb.mu.Unlock()
		}
	}
}

// RenderHTML formats all active/queued tasks plus VPS RAM & NVMe disk telemetry.
func (sb *StatusBoard) RenderHTML() (string, bool) {
	var all []models.TaskSnapshot
	if sb.registry != nil {
		all = sb.registry.ListSnapshots()
	}

	var active []models.TaskSnapshot
	for _, t := range all {
		if !t.Status.IsTerminal() {
			active = append(active, t)
		}
	}

	var b strings.Builder
	b.WriteString("<b>⚡ ANIME STARFALL — ENGINE STATUS</b>\n")
	b.WriteString("━━━━━━━━━━━━━━━━━━━━\n")

	if len(active) == 0 {
		b.WriteString("<i>No active downloads or uploads right now.</i>\n\n")
	} else {
		limit := len(active)
		if limit > 6 {
			limit = 6
		}
		for _, t := range active[:limit] {
			pct := t.Stats.ProgressPercent
			bar := progressBar12(pct)
			stage := string(t.Status)
			if t.StageDetail != "" {
				stage = t.StageDetail
			}
			spd := t.Stats.DownloadSpeedBps
			if t.Status == models.StatusUploading && t.Stats.UploadSpeedBps > 0 {
				spd = t.Stats.UploadSpeedBps
			}

			fmt.Fprintf(&b, "<b>• %s</b>\n", html.EscapeString(truncateRunes(t.Name, 48)))
			fmt.Fprintf(&b, "<code>[%s] %.1f%%</code> • <b>%s</b>\n", bar, pct, html.EscapeString(stage))
			fmt.Fprintf(&b, "⚡ <code>%s/s</code> | 📦 <code>%s / %s</code> | ⏳ <code>%ds</code>\n",
				formatUploadBytes(spd),
				formatUploadBytes(t.Stats.CompletedBytes),
				formatUploadBytes(t.Stats.TotalBytes),
				t.Stats.ETASeconds,
			)
			if t.EngineName == "Aria2" && t.Stats.Seeders > 0 {
				fmt.Fprintf(&b, "🌱 Seeders: <code>%d</code> | Peers: <code>%d</code>\n", t.Stats.Seeders, t.Stats.ActiveConnections)
			}
			fmt.Fprintf(&b, "🛑 Cancel: <code>/cancel %s</code>\n\n", t.ID)
		}
		if len(active) > limit {
			fmt.Fprintf(&b, "<i>...and %d more queued task(s)</i>\n\n", len(active)-limit)
		}
	}

	// Hardware telemetry footer
	var ramLine string
	if sb.watchdog != nil {
		ws := sb.watchdog.Snapshot()
		ramLine = fmt.Sprintf("🧠 RAM: <code>%.1f MiB (%.1f%%)</code>", float64(ws.HeapAllocBytes)/(1024.0*1024.0), ws.UsagePercent)
	}
	var diskLine string
	if freeB, err := safety.AvailableDiskBytes(sb.downloadDir); err == nil {
		diskLine = fmt.Sprintf("💾 Free: <code>%.1f GB</code>", float64(freeB)/(1024*1024*1024))
	}
	b.WriteString("━━━━━━━━━━━━━━━━━━━━\n")
	b.WriteString(strings.TrimSpace(ramLine + "  |  " + diskLine))
	return b.String(), len(active) > 0
}

func progressBar12(pct float64) string {
	if pct < 0 {
		pct = 0
	}
	if pct > 100 {
		pct = 100
	}
	filled := int((pct / 100.0) * 12.0)
	return strings.Repeat("█", filled) + strings.Repeat("░", 12-filled)
}

func truncateRunes(s string, max int) string {
	r := []rune(s)
	if len(r) <= max {
		return s
	}
	return string(r[:max-1]) + "…"
}

func sendBotHTML(
	ctx context.Context,
	cli *Client,
	chatID int64,
	threadID int,
	htmlText string,
	markup tg.ReplyMarkupClass,
) (int, error) {
	api, err := cli.API()
	if err != nil {
		return 0, err
	}
	peer, err := cli.ResolveChatPeer(ctx, chatID)
	if err != nil {
		return 0, err
	}
	plain, entities := ParseHTMLMessage(htmlText)
	req := &tg.MessagesSendMessageRequest{
		NoWebpage: true,
		Peer:      peer,
		Message:   plain,
		Entities:  entities,
		ReplyMarkup: markup,
		RandomID:  randomInt64(),
	}
	if threadID > 0 {
		req.ReplyTo = &tg.InputReplyToMessage{ReplyToMsgID: threadID}
	}
	req.SetFlags()
	upd, err := api.MessagesSendMessage(ctx, req)
	if err != nil {
		return 0, err
	}
	return extractAnyMsgID(upd), nil
}

func editBotHTML(
	ctx context.Context,
	cli *Client,
	chatID int64,
	msgID int,
	htmlText string,
	markup tg.ReplyMarkupClass,
) error {
	if msgID <= 0 {
		return nil
	}
	api, err := cli.API()
	if err != nil {
		return err
	}
	peer, err := cli.ResolveChatPeer(ctx, chatID)
	if err != nil {
		return err
	}
	plain, entities := ParseHTMLMessage(htmlText)
	req := &tg.MessagesEditMessageRequest{
		NoWebpage:   true,
		Peer:        peer,
		ID:          msgID,
		Message:     plain,
		Entities:    entities,
		ReplyMarkup: markup,
	}
	req.SetFlags()
	_, err = api.MessagesEditMessage(ctx, req)
	return err
}

func extractAnyMsgID(u tg.UpdatesClass) int {
	switch v := u.(type) {
	case *tg.UpdateShortSentMessage:
		return v.ID
	case *tg.Updates:
		for _, up := range v.Updates {
			switch m := up.(type) {
			case *tg.UpdateMessageID:
				return m.ID
			case *tg.UpdateNewMessage:
				if msg, ok := m.Message.(*tg.Message); ok {
					return msg.ID
				}
			case *tg.UpdateNewChannelMessage:
				if msg, ok := m.Message.(*tg.Message); ok {
					return msg.ID
				}
			}
		}
	}
	return 0
}
