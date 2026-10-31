package qbit

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/cookiejar"
	"net/url"
	"strings"
	"sync"
	"time"
)

var (
	ErrQbitAuthFailed   = errors.New("qbit: authentication failed")
	ErrQbitTorrentEmpty = errors.New("qbit: no torrent found matching query")
)

// TorrentInfo represents torrent status returned by qBittorrent WebAPI.
type TorrentInfo struct {
	Hash       string  `json:"hash"`
	Name       string  `json:"name"`
	Size       int64   `json:"size"`
	Progress   float64 `json:"progress"`
	Dlspeed    int64   `json:"dlspeed"`
	Upspeed    int64   `json:"upspeed"`
	NumSeeds   int     `json:"num_seeds"`
	NumLeechs  int     `json:"num_leechs"`
	State      string  `json:"state"`
	Eta        int64   `json:"eta"`
	SavePath   string  `json:"save_path"`
	TotalDone  int64   `json:"total_done"`
}

// Config configures the qBittorrent WebAPI client.
type Config struct {
	BaseURL  string
	Username string
	Password string
	Timeout  time.Duration
}

// Client interacts with the qBittorrent WebAPI v2.
type Client struct {
	cfg        Config
	httpClient *http.Client
	mu         sync.Mutex
	loggedIn   bool
}

// NewClient initializes a qBittorrent WebAPI client.
func NewClient(cfg Config) (*Client, error) {
	jar, err := cookiejar.New(nil)
	if err != nil {
		return nil, fmt.Errorf("creating cookie jar: %w", err)
	}

	timeout := cfg.Timeout
	if timeout <= 0 {
		timeout = 10 * time.Second
	}

	baseURL := strings.TrimRight(cfg.BaseURL, "/")
	if baseURL == "" {
		baseURL = "http://127.0.0.1:8090"
	}

	return &Client{
		cfg: Config{
			BaseURL:  baseURL,
			Username: cfg.Username,
			Password: cfg.Password,
			Timeout:  timeout,
		},
		httpClient: &http.Client{
			Jar:     jar,
			Timeout: timeout,
		},
	}, nil
}

// Login authenticates with the qBittorrent WebAPI session endpoint.
func (c *Client) Login(ctx context.Context) error {
	c.mu.Lock()
	defer c.mu.Unlock()

	data := url.Values{}
	data.Set("username", c.cfg.Username)
	data.Set("password", c.cfg.Password)

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.cfg.BaseURL+"/api/v2/auth/login", strings.NewReader(data.Encode()))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("qbit login request failed: %w", err)
	}
	defer resp.Body.Close()

	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusOK || strings.TrimSpace(string(body)) != "Ok." {
		return ErrQbitAuthFailed
	}

	c.loggedIn = true
	return nil
}

// AddTorrent enqueues a magnet link or HTTP torrent URL into qBittorrent.
func (c *Client) AddTorrent(ctx context.Context, uri, savePath string) error {
	data := url.Values{}
	data.Set("urls", uri)
	if savePath != "" {
		data.Set("savepath", savePath)
	}
	data.Set("autoTMM", "false")

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.cfg.BaseURL+"/api/v2/torrents/add", strings.NewReader(data.Encode()))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("adding torrent to qbit: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("qbit add torrent returned status %d", resp.StatusCode)
	}
	return nil
}

// GetTorrent fetches status details for a given torrent info-hash.
func (c *Client) GetTorrent(ctx context.Context, hash string) (*TorrentInfo, error) {
	reqURL := fmt.Sprintf("%s/api/v2/torrents/info?hashes=%s", c.cfg.BaseURL, hash)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, reqURL, nil)
	if err != nil {
		return nil, err
	}

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("querying torrent info: %w", err)
	}
	defer resp.Body.Close()

	var list []TorrentInfo
	if err := json.NewDecoder(resp.Body).Decode(&list); err != nil {
		return nil, fmt.Errorf("decoding qbit torrent info: %w", err)
	}

	if len(list) == 0 {
		return nil, ErrQbitTorrentEmpty
	}
	return &list[0], nil
}

// DeleteTorrent deletes a torrent, optionally removing downloaded files from disk.
func (c *Client) DeleteTorrent(ctx context.Context, hash string, deleteFiles bool) error {
	data := url.Values{}
	data.Set("hashes", hash)
	data.Set("deleteFiles", fmt.Sprintf("%t", deleteFiles))

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.cfg.BaseURL+"/api/v2/torrents/delete", strings.NewReader(data.Encode()))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("deleting torrent: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("qbit delete torrent returned status %d", resp.StatusCode)
	}
	return nil
}
