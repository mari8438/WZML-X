// Package telegram implements the WebSocket Completion Ping & IPC Event Notifier (`notifier.go`)
// so the Web Studio flashes:
// `✅ Complete! Saved to your Telegram Dump Channel [Open in Telegram]`
// immediately when a silent MTProto dump upload finishes.
package telegram

import (
	"fmt"
	"log/slog"
	"strings"
	"time"

	"github.com/starfall-org/starfall-go/internal/ipc"
	"github.com/starfall-org/starfall-go/internal/models"
)

// CompletionBanner is the structured payload broadcast to the Web Studio UI upon task completion.
type CompletionBanner struct {
	TaskID           string    `json:"task_id"`
	UserID           string    `json:"user_id"`
	FileName         string    `json:"file_name"`
	TelegramDumpChat string    `json:"telegram_dump_chat"`
	TelegramDeepLink string    `json:"telegram_deep_link"`
	FlashMessage     string    `json:"flash_message"`
	PartsUploaded    int       `json:"parts_uploaded"`
	TotalBytes       int64     `json:"total_bytes"`
	CompletedAt      time.Time `json:"completed_at"`
}

// Notifier emits real-time upload progress and task completion signals onto `ipc.Bus`.
type Notifier struct {
	bus    *ipc.Bus
	logger *slog.Logger
}

// NewNotifier constructs a Web Studio completion & progress notifier.
func NewNotifier(bus *ipc.Bus, logger *slog.Logger) *Notifier {
	return &Notifier{
		bus:    bus,
		logger: logger,
	}
}

// EmitUploadProgress broadcasts real-time MTProto chunk upload telemetry to the Web Studio.
func (n *Notifier) EmitUploadProgress(
	task *models.Task,
	partNumber int,
	totalParts int,
	uploadedBytes int64,
	totalBytes int64,
	speedBps int64,
) {
	stats := task.UpdateSpeed(uploadedBytes, 0, speedBps, 4)
	if n.bus == nil {
		return
	}

	n.bus.Publish(ipc.Event{
		Type:      ipc.EventTaskProgress,
		TaskID:    task.ID,
		UserID:    task.UserID,
		Progress:  stats.ProgressPercent,
		SpeedBps:  stats.UploadSpeedBps,
		Message:   fmt.Sprintf("Uploading to Telegram Dump Channel (Part %d/%d • %.1f%%)", partNumber, totalParts, stats.ProgressPercent),
		Timestamp: time.Now().UTC(),
		Metadata: map[string]any{
			"stage":            "TELEGRAM_UPLOAD",
			"part_number":      partNumber,
			"total_parts":      totalParts,
			"bytes_uploaded":   uploadedBytes,
			"total_bytes":      totalBytes,
			"speed_mbps":       float64(stats.UploadSpeedBps) / (1024.0 * 1024.0),
			"eta_seconds":      stats.ETASeconds,
			"percent":          stats.ProgressPercent,
		},
	})
}

// EmitTaskCompleted publishes `ipc.EventTaskCompleted` with the deep link into the user's
// private `TelegramDumpChat` so Web Studio flashes:
// `✅ Complete! Saved to your Telegram Dump Channel [Open in Telegram]`
func (n *Notifier) EmitTaskCompleted(
	task *models.Task,
	lastMessageID int,
	partsUploaded int,
	totalBytes int64,
) CompletionBanner {
	deepLink := BuildTelegramChannelLink(task.TelegramDumpChat, lastMessageID)
	flashMsg := "✅ Complete! Saved to your Telegram Dump Channel [Open in Telegram]"

	banner := CompletionBanner{
		TaskID:           task.ID,
		UserID:           task.UserID,
		FileName:         task.Name,
		TelegramDumpChat: task.TelegramDumpChat,
		TelegramDeepLink: deepLink,
		FlashMessage:     flashMsg,
		PartsUploaded:    partsUploaded,
		TotalBytes:       totalBytes,
		CompletedAt:      time.Now().UTC(),
	}

	if n.bus != nil {
		n.bus.Publish(ipc.Event{
			Type:      ipc.EventTaskCompleted,
			TaskID:    task.ID,
			UserID:    task.UserID,
			Progress:  100.0,
			Message:   flashMsg,
			Timestamp: banner.CompletedAt,
			Metadata: map[string]any{
				"flash_message":      flashMsg,
				"telegram_deep_link": deepLink,
				"telegram_dump_chat": task.TelegramDumpChat,
				"parts_uploaded":     partsUploaded,
				"total_bytes":        totalBytes,
			},
		})
	}

	n.logger.Info("silent telegram dump upload completed",
		slog.String("task_id", task.ID),
		slog.String("user_id", task.UserID),
		slog.String("telegram_dump_chat", task.TelegramDumpChat),
		slog.String("deep_link", deepLink),
		slog.Int("parts", partsUploaded),
	)

	return banner
}

// BuildTelegramChannelLink formats a direct `https://t.me/c/<channel_id>/<msg_id>` link
// from a `-100xxxxxxxxxx` channel identifier.
func BuildTelegramChannelLink(dumpChatID string, messageID int) string {
	cleaned := strings.TrimPrefix(strings.TrimSpace(dumpChatID), "-100")
	if cleaned == "" {
		return "https://t.me/"
	}
	if messageID > 0 {
		return fmt.Sprintf("https://t.me/c/%s/%d", cleaned, messageID)
	}
	return fmt.Sprintf("https://t.me/c/%s", cleaned)
}
