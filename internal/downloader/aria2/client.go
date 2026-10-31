// Package aria2 implements a supervised, RAM-capped Aria2c JSON-RPC 2.0 client
// (`--disk-cache=16M --bt-max-peers=30 --seed-time=0 --file-allocation=none`)
// supporting magnet links, `.torrent` files/URLs, interactive selective file filtering,
// pause/resume/cancel, and real-time speed/peer telemetry for Starfall-Go.
package aria2

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/starfall-org/starfall-go/internal/ipc"
	"github.com/starfall-org/starfall-go/internal/models"
	"github.com/starfall-org/starfall-go/internal/safety"
)

// Config configures the embedded Aria2c daemon and JSON-RPC client.
type Config struct {
	Enabled         bool
	BinaryPath      string
	RPCPort         int
	RPCSecret       string
	MaxPeersPerTask int
	DownloadDir     string
}

// TorrentFileEntry describes an individual file inside a torrent/magnet metadata tree.
type TorrentFileEntry struct {
	Index     int    `json:"index"`
	Path      string `json:"path"`
	Name      string `json:"name"`
	SizeBytes int64  `json:"size_bytes"`
	Selected  bool   `json:"selected"`
}

// Client supervises a local `aria2c` RPC subprocess and executes BitTorrent/Magnet transfers.
type Client struct {
	cfg        Config
	httpClient *http.Client
	rpcURL     string
	tokenParam string
	bus        *ipc.Bus
	logger     *slog.Logger

	mu      sync.Mutex
	cmd     *exec.Cmd
	running atomic.Bool
}

// NewClient constructs an Aria2 JSON-RPC manager.
func NewClient(cfg Config, bus *ipc.Bus, logger *slog.Logger) *Client {
	if logger == nil {
		logger = slog.Default()
	}
	if strings.TrimSpace(cfg.BinaryPath) == "" {
		cfg.BinaryPath = "aria2c"
	}
	if cfg.RPCPort <= 0 {
		cfg.RPCPort = 6800
	}
	if strings.TrimSpace(cfg.RPCSecret) == "" {
		cfg.RPCSecret = "starfall_aria2_rpc_local"
	}
	if cfg.MaxPeersPerTask <= 0 || cfg.MaxPeersPerTask > 30 {
		cfg.MaxPeersPerTask = 30
	}
	return &Client{
		cfg:        cfg,
		httpClient: &http.Client{Timeout: 10 * time.Second},
		rpcURL:     fmt.Sprintf("http://127.0.0.1:%d/jsonrpc", cfg.RPCPort),
		tokenParam: "token:" + cfg.RPCSecret,
		bus:        bus,
		logger:     logger,
	}
}

// Start launches and supervises the local `aria2c` RPC daemon if `aria2c` is installed.
func (c *Client) Start(ctx context.Context) error {
	if !c.cfg.Enabled {
		return nil
	}
	if _, err := exec.LookPath(c.cfg.BinaryPath); err != nil {
		c.logger.Warn("aria2c binary not found in PATH; torrent/magnet engine disabled", slog.String("binary", c.cfg.BinaryPath))
		return nil
	}

	_ = os.MkdirAll(c.cfg.DownloadDir, 0o755)

	go c.superviseLoop(ctx)

	// Wait up to 3 seconds for RPC readiness
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if _, err := c.GetVersion(ctx); err == nil {
			c.running.Store(true)
			return nil
		}
		time.Sleep(150 * time.Millisecond)
	}
	return nil
}

func (c *Client) superviseLoop(ctx context.Context) {
	args := []string{
		"--enable-rpc=true",
		"--rpc-listen-all=false",
		fmt.Sprintf("--rpc-listen-port=%d", c.cfg.RPCPort),
		fmt.Sprintf("--rpc-secret=%s", c.cfg.RPCSecret),
		"--daemon=false",
		"--quiet=true",
		"--file-allocation=none",
		"--disk-cache=32M",
		"--continue=true",
		"--max-concurrent-downloads=3",
		"--max-connection-per-server=8",
		"--split=8",
		"--min-split-size=4M",
		fmt.Sprintf("--bt-max-peers=%d", c.cfg.MaxPeersPerTask),
		"--bt-request-peer-speed-limit=256K",
		"--max-overall-upload-limit=64K",
		"--seed-time=0",
		"--bt-detach-seed-only=true",
		"--follow-torrent=mem",
		"--bt-save-metadata=false",
		"--allow-overwrite=true",
		"--auto-file-renaming=false",
		"--summary-interval=0",
	}

	for {
		if ctx.Err() != nil {
			return
		}
		cmd := safety.NewSupervisedCommand(ctx, c.cfg.BinaryPath, args...)
		c.mu.Lock()
		c.cmd = cmd
		c.mu.Unlock()

		c.logger.Info("starting supervised aria2c RPC daemon", slog.Int("port", c.cfg.RPCPort))
		err := cmd.Run()
		c.running.Store(false)
		if ctx.Err() != nil {
			return
		}
		c.logger.Warn("aria2c daemon exited; restarting in 2s", slog.Any("error", err))
		select {
		case <-ctx.Done():
			return
		case <-time.After(2 * time.Second):
		}
	}
}

// IsMagnetOrTorrent reports whether `rawURI` is a magnet link or `.torrent` URL/path.
func IsMagnetOrTorrent(rawURI string) bool {
	trimmed := strings.TrimSpace(rawURI)
	low := strings.ToLower(trimmed)
	if strings.HasPrefix(low, "magnet:?") {
		return true
	}
	if idx := strings.Index(low, "?"); idx != -1 {
		low = low[:idx]
	}
	return strings.HasSuffix(low, ".torrent")
}

// GetVersion pings `aria2.getVersion` to verify RPC liveness.
func (c *Client) GetVersion(ctx context.Context) (string, error) {
	var res struct {
		Version string `json:"version"`
	}
	if err := c.callRPC(ctx, "aria2.getVersion", nil, &res); err != nil {
		return "", err
	}
	c.running.Store(true)
	return res.Version, nil
}

// Download executes a Magnet or `.torrent` download into `destDir`, supporting interactive
// file selection (`task.SelectFiles`), pause/resume, and real-time speed/seeder updates.
func (c *Client) Download(ctx context.Context, task *models.Task, rawURI string, destDir string) (string, int64, error) {
	if _, err := c.GetVersion(ctx); err != nil {
		return "", 0, fmt.Errorf("aria2 RPC daemon unavailable: %w", err)
	}
	if err := os.MkdirAll(destDir, 0o755); err != nil {
		return "", 0, err
	}

	opts := map[string]any{
		"dir":       destDir,
		"seed-time": "0",
	}

	var gid string
	var err error
	if !strings.HasPrefix(strings.ToLower(rawURI), "magnet:") && !strings.HasPrefix(strings.ToLower(rawURI), "http") {
		// Local `.torrent` file on disk
		rawTorrent, rerr := os.ReadFile(rawURI)
		if rerr != nil {
			return "", 0, fmt.Errorf("reading torrent file: %w", rerr)
		}
		b64 := base64.StdEncoding.EncodeToString(rawTorrent)
		err = c.callRPC(ctx, "aria2.addTorrent", []any{b64, []string{}, opts}, &gid)
	} else {
		err = c.callRPC(ctx, "aria2.addUri", []any{[]string{rawURI}, opts}, &gid)
	}
	if err != nil {
		return "", 0, fmt.Errorf("adding torrent/magnet to aria2: %w", err)
	}

	task.SetAria2GID(gid)
	task.SetEngineName("Aria2")
	defer func() {
		bg, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		_ = c.RemoveDownload(bg, gid)
	}()

	selectionApplied := !task.SelectFiles
	wasPaused := false
	ticker := time.NewTicker(1 * time.Second)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return "", 0, ctx.Err()
		case <-ticker.C:
			// Synchronize task pause/resume state with Aria2
			if task.IsPaused() && !wasPaused {
				_ = c.callRPC(ctx, "aria2.pause", []any{gid}, nil)
				wasPaused = true
			} else if !task.IsPaused() && wasPaused {
				_ = c.callRPC(ctx, "aria2.unpause", []any{gid}, nil)
				wasPaused = false
			}

			st, serr := c.tellStatus(ctx, gid)
			if serr != nil {
				continue
			}

			// Follow magnet metadata GID transition (`followedBy` once metadata resolves!)
			if len(st.FollowedBy) > 0 && st.FollowedBy[0] != "" && st.FollowedBy[0] != gid {
				gid = st.FollowedBy[0]
				task.SetAria2GID(gid)
				st, serr = c.tellStatus(ctx, gid)
				if serr != nil {
					continue
				}
			}

			// Update display name once metadata arrives
			if st.Bittorrent.Info.Name != "" {
				task.SetName(st.Bittorrent.Info.Name)
			} else if len(st.Files) > 0 && st.Files[0].Path != "" && !strings.HasPrefix(filepath.Base(st.Files[0].Path), "[METADATA]") {
				task.SetName(filepath.Base(st.Files[0].Path))
			}

			// Handle interactive `-s` selective file picking once real file list is available
			if !selectionApplied && len(st.Files) > 0 && !strings.HasPrefix(filepath.Base(st.Files[0].Path), "[METADATA]") {
				_ = c.callRPC(ctx, "aria2.pause", []any{gid}, nil)
				task.SetStageDetail("Waiting for file selection (/app/files)")
				selectedIndices, werr := task.WaitForFileSelection(ctx, 5*time.Minute)
				if werr != nil {
					return "", 0, werr
				}
				if len(selectedIndices) > 0 {
					_ = c.SelectFiles(ctx, gid, selectedIndices)
				}
				_ = c.callRPC(ctx, "aria2.unpause", []any{gid}, nil)
				task.SetStageDetail("")
				selectionApplied = true
			}

			totalLen, _ := strconv.ParseInt(st.TotalLength, 10, 64)
			compLen, _ := strconv.ParseInt(st.CompletedLength, 10, 64)
			dlSpd, _ := strconv.ParseInt(st.DownloadSpeed, 10, 64)
			conns, _ := strconv.Atoi(st.Connections)
			seeders, _ := strconv.Atoi(st.NumSeeders)

			if totalLen > 0 {
				task.SetTotalBytes(totalLen)
			}
			task.UpdatePeerCounts(seeders, conns)
			stats := task.UpdateSpeed(compLen, dlSpd, 0, conns)

			if c.bus != nil {
				c.bus.Publish(ipc.Event{
					Type:      ipc.EventTaskProgress,
					TaskID:    task.ID,
					UserID:    task.UserID,
					Progress:  stats.ProgressPercent,
					SpeedBps:  stats.DownloadSpeedBps,
					Timestamp: time.Now().UTC(),
					Metadata: map[string]any{
						"engine":           "Aria2",
						"bytes_downloaded": compLen,
						"total_bytes":      totalLen,
						"seeders":          seeders,
						"peers":            conns,
						"percent":          stats.ProgressPercent,
					},
				})
			}

			switch st.Status {
			case "complete":
				// If single file downloaded, return its direct path; otherwise return `destDir`
				if len(st.Files) == 1 && st.Files[0].Path != "" {
					if fi, ferr := os.Stat(st.Files[0].Path); ferr == nil {
						return st.Files[0].Path, fi.Size(), nil
					}
				}
				return destDir, compLen, nil

			case "error":
				errMsg := st.ErrorMessage
				if errMsg == "" {
					errMsg = "aria2 download failed (code " + st.ErrorCode + ")"
				}
				return "", 0, errors.New(errMsg)

			case "removed":
				return "", 0, context.Canceled
			}
		}
	}
}

// ListFiles queries `aria2.getFiles` for an active GID to populate the Web Torrent File Selector UI.
func (c *Client) ListFiles(ctx context.Context, gid string) ([]TorrentFileEntry, error) {
	if strings.TrimSpace(gid) == "" {
		return nil, errors.New("empty aria2 gid")
	}
	var rawFiles []struct {
		Index    string `json:"index"`
		Path     string `json:"path"`
		Length   string `json:"length"`
		Selected string `json:"selected"`
	}
	if err := c.callRPC(ctx, "aria2.getFiles", []any{gid}, &rawFiles); err != nil {
		return nil, err
	}

	out := make([]TorrentFileEntry, 0, len(rawFiles))
	for _, rf := range rawFiles {
		idx, _ := strconv.Atoi(rf.Index)
		sz, _ := strconv.ParseInt(rf.Length, 10, 64)
		out = append(out, TorrentFileEntry{
			Index:     idx,
			Path:      rf.Path,
			Name:      filepath.Base(rf.Path),
			SizeBytes: sz,
			Selected:  strings.EqualFold(rf.Selected, "true"),
		})
	}
	return out, nil
}

// SelectFiles applies Aria2 `select-file` (1-based comma-separated indices) onto `gid`.
func (c *Client) SelectFiles(ctx context.Context, gid string, indices []int) error {
	if len(indices) == 0 {
		return nil
	}
	parts := make([]string, len(indices))
	for i, idx := range indices {
		parts[i] = strconv.Itoa(idx)
	}
	opts := map[string]string{
		"select-file": strings.Join(parts, ","),
	}
	return c.callRPC(ctx, "aria2.changeOption", []any{gid, opts}, nil)
}

// RemoveDownload force-removes an active or completed GID from Aria2 memory.
func (c *Client) RemoveDownload(ctx context.Context, gid string) error {
	if strings.TrimSpace(gid) == "" {
		return nil
	}
	_ = c.callRPC(ctx, "aria2.forceRemove", []any{gid}, nil)
	_ = c.callRPC(ctx, "aria2.removeDownloadResult", []any{gid}, nil)
	return nil
}

type aria2StatusResp struct {
	GID             string   `json:"gid"`
	Status          string   `json:"status"`
	TotalLength     string   `json:"totalLength"`
	CompletedLength string   `json:"completedLength"`
	DownloadSpeed   string   `json:"downloadSpeed"`
	Connections     string   `json:"connections"`
	NumSeeders      string   `json:"numSeeders"`
	FollowedBy      []string `json:"followedBy"`
	ErrorCode       string   `json:"errorCode"`
	ErrorMessage    string   `json:"errorMessage"`
	Bittorrent      struct {
		Info struct {
			Name string `json:"name"`
		} `json:"info"`
	} `json:"bittorrent"`
	Files []struct {
		Index    string `json:"index"`
		Path     string `json:"path"`
		Length   string `json:"length"`
		Selected string `json:"selected"`
	} `json:"files"`
}

func (c *Client) tellStatus(ctx context.Context, gid string) (*aria2StatusResp, error) {
	var resp aria2StatusResp
	if err := c.callRPC(ctx, "aria2.tellStatus", []any{gid}, &resp); err != nil {
		return nil, err
	}
	return &resp, nil
}

func (c *Client) callRPC(ctx context.Context, method string, params []any, out any) error {
	allParams := make([]any, 0, len(params)+1)
	allParams = append(allParams, c.tokenParam)
	allParams = append(allParams, params...)

	payload := map[string]any{
		"jsonrpc": "2.0",
		"id":      "sf",
		"method":  method,
		"params":  allParams,
	}
	bodyBytes, err := json.Marshal(payload)
	if err != nil {
		return err
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.rpcURL, bytes.NewReader(bodyBytes))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	var rpcEnvelope struct {
		Result json.RawMessage `json:"result"`
		Error  *struct {
			Code    int    `json:"code"`
			Message string `json:"message"`
		} `json:"error"`
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, 512*1024)).Decode(&rpcEnvelope); err != nil {
		return err
	}
	if rpcEnvelope.Error != nil {
		return fmt.Errorf("aria2 rpc %s error %d: %s", method, rpcEnvelope.Error.Code, rpcEnvelope.Error.Message)
	}
	if out != nil && len(rpcEnvelope.Result) > 0 {
		return json.Unmarshal(rpcEnvelope.Result, out)
	}
	return nil
}
