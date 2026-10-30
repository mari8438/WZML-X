// Package direct implements direct high-speed CDN URL resolvers for Terabox
// (`terabox.com`, `1024terabox.com`, `teraboxapp.com`) and MediaFire (`mediafire.com`),
// dispatching resolved CDN streams to the 4-connection chunked HTTPDownloader.
package direct

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"time"

	"github.com/starfall-org/starfall-go/internal/models"
)

var (
	teraboxSurlPathRegex  = regexp.MustCompile(`/s/1?([a-zA-Z0-9_-]+)`)
	mediafireDirectLinkRe = regexp.MustCompile(`href="(https?://download[0-9]*\.mediafire\.com/[^"]+)"`)

	// ErrTeraboxResolveFailed indicates no direct CDN `dlink` could be extracted.
	ErrTeraboxResolveFailed = errors.New("unable to resolve Terabox/MediaFire direct CDN link")
)

// TeraboxDownloader resolves Terabox and MediaFire share links into direct high-speed
// CDN URLs and streams them to disk via HTTPDownloader.
type TeraboxDownloader struct {
	httpDL    *HTTPDownloader
	cookie    string
	userAgent string
	logger    *slog.Logger
}

// NewTeraboxDownloader constructs a Terabox & MediaFire direct CDN resolver.
func NewTeraboxDownloader(
	httpDL *HTTPDownloader,
	cookie string,
	userAgent string,
	logger *slog.Logger,
) *TeraboxDownloader {
	return &TeraboxDownloader{
		httpDL:    httpDL,
		cookie:    strings.TrimSpace(cookie),
		userAgent: userAgent,
		logger:    logger,
	}
}

// Download resolves the direct high-speed CDN stream from a Terabox or MediaFire URL
// and executes a 4-connection chunked download into `destDir`.
func (t *TeraboxDownloader) Download(
	ctx context.Context,
	task *models.Task,
	rawURL string,
	destDir string,
) (*DownloadResult, error) {
	parsed, err := url.Parse(strings.TrimSpace(rawURL))
	if err != nil {
		return nil, err
	}

	host := strings.ToLower(parsed.Hostname())
	var directURL, fileName string
	headers := make(map[string]string)

	if strings.Contains(host, "mediafire.com") {
		directURL, fileName, err = t.resolveMediaFireCDN(ctx, rawURL)
		if err != nil {
			return nil, err
		}
		headers["Referer"] = "https://www.mediafire.com/"
	} else {
		directURL, fileName, err = t.resolveTeraboxCDN(ctx, parsed)
		if err != nil {
			return nil, err
		}
		headers["Referer"] = "https://www.terabox.com/"
		if t.cookie != "" {
			headers["Cookie"] = t.cookie
		}
	}

	res, err := t.httpDL.DownloadWithOptions(ctx, task, directURL, destDir, RequestOptions{
		Headers:          headers,
		FilenameOverride: fileName,
	})
	if err != nil {
		return nil, err
	}
	res.Source = SourceTerabox
	return res, nil
}

type teraboxShareInfoResponse struct {
	Errno int `json:"errno"`
	List  []struct {
		ServerFilename string `json:"server_filename"`
		Size           int64  `json:"size"`
		Dlink          string `json:"dlink"`
	} `json:"list"`
}

func (t *TeraboxDownloader) resolveTeraboxCDN(ctx context.Context, parsed *url.URL) (string, string, error) {
	surl := strings.TrimSpace(parsed.Query().Get("surl"))
	surl = strings.TrimPrefix(surl, "1")
	if surl == "" {
		if m := teraboxSurlPathRegex.FindStringSubmatch(parsed.Path); len(m) == 2 {
			surl = m[1]
		}
	}
	if surl == "" {
		return "", "", fmt.Errorf("%w: missing surl identifier", ErrTeraboxResolveFailed)
	}

	apiURL := fmt.Sprintf("https://www.terabox.com/api/shorturlinfo?shorturl=1%s&root=1", url.QueryEscape(surl))
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, apiURL, nil)
	if err != nil {
		return "", "", err
	}
	req.Header.Set("User-Agent", t.userAgent)
	req.Header.Set("Referer", parsed.String())
	if t.cookie != "" {
		req.Header.Set("Cookie", t.cookie)
	}

	client := &http.Client{Timeout: 15 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return "", "", fmt.Errorf("querying terabox shorturlinfo API: %w", err)
	}
	defer resp.Body.Close()

	var info teraboxShareInfoResponse
	if err := json.NewDecoder(io.LimitReader(resp.Body, 128*1024)).Decode(&info); err != nil {
		return "", "", fmt.Errorf("decoding terabox share info: %w", err)
	}
	if info.Errno != 0 || len(info.List) == 0 || info.List[0].Dlink == "" {
		return "", "", fmt.Errorf("%w (errno=%d)", ErrTeraboxResolveFailed, info.Errno)
	}

	item := info.List[0]
	return item.Dlink, item.ServerFilename, nil
}

func (t *TeraboxDownloader) resolveMediaFireCDN(ctx context.Context, rawURL string) (string, string, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, rawURL, nil)
	if err != nil {
		return "", "", err
	}
	req.Header.Set("User-Agent", t.userAgent)

	client := &http.Client{Timeout: 15 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return "", "", fmt.Errorf("fetching mediafire page: %w", err)
	}
	defer resp.Body.Close()

	// If the URL already redirected straight to a download binary, return it immediately.
	if !strings.Contains(strings.ToLower(resp.Header.Get("Content-Type")), "text/html") {
		return resp.Request.URL.String(), extractFileName(resp), nil
	}

	bodyBytes, err := io.ReadAll(io.LimitReader(resp.Body, 256*1024))
	if err != nil {
		return "", "", err
	}
	if m := mediafireDirectLinkRe.FindSubmatch(bodyBytes); len(m) == 2 {
		cdnURL := string(m[1])
		return cdnURL, "", nil
	}
	return "", "", ErrTeraboxResolveFailed
}
