// Package telegram implements the Anime Poster Card & Caption Coordinator (`poster.go`)
// bridging AniList/TMDb lookup (`internal/poster.Provider`), pure-Go 1280x720 Templates 1..11,
// smart video frame fallback (`ffmpeg -vframes 1`), and batch-pack poster deduplication.
package telegram

import (
	"context"
	"fmt"
	"image/color"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/starfall-org/starfall-go/internal/media"
	"github.com/starfall-org/starfall-go/internal/models"
	"github.com/starfall-org/starfall-go/internal/poster"
)

// TemplateSpec defines the visual palette and caption layout style for Templates 1..11.
type TemplateSpec struct {
	ID          int
	Name        string
	PrimaryRGB  color.RGBA
	AccentRGB   color.RGBA
	SurfaceRGB  color.RGBA
	BorderGlyph string
}

// PosterCardInput holds metadata for formatting fallback captions.
type PosterCardInput struct {
	TemplateID        int
	Title             string
	Year              int
	Season            int
	Episode           int
	Resolution        string
	VideoCodec        string
	BitDepth          string
	AudioLanguages    []string
	SubtitleLanguages []string
	FileSizeBytes     int64
	PartNumber        int
	TotalParts        int
	Synopsis          string
	CustomThumbPath   string
}

// PosterGenerator coordinates AniList/TMDb metadata lookup, 16:9 poster rendering,
// smart video frame extraction, and batch-pack poster deduplication.
type PosterGenerator struct {
	downloadDir string
	brandName   string
	provider    *poster.Provider

	batchMu   sync.Mutex
	sentPacks map[string]time.Time
}

// NewPosterGenerator constructs a PosterGenerator for Templates 1 through 11.
func NewPosterGenerator(downloadDir, brandName string, tmdbKey ...string) *PosterGenerator {
	if strings.TrimSpace(brandName) == "" {
		brandName = "Anime Starfall"
	}
	key := ""
	if len(tmdbKey) > 0 {
		key = tmdbKey[0]
	}
	return &PosterGenerator{
		downloadDir: downloadDir,
		brandName:   brandName,
		provider:    poster.NewProvider(key),
		sentPacks:   make(map[string]time.Time),
	}
}

// Provider returns the underlying AniList/TMDb metadata Provider.
func (p *PosterGenerator) Provider() *poster.Provider {
	return p.provider
}

// EnrichMetadata resolves AniList / TMDb cover art, rating, genres, and synopsis.
func (p *PosterGenerator) EnrichMetadata(ctx context.Context, md models.MediaMetadata) models.MediaMetadata {
	if p == nil || p.provider == nil {
		return md
	}
	return p.provider.Enrich(ctx, md)
}

// ShouldSendStandalonePoster enforces batch deduplication so multi-episode folders
// only post the standalone 16:9 poster message once per batch key.
func (p *PosterGenerator) ShouldSendStandalonePoster(batchKey string) bool {
	if strings.TrimSpace(batchKey) == "" {
		return true
	}
	p.batchMu.Lock()
	defer p.batchMu.Unlock()
	// Prune old batch entries (> 6 hours)
	now := time.Now()
	for k, ts := range p.sentPacks {
		if now.Sub(ts) > 6*time.Hour {
			delete(p.sentPacks, k)
		}
	}
	if _, already := p.sentPacks[batchKey]; already {
		return false
	}
	p.sentPacks[batchKey] = now
	return true
}

// RenderPosterCard renders a 1280x720 (16:9) JPEG poster card for `md` using Template 1..11.
func (p *PosterGenerator) RenderPosterCard(
	ctx context.Context,
	md models.MediaMetadata,
	settings models.UserSettings,
	templateID int,
) ([]byte, error) {
	var prov *poster.Provider
	if p != nil {
		prov = p.provider
	}
	return poster.RenderPosterJPEG(ctx, prov, md, settings, templateID)
}

// FormatExpandableCaption builds the user's configured `POST_ANIME_CAPTION` / `POST_TV_CAPTION` / `POST_MOVIE_CAPTION`.
func (p *PosterGenerator) FormatExpandableCaption(md models.MediaMetadata, settings models.UserSettings) string {
	return poster.FormatExpandableCaption(md, settings)
}

// FormatFileUploadCaption builds the per-file Telegram HTML caption (`LEECH_FONT` styled filename + Part badge).
func (p *PosterGenerator) FormatFileUploadCaption(fileName string, settings models.UserSettings, partNum, totalParts int) string {
	label := fileName
	if totalParts > 1 {
		label = fmt.Sprintf("%s [Part %d/%d]", fileName, partNum, totalParts)
	}
	return media.FormatUploadCaption(label, settings.LeechFont)
}

// FormatCaption builds a rich Telegram caption (retained for backward compatibility).
func (p *PosterGenerator) FormatCaption(in PosterCardInput) string {
	md := models.MediaMetadata{
		Title:          in.Title,
		Year:           in.Year,
		Season:         in.Season,
		Episode:        in.Episode,
		Resolution:     in.Resolution,
		VideoCodec:     in.VideoCodec,
		BitDepth:       in.BitDepth,
		AudioLanguages: in.AudioLanguages,
		SubLanguages:   in.SubtitleLanguages,
		Synopsis:       in.Synopsis,
		Category:       "ANIME",
	}
	settings := models.DefaultUserSettings()
	settings.PostTemplateID = in.TemplateID
	settings.PostBrandName = p.brandName
	capStr := poster.FormatExpandableCaption(md, settings)
	if in.TotalParts > 1 {
		capStr = fmt.Sprintf("<b>[Part %d/%d]</b>\n%s", in.PartNumber, in.TotalParts, capStr)
	}
	return capStr
}

// ResolveThumbnailForDelivery selects the optimal 16:9 JPEG thumbnail for a file upload:
//  1. Custom user/task thumbnail on disk (`customPath` or `downloads/thumbnails/<taskID>.jpg`)
//  2. Pre-rendered 16:9 AniList/TMDb poster card (`AUTO_POSTER_USE_AS_THUMBNAIL`)
//  3. Single-frame smart video screenshot (`AUTO_THUMBNAIL` via `ffmpeg -vframes 1`)
//  4. Pure-Go 16:9 fallback poster card
func (p *PosterGenerator) ResolveThumbnailForDelivery(
	ctx context.Context,
	taskID string,
	videoPath string,
	durationSec float64,
	posterBytes []byte,
	md models.MediaMetadata,
	settings models.UserSettings,
	customPath string,
) ([]byte, string, error) {
	if strings.EqualFold(settings.ThumbnailMode, "disabled") || strings.EqualFold(settings.ThumbnailMode, "none") {
		return nil, "", nil
	}

	// 1. Custom thumbnail check
	candidates := []string{}
	if strings.TrimSpace(customPath) != "" {
		candidates = append(candidates, customPath)
	}
	if strings.TrimSpace(settings.CustomThumbnailFilePath) != "" {
		candidates = append(candidates, settings.CustomThumbnailFilePath)
	}
	if p.downloadDir != "" && taskID != "" {
		candidates = append(candidates, filepath.Join(p.downloadDir, "thumbnails", taskID+".jpg"))
	}
	for _, cPath := range candidates {
		if data, err := os.ReadFile(cPath); err == nil && len(data) > 0 {
			return data, filepath.Base(cPath), nil
		}
	}

	// 2. Generated Poster Card as thumbnail (`AUTO_POSTER_USE_AS_THUMBNAIL`)
	if settings.AutoPosterEnabled && settings.AutoPosterUseAsThumb && len(posterBytes) > 0 {
		return posterBytes, fmt.Sprintf("poster_tpl_%d.jpg", settings.PostTemplateID), nil
	}

	// 3. Smart single-frame video capture (`AUTO_THUMBNAIL`)
	if settings.AutoThumbnail && videoPath != "" {
		if frameBytes, ferr := poster.ExtractSmartVideoThumbnail(ctx, videoPath, durationSec); ferr == nil && len(frameBytes) > 0 {
			return frameBytes, "smart_frame.jpg", nil
		}
	}

	// 4. Fallback pure-Go poster card
	return p.ResolveOrRenderThumbnail(taskID, settings.PostTemplateID, customPath)
}

// ResolveOrRenderThumbnail renders or loads a 1280x720 thumbnail card.
func (p *PosterGenerator) ResolveOrRenderThumbnail(taskID string, templateID int, customPath string) ([]byte, string, error) {
	if strings.TrimSpace(customPath) != "" {
		if data, err := os.ReadFile(customPath); err == nil && len(data) > 0 {
			return data, filepath.Base(customPath), nil
		}
	}
	if p.downloadDir != "" && taskID != "" {
		tPath := filepath.Join(p.downloadDir, "thumbnails", taskID+".jpg")
		if data, err := os.ReadFile(tPath); err == nil && len(data) > 0 {
			return data, filepath.Base(tPath), nil
		}
	}
	md := models.MediaMetadata{Title: p.brandName, Category: "ANIME"}
	settings := models.DefaultUserSettings()
	settings.PostBrandName = p.brandName
	jpg, err := p.RenderPosterCard(context.Background(), md, settings, templateID)
	if err != nil {
		return nil, "", err
	}
	return jpg, fmt.Sprintf("poster_tpl_%d.jpg", templateID), nil
}
