package crawler

import (
	"context"
	"encoding/xml"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"time"
)

var (
	magnetRegex  = regexp.MustCompile(`magnet:\?xt=urn:btih:[a-zA-Z0-9]+[^\s"'>]*`)
	torrentRegex = regexp.MustCompile(`https?://[^\s"'>]+\.torrent`)
)

// TamilMVCrawler monitors 1TamilMV forums for new Tamil movies and anime dubs.
type TamilMVCrawler struct {
	baseURL    string
	httpClient *http.Client
}

// NewTamilMVCrawler creates a 1TamilMV crawler.
func NewTamilMVCrawler(baseURL string) *TamilMVCrawler {
	if baseURL == "" {
		baseURL = "https://www.1tamilmv.vet"
	}
	return &TamilMVCrawler{
		baseURL: strings.TrimRight(baseURL, "/"),
		httpClient: &http.Client{
			Timeout: 30 * time.Second,
		},
	}
}

func (c *TamilMVCrawler) Name() string {
	return "tamilmv"
}

// CrawlOnce checks the RSS/discover feed of TamilMV.
func (c *TamilMVCrawler) CrawlOnce(ctx context.Context) ([]DiscoveredRelease, error) {
	feedURL := fmt.Sprintf("%s/index.php?/discover/all.xml/", c.baseURL)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, feedURL, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Starfall-Go/1.2")

	resp, err := c.httpClient.Do(req)
	if err != nil {
		// Fallback to HTML home page crawl
		return c.crawlHTML(ctx)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return c.crawlHTML(ctx)
	}

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}

	var rss struct {
		Channel struct {
			Items []struct {
				Title       string `xml:"title"`
				Link        string `xml:"link"`
				Description string `xml:"description"`
				GUID        string `xml:"guid"`
			} `xml:"item"`
		} `xml:"channel"`
	}

	if err := xml.Unmarshal(body, &rss); err != nil || len(rss.Channel.Items) == 0 {
		return c.crawlHTML(ctx)
	}

	var releases []DiscoveredRelease
	for _, it := range rss.Channel.Items {
		title := strings.TrimSpace(it.Title)
		hasTamil := HasTamilToken(title) || HasTamilToken(it.Description)
		itemKey := it.GUID
		if itemKey == "" {
			itemKey = it.Link
		}

		// Find magnet in description
		var dlURI string
		if m := magnetRegex.FindString(it.Description); m != "" {
			dlURI = m
		} else if t := torrentRegex.FindString(it.Description); t != "" {
			dlURI = t
		} else {
			dlURI = it.Link
		}

		releases = append(releases, DiscoveredRelease{
			Source:      c.Name(),
			ItemKey:     itemKey,
			Title:       title,
			PageURL:     it.Link,
			DownloadURI: dlURI,
			HasTamil:    hasTamil,
		})
	}

	return releases, nil
}

func (c *TamilMVCrawler) crawlHTML(ctx context.Context) ([]DiscoveredRelease, error) {
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
	magnets := magnetRegex.FindAllString(htmlContent, 10)

	var releases []DiscoveredRelease
	for _, m := range magnets {
		// Extract dn parameter from magnet
		title := "TamilMV Release"
		if u, err := url.Parse(m); err == nil {
			if dn := u.Query().Get("dn"); dn != "" {
				title = dn
			}
		}

		releases = append(releases, DiscoveredRelease{
			Source:      c.Name(),
			ItemKey:     m,
			Title:       title,
			PageURL:     c.baseURL,
			DownloadURI: m,
			HasTamil:    HasTamilToken(title),
		})
	}

	return releases, nil
}
