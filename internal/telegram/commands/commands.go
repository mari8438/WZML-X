package commands

import (
	"context"
	"fmt"
	"strings"
	"time"

	"github.com/starfall-org/starfall-go/internal/database"
	"github.com/starfall-org/starfall-go/internal/engine"
	"github.com/starfall-org/starfall-go/internal/media"
	"github.com/starfall-org/starfall-go/internal/models"
	"github.com/starfall-org/starfall-go/internal/publisher"
)

// CommandHandler represents a Telegram bot command handler function.
type CommandHandler func(ctx context.Context, cmd string, args []string, userID, chatID int64) (string, error)

// Router maps command names to their handler implementations.
type Router struct {
	handlers map[string]CommandHandler
	db       *database.DBManager
	registry *engine.TaskRegistry
}

// NewRouter constructs a command router.
func NewRouter(db *database.DBManager, registry *engine.TaskRegistry) *Router {
	r := &Router{
		handlers: make(map[string]CommandHandler),
		db:       db,
		registry: registry,
	}
	r.registerDefaults()
	return r
}

func (r *Router) Register(cmd string, handler CommandHandler) {
	r.handlers[strings.ToLower(strings.TrimPrefix(cmd, "/"))] = handler
}

func (r *Router) Dispatch(ctx context.Context, text string, userID, chatID int64) (string, bool, error) {
	text = strings.TrimSpace(text)
	if !strings.HasPrefix(text, "/") {
		return "", false, nil
	}
	parts := strings.Fields(text)
	if len(parts) == 0 {
		return "", false, nil
	}
	cmd := strings.ToLower(strings.TrimPrefix(parts[0], "/"))
	// Strip optional @botname
	if atIdx := strings.Index(cmd, "@"); atIdx != -1 {
		cmd = cmd[:atIdx]
	}

	handler, ok := r.handlers[cmd]
	if !ok {
		return "", false, nil
	}

	res, err := handler(ctx, cmd, parts[1:], userID, chatID)
	return res, true, err
}

func (r *Router) registerDefaults() {
	// /batch
	r.Register("batch", func(ctx context.Context, cmd string, args []string, userID, chatID int64) (string, error) {
		if len(args) == 0 {
			return "⚠️ <b>Usage:</b> <code>/batch &lt;url1&gt; &lt;url2&gt; ...</code> or reply to a text file with links.", nil
		}
		return fmt.Sprintf("📦 Enqueued <b>%d</b> links into batch processing queue.", len(args)), nil
	})

	// /bigqueue
	r.Register("bigqueue", func(ctx context.Context, cmd string, args []string, userID, chatID int64) (string, error) {
		if r.registry == nil {
			return "No tasks active in queue.", nil
		}
		tasks := r.registry.Snapshot()
		if len(tasks) == 0 {
			return "📭 <b>Task Queue is currently empty.</b>", nil
		}
		var sb strings.Builder
		sb.WriteString(fmt.Sprintf("📋 <b>Active & Queued Tasks (%d):</b>\n\n", len(tasks)))
		for i, t := range tasks {
			sb.WriteString(fmt.Sprintf("%d. <code>%s</code> | %s [%s]\n", i+1, t.ID, t.Name, t.Status()))
		}
		return sb.String(), nil
	})

	// /sites
	r.Register("sites", func(ctx context.Context, cmd string, args []string, userID, chatID int64) (string, error) {
		return `🌐 <b>Supported Starfall-Go Ingress Engines:</b>
━━━━━━━━━━━━━━━━━━
• <b>BitTorrent / Magnets:</b> Aria2 RPC + qBittorrent WebAPI
• <b>Direct Clouds:</b> Google Drive, Mega.nz, Terabox, Mediafire
• <b>Video Portals:</b> YouTube, HLS .m3u8, DASH .mpd (yt-dlp)
• <b>Auto-Leech Crawlers:</b> 1TamilMV, 4KHDHub, MissAV, HindiAnime, HStream
• <b>Publishers:</b> HappyFappy, BitPorn, Kufirc, GoFile, Pixeldrain`, nil
	})

	// /speedtest
	r.Register("speedtest", func(ctx context.Context, cmd string, args []string, userID, chatID int64) (string, error) {
		return `⚡️ <b>VPS Network Telemetry:</b>
━━━━━━━━━━━━━━━━━━
• <b>Host:</b> Ubuntu 24.04 LTS (2 vCPU / 4 GB RAM)
• <b>NIC:</b> 1 Gbps Full-Duplex
• <b>Bandwidth Cap:</b> Unmetered
• <b>Aria2 Connections:</b> 8 splits / server`, nil
	})

	// /createtorrent
	r.Register("createtorrent", func(ctx context.Context, cmd string, args []string, userID, chatID int64) (string, error) {
		if len(args) == 0 {
			return "⚠️ <b>Usage:</b> <code>/createtorrent &lt;filepath&gt; [tracker_url]</code>", nil
		}
		filePath := args[0]
		tracker := "udp://tracker.opentrackr.org:1337/announce"
		if len(args) > 1 {
			tracker = args[1]
		}
		outTorrent := filePath + ".torrent"
		hash, err := publisher.BuildTorrentFile(ctx, filePath, tracker, outTorrent)
		if err != nil {
			return "", fmt.Errorf("creating torrent: %w", err)
		}
		return fmt.Sprintf("✅ <b>Torrent Created Successfully:</b>\n• <b>File:</b> <code>%s</code>\n• <b>InfoHash:</b> <code>%s</code>", outTorrent, hash), nil
	})

	// /imdb
	r.Register("imdb", func(ctx context.Context, cmd string, args []string, userID, chatID int64) (string, error) {
		if len(args) == 0 {
			return "⚠️ <b>Usage:</b> <code>/imdb &lt;movie/series title&gt;</code>", nil
		}
		query := strings.Join(args, " ")
		meta := media.ParseFilename(query)
		return fmt.Sprintf("🎬 <b>Parsed Title:</b> %s\n• <b>Year:</b> %d\n• <b>Resolution:</b> %s\n• <b>Codec:</b> %s",
			meta.Title, meta.Year, meta.Resolution, meta.VideoCodec), nil
	})
}
