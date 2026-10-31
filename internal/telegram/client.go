// Package telegram implements the Dual-Mode MTProto Client (`github.com/gotd/td`)
// supporting both Bot Token sessions (with interactive update dispatching) and
// Telegram Premium User sessions (4,000 MB single-file upload ceiling).
package telegram

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/gotd/td/session"
	"github.com/gotd/td/telegram"
	"github.com/gotd/td/tg"
	"github.com/starfall-org/starfall-go/internal/models"
)

const (
	// FreeUploadCeilingBytes (2,000 MB) is the hard single-file limit for standard bots and free accounts.
	FreeUploadCeilingBytes int64 = 2000 * 1024 * 1024

	// PremiumUploadCeilingBytes (4,000 MB) is the single-file limit for Telegram Premium sessions.
	PremiumUploadCeilingBytes int64 = 4000 * 1024 * 1024

	// FreeSplitTargetMB (1950 MB) is the safe mkvmerge split size when a file exceeds 2,000 MB on a free session.
	FreeSplitTargetMB int = 1950

	// PremiumSplitTargetMB (3900 MB) is the safe mkvmerge split size when a file exceeds 4,000 MB on a Premium session.
	PremiumSplitTargetMB int = 3900
)

var (
	// ErrClientNotConnected indicates the MTProto client has not started or has disconnected.
	ErrClientNotConnected = errors.New("telegram MTProto client is not connected")

	// ErrInvalidDumpChatID indicates the user's `telegram_dump_chat` is malformed.
	ErrInvalidDumpChatID = errors.New("invalid telegram_dump_chat ID: expected -100xxxxxxxxxx or -100xxxxxxxxxx|topic_id")
)

// ClientConfig holds MTProto API credentials, session persistence paths, and optional UpdateHandler.
type ClientConfig struct {
	AppID         int
	AppHash       string
	BotToken      string
	SessionString string
	SessionFile   string
	DeviceModel   string
	UpdateHandler telegram.UpdateHandler
}

// SessionCapabilities reports the live negotiated account tier and upload ceiling.
type SessionCapabilities struct {
	AccountID            int64  `json:"account_id"`
	Username             string `json:"username"`
	IsBot                bool   `json:"is_bot"`
	IsPremium            bool   `json:"is_premium"`
	MaxSingleUploadBytes int64  `json:"max_single_upload_bytes"`
	SafeSplitSizeMB      int    `json:"safe_split_size_mb"`
}

// Client manages a persistent `gotd/td` MTProto session, inspects Premium status,
// caches channel/user access hashes, and resolves dump targets (including forum topics).
type Client struct {
	cfg       ClientConfig
	logger    *slog.Logger
	tdClient  *telegram.Client
	rawAPI    *tg.Client
	connected atomic.Bool
	isPremium atomic.Bool
	isBot     atomic.Bool
	accountID atomic.Int64

	metaMu   sync.RWMutex
	username string

	peerMu    sync.RWMutex
	peerCache map[int64]*tg.InputPeerChannel
	userCache map[int64]*tg.InputPeerUser
}

// NewClient constructs an MTProto Client manager backed by `github.com/gotd/td`.
func NewClient(cfg ClientConfig, logger *slog.Logger) (*Client, error) {
	if logger == nil {
		logger = slog.Default()
	}
	if cfg.AppID <= 0 || strings.TrimSpace(cfg.AppHash) == "" {
		return nil, errors.New("telegram app_id and app_hash are required for MTProto")
	}
	if strings.TrimSpace(cfg.SessionFile) == "" {
		cfg.SessionFile = "data/telegram_session.json"
	}
	if dir := filepath.Dir(cfg.SessionFile); dir != "" && dir != "." {
		_ = os.MkdirAll(dir, 0o750)
	}
	if strings.TrimSpace(cfg.DeviceModel) == "" {
		cfg.DeviceModel = "Starfall-Go Debrid 2vCPU-4GB"
	}

	storage := &session.FileStorage{
		Path: filepath.Clean(cfg.SessionFile),
	}

	c := &Client{
		cfg:       cfg,
		logger:    logger,
		peerCache: make(map[int64]*tg.InputPeerChannel),
		userCache: make(map[int64]*tg.InputPeerUser),
	}

	opts := telegram.Options{
		SessionStorage: storage,
		UpdateHandler: telegram.UpdateHandlerFunc(func(ctx context.Context, u tg.UpdatesClass) error {
			c.harvestAccessHashes(u)
			if cfg.UpdateHandler != nil {
				return cfg.UpdateHandler.Handle(ctx, u)
			}
			return nil
		}),
		Device: telegram.DeviceConfig{
			DeviceModel:    cfg.DeviceModel,
			SystemVersion:  "Linux Ubuntu 24.04 LTS",
			AppVersion:     "1.2.0",
			SystemLangCode: "en",
			LangCode:       "en",
		},
		RetryInterval: 2 * time.Second,
		MaxRetries:    5,
	}

	c.tdClient = telegram.NewClient(cfg.AppID, strings.TrimSpace(cfg.AppHash), opts)
	return c, nil
}

// IsConnected reports whether the MTProto client is authenticated and live.
func (c *Client) IsConnected() bool {
	return c != nil && c.connected.Load()
}

// Username returns the authenticated bot or user `@username` without leading `@`.
func (c *Client) Username() string {
	c.metaMu.RLock()
	defer c.metaMu.RUnlock()
	return c.username
}

// RememberChannel caches a channel's MTProto `AccessHash` for instant peer resolution.
func (c *Client) RememberChannel(channelID, accessHash int64) {
	if channelID <= 0 {
		return
	}
	c.peerMu.Lock()
	c.peerCache[channelID] = &tg.InputPeerChannel{
		ChannelID:  channelID,
		AccessHash: accessHash,
	}
	c.peerMu.Unlock()
}

// RememberUser caches a user's MTProto `AccessHash` for replying to private messages/callbacks.
func (c *Client) RememberUser(userID, accessHash int64) {
	if userID <= 0 {
		return
	}
	c.peerMu.Lock()
	c.userCache[userID] = &tg.InputPeerUser{
		UserID:     userID,
		AccessHash: accessHash,
	}
	c.peerMu.Unlock()
}

// ResolveChatPeer resolves a positive user ID, `-chatID`, or `-100channelID` into `tg.InputPeerClass`.
func (c *Client) ResolveChatPeer(ctx context.Context, chatID int64) (tg.InputPeerClass, error) {
	if chatID > 0 {
		c.peerMu.RLock()
		uPeer, ok := c.userCache[chatID]
		c.peerMu.RUnlock()
		if ok {
			return uPeer, nil
		}
		return &tg.InputPeerUser{UserID: chatID}, nil
	}
	s := strconv.FormatInt(chatID, 10)
	if strings.HasPrefix(s, "-100") {
		chPeer, _, err := c.ResolveDumpChannel(ctx, s)
		return chPeer, err
	}
	return &tg.InputPeerChat{ChatID: -chatID}, nil
}

func (c *Client) harvestAccessHashes(u tg.UpdatesClass) {
	var chats []tg.ChatClass
	var users []tg.UserClass
	switch v := u.(type) {
	case *tg.Updates:
		chats = v.Chats
		users = v.Users
	case *tg.UpdatesCombined:
		chats = v.Chats
		users = v.Users
	}
	for _, ch := range chats {
		if channel, ok := ch.(*tg.Channel); ok && channel.ID > 0 {
			c.RememberChannel(channel.ID, channel.AccessHash)
		}
	}
	for _, usr := range users {
		if tu, ok := usr.(*tg.User); ok && tu.ID > 0 {
			c.RememberUser(tu.ID, tu.AccessHash)
		}
	}
}

// Run starts the persistent MTProto connection, authenticates via session, SessionString, or BotToken,
// dynamically inspects Telegram Premium status (4 GB vs 2 GB ceiling), and blocks until `ctx` cancels.
func (c *Client) Run(ctx context.Context, ready func(caps SessionCapabilities)) error {
	// Import Telethon/Pyrogram/gotd session string if provided and session file does not exist yet
	if strings.TrimSpace(c.cfg.SessionString) != "" {
		if _, err := os.Stat(c.cfg.SessionFile); os.IsNotExist(err) {
			if data, serr := session.TelethonSession(strings.TrimSpace(c.cfg.SessionString)); serr == nil {
				loader := session.Loader{Storage: &session.FileStorage{Path: filepath.Clean(c.cfg.SessionFile)}}
				_ = loader.Save(ctx, data)
			}
		}
	}

	return c.tdClient.Run(ctx, func(runCtx context.Context) error {
		api := c.tdClient.API()
		c.rawAPI = api

		status, err := c.tdClient.Auth().Status(runCtx)
		if err != nil {
			return fmt.Errorf("checking MTProto auth status: %w", err)
		}

		if !status.Authorized {
			if strings.TrimSpace(c.cfg.BotToken) == "" {
				return errors.New("MTProto session is not authorized and no bot_token was provided")
			}
			if _, err := c.tdClient.Auth().Bot(runCtx, strings.TrimSpace(c.cfg.BotToken)); err != nil {
				return fmt.Errorf("authenticating MTProto bot token: %w", err)
			}
		}

		caps, err := c.inspectSessionCapabilities(runCtx, api)
		if err != nil {
			return err
		}

		c.connected.Store(true)
		defer c.connected.Store(false)

		c.logger.Info("MTProto session connected",
			slog.Int64("account_id", caps.AccountID),
			slog.String("username", caps.Username),
			slog.Bool("is_bot", caps.IsBot),
			slog.Bool("is_premium", caps.IsPremium),
			slog.Int64("max_single_upload_mb", caps.MaxSingleUploadBytes/(1024*1024)),
		)

		if ready != nil {
			ready(caps)
		}

		<-runCtx.Done()
		return runCtx.Err()
	})
}

func (c *Client) inspectSessionCapabilities(ctx context.Context, api *tg.Client) (SessionCapabilities, error) {
	users, err := api.UsersGetUsers(ctx, []tg.InputUserClass{&tg.InputUserSelf{}})
	if err != nil || len(users) == 0 {
		return SessionCapabilities{}, fmt.Errorf("querying MTProto self user: %w", err)
	}

	selfUser, ok := users[0].(*tg.User)
	if !ok {
		return SessionCapabilities{}, errors.New("unexpected self user type from MTProto")
	}

	c.accountID.Store(selfUser.ID)
	c.isBot.Store(selfUser.Bot)
	isPrem := !selfUser.Bot && selfUser.Premium
	c.isPremium.Store(isPrem)

	c.metaMu.Lock()
	c.username = selfUser.Username
	c.metaMu.Unlock()

	return c.Capabilities(), nil
}

// Capabilities returns the current session's adaptive 2GB/4GB upload limits.
func (c *Client) Capabilities() SessionCapabilities {
	isPrem := c.isPremium.Load()
	maxBytes := FreeUploadCeilingBytes
	splitMB := FreeSplitTargetMB
	if isPrem {
		maxBytes = PremiumUploadCeilingBytes
		splitMB = PremiumSplitTargetMB
	}
	return SessionCapabilities{
		AccountID:            c.accountID.Load(),
		Username:             c.Username(),
		IsBot:                c.isBot.Load(),
		IsPremium:            isPrem,
		MaxSingleUploadBytes: maxBytes,
		SafeSplitSizeMB:      splitMB,
	}
}

// UploadCeilingBytes returns 4,000 MB if Telegram Premium, else 2,000 MB.
func (c *Client) UploadCeilingBytes() int64 {
	if c.isPremium.Load() {
		return PremiumUploadCeilingBytes
	}
	return FreeUploadCeilingBytes
}

// SafeSplitSizeMB returns 3900 MB for Premium sessions or 1950 MB for Free/Bot sessions.
func (c *Client) SafeSplitSizeMB() int {
	if c.isPremium.Load() {
		return PremiumSplitTargetMB
	}
	return FreeSplitTargetMB
}

// API returns the underlying `*tg.Client` for raw MTProto RPC calls.
func (c *Client) API() (*tg.Client, error) {
	if c == nil || !c.connected.Load() || c.rawAPI == nil {
		return nil, ErrClientNotConnected
	}
	return c.rawAPI, nil
}

// ResolveDumpTarget parses `-100xxxxxxxxxx` or `-100xxxxxxxxxx|topic_id` into `InputPeerChannel` + optional `threadID`.
func (c *Client) ResolveDumpTarget(ctx context.Context, rawTarget string) (*tg.InputPeerChannel, int64, int, error) {
	_, threadID, ok := models.ParseDumpChatTarget(rawTarget)
	if !ok {
		return nil, 0, 0, ErrInvalidDumpChatID
	}
	peer, chID, err := c.ResolveDumpChannel(ctx, rawTarget)
	if err != nil {
		return nil, 0, 0, err
	}
	return peer, chID, threadID, nil
}

// ResolveDumpChannel converts `-100xxxxxxxxxx` (or `-100xxxxxxxxxx|topic`) into a cached `*tg.InputPeerChannel`.
func (c *Client) ResolveDumpChannel(ctx context.Context, dumpChatID string) (*tg.InputPeerChannel, int64, error) {
	channelID, err := ParseTelegramChannelID(dumpChatID)
	if err != nil {
		return nil, 0, err
	}

	c.peerMu.RLock()
	cached, ok := c.peerCache[channelID]
	c.peerMu.RUnlock()
	if ok {
		return cached, channelID, nil
	}

	api, err := c.API()
	if err != nil {
		return nil, 0, err
	}

	chatsClass, err := api.ChannelsGetChannels(ctx, []tg.InputChannelClass{
		&tg.InputChannel{
			ChannelID:  channelID,
			AccessHash: 0,
		},
	})
	if err != nil {
		return nil, 0, fmt.Errorf("resolving dump channel %d via MTProto: %w", channelID, err)
	}

	var accessHash int64
	for _, ch := range chatsClass.GetChats() {
		if channel, ok := ch.(*tg.Channel); ok && channel.ID == channelID {
			accessHash = channel.AccessHash
			break
		}
	}

	peer := &tg.InputPeerChannel{
		ChannelID:  channelID,
		AccessHash: accessHash,
	}

	c.peerMu.Lock()
	c.peerCache[channelID] = peer
	c.peerMu.Unlock()

	return peer, channelID, nil
}

// ParseTelegramChannelID strips `-100` and any optional `|topic_id` suffix to yield the raw MTProto `channel_id`.
func ParseTelegramChannelID(dumpChatID string) (int64, error) {
	trimmed := strings.TrimSpace(dumpChatID)
	if idx := strings.Index(trimmed, "|"); idx != -1 {
		trimmed = strings.TrimSpace(trimmed[:idx])
	}
	if !strings.HasPrefix(trimmed, "-100") {
		return 0, ErrInvalidDumpChatID
	}
	rawDigits := strings.TrimPrefix(trimmed, "-100")
	id, err := strconv.ParseInt(rawDigits, 10, 64)
	if err != nil || id <= 0 {
		return 0, ErrInvalidDumpChatID
	}
	return id, nil
}
