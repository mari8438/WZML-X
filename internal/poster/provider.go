// Package poster implements keyless AniList GraphQL + TMDb v3 metadata resolution
// with a bounded 256-entry TTL cache and pure-Go 1280x720 (16:9) Templates 1-11 rendering.
package poster

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"html"
	"image"
	_ "image/gif"
	_ "image/jpeg"
	_ "image/png"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/starfall-org/starfall-go/internal/models"
	_ "golang.org/x/image/webp"
)

const (
	anilistEndpoint = "https://graphql.anilist.co"
	maxCacheEntries = 256
	cacheTTL        = 1 * time.Hour
	maxImageBytes   = 5 * 1024 * 1024 // 5 MiB strict image download cap
)

var reHTMLTags = regexp.MustCompile(`<[^>]+>`)

type cacheEntry struct {
	md        models.MediaMetadata
	expiresAt time.Time
}

// Provider resolves rich anime/TV/movie metadata and artwork from AniList and TMDb.
type Provider struct {
	httpClient *http.Client
	tmdbAPIKey string

	mu    sync.RWMutex
	cache map[string]cacheEntry
}

// NewProvider constructs a bounded, memory-safe metadata Provider.
func NewProvider(tmdbAPIKey string) *Provider {
	return &Provider{
		httpClient: &http.Client{Timeout: 8 * time.Second},
		tmdbAPIKey: strings.TrimSpace(tmdbAPIKey),
		cache:      make(map[string]cacheEntry, 64),
	}
}

// SetTMDBKey updates the TMDb v3 API key at runtime (e.g. via `/bsetting`).
func (p *Provider) SetTMDBKey(key string) {
	p.mu.Lock()
	p.tmdbAPIKey = strings.TrimSpace(key)
	p.mu.Unlock()
}

// Enrich queries AniList (and optionally TMDb) to populate Rating, Genres, Synopsis,
// CoverURL, BannerURL, TotalEpisodes, and canonical Year/Title onto `base`.
func (p *Provider) Enrich(ctx context.Context, base models.MediaMetadata) models.MediaMetadata {
	searchTitle := strings.TrimSpace(base.Title)
	if searchTitle == "" {
		return base
	}

	cacheKey := strings.ToLower(fmt.Sprintf("%s|%d|%s", searchTitle, base.Year, base.Category))
	if cached, ok := p.getCached(cacheKey); ok {
		return mergeRemoteIntoBase(base, cached)
	}

	var fetched models.MediaMetadata
	var found bool

	// Try AniList first when category is ANIME or TMDb key is absent
	p.mu.RLock()
	tmdbKey := p.tmdbAPIKey
	p.mu.RUnlock()

	if base.Category == "ANIME" || tmdbKey == "" {
		if ani, err := p.queryAniList(ctx, searchTitle, base.Season); err == nil && ani.Title != "" {
			fetched = ani
			found = true
		}
	}

	if !found && tmdbKey != "" {
		if tm, err := p.queryTMDb(ctx, searchTitle, base.Year, base.Category, tmdbKey); err == nil && tm.Title != "" {
			fetched = tm
			found = true
		}
	}

	// Fallback to AniList even for TV/MOVIE if TMDb didn't match (many anime releases lack group tags)
	if !found && base.Category != "ANIME" && tmdbKey != "" {
		if ani, err := p.queryAniList(ctx, searchTitle, base.Season); err == nil && ani.Title != "" {
			fetched = ani
			found = true
		}
	}

	if found {
		p.putCached(cacheKey, fetched)
		return mergeRemoteIntoBase(base, fetched)
	}
	return base
}

// FetchImage downloads and decodes a cover or banner image under a strict 5 MiB memory cap.
func (p *Provider) FetchImage(ctx context.Context, imgURL string) (image.Image, error) {
	imgURL = strings.TrimSpace(imgURL)
	if imgURL == "" {
		return nil, fmt.Errorf("empty image url")
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, imgURL, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("User-Agent", "Starfall-Go/1.2 (+https://t.me/Anime_Starfall)")

	resp, err := p.httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("image http status %d", resp.StatusCode)
	}

	raw, err := io.ReadAll(io.LimitReader(resp.Body, maxImageBytes))
	if err != nil {
		return nil, err
	}
	img, _, err := image.Decode(bytes.NewReader(raw))
	return img, err
}

func (p *Provider) queryAniList(ctx context.Context, title string, season int) (models.MediaMetadata, error) {
	searchQuery := title
	if season > 1 && !strings.Contains(strings.ToLower(title), "season") {
		searchQuery = fmt.Sprintf("%s Season %d", title, season)
	}

	const gql = `query ($search: String) {
		Media(search: $search, type: ANIME) {
			title { english romaji }
			seasonYear
			episodes
			averageScore
			format
			genres
			description(asHtml: false)
			coverImage { extraLarge large }
			bannerImage
		}
	}`

	bodyBytes, _ := json.Marshal(map[string]any{
		"query":     gql,
		"variables": map[string]any{"search": searchQuery},
	})

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, anilistEndpoint, bytes.NewReader(bodyBytes))
	if err != nil {
		return models.MediaMetadata{}, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")

	resp, err := p.httpClient.Do(req)
	if err != nil {
		return models.MediaMetadata{}, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return models.MediaMetadata{}, fmt.Errorf("anilist status %d", resp.StatusCode)
	}

	var parsed struct {
		Data struct {
			Media struct {
				Title struct {
					English string `json:"english"`
					Romaji  string `json:"romaji"`
				} `json:"title"`
				SeasonYear   int      `json:"seasonYear"`
				Episodes     int      `json:"episodes"`
				AverageScore int      `json:"averageScore"`
				Format       string   `json:"format"`
				Genres       []string `json:"genres"`
				Description  string   `json:"description"`
				CoverImage   struct {
					ExtraLarge string `json:"extraLarge"`
					Large      string `json:"large"`
				} `json:"coverImage"`
				BannerImage string `json:"bannerImage"`
			} `json:"Media"`
		} `json:"data"`
	}

	if err := json.NewDecoder(io.LimitReader(resp.Body, 256*1024)).Decode(&parsed); err != nil {
		return models.MediaMetadata{}, err
	}

	m := parsed.Data.Media
	bestTitle := strings.TrimSpace(m.Title.English)
	if bestTitle == "" {
		bestTitle = strings.TrimSpace(m.Title.Romaji)
	}
	if bestTitle == "" {
		return models.MediaMetadata{}, fmt.Errorf("no anilist match")
	}

	rating := "N/A"
	if m.AverageScore > 0 {
		rating = fmt.Sprintf("%.1f / 10", float64(m.AverageScore)/10.0)
	}

	cover := m.CoverImage.ExtraLarge
	if cover == "" {
		cover = m.CoverImage.Large
	}

	cat := "ANIME"
	if strings.EqualFold(m.Format, "MOVIE") {
		cat = "ANIME"
	}

	return models.MediaMetadata{
		Title:         bestTitle,
		Year:          m.SeasonYear,
		TotalEpisodes: m.Episodes,
		Rating:        rating,
		Genres:        m.Genres,
		Synopsis:      cleanSynopsis(m.Description),
		CoverURL:      cover,
		BannerURL:     m.BannerImage,
		Category:      cat,
	}, nil
}

func (p *Provider) queryTMDb(ctx context.Context, title string, year int, category, apiKey string) (models.MediaMetadata, error) {
	endpoint := "https://api.themoviedb.org/3/search/tv"
	isMovie := category == "MOVIE"
	if isMovie {
		endpoint = "https://api.themoviedb.org/3/search/movie"
	}

	q := url.Values{}
	q.Set("api_key", apiKey)
	q.Set("query", title)
	if year > 0 {
		if isMovie {
			q.Set("year", strconv.Itoa(year))
		} else {
			q.Set("first_air_date_year", strconv.Itoa(year))
		}
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint+"?"+q.Encode(), nil)
	if err != nil {
		return models.MediaMetadata{}, err
	}
	resp, err := p.httpClient.Do(req)
	if err != nil {
		return models.MediaMetadata{}, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return models.MediaMetadata{}, fmt.Errorf("tmdb status %d", resp.StatusCode)
	}

	var parsed struct {
		Results []struct {
			Name         string  `json:"name"`
			Title        string  `json:"title"`
			FirstAirDate string  `json:"first_air_date"`
			ReleaseDate  string  `json:"release_date"`
			VoteAverage  float64 `json:"vote_average"`
			Overview     string  `json:"overview"`
			PosterPath   string  `json:"poster_path"`
			BackdropPath string  `json:"backdrop_path"`
		} `json:"results"`
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, 256*1024)).Decode(&parsed); err != nil {
		return models.MediaMetadata{}, err
	}
	if len(parsed.Results) == 0 {
		return models.MediaMetadata{}, fmt.Errorf("no tmdb results")
	}

	r := parsed.Results[0]
	bestTitle := strings.TrimSpace(r.Name)
	if bestTitle == "" {
		bestTitle = strings.TrimSpace(r.Title)
	}
	dateStr := r.FirstAirDate
	if dateStr == "" {
		dateStr = r.ReleaseDate
	}
	yr := 0
	if len(dateStr) >= 4 {
		yr, _ = strconv.Atoi(dateStr[:4])
	}
	rating := "N/A"
	if r.VoteAverage > 0 {
		rating = fmt.Sprintf("%.1f / 10", r.VoteAverage)
	}
	var coverURL, bannerURL string
	if r.PosterPath != "" {
		coverURL = "https://image.tmdb.org/t/p/w500" + r.PosterPath
	}
	if r.BackdropPath != "" {
		bannerURL = "https://image.tmdb.org/t/p/w1280" + r.BackdropPath
	}
	cat := "TV"
	if isMovie {
		cat = "MOVIE"
	}

	return models.MediaMetadata{
		Title:     bestTitle,
		Year:      yr,
		Rating:    rating,
		Synopsis:  cleanSynopsis(r.Overview),
		CoverURL:  coverURL,
		BannerURL: bannerURL,
		Category:  cat,
	}, nil
}

func mergeRemoteIntoBase(base, remote models.MediaMetadata) models.MediaMetadata {
	if base.Year == 0 && remote.Year > 0 {
		base.Year = remote.Year
	}
	if remote.TotalEpisodes > 0 {
		base.TotalEpisodes = remote.TotalEpisodes
	}
	if remote.Rating != "" {
		base.Rating = remote.Rating
	}
	if len(remote.Genres) > 0 {
		base.Genres = remote.Genres
	}
	if remote.Synopsis != "" {
		base.Synopsis = remote.Synopsis
	}
	if remote.CoverURL != "" {
		base.CoverURL = remote.CoverURL
	}
	if remote.BannerURL != "" {
		base.BannerURL = remote.BannerURL
	}
	if remote.Category != "" {
		base.Category = remote.Category
	}
	return base
}

func cleanSynopsis(raw string) string {
	s := strings.ReplaceAll(raw, "<br>", " ")
	s = strings.ReplaceAll(s, "<br/>", " ")
	s = reHTMLTags.ReplaceAllString(s, "")
	s = html.UnescapeString(s)
	s = strings.Join(strings.Fields(s), " ")
	if idx := strings.Index(s, "(Source:"); idx > 20 {
		s = strings.TrimSpace(s[:idx])
	}
	if len(s) > 480 {
		s = strings.TrimSpace(s[:477]) + "..."
	}
	return s
}

func (p *Provider) getCached(key string) (models.MediaMetadata, bool) {
	p.mu.RLock()
	entry, ok := p.cache[key]
	p.mu.RUnlock()
	if !ok || time.Now().After(entry.expiresAt) {
		return models.MediaMetadata{}, false
	}
	return entry.md, true
}

func (p *Provider) putCached(key string, md models.MediaMetadata) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if len(p.cache) >= maxCacheEntries {
		now := time.Now()
		for k, v := range p.cache {
			if now.After(v.expiresAt) {
				delete(p.cache, k)
			}
		}
		if len(p.cache) >= maxCacheEntries {
			for k := range p.cache {
				delete(p.cache, k)
				break
			}
		}
	}
	p.cache[key] = cacheEntry{
		md:        md,
		expiresAt: time.Now().Add(cacheTTL),
	}
}
