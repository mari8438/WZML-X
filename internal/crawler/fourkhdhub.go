package crawler

import (
	"context"
	"io"
	"net/http"
	"regexp"
	"strings"
	"time"
)

var (
	postArticleRegex = regexp.MustCompile(`(?s)<article[^>]*>.*?<h2[^>]*><a\s+href="([^"]+)"[^>]*>(.*?)</a>.*?</h2>.*?</article>`)
	hubcloudRegex    = regexp.MustCompile(`https?://[^\s"'>]*hubcloud[^\s"'>]*`)
)

// FourKHDHubCrawler monitors 4KHDHub for high-bitrate releases.
type FourKHDHubCrawler struct {
	baseURL    string
	httpClient *http.Client
}

// NewFourKHDHubCrawler creates a 4KHDHub crawler.
func NewFourKHDHubCrawler(baseURL string) *FourKHDHubCrawler {
	if baseURL == "" {
		baseURL = "https://4khdhub.dad"
	}
	return &FourKHDHubCrawler{
		baseURL: strings.TrimRight(baseURL, "/"),
		httpClient: &http.Client{
			Timeout: 30 * time.Second,
		},
	}
}

func (c *FourKHDHubCrawler) Name() string {
	return "fourkhdhub"
}

// CrawlOnce scrapes the latest movie and anime posts.
func (c *FourKHDHubCrawler) CrawlOnce(ctx context.Context) ([]DiscoveredRelease, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.baseURL, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Starfall-Go/1.2")

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}

	htmlContent := string(body)
	matches := postArticleRegex.FindAllStringSubmatch(htmlContent, 20)

	var releases []DiscoveredRelease
	for _, m := range matches {
		if len(m) < 3 {
			continue
		}
		pageURL := m[1]
		rawTitle := strings.TrimSpace(m[2])

		hasTamil := HasTamilToken(rawTitle)

		releases = append(releases, DiscoveredRelease{
			Source:      c.Name(),
			ItemKey:     pageURL,
			Title:       rawTitle,
			PageURL:     pageURL,
			DownloadURI: pageURL,
			HasTamil:    hasTamil,
		})
	}

	return releases, nil
}
