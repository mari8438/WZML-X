// Package telegram implements the Silent Telegram Cloud Vault & Adaptive 2GB/4GB
// Direct Dump Uploader over MTProto (`github.com/gotd/td`) for Starfall-Go Debrid.
//
// Core Design Guarantees:
//   - 100% Silent Operation: Pushes completed files directly into the user's
//     `TelegramDumpChat` (-100xxxxxxxxxx) without bot command spam.
//   - Adaptive 2GB / 4GB Ceiling: Dynamically inspects session status to enforce
//     a 2,000 MB ceiling for Free/Bot sessions and a 4,000 MB ceiling for Telegram
//     Premium user sessions.
package telegram

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/gotd/td/session"
	"github.com/gotd/td/telegram"
	"github.com/gotd/td/tg"
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
	ErrInvalidDumpChatID = errors.New("invalid telegram_dump_chat ID: expected -100xxxxxxxxxx")
)

// ClientConfig holds MTProto API credentials and session persistence paths.
type ClientConfig struct {
	AppID         int
	AppHash       string
	BotToken      string
	SessionString string
	SessionFile   string
	DeviceModel   string
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
// and resolves `-100xxxxxxxxxx` channel IDs into `tg.InputPeerClass` targets.
type Client struct {
	cfg        ClientConfig
	logger     *slog.Logger
	tdClient   *telegram.Client
	rawAPI     *tg.Client
	connected  atomic.Bool
	isPremium  atomic.Bool
	isBot      atomic.Bool
	accountID  atomic.Int64

	peerMu    sync.RWMutex
	peerCache map[int64]*tg.InputPeerChannel
}

// NewClient constructs an MTProto Client manager backed by `github.com/gotd/td`.
func NewClient(cfg ClientConfig, logger *slog.Logger) (*Client, error) {
	if cfg.AppID <= 0 || strings.TrimSpace(cfg.AppHash) == "" {
		return nil, errors.New("telegram app_id and app_hash are required for MTProto")
	}
	if strings.TrimSpace(cfg.SessionFile) == "" {
		cfg.SessionFile = "data/telegram_session.json"
	}
	if strings.TrimSpace(cfg.DeviceModel) == "" {
		cfg.DeviceModel = "Starfall-Go Debrid 2vCPU-4GB"
	}

	storage := &session.FileStorage{
		Path: filepath.Clean(cfg.SessionFile),
	}

	opts := telegram.Options{
		SessionStorage: storage,
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

	tdCli := telegram.NewClient(cfg.AppID, strings.TrimSpace(cfg.AppHash), opts)

	return &Client{
		cfg:       cfg,
		logger:    logger,
		tdClient:  tdCli,
		peerCache: make(map[int64]*tg.InputPeerChannel),
	}, nil
}

// Run starts the persistent MTProto connection, authenticates via session or BotToken,
// dynamically inspects whether the session has Telegram Premium (4 GB vs 2 GB ceiling),
// and blocks until `ctx` is canceled.
func (c *Client) Run(ctx context.Context, ready func(caps SessionCapabilities)) error {
	return c.tdClient.Run(ctx, func(runCtx context.Context) error {
		api := c.tdClient.API()
		c.rawAPI = api

		status, err := c.tdClient.Auth().Status(runCtx)
		if err != nil {
			return fmt.Errorf("checking MTProto auth status: %w", err)
		}

		if !status.Authorized {
			if strings.TrimSpace(c.cfg.BotToken) == "" {
				return errors.New("MTProto session is not authorized and no bot_token fallback was provided")
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

		c.logger.Info("MTProto silent cloud vault initialized",
			slog.Int64("account_id", caps.AccountID),
			slog.String("username", caps.Username),
			slog.Bool("is_bot", caps.IsBot),
			slog.Bool("is_premium", caps.IsPremium),
			slog.Int64("max_single_upload_mb", caps.MaxSingleUploadBytes/(1024*1024)),
			slog.Int("safe_split_mb", caps.SafeSplitSizeMB),
		)

		if ready != nil {
			ready(caps)
		}

		<-runCtx.Done()
		return runCtx.Err()
	})
}

// inspectSessionCapabilities queries `users.getUsers` (`InputUserSelf`) to detect
// whether the active MTProto session is a Telegram Premium user (4,000 MB limit)
// or a Free / Standard Bot account (2,000 MB limit).
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
	// Telegram Premium status is only active on non-bot user accounts with selfUser.Premium == true.
	isPrem := !selfUser.Bot && selfUser.Premium
	c.isPremium.Store(isPrem)

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
		IsBot:                c.isBot.Load(),
		IsPremium:            isPrem,
		MaxSingleUploadBytes: maxBytes,
		SafeSplitSizeMB:      splitMB,
	}
}

// UploadCeilingBytes returns 4,000 MB if the active session is Telegram Premium,
// or 2,000 MB for Free / Standard Bot sessions.
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
	if !c.connected.Load() || c.rawAPI == nil {
		return nil, ErrClientNotConnected
	}
	return c.rawAPI, nil
}

// ResolveDumpChannel converts a `-100xxxxxxxxxx` channel ID string into a cached
// `*tg.InputPeerChannel` (fetching the channel's `AccessHash` via `channels.getChannels`).
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

// ParseTelegramChannelID strips the `-100` Bot API prefix from a supergroup/channel ID
// (e.g. `-1002345678901` -> `2345678901`) to yield the raw MTProto `channel_id`.
func ParseTelegramChannelID(dumpChatID string) (int64, error) {
	trimmed := strings.TrimSpace(dumpChatID)
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
