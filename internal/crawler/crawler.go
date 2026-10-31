package crawler

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"regexp"
	"strings"
	"sync"
	"time"

	"github.com/starfall-org/starfall-go/internal/database"
	"github.com/starfall-org/starfall-go/internal/safety"
)

var (
	ErrScraperDriftDetected = errors.New("crawler: scraper drift detected; 0 valid candidates found")
	tamilTokenRegex         = regexp.MustCompile(`(?i)\b(tam(?:il)?|multi[\s-]*audio)\b`)
)

// DiscoveredRelease represents a newly found release candidate from a crawler.
type DiscoveredRelease struct {
	Source      string   `json:"source"`
	ItemKey     string   `json:"item_key"`
	Title       string   `json:"title"`
	PageURL     string   `json:"page_url"`
	DownloadURI string   `json:"download_uri"` // Magnet, torrent URL, or direct link
	HasTamil    bool     `json:"has_tamil"`
	Languages   []string `json:"languages,omitempty"`
	Quality     string   `json:"quality,omitempty"`
}

// SiteCrawler defines the interface implemented by each tracker/site crawler.
type SiteCrawler interface {
	Name() string
	CrawlOnce(ctx context.Context) ([]DiscoveredRelease, error)
}

// AutoLeechHook is invoked when a new unseen release is discovered and accepted.
type AutoLeechHook func(ctx context.Context, rel DiscoveredRelease) error

// Scheduler orchestrates periodic crawl jobs within a bounded worker pool.
type Scheduler struct {
	db          *database.DBManager
	pool        *safety.BoundedPool
	crawlers    []SiteCrawler
	leechHook   AutoLeechHook
	logger      *slog.Logger
	cancelFunc  context.CancelFunc
	mu          sync.RWMutex
}

// NewScheduler creates a Crawler Scheduler.
func NewScheduler(db *database.DBManager, hook AutoLeechHook, logger *slog.Logger) *Scheduler {
	if logger == nil {
		logger = slog.Default()
	}
	return &Scheduler{
		db:        db,
		leechHook: hook,
		logger:    logger,
	}
}

// Register adds a SiteCrawler to the scheduler.
func (s *Scheduler) Register(c SiteCrawler) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.crawlers = append(s.crawlers, c)
}

// Start begins periodic crawling for all registered crawlers.
func (s *Scheduler) Start(parentCtx context.Context, interval time.Duration) {
	ctx, cancel := context.WithCancel(parentCtx)
	s.cancelFunc = cancel
	s.pool = safety.NewBoundedPool(ctx, 2, 32)

	if interval <= 0 {
		interval = 15 * time.Minute
	}

	go func() {
		ticker := time.NewTicker(interval)
		defer ticker.Stop()

		// Run immediately on boot
		s.dispatchAll(ctx)

		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				s.dispatchAll(ctx)
			}
		}
	}()
}

func (s *Scheduler) dispatchAll(ctx context.Context) {
	s.mu.RLock()
	crawlers := append([]SiteCrawler(nil), s.crawlers...)
	s.mu.RUnlock()

	for _, c := range crawlers {
		crawler := c
		_ = s.pool.TrySubmit(func(jobCtx context.Context) error {
			s.logger.Info("running crawler", slog.String("crawler", crawler.Name()))
			releases, err := crawler.CrawlOnce(jobCtx)
			if err != nil {
				s.logger.Warn("crawler run encountered error", slog.String("crawler", crawler.Name()), slog.Any("error", err))
				return err
			}

			for _, rel := range releases {
				if s.db != nil {
					seen, err := s.db.IsItemSeen(jobCtx, rel.Source, rel.ItemKey)
					if err == nil && seen {
						continue // Skip duplicate
					}
				}

				s.logger.Info("discovered new release",
					slog.String("source", rel.Source),
					slog.String("title", rel.Title),
					slog.Bool("has_tamil", rel.HasTamil),
				)

				if s.db != nil {
					_ = s.db.MarkItemSeen(jobCtx, rel.Source, rel.ItemKey, rel.Title, rel.PageURL)
				}

				if s.leechHook != nil {
					_ = s.leechHook(jobCtx, rel)
				}
			}
			return nil
		})
	}
}

// HasTamilToken checks if a string contains "Tamil" or "Multi Audio".
func HasTamilToken(s string) bool {
	return tamilTokenRegex.MatchString(s)
}
