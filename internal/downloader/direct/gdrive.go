// Package direct implements the Google Drive quota-bypass stream resolver and
// chunked downloader for Starfall-Go Debrid.
package direct

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/cookiejar"
	"net/url"
	"regexp"
	"strings"
	"time"

	"github.com/starfall-org/starfall-go/internal/models"
)

var (
	gdrivePathFileIDRegex = regexp.MustCompile(`/file/d/([a-zA-Z0-9_-]{10,})`)
	gdriveFormUUIDRegex   = regexp.MustCompile(`name="uuid"\s+value="([^"]+)"`)
	gdriveFormConfirmRe   = regexp.MustCompile(`name="confirm"\s+value="([^"]+)"`)
	gdriveFormATRegex     = regexp.MustCompile(`name="at"\s+value="([^"]+)"`)
	gdriveTitleRegex      = regexp.MustCompile(`<span class="uc-name-size"><a [^>]*>([^<]+)</a>`)

	// ErrInvalidGDriveURL is returned when no Google Drive file ID can be extracted.
	ErrInvalidGDriveURL = errors.New("unable to extract file ID from Google Drive URL")

	// ErrGDriveQuotaExceeded is returned when Google Drive blocks all bypass attempts.
	ErrGDriveQuotaExceeded = errors.New("google drive download quota exceeded for this file")
)

// GDriveDownloader resolves Google Drive share links into direct chunked media streams,
// bypassing large-file virus-scan interstitials and quota warning tokens before streaming
// via the 4-connection HTTPDownloader.
type GDriveDownloader struct {
	httpDL    *HTTPDownloader
	apiKey    string
	userAgent string
	logger    *slog.Logger
}

// NewGDriveDownloader constructs a Google Drive quota-bypass downloader.
func NewGDriveDownloader(
	httpDL *HTTPDownloader,
	apiKey string,
	userAgent string,
	logger *slog.Logger,
) *GDriveDownloader {
	return &GDriveDownloader{
		httpDL:    httpDL,
		apiKey:    strings.TrimSpace(apiKey),
		userAgent: userAgent,
		logger:    logger,
	}
}

// ExtractFileID parses a Google Drive file ID from `/file/d/<id>`, `?id=<id>`, or `/uc?id=<id>`.
func ExtractFileID(rawURL string) (string, error) {
	parsed, err := url.Parse(strings.TrimSpace(rawURL))
	if err != nil {
		return "", ErrInvalidGDriveURL
	}
	if m := gdrivePathFileIDRegex.FindStringSubmatch(parsed.Path); len(m) == 2 {
		return m[1], nil
	}
	if id := strings.TrimSpace(parsed.Query().Get("id")); len(id) >= 10 {
		return id, nil
	}
	return "", ErrInvalidGDriveURL
}

// Download resolves the direct Google Drive media stream (bypassing virus-scan/quota
// confirmation tokens) and delegates the chunked transfer to HTTPDownloader.
func (g *GDriveDownloader) Download(
	ctx context.Context,
	task *models.Task,
	rawURL string,
	destDir string,
) (*DownloadResult, error) {
	fileID, err := ExtractFileID(rawURL)
	if err != nil {
		return nil, err
	}

	// If an optional Drive API v3 key is configured, try the direct alt=media endpoint first.
	if g.apiKey != "" {
		apiURL := fmt.Sprintf("https://www.googleapis.com/drive/v3/files/%s?alt=media&key=%s",
			url.PathEscape(fileID), url.QueryEscape(g.apiKey))
		res, apiErr := g.httpDL.DownloadWithOptions(ctx, task, apiURL, destDir, RequestOptions{})
		if apiErr == nil {
			res.Source = SourceGDrive
			return res, nil
		}
		g.logger.Warn("gdrive API v3 alt=media fallback triggered",
			slog.String("file_id", fileID),
			slog.String("reason", apiErr.Error()),
		)
	}

	directURL, cookies, fileName, err := g.resolveQuotaBypassStream(ctx, fileID)
	if err != nil {
		return nil, err
	}

	res, err := g.httpDL.DownloadWithOptions(ctx, task, directURL, destDir, RequestOptions{
		Cookies:          cookies,
		FilenameOverride: fileName,
		Headers: map[string]string{
			"Referer": "https://drive.google.com/",
		},
	})
	if err != nil {
		return nil, err
	}
	res.Source = SourceGDrive
	return res, nil
}

// resolveQuotaBypassStream negotiates with `drive.usercontent.google.com` to bypass the
// virus-scan warning page and extract the signed download stream URL and session cookies.
func (g *GDriveDownloader) resolveQuotaBypassStream(
	ctx context.Context,
	fileID string,
) (string, []*http.Cookie, string, error) {
	jar, _ := cookiejar.New(nil)
	client := &http.Client{
		Jar:     jar,
		Timeout: 20 * time.Second,
	}

	// Direct usercontent endpoint with confirm=t bypasses standard interstitials.
	initialURL := fmt.Sprintf(
		"https://drive.usercontent.google.com/download?id=%s&export=download&confirm=t",
		url.QueryEscape(fileID),
	)

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, initialURL, nil)
	if err != nil {
		return "", nil, "", err
	}
	req.Header.Set("User-Agent", g.userAgent)
	req.Header.Set("Range", "bytes=0-16383")

	resp, err := client.Do(req)
	if err != nil {
		return "", nil, "", fmt.Errorf("requesting gdrive stream for %s: %w", fileID, err)
	}
	defer resp.Body.Close()

	contentType := strings.ToLower(resp.Header.Get("Content-Type"))
	if !strings.Contains(contentType, "text/html") {
		// Already resolved to binary stream!
		u := resp.Request.URL
		return u.String(), jar.Cookies(u), extractFileName(resp), nil
	}

	// Parse HTML warning page for hidden `uuid`, `confirm`, and `at` tokens.
	bodyBytes, err := io.ReadAll(io.LimitReader(resp.Body, 256*1024))
	if err != nil {
		return "", nil, "", err
	}
	htmlStr := string(bodyBytes)

	if strings.Contains(htmlStr, "Too many users have viewed or downloaded this file") {
		return "", nil, "", ErrGDriveQuotaExceeded
	}

	var fileName string
	if m := gdriveTitleRegex.FindStringSubmatch(htmlStr); len(m) == 2 {
		fileName = strings.TrimSpace(m[1])
	}

	params := url.Values{}
	params.Set("id", fileID)
	params.Set("export", "download")

	if m := gdriveFormConfirmRe.FindStringSubmatch(htmlStr); len(m) == 2 {
		params.Set("confirm", m[1])
	} else {
		params.Set("confirm", "t")
	}
	if m := gdriveFormUUIDRegex.FindStringSubmatch(htmlStr); len(m) == 2 {
		params.Set("uuid", m[1])
	}
	if m := gdriveFormATRegex.FindStringSubmatch(htmlStr); len(m) == 2 {
		params.Set("at", m[1])
	}

	confirmedURL := "https://drive.usercontent.google.com/download?" + params.Encode()
	parsedURL, _ := url.Parse(confirmedURL)
	return confirmedURL, jar.Cookies(parsedURL), fileName, nil
}
