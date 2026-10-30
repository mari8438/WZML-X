// Package web implements the HTTP handlers for the Starfall Web Studio Dashboard,
// User Settings Deck, and direct leech task lifecycle endpoints (`GET /app/dashboard`,
// `GET /api/user/session`, `POST /api/user/mode`, `GET /api/user/tasks`,
// `POST /api/leech/start`, `POST /api/leech/cancel`, `GET/POST /api/user/settings`).
package web

import (
	"crypto/sha1"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/starfall-org/starfall-go/internal/auth"
	"github.com/starfall-org/starfall-go/internal/downloader"
	"github.com/starfall-org/starfall-go/internal/engine"
	"github.com/starfall-org/starfall-go/internal/models"
	"github.com/starfall-org/starfall-go/internal/user"
)

// DashboardHandler serves the Cyberpunk Anime Web Studio UI and handles direct
// leech submissions and user preferences.
type DashboardHandler struct {
	assets      fs.FS
	dlManager   *downloader.Manager
	registry    *engine.TaskRegistry
	userService *user.Service
	jwtManager  *auth.JWTManager
	downloadDir string
	logger      *slog.Logger
}

// NewDashboardHandler constructs a DashboardHandler.
func NewDashboardHandler(
	assets fs.FS,
	dlManager *downloader.Manager,
	registry *engine.TaskRegistry,
	userService *user.Service,
	jwtManager *auth.JWTManager,
	downloadDir string,
	logger *slog.Logger,
) *DashboardHandler {
	if strings.TrimSpace(downloadDir) == "" {
		downloadDir = "/app/downloads"
	}
	return &DashboardHandler{
		assets:      assets,
		dlManager:   dlManager,
		registry:    registry,
		userService: userService,
		jwtManager:  jwtManager,
		downloadDir: downloadDir,
		logger:      logger,
	}
}

// RegisterRoutes mounts the Web Studio UI and API endpoints onto `mux`.
func (h *DashboardHandler) RegisterRoutes(mux *http.ServeMux, authMW *auth.Middleware) {
	// Web pages
	mux.Handle("GET /", http.HandlerFunc(h.HandleRootPage))
	mux.Handle("GET /app/dashboard", authMW.RequireAuthFunc(h.HandleDashboardPage))
	mux.Handle("GET /app/user/settings", authMW.RequireAuthFunc(h.HandleUserSettingsPage))
	mux.Handle("GET /app/user", authMW.RequireAuthFunc(h.HandleUserSettingsPage))

	// User session and mode
	mux.Handle("GET /api/user/session", authMW.RequireAuthFunc(h.HandleGetSession))
	mux.Handle("POST /api/user/mode", authMW.RequireAuthFunc(h.HandleSetMode))

	// Live tasks stream and control
	mux.Handle("GET /api/user/tasks", authMW.RequireAuthFunc(h.HandleGetTasks))
	mux.Handle("POST /api/leech/start", authMW.RequireAuthFunc(h.HandleStartLeech))
	mux.Handle("POST /api/leech/cancel", authMW.RequireAuthFunc(h.HandleCancelLeech))

	// User settings and thumbnail
	mux.Handle("GET /api/user/settings", authMW.RequireAuthFunc(h.HandleGetSettings))
	mux.Handle("POST /api/user/settings", authMW.RequireAuthFunc(h.HandlePostSettings))
	mux.Handle("POST /api/user/thumbnail", authMW.RequireAuthFunc(h.HandleUploadUserThumbnail))

	// Real-time WebSocket task telemetry
	mux.Handle("GET /ws/tasks", authMW.RequireAuthFunc(h.HandleTasksWebSocket))
}

// HandleRootPage (`GET /`) redirects to `/app/dashboard` if authenticated or serves landing page.
func (h *DashboardHandler) HandleRootPage(w http.ResponseWriter, r *http.Request) {
	if _, err := h.jwtManager.ExtractFromRequest(r); err == nil {
		http.Redirect(w, r, "/app/dashboard", http.StatusSeeOther)
		return
	}
	// Fallback to serving user studio directly so user can interact immediately
	h.HandleDashboardPage(w, r)
}

// HandleDashboardPage (`GET /app/dashboard`) serves `templates/user_studio.html`.
func (h *DashboardHandler) HandleDashboardPage(w http.ResponseWriter, r *http.Request) {
	rawHTML, err := fs.ReadFile(h.assets, "templates/user_studio.html")
	if err != nil {
		http.Error(w, "user_studio.html template not found", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(rawHTML)
}

// HandleUserSettingsPage (`GET /app/user/settings`) serves `templates/user_settings.html`.
func (h *DashboardHandler) HandleUserSettingsPage(w http.ResponseWriter, r *http.Request) {
	rawHTML, err := fs.ReadFile(h.assets, "templates/user_settings.html")
	if err != nil {
		http.Error(w, "user_settings.html template not found", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(rawHTML)
}

// HandleGetSession (`GET /api/user/session`) returns current user state and preferences.
func (h *DashboardHandler) HandleGetSession(w http.ResponseWriter, r *http.Request) {
	u, ok := auth.UserFromContext(r.Context())
	if !ok {
		writeJSONResponse(w, http.StatusUnauthorized, map[string]any{"ok": false, "error": "unauthorized"})
		return
	}

	tokenStr := ""
	if claims, ok := auth.ClaimsFromContext(r.Context()); ok && claims != nil {
		tokenStr, _, _ = h.jwtManager.MintToken(u)
	}

	writeJSONResponse(w, http.StatusOK, map[string]any{
		"ok":              true,
		"user_id":         u.ID,
		"username":        u.Name,
		"email":           u.Email,
		"avatar_url":      u.AvatarURL,
		"dump_channel":    u.TelegramDumpChat,
		"anime_theme":     u.AnimeTheme,
		"poster_template": u.PosterTemplate,
		"mode":            "web",
		"token":           tokenStr,
	})
}

// HandleSetMode (`POST /api/user/mode`) switches between web and telegram modes.
func (h *DashboardHandler) HandleSetMode(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Mode string `json:"mode"`
	}
	if err := json.NewDecoder(io.LimitReader(r.Body, 4096)).Decode(&body); err != nil {
		writeJSONResponse(w, http.StatusBadRequest, map[string]any{"ok": false, "error": "invalid JSON body"})
		return
	}

	mode := strings.ToLower(strings.TrimSpace(body.Mode))
	if mode != "telegram" {
		mode = "web"
	}

	writeJSONResponse(w, http.StatusOK, map[string]any{
		"ok":   true,
		"mode": mode,
	})
}

// TaskJSONView maps an internal TaskSnapshot into the JSON format expected by `user_studio.html`.
type TaskJSONView struct {
	TaskID         string  `json:"task_id"`
	GID            string  `json:"gid"`
	Name           string  `json:"name"`
	Status         string  `json:"status"`
	Progress       string  `json:"progress"`
	PctNum         float64 `json:"pctNum"`
	Speed          string  `json:"speed"`
	SpeedMBps      float64 `json:"speedMBps"`
	ProcessedBytes string  `json:"processed_bytes"`
	Size           string  `json:"size"`
	ETA            string  `json:"eta"`
	Engine         string  `json:"engine"`
	ErrorMessage   string  `json:"error_message,omitempty"`
}

// HandleGetTasks (`GET /api/user/tasks`) streams live active and recent tasks for the user.
func (h *DashboardHandler) HandleGetTasks(w http.ResponseWriter, r *http.Request) {
	u, _ := auth.UserFromContext(r.Context())
	currentUserID := ""
	if u != nil {
		currentUserID = u.ID
	}

	snapshots := h.registry.ListSnapshots()
	out := make([]TaskJSONView, 0, len(snapshots))

	for _, snap := range snapshots {
		// If authenticated, only show tasks owned by this user (or all if admin/unrestricted)
		if currentUserID != "" && snap.UserID != "" && snap.UserID != currentUserID {
			continue
		}

		pct := snap.Stats.ProgressPercent
		speedBps := snap.Stats.DownloadSpeedBps
		if snap.Status == models.StatusUploading {
			speedBps = snap.Stats.UploadSpeedBps
		}

		engineName := "Direct-Leech"
		if strings.HasPrefix(snap.SourceURI, "magnet:") || strings.HasSuffix(snap.SourceURI, ".torrent") {
			engineName = "qBittorrent"
		} else if strings.HasSuffix(snap.SourceURI, ".nzb") {
			engineName = "SABnzbd"
		} else if strings.Contains(snap.SourceURI, "youtube.com") || strings.Contains(snap.SourceURI, "youtu.be") {
			engineName = "yt-dlp"
		}

		statusStr := string(snap.Status)
		switch snap.Status {
		case models.StatusDownloading:
			statusStr = "Downloading"
		case models.StatusUploading:
			statusStr = "Uploading"
		case models.StatusRemuxing:
			statusStr = "Remuxing"
		case models.StatusQueued:
			statusStr = "Queued"
		case models.StatusCompleted:
			statusStr = "Completed"
		case models.StatusPaused:
			statusStr = "Paused"
		case models.StatusFailed:
			statusStr = "Failed"
		case models.StatusCanceled:
			statusStr = "Canceled"
		}

		out = append(out, TaskJSONView{
			TaskID:         snap.ID,
			GID:            snap.ID,
			Name:           snap.Name,
			Status:         statusStr,
			Progress:       fmt.Sprintf("%.1f%%", pct),
			PctNum:         pct,
			Speed:          formatSpeedBps(speedBps),
			SpeedMBps:      float64(speedBps) / (1024 * 1024),
			ProcessedBytes: formatBytes(snap.Stats.CompletedBytes),
			Size:           formatBytes(snap.Stats.TotalBytes),
			ETA:            formatETASecs(snap.Stats.ETASeconds),
			Engine:         engineName,
			ErrorMessage:   snap.ErrorMessage,
		})
	}

	writeJSONResponse(w, http.StatusOK, out)
}

// LeechStartPayload represents the request body for `/api/leech/start`.
type LeechStartPayload struct {
	URL        string `json:"url"`
	Name       string `json:"name,omitempty"`
	Engine     string `json:"engine,omitempty"`
	Remux      bool   `json:"remux,omitempty"`
	NeedsRemux bool   `json:"needs_remux,omitempty"`
}

// HandleStartLeech (`POST /api/leech/start`) submits a direct download to the Dispatcher.
func (h *DashboardHandler) HandleStartLeech(w http.ResponseWriter, r *http.Request) {
	u, ok := auth.UserFromContext(r.Context())
	if !ok {
		writeJSONResponse(w, http.StatusUnauthorized, map[string]any{"ok": false, "error": "unauthorized"})
		return
	}

	var req LeechStartPayload
	if err := json.NewDecoder(io.LimitReader(r.Body, 16384)).Decode(&req); err != nil {
		writeJSONResponse(w, http.StatusBadRequest, map[string]any{"ok": false, "error": "invalid JSON body"})
		return
	}

	urlStr := strings.TrimSpace(req.URL)
	if urlStr == "" {
		writeJSONResponse(w, http.StatusBadRequest, map[string]any{"ok": false, "error": "url parameter is required"})
		return
	}

	needsRemux := req.Remux || req.NeedsRemux
	task, err := h.dlManager.Start(u, urlStr, needsRemux)
	if err != nil {
		if errorsIs(err, downloader.ErrTelegramDumpNotLinked) {
			writeJSONResponse(w, http.StatusUnprocessableEntity, map[string]any{
				"ok":    false,
				"error": "Please configure your private Telegram Dump Channel ID in Settings before launching leech tasks.",
			})
			return
		}
		writeJSONResponse(w, http.StatusBadRequest, map[string]any{
			"ok":    false,
			"error": err.Error(),
		})
		return
	}

	writeJSONResponse(w, http.StatusAccepted, map[string]any{
		"ok":      true,
		"task_id": task.ID,
		"gid":     task.ID,
		"name":    task.Name,
		"status":  "queued",
	})
}

// HandleCancelLeech (`POST /api/leech/cancel`) aborts an in-flight or queued task.
func (h *DashboardHandler) HandleCancelLeech(w http.ResponseWriter, r *http.Request) {
	var body struct {
		TaskID string `json:"task_id"`
		GID    string `json:"gid"`
	}
	if err := json.NewDecoder(io.LimitReader(r.Body, 4096)).Decode(&body); err != nil {
		writeJSONResponse(w, http.StatusBadRequest, map[string]any{"ok": false, "error": "invalid JSON body"})
		return
	}

	targetID := strings.TrimSpace(body.TaskID)
	if targetID == "" {
		targetID = strings.TrimSpace(body.GID)
	}
	if targetID == "" {
		writeJSONResponse(w, http.StatusBadRequest, map[string]any{"ok": false, "error": "task_id or gid is required"})
		return
	}

	if err := h.dlManager.Abort(targetID, "canceled by user via web studio"); err != nil {
		writeJSONResponse(w, http.StatusNotFound, map[string]any{"ok": false, "error": err.Error()})
		return
	}

	writeJSONResponse(w, http.StatusOK, map[string]any{
		"ok":      true,
		"task_id": targetID,
		"message": "task canceled successfully",
	})
}

// HandleGetSettings (`GET /api/user/settings`) returns user configuration.
func (h *DashboardHandler) HandleGetSettings(w http.ResponseWriter, r *http.Request) {
	u, ok := auth.UserFromContext(r.Context())
	if !ok {
		writeJSONResponse(w, http.StatusUnauthorized, map[string]any{"ok": false, "error": "unauthorized"})
		return
	}

	writeJSONResponse(w, http.StatusOK, map[string]any{
		"ok":                           true,
		"LEECH_DUMP_CHAT":              u.TelegramDumpChat,
		"AUTO_POSTER_ENABLED":          true,
		"AUTO_POSTER_USE_AS_THUMBNAIL": true,
		"AUTO_POSTER_TEMPLATE":         u.PosterTemplate,
		"ANIME_THEME":                  u.AnimeTheme,
	})
}

// UserSettingsUpdatePayload represents the payload to update user settings.
type UserSettingsUpdatePayload struct {
	LeechDumpChat      string `json:"LEECH_DUMP_CHAT"`
	AnimeTheme         string `json:"ANIME_THEME"`
	AutoPosterTemplate int    `json:"AUTO_POSTER_TEMPLATE"`
}

// HandlePostSettings (`POST /api/user/settings`) updates user channel and theme preferences.
func (h *DashboardHandler) HandlePostSettings(w http.ResponseWriter, r *http.Request) {
	u, ok := auth.UserFromContext(r.Context())
	if !ok {
		writeJSONResponse(w, http.StatusUnauthorized, map[string]any{"ok": false, "error": "unauthorized"})
		return
	}

	var req UserSettingsUpdatePayload
	if err := json.NewDecoder(io.LimitReader(r.Body, 8192)).Decode(&req); err != nil {
		writeJSONResponse(w, http.StatusBadRequest, map[string]any{"ok": false, "error": "invalid JSON body"})
		return
	}

	// 1. Update Telegram Dump Channel if provided
	if req.LeechDumpChat != "" {
		if _, err := h.userService.UpdateTelegramDump(r.Context(), u.ID, req.LeechDumpChat); err != nil {
			writeJSONResponse(w, http.StatusBadRequest, map[string]any{"ok": false, "error": err.Error()})
			return
		}
	}

	// 2. Update theme preferences if provided
	theme := req.AnimeTheme
	if theme == "" {
		theme = u.AnimeTheme
	}
	tpl := req.AutoPosterTemplate
	if tpl <= 0 {
		tpl = u.PosterTemplate
	}

	updated, err := h.userService.UpdateThemePreferences(r.Context(), u.ID, theme, tpl)
	if err != nil {
		writeJSONResponse(w, http.StatusBadRequest, map[string]any{"ok": false, "error": err.Error()})
		return
	}

	writeJSONResponse(w, http.StatusOK, map[string]any{
		"ok":                   true,
		"message":              "settings updated successfully",
		"LEECH_DUMP_CHAT":      updated.TelegramDumpChat,
		"ANIME_THEME":          updated.AnimeTheme,
		"AUTO_POSTER_TEMPLATE": updated.PosterTemplate,
	})
}

// HandleUploadUserThumbnail (`POST /api/user/thumbnail`) saves custom user thumbnail.
func (h *DashboardHandler) HandleUploadUserThumbnail(w http.ResponseWriter, r *http.Request) {
	u, ok := auth.UserFromContext(r.Context())
	if !ok {
		writeJSONResponse(w, http.StatusUnauthorized, map[string]any{"ok": false, "error": "unauthorized"})
		return
	}

	destDir := filepath.Join(h.downloadDir, "thumbnails")
	_ = os.MkdirAll(destDir, 0o755)
	destPath := filepath.Join(destDir, fmt.Sprintf("user_%s.jpg", u.ID))

	// Check if this is a DELETE request action
	if r.URL.Query().Get("action") == "delete" {
		_ = os.Remove(destPath)
		writeJSONResponse(w, http.StatusOK, map[string]any{"ok": true, "message": "thumbnail removed"})
		return
	}

	ct := strings.ToLower(r.Header.Get("Content-Type"))
	if strings.Contains(ct, "multipart/form-data") {
		if err := r.ParseMultipartForm(8 * 1024 * 1024); err != nil {
			writeJSONResponse(w, http.StatusBadRequest, map[string]any{"ok": false, "error": "file too large (max 8 MiB)"})
			return
		}
		file, _, err := r.FormFile("thumbnail")
		if err != nil {
			writeJSONResponse(w, http.StatusBadRequest, map[string]any{"ok": false, "error": "missing thumbnail field"})
			return
		}
		defer file.Close()

		outFile, err := os.Create(destPath)
		if err != nil {
			writeJSONResponse(w, http.StatusInternalServerError, map[string]any{"ok": false, "error": err.Error()})
			return
		}
		defer outFile.Close()

		if _, err := io.Copy(outFile, file); err != nil {
			writeJSONResponse(w, http.StatusInternalServerError, map[string]any{"ok": false, "error": err.Error()})
			return
		}
	} else {
		var body struct {
			DataURL string `json:"data_url"`
		}
		if err := json.NewDecoder(io.LimitReader(r.Body, 10*1024*1024)).Decode(&body); err != nil {
			writeJSONResponse(w, http.StatusBadRequest, map[string]any{"ok": false, "error": "invalid JSON body"})
			return
		}
		parts := strings.Split(body.DataURL, ",")
		if len(parts) != 2 {
			writeJSONResponse(w, http.StatusBadRequest, map[string]any{"ok": false, "error": "invalid data_url"})
			return
		}
		imgBytes, err := base64.StdEncoding.DecodeString(parts[1])
		if err != nil {
			writeJSONResponse(w, http.StatusBadRequest, map[string]any{"ok": false, "error": "invalid base64 image data"})
			return
		}
		if err := os.WriteFile(destPath, imgBytes, 0o644); err != nil {
			writeJSONResponse(w, http.StatusInternalServerError, map[string]any{"ok": false, "error": err.Error()})
			return
		}
	}

	writeJSONResponse(w, http.StatusOK, map[string]any{
		"ok":             true,
		"thumbnail_path": destPath,
	})
}

// HandleTasksWebSocket (`GET /ws/tasks`) upgrades the connection to an RFC 6455
// WebSocket streaming tasks JSON every 750ms.
func (h *DashboardHandler) HandleTasksWebSocket(w http.ResponseWriter, r *http.Request) {
	if strings.ToLower(r.Header.Get("Upgrade")) != "websocket" {
		http.Error(w, "expected WebSocket Upgrade", http.StatusBadRequest)
		return
	}

	key := r.Header.Get("Sec-WebSocket-Key")
	if key == "" {
		http.Error(w, "missing Sec-WebSocket-Key", http.StatusBadRequest)
		return
	}

	hash := sha1.Sum([]byte(key + wsGUID))
	accept := base64.StdEncoding.EncodeToString(hash[:])

	hijacker, ok := w.(http.Hijacker)
	if !ok {
		http.Error(w, "webserver does not support hijacking", http.StatusInternalServerError)
		return
	}
	conn, bufrw, err := hijacker.Hijack()
	if err != nil {
		h.logger.Error("websocket hijack failed", slog.String("error", err.Error()))
		return
	}
	defer conn.Close()

	handshake := fmt.Sprintf("HTTP/1.1 101 Switching Protocols\r\n"+
		"Upgrade: websocket\r\n"+
		"Connection: Upgrade\r\n"+
		"Sec-WebSocket-Accept: %s\r\n\r\n", accept)
	if _, err := bufrw.WriteString(handshake); err != nil {
		return
	}
	_ = bufrw.Flush()

	ticker := time.NewTicker(750 * time.Millisecond)
	defer ticker.Stop()

	for {
		select {
		case <-r.Context().Done():
			return
		case <-ticker.C:
			snapshots := h.registry.ListSnapshots()
			data, err := json.Marshal(snapshots)
			if err != nil {
				continue
			}

			// Encode WebSocket text frame (Opcode 0x1, FIN 0x80)
			frame := make([]byte, 0, len(data)+10)
			frame = append(frame, 0x81) // FIN + text
			payloadLen := len(data)
			if payloadLen <= 125 {
				frame = append(frame, byte(payloadLen))
			} else if payloadLen <= 65535 {
				frame = append(frame, 126, byte(payloadLen>>8), byte(payloadLen&0xff))
			} else {
				frame = append(frame, 127,
					0, 0, 0, 0,
					byte(payloadLen>>24), byte(payloadLen>>16), byte(payloadLen>>8), byte(payloadLen&0xff),
				)
			}
			frame = append(frame, data...)

			_ = conn.SetWriteDeadline(time.Now().Add(3 * time.Second))
			if _, err := conn.Write(frame); err != nil {
				return // Client disconnected
			}
		}
	}
}

// formatBytes formats raw bytes into human readable KiB, MiB, GiB string.
func formatBytes(b int64) string {
	const unit = 1024
	if b < unit {
		return fmt.Sprintf("%d B", b)
	}
	div, exp := int64(unit), 0
	for n := b / unit; n >= unit; n /= unit {
		div *= unit
		exp++
	}
	return fmt.Sprintf("%.1f %ciB", float64(b)/float64(div), "KMGTPE"[exp])
}

// formatSpeedBps formats bytes per second into human readable speed string.
func formatSpeedBps(bps int64) string {
	if bps <= 0 {
		return "0 B/s"
	}
	return formatBytes(bps) + "/s"
}

// formatETASecs formats remaining seconds into human readable ETA string.
func formatETASecs(sec int64) string {
	if sec <= 0 {
		return "0s"
	}
	if sec < 60 {
		return fmt.Sprintf("%ds", sec)
	}
	if sec < 3600 {
		return fmt.Sprintf("%dm %ds", sec/60, sec%60)
	}
	return fmt.Sprintf("%dh %dm", sec/3600, (sec%3600)/60)
}

func errorsIs(err, target error) bool {
	if err == nil || target == nil {
		return err == target
	}
	return err.Error() == target.Error()
}
