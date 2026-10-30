// Package direct implements the zero-memory-ballooning Direct Link Leech Engine
// for Starfall-Go Debrid, supporting Google Drive quota bypass, Mega.nz AES-CTR
// decrypted streaming, Terabox/MediaFire CDN extraction, yt-dlp video streaming,
// and 4-connection parallel HTTP Range downloading.
package direct

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/url"
	"path"
	"strings"

	"github.com/starfall-org/starfall-go/internal/downloader/buffer"
	"github.com/starfall-org/starfall-go/internal/ipc"
	"github.com/starfall-org/starfall-go/internal/models"
	"github.com/starfall-org/starfall-go/internal/safety"
)

// SourceKind identifies which specialized direct resolver handles a given URL.
type SourceKind string

const (
	SourceGDrive     SourceKind = "GDRIVE"
	SourceMega       SourceKind = "MEGA"
	SourceTerabox    SourceKind = "TERABOX_MEDIAFIRE"
	SourceYTDLP      SourceKind = "YTDLP"
	SourceDirectHTTP SourceKind = "DIRECT_HTTP"
)

var (
	// ErrEmptyURL indicates an empty or whitespace-only URL was passed to the resolver.
	ErrEmptyURL = errors.New("source URL cannot be empty")

	// ErrUnsupportedScheme indicates a non-HTTP/HTTPS URL was provided to the direct engine.
	ErrUnsupportedScheme = errors.New("unsupported URL scheme: only http and https are supported")
)

// DownloadResult summarizes the completed on-disk artifact produced by a direct downloader.
type DownloadResult struct {
	FilePath   string
	FileName   string
	BytesSaved int64
	Source     SourceKind
}

// Provider defines the execution interface implemented by all specialized direct downloaders.
type Provider interface {
	Download(ctx context.Context, task *models.Task, rawURL string, destDir string) (*DownloadResult, error)
}

// ResolverConfig holds shared dependencies for all direct link resolvers and downloaders.
type ResolverConfig struct {
	DefaultDownloadDir string
	UserAgent          string
	GDriveAPIKey       string
	TeraboxCookie      string
	YTDLPBinaryPath    string
}

// Resolver inspects incoming URLs and dispatches downloads to the appropriate
// specialized extractor (Google Drive, Mega.nz, Terabox/MediaFire, yt-dlp, or Parallel HTTP).
type Resolver struct {
	cfg        ResolverConfig
	bufPool    *buffer.Pool
	watchdog   *safety.Watchdog
	limiter    *safety.RateLimiter
	bus        *ipc.Bus
	logger     *slog.Logger

	httpDL    *HTTPDownloader
	gdriveDL  *GDriveDownloader
	megaDL    *MegaDownloader
	teraboxDL *TeraboxDownloader
	ytdlpDL   *YTDLPDownloader
}

// NewResolver constructs a Universal Link Resolver and wires all 5 direct download backends.
func NewResolver(
	cfg ResolverConfig,
	bufPool *buffer.Pool,
	watchdog *safety.Watchdog,
	limiter *safety.RateLimiter,
	bus *ipc.Bus,
	logger *slog.Logger,
) *Resolver {
	if strings.TrimSpace(cfg.DefaultDownloadDir) == "" {
		cfg.DefaultDownloadDir = "/app/downloads"
	}
	if strings.TrimSpace(cfg.UserAgent) == "" {
		cfg.UserAgent = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
	}
	if bufPool == nil {
		bufPool = buffer.DefaultPool
	}

	httpDL := NewHTTPDownloader(bufPool, watchdog, limiter, bus, logger, cfg.UserAgent)
	return &Resolver{
		cfg:       cfg,
		bufPool:   bufPool,
		watchdog:  watchdog,
		limiter:   limiter,
		bus:       bus,
		logger:    logger,
		httpDL:    httpDL,
		gdriveDL:  NewGDriveDownloader(httpDL, cfg.GDriveAPIKey, cfg.UserAgent, logger),
		megaDL:    NewMegaDownloader(bufPool, watchdog, limiter, bus, logger, cfg.UserAgent),
		teraboxDL: NewTeraboxDownloader(httpDL, cfg.TeraboxCookie, cfg.UserAgent, logger),
		ytdlpDL:   NewYTDLPDownloader(cfg.YTDLPBinaryPath, watchdog, bus, logger),
	}
}

// Classify inspects `rawURL` and determines which direct extractor handles it:
//   - drive.google.com / docs.google.com  -> SourceGDrive (gdrive.go)
//   - mega.nz / mega.co.nz                -> SourceMega (mega.go)
//   - terabox.com / mediafire.com         -> SourceTerabox (terabox.go)
//   - video portals or .m3u8 / .mpd       -> SourceYTDLP (ytdlp.go)
//   - standard HTTP/HTTPS direct links    -> SourceDirectHTTP (http_downloader.go)
func (r *Resolver) Classify(rawURL string) (SourceKind, *url.URL, error) {
	trimmed := strings.TrimSpace(rawURL)
	if trimmed == "" {
		return "", nil, ErrEmptyURL
	}

	parsed, err := url.Parse(trimmed)
	if err != nil || parsed.Host == "" {
		return "", nil, fmt.Errorf("invalid URL %q: %w", rawURL, err)
	}

	scheme := strings.ToLower(parsed.Scheme)
	if scheme != "http" && scheme != "https" {
		return "", nil, fmt.Errorf("%w: %s", ErrUnsupportedScheme, parsed.Scheme)
	}

	host := strings.ToLower(parsed.Hostname())
	lowerPath := strings.ToLower(parsed.Path)

	switch {
	case isGoogleDriveHost(host):
		return SourceGDrive, parsed, nil
	case isMegaHost(host):
		return SourceMega, parsed, nil
	case isTeraboxOrMediaFireHost(host):
		return SourceTerabox, parsed, nil
	case isVideoPortalOrStream(host, lowerPath, parsed.RawQuery):
		return SourceYTDLP, parsed, nil
	default:
		return SourceDirectHTTP, parsed, nil
	}
}

// ResolveAndDownload classifies `rawURL` and dispatches execution to the matching backend,
// streaming chunks directly into `destDir` (defaults to `/app/downloads/`).
func (r *Resolver) ResolveAndDownload(
	ctx context.Context,
	task *models.Task,
	rawURL string,
	destDir string,
) (*DownloadResult, error) {
	if strings.TrimSpace(destDir) == "" {
		destDir = r.cfg.DefaultDownloadDir
	}

	kind, _, err := r.Classify(rawURL)
	if err != nil {
		return nil, err
	}

	r.logger.Info("universal resolver classified source URL",
		slog.String("task_id", task.ID),
		slog.String("source_kind", string(kind)),
		slog.String("dest_dir", destDir),
	)

	switch kind {
	case SourceGDrive:
		return r.gdriveDL.Download(ctx, task, rawURL, destDir)
	case SourceMega:
		return r.megaDL.Download(ctx, task, rawURL, destDir)
	case SourceTerabox:
		return r.teraboxDL.Download(ctx, task, rawURL, destDir)
	case SourceYTDLP:
		return r.ytdlpDL.Download(ctx, task, rawURL, destDir)
	default:
		return r.httpDL.Download(ctx, task, rawURL, destDir)
	}
}

func isGoogleDriveHost(host string) bool {
	return host == "drive.google.com" ||
		host == "docs.google.com" ||
		host == "drive.usercontent.google.com"
}

func isMegaHost(host string) bool {
	return host == "mega.nz" ||
		strings.HasSuffix(host, ".mega.nz") ||
		host == "mega.co.nz" ||
		strings.HasSuffix(host, ".mega.co.nz")
}

func isTeraboxOrMediaFireHost(host string) bool {
	teraboxDomains := []string{
		"terabox.com",
		"1024terabox.com",
		"teraboxapp.com",
		"nephobox.com",
		"4funbox.com",
		"mirrobox.com",
		"momerybox.com",
		"teraboxlink.com",
		"mediafire.com",
	}
	for _, d := range teraboxDomains {
		if host == d || strings.HasSuffix(host, "."+d) {
			return true
		}
	}
	return false
}

func isVideoPortalOrStream(host, lowerPath, rawQuery string) bool {
	ext := strings.ToLower(path.Ext(lowerPath))
	if ext == ".m3u8" || ext == ".mpd" || strings.Contains(strings.ToLower(rawQuery), ".m3u8") {
		return true
	}

	videoHosts := []string{
		"youtube.com",
		"youtu.be",
		"twitch.tv",
		"crunchyroll.com",
		"bilibili.com",
		"b23.tv",
		"vimeo.com",
		"dailymotion.com",
		"nicovideo.jp",
		"streamtape.com",
		"doodstream.com",
		"filemoon.sx",
		"mp4upload.com",
	}
	for _, vh := range videoHosts {
		if host == vh || strings.HasSuffix(host, "."+vh) {
			return true
		}
	}
	return false
}
