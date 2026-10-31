// Package web implements the HTTP handlers for the Starfall Video Tools Studio
// (`GET /app/video-tools`, `GET /api/video/probe`, `GET /api/video/frame`,
// `POST /api/video/mux`, `POST /api/video/thumbnail`) and Owner Bot Settings UI.
//
// HARD VPS RULE: ZERO VIDEO TRANSCODING IS PERMITTED. All remuxing strictly uses
// `mkvmerge` (or `ffmpeg -c copy`) stream-copy operations scheduled on the single
// lossless remux slot of the Dispatcher.
package web

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"log/slog"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/starfall-org/starfall-go/internal/auth"
	"github.com/starfall-org/starfall-go/internal/engine"
	"github.com/starfall-org/starfall-go/internal/models"
	"github.com/starfall-org/starfall-go/internal/safety"
)

var (
	timestampRegex = regexp.MustCompile(`^[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,3})?$`)
	isoLangRegex   = regexp.MustCompile(`^[a-z]{3}$`)
)

// VideoStudioHandler serves the Video Tools Studio UI, FFprobe inspector,
// FFmpeg single-frame JPEG extractor, custom 16:9 thumbnail uploader, and
// lossless `mkvmerge` execution queue.
type VideoStudioHandler struct {
	assets      fs.FS
	registry    *engine.TaskRegistry
	dispatcher  *engine.Dispatcher
	watchdog    *safety.Watchdog
	downloadDir string
	logger      *slog.Logger
}

// NewVideoStudioHandler constructs a VideoStudioHandler backed by embedded web assets.
func NewVideoStudioHandler(
	assets fs.FS,
	registry *engine.TaskRegistry,
	dispatcher *engine.Dispatcher,
	watchdog *safety.Watchdog,
	downloadDir string,
	logger *slog.Logger,
) *VideoStudioHandler {
	if strings.TrimSpace(downloadDir) == "" {
		downloadDir = "/app/downloads"
	}
	return &VideoStudioHandler{
		assets:      assets,
		registry:    registry,
		dispatcher:  dispatcher,
		watchdog:    watchdog,
		downloadDir: downloadDir,
		logger:      logger,
	}
}

// RegisterRoutes mounts all Video Studio UI and API endpoints onto `mux`.
func (h *VideoStudioHandler) RegisterRoutes(mux *http.ServeMux, authMW *auth.Middleware) {
	if staticSub, err := fs.Sub(h.assets, "static"); err == nil {
		mux.Handle("GET /static/", http.StripPrefix("/static/", http.FileServer(http.FS(staticSub))))
	}

	mux.Handle("GET /app/video-tools", authMW.RequireAuthFunc(h.HandleVideoStudioPage))
	mux.Handle("GET /app/video-studio", authMW.RequireAuthFunc(h.HandleVideoStudioPage))
	mux.Handle("GET /api/video/probe", authMW.RequireAuthFunc(h.HandleProbeMedia))
	mux.Handle("GET /api/video/frame", authMW.RequireAuthFunc(h.HandleExtractFrame))
	mux.Handle("POST /api/video/mux", authMW.RequireAuthFunc(h.HandleLosslessMux))
	mux.Handle("POST /api/video/thumbnail", authMW.RequireAuthFunc(h.HandleUploadThumbnail))
}

// HandleVideoStudioPage (`GET /app/video-tools?task_id=...`) serves `video_studio.html`.
func (h *VideoStudioHandler) HandleVideoStudioPage(w http.ResponseWriter, r *http.Request) {
	rawHTML, err := fs.ReadFile(h.assets, "templates/video_studio.html")
	if err != nil {
		http.Error(w, "video studio template not found", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(rawHTML)
}

// ProbeStreamInfo represents a single parsed video, audio, or subtitle stream from `ffprobe`.
type ProbeStreamInfo struct {
	Index         int    `json:"index"`
	CodecType     string `json:"codec_type"`
	CodecName     string `json:"codec_name"`
	Profile       string `json:"profile,omitempty"`
	Width         int    `json:"width,omitempty"`
	Height        int    `json:"height,omitempty"`
	ResolutionTag string `json:"resolution_tag,omitempty"`
	BitDepth      string `json:"bit_depth,omitempty"`
	AspectRatio   string `json:"aspect_ratio,omitempty"`
	Channels      int    `json:"channels,omitempty"`
	Language      string `json:"language"`
	Title         string `json:"title"`
	Default       bool   `json:"default"`
	Forced        bool   `json:"forced"`
}

// ProbeResponse is the structured JSON returned by `GET /api/video/probe?task_id=...`.
type ProbeResponse struct {
	TaskID          string            `json:"task_id"`
	FileName        string            `json:"file_name"`
	ContainerFormat string            `json:"container_format"`
	DurationSeconds float64           `json:"duration_seconds"`
	SizeBytes       int64             `json:"size_bytes"`
	VideoStreams    []ProbeStreamInfo `json:"video_streams"`
	AudioStreams    []ProbeStreamInfo `json:"audio_streams"`
	SubtitleStreams []ProbeStreamInfo `json:"subtitle_streams"`
}

type ffprobeRawOutput struct {
	Streams []struct {
		Index              int    `json:"index"`
		CodecType          string `json:"codec_type"`
		CodecName          string `json:"codec_name"`
		Profile            string `json:"profile"`
		Width              int    `json:"width"`
		Height             int    `json:"height"`
		PixFmt             string `json:"pix_fmt"`
		ColorTransfer      string `json:"color_transfer"`
		DisplayAspectRatio string `json:"display_aspect_ratio"`
		Channels           int    `json:"channels"`
		Disposition        struct {
			Default int `json:"default"`
			Forced  int `json:"forced"`
		} `json:"disposition"`
		Tags map[string]string `json:"tags"`
	} `json:"streams"`
	Format struct {
		Filename   string `json:"filename"`
		FormatName string `json:"format_name"`
		Duration   string `json:"duration"`
		Size       string `json:"size"`
	} `json:"format"`
}

// HandleProbeMedia (`GET /api/video/probe?task_id=...`) executes
// `ffprobe -v quiet -print_format json -show_streams -show_format` and returns structured JSON.
func (h *VideoStudioHandler) HandleProbeMedia(w http.ResponseWriter, r *http.Request) {
	taskID := strings.TrimSpace(r.URL.Query().Get("task_id"))
	task, err := h.registry.Get(taskID)
	if err != nil || task.Snapshot().OutputPath == "" {
		writeJSONResponse(w, http.StatusNotFound, map[string]any{
			"ok":    false,
			"error": "task or media file not found on disk",
		})
		return
	}

	inputPath := task.Snapshot().OutputPath
	ctx, cancel := context.WithTimeout(r.Context(), 15*time.Second)
	defer cancel()

	cmd := exec.CommandContext(
		ctx,
		"ffprobe",
		"-v", "quiet",
		"-print_format", "json",
		"-show_streams",
		"-show_format",
		inputPath,
	)
	outBytes, err := cmd.Output()
	if err != nil {
		writeJSONResponse(w, http.StatusBadGateway, map[string]any{
			"ok":    false,
			"error": fmt.Sprintf("ffprobe failed: %v", err),
		})
		return
	}

	var raw ffprobeRawOutput
	if err := json.Unmarshal(outBytes, &raw); err != nil {
		writeJSONResponse(w, http.StatusInternalServerError, map[string]any{
			"ok":    false,
			"error": "failed to parse ffprobe JSON output",
		})
		return
	}

	durSec, _ := strconv.ParseFloat(raw.Format.Duration, 64)
	sizeBytes, _ := strconv.ParseInt(raw.Format.Size, 10, 64)

	resp := ProbeResponse{
		TaskID:          taskID,
		FileName:        filepath.Base(inputPath),
		ContainerFormat: raw.Format.FormatName,
		DurationSeconds: durSec,
		SizeBytes:       sizeBytes,
	}

	for _, st := range raw.Streams {
		lang := strings.ToLower(strings.TrimSpace(st.Tags["language"]))
		if lang == "" {
			lang = "und"
		}
		title := strings.TrimSpace(st.Tags["title"])
		info := ProbeStreamInfo{
			Index:       st.Index,
			CodecType:   st.CodecType,
			CodecName:   strings.ToUpper(st.CodecName),
			Profile:     st.Profile,
			Width:       st.Width,
			Height:      st.Height,
			AspectRatio: st.DisplayAspectRatio,
			Channels:    st.Channels,
			Language:    lang,
			Title:       title,
			Default:     st.Disposition.Default == 1,
			Forced:      st.Disposition.Forced == 1,
		}

		switch st.CodecType {
		case "video":
			info.ResolutionTag = classifyResolution(st.Width, st.Height)
			info.BitDepth = classifyBitDepth(st.PixFmt, st.ColorTransfer)
			resp.VideoStreams = append(resp.VideoStreams, info)
		case "audio":
			resp.AudioStreams = append(resp.AudioStreams, info)
		case "subtitle":
			resp.SubtitleStreams = append(resp.SubtitleStreams, info)
		}
	}

	writeJSONResponse(w, http.StatusOK, resp)
}

// HandleExtractFrame (`GET /api/video/frame?task_id=...&ss=HH:MM:SS`) runs
// `ffmpeg -ss {ss} -i {file} -vframes 1 -q:v 2` and streams the extracted JPEG directly
// to `http.ResponseWriter` without creating temporary disk files.
func (h *VideoStudioHandler) HandleExtractFrame(w http.ResponseWriter, r *http.Request) {
	taskID := strings.TrimSpace(r.URL.Query().Get("task_id"))
	ss := strings.TrimSpace(r.URL.Query().Get("ss"))
	if ss == "" {
		ss = "00:00:05"
	}
	if !timestampRegex.MatchString(ss) {
		http.Error(w, "invalid timestamp format: expected HH:MM:SS", http.StatusBadRequest)
		return
	}

	task, err := h.registry.Get(taskID)
	if err != nil || task.Snapshot().OutputPath == "" {
		http.Error(w, "media file not found for task", http.StatusNotFound)
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 15*time.Second)
	defer cancel()

	cmd := exec.CommandContext(
		ctx,
		"ffmpeg",
		"-nostdin",
		"-v", "error",
		"-ss", ss,
		"-i", task.Snapshot().OutputPath,
		"-vframes", "1",
		"-q:v", "2",
		"-f", "image2pipe",
		"-vcodec", "mjpeg",
		"pipe:1",
	)

	var frameBuf bytes.Buffer
	cmd.Stdout = &frameBuf
	if err := cmd.Run(); err != nil || frameBuf.Len() == 0 {
		http.Error(w, "failed to extract video frame", http.StatusBadGateway)
		return
	}

	w.Header().Set("Content-Type", "image/jpeg")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(frameBuf.Bytes())
}

// AudioTrackMuxSpec specifies an audio stream to keep, its priority order, and ISO-639 tag.
type AudioTrackMuxSpec struct {
	ID        int    `json:"id"`
	Lang      string `json:"lang"`
	IsDefault bool   `json:"is_default"`
	IsForced  bool   `json:"is_forced"`
}

// SubtitleTrackMuxSpec specifies a subtitle stream to keep and its ISO-639 tag.
type SubtitleTrackMuxSpec struct {
	ID        int    `json:"id"`
	Lang      string `json:"lang"`
	IsDefault bool   `json:"is_default"`
	IsForced  bool   `json:"is_forced"`
}

// LosslessMuxRequest is the JSON payload for `POST /api/video/mux`.
type LosslessMuxRequest struct {
	TaskID            string                 `json:"task_id"`
	OutputFile        string                 `json:"output_file"`
	AudioTracks       []AudioTrackMuxSpec    `json:"audio_tracks"`
	SubtitleTracks    []SubtitleTrackMuxSpec `json:"subtitle_tracks"`
	StripAllSubtitles bool                   `json:"strip_all_subtitles"`
	AudioDelayMs      int                    `json:"audio_delay_ms"`
	ThumbnailMode     string                 `json:"thumbnail_mode"`
	FrameTimestamp    string                 `json:"frame_timestamp"`
}

// HandleLosslessMux (`POST /api/video/mux`) validates the track selection, audio delay offset
// (-2500ms to +2500ms), and ISO-639 language codes, constructs a strictly lossless `mkvmerge`
// command (0% video transcoding), and schedules it on the Dispatcher's single remux slot.
func (h *VideoStudioHandler) HandleLosslessMux(w http.ResponseWriter, r *http.Request) {
	var req LosslessMuxRequest
	dec := json.NewDecoder(io.LimitReader(r.Body, 32*1024))
	if err := dec.Decode(&req); err != nil {
		writeJSONResponse(w, http.StatusBadRequest, map[string]any{"ok": false, "error": "invalid JSON payload"})
		return
	}

	if req.AudioDelayMs < -2500 || req.AudioDelayMs > 2500 {
		writeJSONResponse(w, http.StatusUnprocessableEntity, map[string]any{
			"ok":    false,
			"error": "audio_delay_ms must be between -2500 and +2500",
		})
		return
	}

	task, err := h.registry.Get(strings.TrimSpace(req.TaskID))
	if err != nil {
		writeJSONResponse(w, http.StatusNotFound, map[string]any{"ok": false, "error": "task not found"})
		return
	}

	snap := task.Snapshot()
	inputPath := snap.OutputPath
	if inputPath == "" {
		inputPath = filepath.Join(h.downloadDir, snap.Name)
	}

	ext := filepath.Ext(inputPath)
	outputPath := strings.TrimSuffix(inputPath, ext) + ".starfall.mkv"

	mkvArgs, cliPreview, err := buildValidatedMkvmergeArgs(inputPath, outputPath, req)
	if err != nil {
		writeJSONResponse(w, http.StatusUnprocessableEntity, map[string]any{"ok": false, "error": err.Error()})
		return
	}

	user, _ := auth.UserFromContext(r.Context())
	muxTaskID := fmt.Sprintf("%s_mux_%d", task.ID, time.Now().Unix()%10000)
	muxTask := models.NewTask(muxTaskID, user, filepath.Base(outputPath), inputPath, models.TaskTypeRemux, snap.Stats.TotalBytes)
	muxTask.OutputPath = outputPath
	muxTask.Work = func(ctx context.Context, t *models.Task) error {
		return h.dispatcher.RunMkvmergeRemux(ctx, inputPath, outputPath, mkvArgs...)
	}

	if err := h.dispatcher.Submit(muxTask); err != nil {
		writeJSONResponse(w, http.StatusServiceUnavailable, map[string]any{
			"ok":    false,
			"error": err.Error(),
		})
		return
	}

	writeJSONResponse(w, http.StatusAccepted, map[string]any{
		"ok":                   true,
		"mux_task_id":          muxTask.ID,
		"output_path":          outputPath,
		"cli_command":          cliPreview,
		"zero_cpu_transcoding": true,
		"telegram_dump_chat":   muxTask.TelegramDumpChat,
	})
}

// HandleUploadThumbnail (`POST /api/video/thumbnail`) accepts either a multipart image file
// or a JSON base64 data URI from the 16:9 canvas cropper and writes it to `/app/downloads/thumbnails/`.
func (h *VideoStudioHandler) HandleUploadThumbnail(w http.ResponseWriter, r *http.Request) {
	thumbDir := filepath.Join(h.downloadDir, "thumbnails")
	if err := os.MkdirAll(thumbDir, 0o755); err != nil {
		writeJSONResponse(w, http.StatusInternalServerError, map[string]any{"ok": false, "error": err.Error()})
		return
	}

	contentType := strings.ToLower(r.Header.Get("Content-Type"))
	if strings.HasPrefix(contentType, "multipart/form-data") {
		if err := r.ParseMultipartForm(8 * 1024 * 1024); err != nil {
			writeJSONResponse(w, http.StatusBadRequest, map[string]any{"ok": false, "error": "multipart file too large (max 8 MiB)"})
			return
		}
		taskID := sanitizeIdentifier(r.FormValue("task_id"))
		file, _, err := r.FormFile("thumbnail")
		if err != nil {
			writeJSONResponse(w, http.StatusBadRequest, map[string]any{"ok": false, "error": "missing thumbnail file field"})
			return
		}
		defer file.Close()

		destPath := filepath.Join(thumbDir, taskID+".jpg")
		out, err := os.OpenFile(destPath, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o644)
		if err != nil {
			writeJSONResponse(w, http.StatusInternalServerError, map[string]any{"ok": false, "error": err.Error()})
			return
		}
		defer out.Close()
		_, _ = io.Copy(out, io.LimitReader(file, 8*1024*1024))

		writeJSONResponse(w, http.StatusOK, map[string]any{"ok": true, "thumbnail_path": destPath})
		return
	}

	var body struct {
		TaskID  string `json:"task_id"`
		DataURL string `json:"data_url"`
	}
	if err := json.NewDecoder(io.LimitReader(r.Body, 8*1024*1024)).Decode(&body); err != nil {
		writeJSONResponse(w, http.StatusBadRequest, map[string]any{"ok": false, "error": "invalid JSON body"})
		return
	}
	taskID := sanitizeIdentifier(body.TaskID)
	commaIdx := strings.Index(body.DataURL, ",")
	if commaIdx == -1 {
		writeJSONResponse(w, http.StatusBadRequest, map[string]any{"ok": false, "error": "invalid image data_url"})
		return
	}
	rawBytes, err := base64.StdEncoding.DecodeString(body.DataURL[commaIdx+1:])
	if err != nil {
		writeJSONResponse(w, http.StatusBadRequest, map[string]any{"ok": false, "error": "invalid base64 image payload"})
		return
	}

	destPath := filepath.Join(thumbDir, taskID+".jpg")
	if err := os.WriteFile(destPath, rawBytes, 0o644); err != nil {
		writeJSONResponse(w, http.StatusInternalServerError, map[string]any{"ok": false, "error": err.Error()})
		return
	}

	writeJSONResponse(w, http.StatusOK, map[string]any{"ok": true, "thumbnail_path": destPath})
}

func buildValidatedMkvmergeArgs(
	inputPath string,
	outputPath string,
	req LosslessMuxRequest,
) ([]string, string, error) {
	var extraArgs []string

	if len(req.AudioTracks) == 0 {
		extraArgs = append(extraArgs, "--no-audio")
	} else {
		var ids []string
		var trackOrder []string
		trackOrder = append(trackOrder, "0:0")
		for _, a := range req.AudioTracks {
			if a.ID < 0 || a.ID > 64 {
				return nil, "", errors.New("invalid audio track ID")
			}
			lang := strings.ToLower(strings.TrimSpace(a.Lang))
			if !isoLangRegex.MatchString(lang) {
				lang = "und"
			}
			idStr := strconv.Itoa(a.ID)
			ids = append(ids, idStr)
			trackOrder = append(trackOrder, "0:"+idStr)

			extraArgs = append(extraArgs, "--sync", fmt.Sprintf("%d:%d", a.ID, req.AudioDelayMs))
			extraArgs = append(extraArgs, "--language", fmt.Sprintf("%d:%s", a.ID, lang))
			if a.IsDefault {
				extraArgs = append(extraArgs, "--default-track-flag", fmt.Sprintf("%d:yes", a.ID))
			}
			if a.IsForced {
				extraArgs = append(extraArgs, "--forced-display-flag", fmt.Sprintf("%d:yes", a.ID))
			}
		}
		extraArgs = append([]string{"--audio-tracks", strings.Join(ids, ",")}, extraArgs...)
		if len(req.AudioTracks) > 1 {
			extraArgs = append(extraArgs, "--track-order", strings.Join(trackOrder, ","))
		}
	}

	if req.StripAllSubtitles || len(req.SubtitleTracks) == 0 {
		extraArgs = append(extraArgs, "--no-subtitles")
	} else {
		var subIDs []string
		for _, s := range req.SubtitleTracks {
			if s.ID < 0 || s.ID > 128 {
				return nil, "", errors.New("invalid subtitle track ID")
			}
			lang := strings.ToLower(strings.TrimSpace(s.Lang))
			if !isoLangRegex.MatchString(lang) {
				lang = "und"
			}
			subIDs = append(subIDs, strconv.Itoa(s.ID))
			extraArgs = append(extraArgs, "--language", fmt.Sprintf("%d:%s", s.ID, lang))
			if s.IsDefault {
				extraArgs = append(extraArgs, "--default-track-flag", fmt.Sprintf("%d:yes", s.ID))
			}
			if s.IsForced {
				extraArgs = append(extraArgs, "--forced-display-flag", fmt.Sprintf("%d:yes", s.ID))
			}
		}
		extraArgs = append(extraArgs, "--subtitle-tracks", strings.Join(subIDs, ","))
	}

	preview := fmt.Sprintf("mkvmerge -o %q %s %q", outputPath, strings.Join(extraArgs, " "), inputPath)
	return extraArgs, preview, nil
}

func classifyResolution(w, h int) string {
	switch {
	case w >= 3800 || h >= 2100:
		return "4K UHD"
	case w >= 2500 || h >= 1400:
		return "1440p QHD"
	case w >= 1900 || h >= 1000:
		return "1080p FHD"
	case w >= 1200 || h >= 700:
		return "720p HD"
	default:
		return fmt.Sprintf("%dx%d", w, h)
	}
}

func classifyBitDepth(pixFmt, colorTransfer string) string {
	is10Bit := strings.Contains(pixFmt, "10")
	isHDR := strings.Contains(strings.ToLower(colorTransfer), "smpte2084") ||
		strings.Contains(strings.ToLower(colorTransfer), "arib-std-b67")
	if is10Bit && isHDR {
		return "10-bit HDR"
	}
	if is10Bit {
		return "10-bit SDR"
	}
	return "8-bit SDR"
}

func sanitizeIdentifier(s string) string {
	s = strings.TrimSpace(s)
	if s == "" {
		return "default"
	}
	var out strings.Builder
	for _, r := range s {
		if (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') || (r >= '0' && r <= '9') || r == '_' || r == '-' {
			out.WriteRune(r)
		}
	}
	if out.Len() == 0 {
		return "default"
	}
	return out.String()
}

func writeJSONResponse(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(payload)
}
