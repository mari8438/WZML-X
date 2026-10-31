// Package telegram implements the Dual-Mode Interactive Telegram Bot (`bot.go`)
// handling `/start`, `/help`, `/web`, `/leech`, `/l`, `/ytdl`, `/yl`, `/usetting`, `/us`,
// `/bsetting`, `/bs`, `/status`, `/cancel`, `/mode`, and `/poster` over `gotd/td`.
package telegram

import (
	"context"
	"fmt"
	"html"
	"log/slog"
	"strings"
	"sync"
	"time"

	"github.com/gotd/td/tg"
	"github.com/starfall-org/starfall-go/internal/engine"
	"github.com/starfall-org/starfall-go/internal/media"
	"github.com/starfall-org/starfall-go/internal/models"
	"github.com/starfall-org/starfall-go/internal/safety"
)

// BotLeechRequest carries parsed `/leech` or `/ytdl` arguments to the download manager.
type BotLeechRequest struct {
	RawURI         string
	CustomName     string
	SelectFiles    bool
	ExtractArchive *bool
	MergeDirectory *bool
	OriginChatID   int64
	OriginMsgID    int
}

// BotTaskController decouples `telegram.Bot` from `downloader.Manager` without circular imports.
type BotTaskController interface {
	StartFromBot(owner *models.User, req BotLeechRequest) (*models.Task, error)
	Abort(taskID string, reason string) error
}

// BotUserStore decouples `telegram.Bot` from `user.Service`.
type BotUserStore interface {
	GetOrCreateByTelegramID(ctx context.Context, telegramID int64, displayName string) (*models.User, error)
	UpdateUserSettings(ctx context.Context, userID string, settings models.UserSettings) (*models.User, error)
	GetBotSettings(ctx context.Context) (map[string]string, error)
	UpdateBotSettings(ctx context.Context, kv map[string]string) error
}

// MagicTokenMinter mints 30-day JWT tokens for `/web` magic links.
type MagicTokenMinter interface {
	MintToken(user *models.User) (string, time.Time, error)
}

// BotConfig holds runtime authorization and URL parameters for the interactive bot.
type BotConfig struct {
	OwnerID         int64
	SudoUserIDs     []int64
	AuthorizedChats []int64
	PublicBaseURL   string
	BrandName       string
	DownloadDir     string
}

type pendingPrompt struct {
	Key       string
	IsBotMenu bool
	ExpiresAt time.Time
}

// Bot dispatches incoming MTProto updates (`UpdateNewMessage`, `UpdateNewChannelMessage`,
// `UpdateBotCallbackQuery`) for all StarfallX interactive commands and inline menus.
type Bot struct {
	cfg         BotConfig
	dispatcher  tg.UpdateDispatcher
	client      *Client
	tasks       BotTaskController
	users       BotUserStore
	minter      MagicTokenMinter
	poster      *PosterGenerator
	statusBoard *StatusBoard
	registry    *engine.TaskRegistry
	watchdog    *safety.Watchdog
	logger      *slog.Logger

	promptMu sync.Mutex
	prompts  map[int64]pendingPrompt
}

// NewBot creates an interactive Telegram Bot and registers message + callback handlers on `tg.UpdateDispatcher`.
func NewBot(
	cfg BotConfig,
	tasks BotTaskController,
	users BotUserStore,
	minter MagicTokenMinter,
	poster *PosterGenerator,
	registry *engine.TaskRegistry,
	watchdog *safety.Watchdog,
	logger *slog.Logger,
) *Bot {
	if logger == nil {
		logger = slog.Default()
	}
	if strings.TrimSpace(cfg.BrandName) == "" {
		cfg.BrandName = "Anime Starfall"
	}
	cfg.PublicBaseURL = strings.TrimRight(strings.TrimSpace(cfg.PublicBaseURL), "/")

	d := tg.NewUpdateDispatcher()
	b := &Bot{
		cfg:        cfg,
		dispatcher: d,
		tasks:      tasks,
		users:      users,
		minter:     minter,
		poster:     poster,
		registry:   registry,
		watchdog:   watchdog,
		logger:     logger,
		prompts:    make(map[int64]pendingPrompt),
	}

	d.OnNewMessage(func(ctx context.Context, e tg.Entities, update *tg.UpdateNewMessage) error {
		msg, ok := update.Message.(*tg.Message)
		if !ok || msg.Out {
			return nil
		}
		b.handleIncomingMessage(ctx, e, msg)
		return nil
	})

	d.OnNewChannelMessage(func(ctx context.Context, e tg.Entities, update *tg.UpdateNewChannelMessage) error {
		msg, ok := update.Message.(*tg.Message)
		if !ok || msg.Out {
			return nil
		}
		b.handleIncomingMessage(ctx, e, msg)
		return nil
	})

	d.OnBotCallbackQuery(func(ctx context.Context, e tg.Entities, update *tg.UpdateBotCallbackQuery) error {
		b.handleCallbackQuery(ctx, e, update)
		return nil
	})

	return b
}

// UpdateHandler returns the `tg.UpdateDispatcher` to pass into `telegram.ClientConfig`.
func (b *Bot) UpdateHandler() tg.UpdateDispatcher {
	return b.dispatcher
}

// BindClient attaches the live MTProto Bot Client and starts the throttled StatusBoard loop.
func (b *Bot) BindClient(ctx context.Context, cli *Client) {
	b.client = cli
	b.statusBoard = NewStatusBoard(cli, b.registry, b.watchdog, b.cfg.DownloadDir)
	b.statusBoard.Start(ctx)
}

func (b *Bot) isAuthorized(ctx context.Context, senderID, chatID int64) bool {
	if senderID == b.cfg.OwnerID && b.cfg.OwnerID > 0 {
		return true
	}
	for _, id := range b.cfg.SudoUserIDs {
		if senderID == id {
			return true
		}
	}
	for _, cid := range b.cfg.AuthorizedChats {
		if chatID == cid {
			return true
		}
	}
	if b.users != nil {
		if bs, err := b.users.GetBotSettings(ctx); err == nil && strings.EqualFold(bs["PUBLIC_MODE"], "true") {
			return true
		}
	}
	return false
}

func (b *Bot) isSudo(senderID int64) bool {
	if senderID == b.cfg.OwnerID && b.cfg.OwnerID > 0 {
		return true
	}
	for _, id := range b.cfg.SudoUserIDs {
		if senderID == id {
			return true
		}
	}
	return false
}

func (b *Bot) handleIncomingMessage(ctx context.Context, e tg.Entities, msg *tg.Message) {
	if b.client == nil || !b.client.IsConnected() {
		return
	}
	chatID, senderID, senderName := extractMessageActors(e, msg)
	if senderID == 0 || chatID == 0 {
		return
	}

	text := strings.TrimSpace(msg.Message)
	if text == "" {
		return
	}

	// Check conversational setting input prompt first
	if !strings.HasPrefix(text, "/") {
		b.promptMu.Lock()
		pr, hasPrompt := b.prompts[senderID]
		if hasPrompt {
			delete(b.prompts, senderID)
		}
		b.promptMu.Unlock()

		if hasPrompt && time.Now().Before(pr.ExpiresAt) {
			b.handlePromptReply(ctx, chatID, senderID, senderName, pr, text)
		}
		return
	}

	if !b.isAuthorized(ctx, senderID, chatID) {
		if chatID > 0 {
			_, _ = sendBotHTML(ctx, b.client, chatID, 0,
				fmt.Sprintf("<b>🔒 %s Private Engine</b>\nYour Telegram ID (<code>%d</code>) is not authorized yet.",
					html.EscapeString(b.cfg.BrandName), senderID), nil)
		}
		return
	}

	fields := strings.Fields(text)
	cmdToken := strings.ToLower(fields[0])
	if atIdx := strings.IndexByte(cmdToken, '@'); atIdx != -1 {
		cmdToken = cmdToken[:atIdx]
	}
	restText := strings.TrimSpace(strings.TrimPrefix(text, fields[0]))

	userRec, err := b.users.GetOrCreateByTelegramID(ctx, senderID, senderName)
	if err != nil {
		return
	}

	switch cmdToken {
	case "/start", "/help":
		dashURL := b.magicURL(userRec, "/app/dashboard")
		setURL := b.magicURL(userRec, "/app/settings")
		welcome := fmt.Sprintf(
			"<b>⚡ %s — Dual-Mode Cloud Debrid v1.2</b>\n━━━━━━━━━━━━━━━━━━━━\n"+
				"Welcome, <b>%s</b>!\n\n"+
				"<b>Core Commands:</b>\n"+
				"• <code>/leech &lt;link/magnet&gt; [-n Name] [-s] [-e] [-m]</code>\n"+
				"• <code>/ytdl &lt;video_url&gt;</code> — 1,000+ Streaming Sites\n"+
				"• <code>/usetting</code> (<code>/us</code>) — 40+ Workflow Settings\n"+
				"• <code>/poster &lt;Anime/Movie Title&gt;</code> — Preview 16:9 Poster\n"+
				"• <code>/status</code> — Live Speed, RAM &amp; Disk Telemetry\n"+
				"• <code>/web</code> — Instant 30-Day Web Studio Login\n"+
				"• <code>/mode</code> — Switch Telegram / Web Mode\n"+
				"• <code>/bsetting</code> — Owner Admin Console",
			html.EscapeString(b.cfg.BrandName),
			html.EscapeString(userRec.Name),
		)
		var rows []tg.KeyboardButtonRow
		if dashURL != "" {
			rows = append(rows, tg.KeyboardButtonRow{Buttons: []tg.KeyboardButtonClass{
				&tg.KeyboardButtonURL{Text: "🚀 Launch Web Studio", URL: dashURL},
				&tg.KeyboardButtonURL{Text: "⚙️ Web Settings", URL: setURL},
			}})
		}
		rows = append(rows, tg.KeyboardButtonRow{Buttons: []tg.KeyboardButtonClass{
			&tg.KeyboardButtonCallback{Text: "⚙️ User Settings", Data: []byte("us:page:main")},
			&tg.KeyboardButtonCallback{Text: "📊 Live Status", Data: []byte("st:refresh")},
		}})
		_, _ = sendBotHTML(ctx, b.client, chatID, 0, welcome, &tg.ReplyInlineMarkup{Rows: rows})

	case "/web":
		dashURL := b.magicURL(userRec, "/app/dashboard")
		setURL := b.magicURL(userRec, "/app/settings")
		vidURL := b.magicURL(userRec, "/app/video-studio")
		msgHTML := fmt.Sprintf(
			"<b>🌐 %s — Web Studio Magic Session</b>\n━━━━━━━━━━━━━━━━━━━━\n"+
				"Tap below to open your authenticated 30-day session (synced 1:1 with <code>/usetting</code>):",
			html.EscapeString(b.cfg.BrandName),
		)
		markup := &tg.ReplyInlineMarkup{Rows: []tg.KeyboardButtonRow{
			{Buttons: []tg.KeyboardButtonClass{&tg.KeyboardButtonURL{Text: "🚀 Open Leech Dashboard", URL: dashURL}}},
			{Buttons: []tg.KeyboardButtonClass{
				&tg.KeyboardButtonURL{Text: "⚙️ Settings Studio", URL: setURL},
				&tg.KeyboardButtonURL{Text: "🎬 Video Studio", URL: vidURL},
			}},
		}}
		_, _ = sendBotHTML(ctx, b.client, chatID, 0, msgHTML, markup)

	case "/usetting", "/us":
		menuHTML, markup := buildUSettingMenu(userRec, "main", b.magicURL(userRec, "/app/settings"))
		_, _ = sendBotHTML(ctx, b.client, chatID, 0, menuHTML, markup)

	case "/bsetting", "/bs":
		if !b.isSudo(senderID) {
			_, _ = sendBotHTML(ctx, b.client, chatID, 0, "⛔ <b>Owner / Sudo access required for /bsetting.</b>", nil)
			return
		}
		b.sendBotSettingsMenu(ctx, chatID, userRec)

	case "/mode":
		nextMode := "telegram"
		if strings.EqualFold(userRec.Settings.TaskMode, "telegram") {
			nextMode = "web"
		}
		userRec.Settings.TaskMode = nextMode
		_, _ = b.users.UpdateUserSettings(ctx, userRec.ID, userRec.Settings)
		_, _ = sendBotHTML(ctx, b.client, chatID, 0,
			fmt.Sprintf("🧭 <b>Task Mode switched to:</b> <code>%s</code>", strings.ToUpper(nextMode)), nil)

	case "/status", "/s":
		if b.statusBoard != nil {
			_ = b.statusBoard.SendOrReplace(ctx, chatID, 0)
		}

	case "/cancel":
		taskID := strings.TrimSpace(restText)
		if taskID == "" {
			_, _ = sendBotHTML(ctx, b.client, chatID, 0, "Usage: <code>/cancel sf_xxxxxxxx</code>", nil)
			return
		}
		if err := b.tasks.Abort(taskID, "canceled via /cancel"); err != nil {
			_, _ = sendBotHTML(ctx, b.client, chatID, 0, fmt.Sprintf("⚠️ <code>%s</code>", html.EscapeString(err.Error())), nil)
			return
		}
		_, _ = sendBotHTML(ctx, b.client, chatID, 0, fmt.Sprintf("🛑 Aborted task <code>%s</code>.", html.EscapeString(taskID)), nil)

	case "/poster":
		if restText == "" {
			_, _ = sendBotHTML(ctx, b.client, chatID, 0, "Usage: <code>/poster Solo Leveling S02E05 1080p</code>", nil)
			return
		}
		b.handlePosterPreview(ctx, chatID, userRec, restText)

	case "/leech", "/l", "/ytdl", "/yl":
		if restText == "" {
			_, _ = sendBotHTML(ctx, b.client, chatID, 0,
				"Usage:\n<code>/leech &lt;magnet/url&gt; [-n Custom Name] [-s] [-e] [-m]</code>", nil)
			return
		}
		req := parseLeechCommandArgs(restText)
		req.OriginChatID = chatID
		req.OriginMsgID = msg.ID
		if req.RawURI == "" {
			_, _ = sendBotHTML(ctx, b.client, chatID, 0, "⚠️ Please provide a valid URL or magnet link.", nil)
			return
		}

		task, sErr := b.tasks.StartFromBot(userRec, req)
		if sErr != nil {
			_, _ = sendBotHTML(ctx, b.client, chatID, 0, fmt.Sprintf("❌ <b>Failed to queue task:</b>\n<code>%s</code>", html.EscapeString(sErr.Error())), nil)
			return
		}

		if req.SelectFiles {
			selURL := b.magicURL(userRec, "/app/files?task_id="+task.ID)
			selHTML := fmt.Sprintf(
				"<b>🧲 Selective Torrent Queued</b> (<code>%s</code>)\nTap below once metadata resolves to pick episodes:",
				task.ID,
			)
			markup := &tg.ReplyInlineMarkup{Rows: []tg.KeyboardButtonRow{
				{Buttons: []tg.KeyboardButtonClass{&tg.KeyboardButtonURL{Text: "📂 Select Torrent Files", URL: selURL}}},
			}}
			_, _ = sendBotHTML(ctx, b.client, chatID, 0, selHTML, markup)
		}

		if b.statusBoard != nil {
			_ = b.statusBoard.SendOrReplace(ctx, chatID, 0)
		}
	}
}

func (b *Bot) handleCallbackQuery(ctx context.Context, e tg.Entities, cb *tg.UpdateBotCallbackQuery) {
	if b.client == nil || !b.client.IsConnected() {
		return
	}
	api, err := b.client.API()
	if err != nil {
		return
	}
	defer func() {
		_, _ = api.MessagesSetBotCallbackAnswer(ctx, &tg.MessagesSetBotCallbackAnswerRequest{
			QueryID:   cb.QueryID,
			CacheTime: 1,
		})
	}()

	data := string(cb.Data)
	senderID := cb.UserID
	chatID := extractPeerChatID(cb.Peer)
	if !b.isAuthorized(ctx, senderID, chatID) {
		return
	}

	senderName := ""
	if u, ok := e.Users[senderID]; ok {
		senderName = strings.TrimSpace(u.FirstName + " " + u.LastName)
	}
	userRec, err := b.users.GetOrCreateByTelegramID(ctx, senderID, senderName)
	if err != nil {
		return
	}

	if data == "st:refresh" {
		if b.statusBoard != nil {
			_ = b.statusBoard.SendOrReplace(ctx, chatID, 0)
		}
		return
	}

	if data == "us:close" {
		_ = editBotHTML(ctx, b.client, chatID, cb.MsgID, "✅ <i>Settings menu closed.</i>", nil)
		return
	}

	parts := strings.Split(data, ":")
	if len(parts) >= 3 && parts[0] == "us" {
		switch parts[1] {
		case "page":
			menuHTML, markup := buildUSettingMenu(userRec, parts[2], b.magicURL(userRec, "/app/settings"))
			_ = editBotHTML(ctx, b.client, chatID, cb.MsgID, menuHTML, markup)

		case "tog":
			key := parts[2]
			retPage := "main"
			if len(parts) >= 4 {
				retPage = parts[3]
			}
			applySettingToggle(&userRec.Settings, key)
			if updated, uerr := b.users.UpdateUserSettings(ctx, userRec.ID, userRec.Settings); uerr == nil {
				userRec = updated
			}
			menuHTML, markup := buildUSettingMenu(userRec, retPage, b.magicURL(userRec, "/app/settings"))
			_ = editBotHTML(ctx, b.client, chatID, cb.MsgID, menuHTML, markup)

		case "set":
			action := parts[2]
			retPage := "main"
			if len(parts) >= 4 {
				retPage = parts[3]
			}
			switch action {
			case "SPLIT_2G":
				userRec.Settings.LeechSplitSize = 2097152000
			case "SPLIT_4G":
				userRec.Settings.LeechSplitSize = 4194304000
			case "TOGGLE_MODE":
				if strings.EqualFold(userRec.Settings.TaskMode, "telegram") {
					userRec.Settings.TaskMode = "web"
				} else {
					userRec.Settings.TaskMode = "telegram"
				}
			}
			if updated, uerr := b.users.UpdateUserSettings(ctx, userRec.ID, userRec.Settings); uerr == nil {
				userRec = updated
			}
			menuHTML, markup := buildUSettingMenu(userRec, retPage, b.magicURL(userRec, "/app/settings"))
			_ = editBotHTML(ctx, b.client, chatID, cb.MsgID, menuHTML, markup)

		case "ask":
			key := parts[2]
			b.promptMu.Lock()
			b.prompts[senderID] = pendingPrompt{
				Key:       key,
				IsBotMenu: false,
				ExpiresAt: time.Now().Add(3 * time.Minute),
			}
			b.promptMu.Unlock()
			promptText := fmt.Sprintf(
				"✍️ <b>Send the new value for <code>%s</code> within 3 minutes</b> (or send <code>clear</code> to reset):",
				html.EscapeString(key),
			)
			_, _ = sendBotHTML(ctx, b.client, chatID, 0, promptText, nil)
		}
		return
	}

	if len(parts) >= 3 && parts[0] == "bs" && b.isSudo(senderID) {
		if parts[1] == "tog" && parts[2] == "PUBLIC_MODE" {
			bs, _ := b.users.GetBotSettings(ctx)
			next := "true"
			if strings.EqualFold(bs["PUBLIC_MODE"], "true") {
				next = "false"
			}
			_ = b.users.UpdateBotSettings(ctx, map[string]string{"PUBLIC_MODE": next})
			b.sendBotSettingsMenu(ctx, chatID, userRec)
		} else if parts[1] == "ask" {
			b.promptMu.Lock()
			b.prompts[senderID] = pendingPrompt{
				Key:       parts[2],
				IsBotMenu: true,
				ExpiresAt: time.Now().Add(3 * time.Minute),
			}
			b.promptMu.Unlock()
			_, _ = sendBotHTML(ctx, b.client, chatID, 0,
				fmt.Sprintf("✍️ Send new global bot value for <code>%s</code>:", html.EscapeString(parts[2])), nil)
		}
	}
}

func (b *Bot) handlePromptReply(ctx context.Context, chatID, senderID int64, senderName string, pr pendingPrompt, val string) {
	if pr.IsBotMenu {
		if !b.isSudo(senderID) {
			return
		}
		_ = b.users.UpdateBotSettings(ctx, map[string]string{pr.Key: strings.TrimSpace(val)})
		if pr.Key == "TMDB_API_KEY" && b.poster != nil && b.poster.Provider() != nil {
			b.poster.Provider().SetTMDBKey(strings.TrimSpace(val))
		}
		_, _ = sendBotHTML(ctx, b.client, chatID, 0, fmt.Sprintf("✅ Updated global <code>%s</code>.", html.EscapeString(pr.Key)), nil)
		return
	}

	userRec, err := b.users.GetOrCreateByTelegramID(ctx, senderID, senderName)
	if err != nil {
		return
	}
	if err := applyTextSettingInput(&userRec.Settings, pr.Key, val); err != nil {
		_, _ = sendBotHTML(ctx, b.client, chatID, 0, fmt.Sprintf("⚠️ Invalid value: <code>%s</code>", html.EscapeString(err.Error())), nil)
		return
	}
	updated, err := b.users.UpdateUserSettings(ctx, userRec.ID, userRec.Settings)
	if err != nil {
		_, _ = sendBotHTML(ctx, b.client, chatID, 0, fmt.Sprintf("⚠️ Save error: <code>%s</code>", html.EscapeString(err.Error())), nil)
		return
	}
	menuHTML, markup := buildUSettingMenu(updated, "main", b.magicURL(updated, "/app/settings"))
	_, _ = sendBotHTML(ctx, b.client, chatID, 0, "✅ <b>Saved!</b>\n\n"+menuHTML, markup)
}

func (b *Bot) sendBotSettingsMenu(ctx context.Context, chatID int64, owner *models.User) {
	bs, _ := b.users.GetBotSettings(ctx)
	var ws safety.MemorySnapshot
	if b.watchdog != nil {
		ws = b.watchdog.Snapshot()
	}
	pubMode := strings.EqualFold(bs["PUBLIC_MODE"], "true")
	adminURL := b.magicURL(owner, "/app/admin")

	text := fmt.Sprintf(
		"<b>🛡 %s — GLOBAL ADMIN (/bsetting)</b>\n━━━━━━━━━━━━━━━━━━━━\n"+
			"👑 <b>Owner ID:</b> <code>%d</code>\n"+
			"🌍 <b>Public Bot Access:</b> %s\n"+
			"🎬 <b>TMDb API Key:</b> <code>%s</code>\n"+
			"🧠 <b>Heap / System RAM:</b> <code>%.1f MiB / %.1f%% (%s)</code>",
		html.EscapeString(b.cfg.BrandName),
		b.cfg.OwnerID,
		boolBadge(pubMode),
		valOrUnset(bs["TMDB_API_KEY"]),
		float64(ws.HeapAllocBytes)/(1024.0*1024.0),
		ws.UsagePercent,
		ws.State.String(),
	)

	var rows []tg.KeyboardButtonRow
	rows = append(rows, tg.KeyboardButtonRow{Buttons: []tg.KeyboardButtonClass{
		&tg.KeyboardButtonCallback{Text: "🌍 Public: " + boolBadge(pubMode), Data: []byte("bs:tog:PUBLIC_MODE")},
		&tg.KeyboardButtonCallback{Text: "🎬 Set TMDb Key", Data: []byte("bs:ask:TMDB_API_KEY")},
	}})
	if adminURL != "" {
		rows = append(rows, tg.KeyboardButtonRow{Buttons: []tg.KeyboardButtonClass{
			&tg.KeyboardButtonURL{Text: "🖥 Open Web Admin Console", URL: adminURL},
		}})
	}
	rows = append(rows, tg.KeyboardButtonRow{Buttons: []tg.KeyboardButtonClass{
		&tg.KeyboardButtonCallback{Text: "✖️ Close", Data: []byte("us:close")},
	}})
	_, _ = sendBotHTML(ctx, b.client, chatID, 0, text, &tg.ReplyInlineMarkup{Rows: rows})
}

func (b *Bot) handlePosterPreview(ctx context.Context, chatID int64, userRec *models.User, query string) {
	if b.poster == nil {
		return
	}
	md := media.ParseFilename(query)
	md = b.poster.EnrichMetadata(ctx, md)
	jpg, err := b.poster.RenderPosterCard(ctx, md, userRec.Settings, userRec.Settings.PostTemplateID)
	if err != nil {
		_, _ = sendBotHTML(ctx, b.client, chatID, 0, fmt.Sprintf("⚠️ Poster error: <code>%s</code>", html.EscapeString(err.Error())), nil)
		return
	}
	capHTML := b.poster.FormatExpandableCaption(md, userRec.Settings)

	api, err := b.client.API()
	if err != nil {
		return
	}
	peer, err := b.client.ResolveChatPeer(ctx, chatID)
	if err != nil {
		return
	}
	uTmp := &Uploader{}
	uploaded, err := uTmp.uploadSmallThumbnail(ctx, api, jpg, "poster_preview.jpg")
	if err != nil {
		return
	}
	plain, entities := ParseHTMLMessage(capHTML)
	req := &tg.MessagesSendMediaRequest{
		Peer:     peer,
		Media:    &tg.InputMediaUploadedPhoto{File: uploaded},
		Message:  plain,
		Entities: entities,
		RandomID: randomInt64(),
	}
	req.SetFlags()
	_, _ = api.MessagesSendMedia(ctx, req)
}

func (b *Bot) magicURL(u *models.User, pathWithOptQuery string) string {
	if b.cfg.PublicBaseURL == "" || b.minter == nil || u == nil {
		return ""
	}
	tok, _, err := b.minter.MintToken(u)
	if err != nil {
		return ""
	}
	sep := "?"
	if strings.Contains(pathWithOptQuery, "?") {
		sep = "&"
	}
	return b.cfg.PublicBaseURL + pathWithOptQuery + sep + "token=" + tok
}

func parseLeechCommandArgs(raw string) BotLeechRequest {
	var req BotLeechRequest
	tokens := strings.Fields(raw)
	for i := 0; i < len(tokens); i++ {
		tok := tokens[i]
		switch tok {
		case "-s", "--select":
			req.SelectFiles = true
		case "-e", "-z", "--extract":
			v := true
			req.ExtractArchive = &v
		case "-m", "--merge":
			v := true
			req.MergeDirectory = &v
		case "-n":
			var nameParts []string
			for j := i + 1; j < len(tokens); j++ {
				if strings.HasPrefix(tokens[j], "-") && len(tokens[j]) <= 3 {
					break
				}
				nameParts = append(nameParts, tokens[j])
				i = j
			}
			req.CustomName = strings.Join(nameParts, " ")
		default:
			if req.RawURI == "" && (strings.Contains(tok, "://") || strings.HasPrefix(strings.ToLower(tok), "magnet:?")) {
				req.RawURI = tok
			}
		}
	}
	return req
}

func extractMessageActors(e tg.Entities, msg *tg.Message) (chatID, senderID int64, senderName string) {
	chatID = extractPeerChatID(msg.PeerID)
	if msg.FromID != nil {
		senderID = extractPeerChatID(msg.FromID)
	} else if chatID > 0 {
		senderID = chatID
	}
	if u, ok := e.Users[senderID]; ok {
		senderName = strings.TrimSpace(u.FirstName + " " + u.LastName)
		if senderName == "" {
			senderName = u.Username
		}
	}
	return chatID, senderID, senderName
}

func extractPeerChatID(p tg.PeerClass) int64 {
	switch v := p.(type) {
	case *tg.PeerUser:
		return v.UserID
	case *tg.PeerChat:
		return -v.ChatID
	case *tg.PeerChannel:
		id, _ := strconv.ParseInt(fmt.Sprintf("-100%d", v.ChannelID), 10, 64)
		return id
	}
	return 0
}
