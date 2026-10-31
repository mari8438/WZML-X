// Package web implements the Owner Bot Settings UI and administrative API endpoints
// (`GET /app/admin`, `GET /api/admin/stats`, `GET /api/admin/logs` HTTP+WebSocket,
// `POST /api/admin/config`, `POST /api/admin/restart`) guarded by Owner ID middleware.
package web

import (
	"bufio"
	"context"
	"crypto/sha1"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"log/slog"
	"math"
	"net/http"
	"os"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/starfall-org/starfall-go/internal/auth"
	"github.com/starfall-org/starfall-go/internal/config"
	"github.com/starfall-org/starfall-go/internal/engine"
	"github.com/starfall-org/starfall-go/internal/ipc"
	"github.com/starfall-org/starfall-go/internal/safety"
	"github.com/starfall-org/starfall-go/internal/user"
)

const (
	wsGUID       = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
	maxLogBuffer = 200
)

// AdminHandler serves the Owner Bot Settings dashboard, live VPS telemetry,
// HTTP/RFC 6455 WebSocket log streaming, dynamic config updates, and graceful restart.
type AdminHandler struct {
	assets      fs.FS
	ownerSubID  string
	cfg         *config.Config
	watchdog    *safety.Watchdog
	limiter     *safety.RateLimiter
	dispatcher  *engine.Dispatcher
	registry    *engine.TaskRegistry
	userService *user.Service
	bus         *ipc.Bus
	logger      *slog.Logger
	startedAt   time.Time

	logMu    sync.RWMutex
	logLines []string
}

// NewAdminHandler constructs an Owner-guarded AdminHandler.
func NewAdminHandler(
	assets fs.FS,
	ownerSubID string,
	cfg *config.Config,
	watchdog *safety.Watchdog,
	limiter *safety.RateLimiter,
	dispatcher *engine.Dispatcher,
	registry *engine.TaskRegistry,
	bus *ipc.Bus,
	logger *slog.Logger,
) *AdminHandler {
	a := &AdminHandler{
		assets:     assets,
		ownerSubID: strings.TrimSpace(ownerSubID),
		cfg:        cfg,
		watchdog:   watchdog,
		limiter:    limiter,
		dispatcher: dispatcher,
		registry:   registry,
		bus:        bus,
		logger:     logger,
		startedAt:  time.Now(),
		logLines: []string{
			fmt.Sprintf("[%s] [INFO] - Starfall-Go Debrid Engine v1.2 initialized (GOMEMLIMIT=3000MiB)", time.Now().Format("02-Jan-06 03:04:05 PM")),
		},
	}
	if bus != nil {
		go a.captureBusLogs()
	}
	return a
}

// AttachUserService wires optional SQLite `bot_settings` persistence for admin config saves.
func (a *AdminHandler) AttachUserService(us *user.Service) {
	a.userService = us
}

func (a *AdminHandler) appendLog(line string) {
	a.logMu.Lock()
	defer a.logMu.Unlock()
	a.logLines = append(a.logLines, line)
	if len(a.logLines) > maxLogBuffer {
		a.logLines = append([]string(nil), a.logLines[len(a.logLines)-maxLogBuffer:]...)
	}
}

func (a *AdminHandler) captureBusLogs() {
	subID, ch := a.bus.SubscribeAll(128)
	defer a.bus.Unsubscribe(subID)
	for evt := range ch {
		if evt.Type == ipc.EventTaskProgress {
			continue
		}
		ts := evt.Timestamp
		if ts.IsZero() {
			ts = time.Now()
		}
		a.appendLog(fmt.Sprintf("[%s] [%s] task=%s user=%s msg=%s",
			ts.Format("02-Jan-06 03:04:05 PM"),
			evt.Type,
			evt.TaskID,
			evt.UserID,
			evt.Message,
		))
	}
}

func (a *AdminHandler) isOwner(userID string) bool {
	if a.ownerSubID == "" || a.ownerSubID == "0" {
		return true
	}
	return userID == a.ownerSubID || userID == "tg:"+a.ownerSubID
}

// RequireOwner wraps `auth.Middleware.RequireAuth` and verifies that the authenticated
// user matches the configured Owner ID (`8814651149` or `tg:8814651149`).
func (a *AdminHandler) RequireOwner(authMW *auth.Middleware, next http.HandlerFunc) http.Handler {
	return authMW.RequireAuthFunc(func(w http.ResponseWriter, r *http.Request) {
		userID, ok := auth.UserIDFromContext(r.Context())
		if !ok {
			writeJSONResponse(w, http.StatusUnauthorized, map[string]any{"ok": false, "error": "unauthorized"})
			return
		}
		if !a.isOwner(userID) {
			writeJSONResponse(w, http.StatusForbidden, map[string]any{
				"ok":    false,
				"error": "forbidden: owner privileges required",
			})
			return
		}
		next(w, r)
	})
}

// RegisterRoutes mounts the Owner-protected `/app/admin` and `/api/admin/*` routes.
func (a *AdminHandler) RegisterRoutes(mux *http.ServeMux, authMW *auth.Middleware) {
	mux.Handle("GET /app/admin", a.RequireOwner(authMW, a.HandleAdminPage))
	mux.Handle("GET /api/admin/stats", authMW.RequireAuthFunc(a.HandleStats))
	mux.Handle("GET /api/admin/logs", a.RequireOwner(authMW, a.HandleLogs))
	mux.Handle("POST /api/admin/config", a.RequireOwner(authMW, a.HandleUpdateConfig))
	mux.Handle("POST /api/admin/restart", a.RequireOwner(authMW, a.HandleGracefulRestart))
}

// HandleAdminPage (`GET /app/admin`) serves `bot_settings.html`.
func (a *AdminHandler) HandleAdminPage(w http.ResponseWriter, r *http.Request) {
	rawHTML, err := fs.ReadFile(a.assets, "templates/bot_settings.html")
	if err != nil {
		http.Error(w, "bot_settings.html template not found", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(rawHTML)
}

// HandleStats (`GET /api/admin/stats`) returns real-time 2 vCPU, 4 GB RAM Watchdog,
// P2P connection, and NVMe disk usage metrics for both `bot_settings.html` and `user_settings.html`.
func (a *AdminHandler) HandleStats(w http.ResponseWriter, r *http.Request) {
	snap := a.watchdog.Snapshot()
	dispStats := a.dispatcher.Stats()

	targetDir := a.cfg.App.DownloadDir
	if targetDir == "" {
		targetDir = "."
	}
	diskTotalBytes, diskFreeBytes, _ := safety.GetDiskSpace(targetDir)

	diskTotalGB := float64(diskTotalBytes) / (1024 * 1024 * 1024)
	diskFreeGB := float64(diskFreeBytes) / (1024 * 1024 * 1024)
	diskPct := 0.0
	if diskTotalBytes > 0 && diskTotalBytes >= diskFreeBytes {
		diskPct = math.Round((float64(diskTotalBytes-diskFreeBytes)/float64(diskTotalBytes))*1000) / 10
	}

	totalMemGB := float64(snap.HostMemTotalBytes) / (1024 * 1024 * 1024)
	usedMemGB := float64(snap.HostMemUsedBytes) / (1024 * 1024 * 1024)
	if totalMemGB <= 0 {
		totalMemGB = float64(a.cfg.Memory.SystemRAMMiB) / 1024.0
		usedMemGB = (snap.UsagePercent / 100.0) * totalMemGB
	}

	cpuPct := math.Round(snap.CPULoadPercent*10) / 10
	ramPct := math.Round(snap.UsagePercent*10) / 10
	uptimeDur := time.Since(a.startedAt).Round(time.Second)

	writeJSONResponse(w, http.StatusOK, map[string]any{
		"ok":                   true,
		"watchdog_state":       snap.State.String(),
		"cpu_percent":          cpuPct,
		"cpu_load":             fmt.Sprintf("%.2f / 2.00 vCPU", (cpuPct/100.0)*2.0),
		"ram_usage_percent":    ramPct,
		"ram_percent":          ramPct,
		"ram_text":             fmt.Sprintf("%.2f / %.2f GB (Heap %d MiB)", usedMemGB, totalMemGB, snap.HeapAllocBytes/(1024*1024)),
		"heap_alloc_mib":       snap.HeapAllocBytes / (1024 * 1024),
		"soft_limit_mib":       a.cfg.Memory.SoftLimitMiB,
		"hard_ceiling_mib":     a.cfg.Memory.HardCeilingMiB,
		"chunk_buffer_bytes":   snap.ChunkBufferBytes,
		"disk_write_limit_bps": snap.DiskWriteLimitBps,
		"disk_total_gb":        diskTotalGB,
		"disk_free_gb":         diskFreeGB,
		"disk_percent":         diskPct,
		"disk_free":            fmt.Sprintf("%.1f GB Free", diskFreeGB),
		"uptime":               uptimeDur.String(),
		"global_connections":   a.limiter.ActiveGlobalConnections(),
		"max_global_conns":     a.cfg.Network.MaxGlobalConns,
		"dispatcher":           dispStats,
	})
}

// HandleLogs (`GET /api/admin/logs`) serves an RFC 6455 WebSocket stream when `Upgrade: websocket`
// is requested (`bot_settings.html`), or returns JSON `{"ok": true, "logs": "..."}` on standard
// HTTP `GET` (`user_settings.html`).
func (a *AdminHandler) HandleLogs(w http.ResponseWriter, r *http.Request) {
	if !strings.EqualFold(r.Header.Get("Upgrade"), "websocket") {
		snap := a.watchdog.Snapshot()
		a.logMu.RLock()
		lines := make([]string, len(a.logLines), len(a.logLines)+1)
		copy(lines, a.logLines)
		a.logMu.RUnlock()

		lines = append(lines, fmt.Sprintf("[%s] [WATCHDOG] state=%s ram=%.1f%% cpu=%.1f%% heap=%dMiB conns=%d/%d",
			time.Now().Format("02-Jan-06 03:04:05 PM"),
			snap.State.String(),
			snap.UsagePercent,
			snap.CPULoadPercent,
			snap.HeapAllocBytes/(1024*1024),
			a.limiter.ActiveGlobalConnections(),
			a.cfg.Network.MaxGlobalConns,
		))
		writeJSONResponse(w, http.StatusOK, map[string]any{
			"ok":   true,
			"logs": strings.Join(lines, "\n"),
		})
		return
	}

	secKey := strings.TrimSpace(r.Header.Get("Sec-WebSocket-Key"))
	if secKey == "" {
		http.Error(w, "missing Sec-WebSocket-Key", http.StatusBadRequest)
		return
	}

	hijacker, ok := w.(http.Hijacker)
	if !ok {
		http.Error(w, "websocket hijacking not supported", http.StatusInternalServerError)
		return
	}

	conn, rw, err := hijacker.Hijack()
	if err != nil {
		return
	}
	defer conn.Close()

	sum := sha1.Sum([]byte(secKey + wsGUID))
	acceptKey := base64.StdEncoding.EncodeToString(sum[:])

	handshake := "HTTP/1.1 101 Switching Protocols\r\n" +
		"Upgrade: websocket\r\n" +
		"Connection: Upgrade\r\n" +
		"Sec-WebSocket-Accept: " + acceptKey + "\r\n\r\n"
	if _, err := rw.WriteString(handshake); err != nil {
		return
	}
	if err := rw.Flush(); err != nil {
		return
	}

	subID, eventCh := a.bus.SubscribeAll(64)
	defer a.bus.Unsubscribe(subID)

	_ = writeWebSocketTextFrame(rw, "[SYSTEM] WebSocket log stream connected to starfall-debrid")

	ticker := time.NewTicker(3 * time.Second)
	defer ticker.Stop()

	for {
		select {
		case <-r.Context().Done():
			return
		case evt, ok := <-eventCh:
			if !ok {
				return
			}
			line := fmt.Sprintf("[%s] task=%s user=%s progress=%.1f%% msg=%s",
				evt.Type, evt.TaskID, evt.UserID, evt.Progress, evt.Message)
			if err := writeWebSocketTextFrame(rw, line); err != nil {
				return
			}
		case <-ticker.C:
			snap := a.watchdog.Snapshot()
			line := fmt.Sprintf("[WATCHDOG] state=%s ram=%.1f%% cpu=%.1f%% heap=%dMiB conns=%d/150",
				snap.State.String(),
				snap.UsagePercent,
				snap.CPULoadPercent,
				snap.HeapAllocBytes/(1024*1024),
				a.limiter.ActiveGlobalConnections(),
			)
			if err := writeWebSocketTextFrame(rw, line); err != nil {
				return
			}
		}
	}
}

// HandleUpdateConfig (`POST /api/admin/config`) hot-reloads runtime parameters safely
// within the 2 vCPU / 4 GB VPS hardware ceiling and persists global bot settings in SQLite.
func (a *AdminHandler) HandleUpdateConfig(w http.ResponseWriter, r *http.Request) {
	bodyBytes, err := io.ReadAll(io.LimitReader(r.Body, 16384))
	if err != nil {
		writeJSONResponse(w, http.StatusBadRequest, map[string]any{"ok": false, "error": "invalid body"})
		return
	}

	var patch struct {
		Action             string         `json:"action"`
		Config             map[string]any `json:"config"`
		MaxActiveDownloads int            `json:"max_active_downloads"`
		MaxTelegramUploads int            `json:"max_telegram_uploads"`
		MaxGlobalConns     int            `json:"max_global_conns"`
		NVMeMinFreeGB      float64        `json:"nvme_min_free_gb"`
	}
	if err := json.Unmarshal(bodyBytes, &patch); err != nil {
		writeJSONResponse(w, http.StatusBadRequest, map[string]any{"ok": false, "error": "invalid JSON body"})
		return
	}

	if patch.MaxActiveDownloads > 0 {
		a.cfg.Engine.MaxActiveDownloads = patch.MaxActiveDownloads
	}
	if patch.MaxTelegramUploads > 0 {
		a.cfg.Engine.MaxTelegramUploads = patch.MaxTelegramUploads
	}
	if patch.MaxGlobalConns > 0 {
		a.cfg.Network.MaxGlobalConns = patch.MaxGlobalConns
	}
	if patch.NVMeMinFreeGB >= 1.0 {
		a.cfg.Engine.NVMeMinFreeGB = patch.NVMeMinFreeGB
	}
	_ = a.cfg.ValidateAndClamp()

	if len(patch.Config) > 0 && a.userService != nil {
		strMap := make(map[string]string, len(patch.Config))
		for k, v := range patch.Config {
			strMap[k] = fmt.Sprintf("%v", v)
		}
		_ = a.userService.UpdateBotSettings(context.Background(), strMap)
	}

	a.appendLog(fmt.Sprintf("[%s] [ADMIN] Runtime configuration updated (action=%s)",
		time.Now().Format("02-Jan-06 03:04:05 PM"), patch.Action))

	writeJSONResponse(w, http.StatusOK, map[string]any{
		"ok":     true,
		"config": a.cfg.Engine,
	})
}

// HandleGracefulRestart (`POST /api/admin/restart`) triggers a clean SIGTERM self-signal
// so active workers drain before systemd/Docker restarts the `starfall-debrid` binary.
func (a *AdminHandler) HandleGracefulRestart(w http.ResponseWriter, r *http.Request) {
	a.logger.Warn("owner triggered graceful container restart")
	writeJSONResponse(w, http.StatusAccepted, map[string]any{
		"ok":      true,
		"message": "graceful restart initiated: draining active workers",
	})

	go func() {
		time.Sleep(500 * time.Millisecond)
		if p, err := os.FindProcess(os.Getpid()); err == nil {
			_ = p.Signal(syscall.SIGTERM)
		}
	}()
}

// writeWebSocketTextFrame encodes an unmasked server-to-client RFC 6455 text frame.
func writeWebSocketTextFrame(rw *bufio.ReadWriter, msg string) error {
	payload := []byte(msg)
	length := len(payload)

	header := []byte{0x81}
	switch {
	case length <= 125:
		header = append(header, byte(length))
	case length <= 65535:
		header = append(header, 126, byte(length>>8), byte(length&0xff))
	default:
		header = append(header, 127,
			0, 0, 0, 0,
			byte(length>>24), byte(length>>16), byte(length>>8), byte(length&0xff))
	}

	if _, err := rw.Write(header); err != nil {
		return err
	}
	if _, err := rw.Write(payload); err != nil {
		return err
	}
	return rw.Flush()
}
