// Package web implements the HTTP handlers for the Starfall Web Studio Dashboard,
// User Settings Deck, Torrent File Selector (`page.html`), and direct leech task lifecycle
// endpoints (`GET /app/dashboard`, `GET /app/settings`, `GET /app/files`,
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
	"path"
	"path/filepath"
	"strings"
	"time"

	"github.com/starfall-org/starfall-go/internal/auth"
	"github.com/starfall-org/starfall-go/internal/downloader"
	"github.com/starfall-org/starfall-go/internal/downloader/aria2"
	"github.com/starfall-org/starfall-go/internal/engine"
	"github.com/starfall-org/starfall-go/internal/models"
	"github.com/starfall-org/starfall-go/internal/user"
)

// DashboardHandler serves the Cyberpunk Anime Web Studio UI, Settings Deck,
// Torrent File Selector, and direct leech & settings endpoints.
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
	// Web pages & aliases
	mux.Handle("GET /{$}", authMW.RequireAuthFunc(h.HandleRootPage))
	mux.Handle("GET /app/dashboard", authMW.RequireAuthFunc(h.HandleDashboardPage))
	mux.Handle("GET /app/settings", authMW.RequireAuthFunc(h.HandleUserSettingsPage))
	mux.Handle("GET /app/user/settings", authMW.RequireAuthFunc(h.HandleUserSettingsPage))
	mux.Handle("GET /app/user", authMW.RequireAuthFunc(h.HandleUserSettingsPage))
	mux.Handle("GET /app/files", authMW.RequireAuthFunc(h.HandleTorrentFilesPage))

	// Torrent / Aria2 interactive file selector APIs (`page.html` + REST)
	mux.Handle("GET /app/files/torrent", authMW.RequireAuthFunc(h.HandleTorrentFilesAPI))
	mux.Handle("POST /app/files/torrent", authMW.RequireAuthFunc(h.HandleTorrentFilesAPI))
	mux.Handle("GET /api/torrent/files", authMW.RequireAuthFunc(h.HandleTorrentFilesAPI))
	mux.Handle("POST /api/torrent/select", authMW.RequireAuthFunc(h.HandleTorrentFilesAPI))

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

// HandleRootPage (`GET /`) serves the Web Studio dashboard directly.
func (h *DashboardHandler) HandleRootPage(w http.ResponseWriter, r *http.Request) {
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

// HandleUserSettingsPage (`GET /app/settings` & `GET /app/user/settings`) serves `templates/user_settings.html`.
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

// HandleTorrentFilesPage (`GET /app/files`) serves `templates/page.html`.
func (h *DashboardHandler) HandleTorrentFilesPage(w http.ResponseWriter, r *http.Request) {
	rawHTML, err := fs.ReadFile(h.assets, "templates/page.html")
	if err != nil {
		http.Error(w, "page.html template not found", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(rawHTML)
}

// HandleGetSession (`GET /api/user/session`) returns current user state and preferences.
func (h *DashboardHandler) HandleGetSession(w http.ResponseWriter, r *http.Request) {
	u, ok := auth.UserFromContext(r.Context())
	if !ok || u == nil {
		writeJSONResponse(w, http.StatusUnauthorized, map[string]any{"ok": false, "error": "unauthorized"})
		return
	}

	tokenStr, _, _ := h.jwtManager.MintToken(u)
	mode := strings.ToLower(strings.TrimSpace(u.Settings.TaskMode))
	if mode != "telegram" {
		mode = "web"
	}

	dumpChan := strings.TrimSpace(u.TelegramDumpChat)
	if dumpChan == "" {
		dumpChan = strings.TrimSpace(u.Settings.LeechDumpChat)
	}

	displayID := u.ID
	if u.TelegramID > 0 {
		displayID = fmt.Sprintf("%d", u.TelegramID)
	}

	writeJSONResponse(w, http.StatusOK, map[string]any{
		"ok":              true,
		"user_id":         displayID,
		"internal_id":     u.ID,
		"username":        u.Name,
		"email":           u.Email,
		"avatar_url":      u.AvatarURL,
		"dump_channel":    dumpChan,
		"anime_theme":     u.AnimeTheme,
		"poster_template": u.PosterTemplate,
		"mode":            mode,
		"token":           tokenStr,
	})
}

// HandleSetMode (`POST /api/user/mode`) switches between `web` and `telegram` task modes in SQLite.
func (h *DashboardHandler) HandleSetMode(w http.ResponseWriter, r *http.Request) {
	u, ok := auth.UserFromContext(r.Context())
	if !ok || u == nil {
		writeJSONResponse(w, http.StatusUnauthorized, map[string]any{"ok": false, "error": "unauthorized"})
		return
	}

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

	settings := u.Settings
	settings.TaskMode = mode
	_, _ = h.userService.UpdateUserSettings(r.Context(), u.ID, settings)

	writeJSONResponse(w, http.StatusOK, map[string]any{
		"ok":   true,
		"mode": mode,
	})
}

// TaskJSONView maps an internal TaskSnapshot into the JSON format expected by `user_studio.html`.
type TaskJSONView struct {
	TaskID           string   `json:"task_id"`
	GID              string   `json:"gid"`
	Name             string   `json:"name"`
	Status           string   `json:"status"`
	Progress         string   `json:"progress"`
	PctNum           float64  `json:"pctNum"`
	Speed            string   `json:"speed"`
	SpeedMBps        float64  `json:"speedMBps"`
	ProcessedBytes   string   `json:"processed_bytes"`
	Size             string   `json:"size"`
	ETA              string   `json:"eta"`
	Engine           string   `json:"engine"`
	OutputPath       string   `json:"output_path,omitempty"`
	ResultFiles      []string `json:"result_files,omitempty"`
	TelegramDeepLink string   `json:"telegram_deep_link,omitempty"`
	ErrorMessage     string   `json:"error_message,omitempty"`
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
		if currentUserID != "" && snap.UserID != "" && snap.UserID != currentUserID {
			continue
		}

		pct := snap.Stats.ProgressPercent
		speedBps := snap.Stats.DownloadSpeedBps
		if snap.Status == models.StatusUploading {
			speedBps = snap.Stats.UploadSpeedBps
		}

		engineName := strings.TrimSpace(snap.EngineName)
		if engineName == "" {
			engineName = "Direct-Leech"
			if strings.HasPrefix(snap.SourceURI, "magnet:") || strings.HasSuffix(snap.SourceURI, ".torrent") {
				engineName = "Aria2c"
			} else if strings.Contains(snap.SourceURI, "youtube.com") || strings.Contains(snap.SourceURI, "youtu.be") {
				engineName = "yt-dlp"
			}
		}

		statusStr := string(snap.Status)
		switch snap.Status {
		case models.StatusDownloading:
			statusStr = "Downloading"
		case models.StatusUploading:
			statusStr = "Uploading"
		case models.StatusRemuxing:
			statusStr = "Lossless Remux & Auto-Rename"
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
		if strings.TrimSpace(snap.StageDetail) != "" && snap.Status != models.StatusCompleted && snap.Status != models.StatusFailed {
			statusStr = snap.StageDetail
		}

		out = append(out, TaskJSONView{
			TaskID:           snap.ID,
			GID:              snap.ID,
			Name:             snap.Name,
			Status:           statusStr,
			Progress:         fmt.Sprintf("%.1f%%", pct),
			PctNum:           pct,
			Speed:            formatSpeedBps(speedBps),
			SpeedMBps:        float64(speedBps) / (1024 * 1024),
			ProcessedBytes:   formatBytes(snap.Stats.CompletedBytes),
			Size:             formatBytes(snap.Stats.TotalBytes),
			ETA:              formatETASecs(snap.Stats.ETASeconds),
			Engine:           engineName,
			OutputPath:       snap.OutputPath,
			ResultFiles:      snap.ResultFiles,
			TelegramDeepLink: snap.TelegramDeepLink,
			ErrorMessage:     snap.ErrorMessage,
		})
	}

	writeJSONResponse(w, http.StatusOK, map[string]any{
		"ok":    true,
		"tasks": out,
	})
}

// LeechStartPayload represents the request body for `/api/leech/start`.
type LeechStartPayload struct {
	URL        string `json:"url"`
	Name       string `json:"name,omitempty"`
	Mode       string `json:"mode,omitempty"`
	Engine     string `json:"engine,omitempty"`
	Select     bool   `json:"select,omitempty"`
	Extract    bool   `json:"extract,omitempty"`
	Compress   bool   `json:"compress,omitempty"`
	UpDest     string `json:"up_dest,omitempty"`
	Remux      bool   `json:"remux,omitempty"`
	NeedsRemux bool   `json:"needs_remux,omitempty"`
	SkipUpload bool   `json:"skip_upload,omitempty"`
}

// HandleStartLeech (`POST /api/leech/start`) submits a magnet, torrent, yt-dlp, or cloud link to the Dispatcher.
func (h *DashboardHandler) HandleStartLeech(w http.ResponseWriter, r *http.Request) {
	u, ok := auth.UserFromContext(r.Context())
	if !ok || u == nil {
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

	ownerCopy := *u
	if strings.TrimSpace(req.UpDest) != "" {
		ownerCopy.TelegramDumpChat = strings.TrimSpace(req.UpDest)
	}

	var extractPtr *bool
	if req.Extract {
		t := true
		extractPtr = &t
	}

	task, err := h.dlManager.StartWithOptions(&ownerCopy, downloader.StartOptions{
		RawURI:         urlStr,
		CustomName:     req.Name,
		SelectFiles:    req.Select,
		NeedsRemux:     req.Remux || req.NeedsRemux,
		ExtractArchive: extractPtr,
		SkipUpload:     req.SkipUpload,
	})
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
		"ok":       true,
		"task_id":  task.ID,
		"gid":      task.ID,
		"name":     task.Name,
		"status":   "queued",
		"file_url": fmt.Sprintf("/app/files?gid=%s", task.ID),
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

// HandleGetSettings (`GET /api/user/settings`) returns all 40+ StarfallX user preferences.
func (h *DashboardHandler) HandleGetSettings(w http.ResponseWriter, r *http.Request) {
	u, ok := auth.UserFromContext(r.Context())
	if !ok || u == nil {
		writeJSONResponse(w, http.StatusUnauthorized, map[string]any{"ok": false, "error": "unauthorized"})
		return
	}

	settings := u.Settings
	if settings.LeechDumpChat == "" && u.TelegramDumpChat != "" {
		settings.LeechDumpChat = u.TelegramDumpChat
	}
	if settings.PostTemplateID <= 0 {
		settings.PostTemplateID = u.PosterTemplate
	}
	if settings.AnimeTheme == "" {
		settings.AnimeTheme = u.AnimeTheme
	}

	rawBytes, _ := json.Marshal(settings)
	out := make(map[string]any, 48)
	_ = json.Unmarshal(rawBytes, &out)
	out["ok"] = true
	out["AUTO_POSTER_TEMPLATE"] = settings.PostTemplateID
	if settings.Metadata != nil {
		out["METADATA_TITLE"] = settings.Metadata["title"]
	}
	if settings.AudioMetadata != nil {
		out["METADATA_AUDIO_TITLE"] = settings.AudioMetadata["title"]
	}
	if settings.SubtitleMetadata != nil {
		out["METADATA_SUBTITLE_TITLE"] = settings.SubtitleMetadata["title"]
	}

	writeJSONResponse(w, http.StatusOK, out)
}

// HandlePostSettings (`POST /api/user/settings`) merges incoming JSON keys onto the user's
// full `models.UserSettings` struct and persists it in SQLite.
func (h *DashboardHandler) HandlePostSettings(w http.ResponseWriter, r *http.Request) {
	u, ok := auth.UserFromContext(r.Context())
	if !ok || u == nil {
		writeJSONResponse(w, http.StatusUnauthorized, map[string]any{"ok": false, "error": "unauthorized"})
		return
	}

	bodyBytes, err := io.ReadAll(io.LimitReader(r.Body, 32768))
	if err != nil {
		writeJSONResponse(w, http.StatusBadRequest, map[string]any{"ok": false, "error": "failed to read body"})
		return
	}

	merged := u.Settings
	if merged.Metadata == nil {
		merged.Metadata = map[string]string{}
	}
	if merged.VideoMetadata == nil {
		merged.VideoMetadata = map[string]string{}
	}
	if merged.AudioMetadata == nil {
		merged.AudioMetadata = map[string]string{}
	}
	if merged.SubtitleMetadata == nil {
		merged.SubtitleMetadata = map[string]string{}
	}

	if err := json.Unmarshal(bodyBytes, &merged); err != nil {
		writeJSONResponse(w, http.StatusBadRequest, map[string]any{"ok": false, "error": "invalid JSON body"})
		return
	}

	// Support flattened UI aliases sent by `user_settings.html`
	var flat struct {
		AutoPosterTemplate *int    `json:"AUTO_POSTER_TEMPLATE"`
		MetaTitle          *string `json:"METADATA_TITLE"`
		MetaAudioTitle     *string `json:"METADATA_AUDIO_TITLE"`
		MetaSubTitle       *string `json:"METADATA_SUBTITLE_TITLE"`
	}
	_ = json.Unmarshal(bodyBytes, &flat)
	if flat.AutoPosterTemplate != nil && *flat.AutoPosterTemplate >= 1 && *flat.AutoPosterTemplate <= 11 {
		merged.PostTemplateID = *flat.AutoPosterTemplate
	}
	if flat.MetaTitle != nil {
		merged.Metadata["title"] = *flat.MetaTitle
		if *flat.MetaTitle != "" {
			merged.AutoMetadata = true
		}
	}
	if flat.MetaAudioTitle != nil {
		merged.AudioMetadata["title"] = *flat.MetaAudioTitle
	}
	if flat.MetaSubTitle != nil {
		merged.SubtitleMetadata["title"] = *flat.MetaSubTitle
	}

	updated, err := h.userService.UpdateUserSettings(r.Context(), u.ID, merged)
	if err != nil {
		writeJSONResponse(w, http.StatusBadRequest, map[string]any{"ok": false, "error": err.Error()})
		return
	}

	writeJSONResponse(w, http.StatusOK, map[string]any{
		"ok":                   true,
		"message":              "settings updated successfully",
		"LEECH_DUMP_CHAT":      updated.TelegramDumpChat,
		"ANIME_THEME":          updated.AnimeTheme,
		"POST_TEMPLATE_ID":     updated.PosterTemplate,
		"AUTO_POSTER_TEMPLATE": updated.PosterTemplate,
	})
}

// HandleUploadUserThumbnail (`POST /api/user/thumbnail`) saves or deletes a custom user 16:9 thumbnail.
func (h *DashboardHandler) HandleUploadUserThumbnail(w http.ResponseWriter, r *http.Request) {
	u, ok := auth.UserFromContext(r.Context())
	if !ok || u == nil {
		writeJSONResponse(w, http.StatusUnauthorized, map[string]any{"ok": false, "error": "unauthorized"})
		return
	}

	destDir := filepath.Join(h.downloadDir, "thumbnails")
	_ = os.MkdirAll(destDir, 0o755)
	safeUID := sanitizeIdentifier(u.ID)
	destPath := filepath.Join(destDir, fmt.Sprintf("user_%s.jpg", safeUID))

	if r.URL.Query().Get("action") == "delete" {
		_ = os.Remove(destPath)
		settings := u.Settings
		settings.CustomThumbnailFilePath = ""
		_, _ = h.userService.UpdateUserSettings(r.Context(), u.ID, settings)
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
			Action      string `json:"action"`
			DataURL     string `json:"data_url"`
			ImageBase64 string `json:"image_base64"`
		}
		if err := json.NewDecoder(io.LimitReader(r.Body, 10*1024*1024)).Decode(&body); err != nil {
			writeJSONResponse(w, http.StatusBadRequest, map[string]any{"ok": false, "error": "invalid JSON body"})
			return
		}
		if strings.EqualFold(body.Action, "delete") {
			_ = os.Remove(destPath)
			settings := u.Settings
			settings.CustomThumbnailFilePath = ""
			_, _ = h.userService.UpdateUserSettings(r.Context(), u.ID, settings)
			writeJSONResponse(w, http.StatusOK, map[string]any{"ok": true, "message": "thumbnail removed"})
			return
		}

		rawData := strings.TrimSpace(body.DataURL)
		if rawData == "" {
			rawData = strings.TrimSpace(body.ImageBase64)
		}
		if idx := strings.Index(rawData, ","); idx != -1 {
			rawData = rawData[idx+1:]
		}
		imgBytes, err := base64.StdEncoding.DecodeString(rawData)
		if err != nil || len(imgBytes) == 0 {
			writeJSONResponse(w, http.StatusBadRequest, map[string]any{"ok": false, "error": "invalid base64 image data"})
			return
		}
		if err := os.WriteFile(destPath, imgBytes, 0o644); err != nil {
			writeJSONResponse(w, http.StatusInternalServerError, map[string]any{"ok": false, "error": err.Error()})
			return
		}
	}

	settings := u.Settings
	settings.CustomThumbnailFilePath = destPath
	_, _ = h.userService.UpdateUserSettings(r.Context(), u.ID, settings)

	writeJSONResponse(w, http.StatusOK, map[string]any{
		"ok":             true,
		"thumbnail_path": destPath,
	})
}

// torrentTreeNode matches the JSON node schema consumed & submitted by `web/templates/page.html`.
type torrentTreeNode struct {
	ID       any                `json:"id"`
	Name     string             `json:"name"`
	Type     string             `json:"type"` // "file" | "folder"
	Size     int64              `json:"size,omitempty"`
	Selected bool               `json:"selected"`
	Children []*torrentTreeNode `json:"children,omitempty"`
}

// HandleTorrentFilesAPI serves `GET/POST /app/files/torrent`, `GET /api/torrent/files`,
// and `POST /api/torrent/select` for selective Aria2 torrent downloads.
func (h *DashboardHandler) HandleTorrentFilesAPI(w http.ResponseWriter, r *http.Request) {
	gid := strings.TrimSpace(r.URL.Query().Get("gid"))
	if gid == "" {
		gid = strings.TrimSpace(r.URL.Query().Get("task_id"))
	}
	mode := strings.ToLower(strings.TrimSpace(r.URL.Query().Get("mode")))

	if r.Method == http.MethodGet || mode == "get" {
		if gid == "" {
			writeJSONResponse(w, http.StatusBadRequest, map[string]any{"error": "missing_gid", "message": "Task GID is required."})
			return
		}
		entries, err := h.dlManager.ListTorrentFiles(r.Context(), gid)
		if err != nil {
			writeJSONResponse(w, http.StatusNotFound, map[string]any{"error": "not_found", "message": err.Error()})
			return
		}
		tree := buildTorrentTree(entries)
		writeJSONResponse(w, http.StatusOK, map[string]any{
			"ok":     true,
			"gid":    gid,
			"engine": "aria2",
			"files":  tree,
		})
		return
	}

	if mode == "rename" {
		writeJSONResponse(w, http.StatusOK, map[string]any{"ok": true})
		return
	}

	// POST selection (`page.html` sends `state.files` array, or REST sends `{"gid": "...", "indices": [1,2]}`)
	bodyBytes, err := io.ReadAll(io.LimitReader(r.Body, 2*1024*1024))
	if err != nil {
		writeJSONResponse(w, http.StatusBadRequest, map[string]any{"error": "bad_body", "message": err.Error()})
		return
	}

	var selectedIndices []int
	var treePayload []torrentTreeNode
	if err := json.Unmarshal(bodyBytes, &treePayload); err == nil && len(treePayload) > 0 {
		selectedIndices = collectSelectedTreeIndices(treePayload)
	} else {
		var objPayload struct {
			GID     string `json:"gid"`
			TaskID  string `json:"task_id"`
			Indices []int  `json:"indices"`
		}
		if err := json.Unmarshal(bodyBytes, &objPayload); err == nil {
			if gid == "" {
				gid = strings.TrimSpace(objPayload.GID)
			}
			if gid == "" {
				gid = strings.TrimSpace(objPayload.TaskID)
			}
			selectedIndices = objPayload.Indices
		}
	}

	if gid == "" {
		writeJSONResponse(w, http.StatusBadRequest, map[string]any{"error": "missing_gid", "message": "Task GID is required."})
		return
	}

	if err := h.dlManager.SelectTorrentFiles(r.Context(), gid, selectedIndices); err != nil {
		writeJSONResponse(w, http.StatusNotFound, map[string]any{"error": "select_failed", "message": err.Error()})
		return
	}

	writeJSONResponse(w, http.StatusOK, map[string]any{
		"ok":             true,
		"gid":            gid,
		"selected_count": len(selectedIndices),
	})
}

func buildTorrentTree(entries []aria2.TorrentFileEntry) []*torrentTreeNode {
	var roots []*torrentTreeNode
	folderMap := make(map[string]*torrentTreeNode)

	for _, e := range entries {
		cleanRel := strings.TrimPrefix(filepath.ToSlash(e.Path), "/")
		parts := strings.Split(cleanRel, "/")
		var currentChildren *[]*torrentTreeNode = &roots
		curPrefix := ""

		for i, part := range parts {
			if part == "" {
				continue
			}
			isLast := i == len(parts)-1
			if isLast {
				*currentChildren = append(*currentChildren, &torrentTreeNode{
					ID:       e.Index,
					Name:     path.Base(part),
					Type:     "file",
					Size:     e.SizeBytes,
					Selected: e.Selected,
				})
			} else {
				if curPrefix == "" {
					curPrefix = part
				} else {
					curPrefix = curPrefix + "/" + part
				}
				folder, exists := folderMap[curPrefix]
				if !exists {
					folder = &torrentTreeNode{
						ID:       "dir_" + curPrefix,
						Name:     part,
						Type:     "folder",
						Selected: true,
					}
					folderMap[curPrefix] = folder
					*currentChildren = append(*currentChildren, folder)
				}
				currentChildren = &folder.Children
			}
		}
	}
	return roots
}

func collectSelectedTreeIndices(nodes []torrentTreeNode) []int {
	var indices []int
	var walk func(items []torrentTreeNode)
	walk = func(items []torrentTreeNode) {
		for _, n := range items {
			if n.Type == "folder" && len(n.Children) > 0 {
				deref := make([]torrentTreeNode, 0, len(n.Children))
				for _, ch := range n.Children {
					if ch != nil {
						deref = append(deref, *ch)
					}
				}
				walk(deref)
			} else if n.Type == "file" && n.Selected {
				switch v := n.ID.(type) {
				case float64:
					if int(v) > 0 {
						indices = append(indices, int(v))
					}
				case int:
					if v > 0 {
						indices = append(indices, v)
					}
				}
			}
		}
	}
	walk(nodes)
	return indices
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

			frame := make([]byte, 0, len(data)+10)
			frame = append(frame, 0x81)
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
				return
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
