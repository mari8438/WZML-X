// Package telegram implements the Anime Poster Card & Caption Generator (`poster.go`)
// supporting Templates 1 through 11 for Starfall-Go Debrid.
//
// It formats custom HTML captions with stream metadata (Resolution, Codec, Bit Depth,
// Audio languages, Subtitles, File Size, Part X/Y badges) and generates or attaches
// a 16:9 (1280x720) thumbnail card when no custom thumbnail was uploaded in Video Studio.
package telegram

import (
	"bytes"
	"fmt"
	"html"
	"image"
	"image/color"
	"image/draw"
	"image/jpeg"
	"os"
	"path/filepath"
	"strings"

	"github.com/starfall-org/starfall-go/internal/models"
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

var posterTemplates = map[int]TemplateSpec{
	1:  {1, "Classic Amber Hero", color.RGBA{18, 14, 10, 255}, color.RGBA{224, 167, 45, 255}, color.RGBA{38, 28, 18, 255}, "◇"},
	2:  {2, "Frosted Glass Blur", color.RGBA{15, 19, 28, 255}, color.RGBA{173, 31, 209, 255}, color.RGBA{35, 39, 47, 255}, "✦"},
	3:  {3, "Minimalist Coral Daylight", color.RGBA{24, 28, 34, 255}, color.RGBA{247, 105, 110, 255}, color.RGBA{42, 46, 56, 255}, "●"},
	4:  {4, "Neon Cyberpunk Grid", color.RGBA{13, 2, 26, 255}, color.RGBA{0, 240, 255, 255}, color.RGBA{36, 10, 66, 255}, "⚡"},
	5:  {5, "Crimson Split Blade", color.RGBA{18, 9, 7, 255}, color.RGBA{239, 68, 68, 255}, color.RGBA{45, 16, 16, 255}, "⚔"},
	6:  {6, "Emerald Scout Banner", color.RGBA{8, 17, 11, 255}, color.RGBA{16, 185, 129, 255}, color.RGBA{20, 42, 29, 255}, "❖"},
	7:  {7, "Royal Monochrome Gold", color.RGBA{12, 12, 14, 255}, color.RGBA{234, 179, 8, 255}, color.RGBA{28, 28, 32, 255}, "♛"},
	8:  {8, "Vaporwave Sunset Card", color.RGBA{22, 10, 38, 255}, color.RGBA{244, 114, 182, 255}, color.RGBA{44, 18, 68, 255}, "★"},
	9:  {9, "Obsidian Shadow Monarch", color.RGBA{10, 5, 24, 255}, color.RGBA{139, 92, 246, 255}, color.RGBA{28, 16, 58, 255}, "◈"},
	10: {10, "Domain Expansion Prism", color.RGBA{11, 15, 25, 255}, color.RGBA{168, 85, 247, 255}, color.RGBA{26, 33, 56, 255}, "❖"},
	11: {11, "Ultra-Pro Flagship Hero Card", color.RGBA{9, 12, 22, 255}, color.RGBA{6, 182, 212, 255}, color.RGBA{22, 29, 48, 255}, "⬢"},
}

// PosterCardInput holds metadata for formatting the caption and resolving the 16:9 thumbnail.
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

// PosterGenerator creates formatted HTML captions and 16:9 JPEG thumbnails for Templates 1..11.
type PosterGenerator struct {
	downloadDir string
	brandName   string
}

// NewPosterGenerator constructs a PosterGenerator for Templates 1 through 11.
func NewPosterGenerator(downloadDir, brandName string) *PosterGenerator {
	if strings.TrimSpace(brandName) == "" {
		brandName = "Anime Starfall"
	}
	return &PosterGenerator{
		downloadDir: downloadDir,
		brandName:   brandName,
	}
}

// GetTemplateSpec returns the TemplateSpec for `id` (clamping to 1..11, default 1).
func GetTemplateSpec(id int) TemplateSpec {
	if spec, ok := posterTemplates[id]; ok {
		return spec
	}
	return posterTemplates[models.DefaultPosterTemplate]
}

// FormatCaption builds a rich Telegram caption according to the user's selected
// PosterTemplate (1..11), embedding resolution, codec, audio tracks, subtitles,
// file size, and multi-part indicators when split by `splitter.go`.
func (p *PosterGenerator) FormatCaption(in PosterCardInput) string {
	spec := GetTemplateSpec(in.TemplateID)

	title := strings.TrimSpace(in.Title)
	if title == "" {
		title = "Starfall Media Release"
	}
	escapedTitle := html.EscapeString(title)

	res := strings.TrimSpace(in.Resolution)
	if res == "" {
		res = "1080p"
	}
	codec := strings.TrimSpace(in.VideoCodec)
	if codec == "" {
		codec = "HEVC"
	}
	bit := strings.TrimSpace(in.BitDepth)
	if bit == "" {
		bit = "10-Bit"
	}

	audioStr := "Japanese [Original]"
	if len(in.AudioLanguages) > 0 {
		audioStr = strings.ToUpper(strings.Join(in.AudioLanguages, " + "))
	}

	subStr := "None (Stripped)"
	if len(in.SubtitleLanguages) > 0 {
		subStr = strings.ToUpper(strings.Join(in.SubtitleLanguages, " + "))
	}

	sizeStr := formatHumanBytes(in.FileSizeBytes)

	var partBadge string
	if in.TotalParts > 1 {
		partBadge = fmt.Sprintf(" • [Part %d/%d]", in.PartNumber, in.TotalParts)
	}

	epTag := ""
	if in.Season > 0 || in.Episode > 0 {
		s := in.Season
		if s <= 0 {
			s = 1
		}
		epTag = fmt.Sprintf(" S%02dE%02d", s, in.Episode)
	}

	synopsis := strings.TrimSpace(in.Synopsis)
	if synopsis == "" {
		synopsis = fmt.Sprintf("Delivered silently via %s • Template #%d (%s) • 0%% CPU Lossless Stream-Copy.",
			p.brandName, spec.ID, spec.Name)
	}

	var b strings.Builder
	fmt.Fprintf(&b, "「 %s%s 」%s
", escapedTitle, epTag, partBadge)
	fmt.Fprintf(&b, "━━━━━━━━━━━━━━━━━━
")
	fmt.Fprintf(&b, "╔════%s═══════════%s════
", spec.BorderGlyph, spec.BorderGlyph)
	fmt.Fprintf(&b, "║ Quality ➤ %s %s %s
", res, bit, codec)
	fmt.Fprintf(&b, "║ Audio   ➤ %s
", audioStr)
	fmt.Fprintf(&b, "║ Subs    ➤ %s
", subStr)
	fmt.Fprintf(&b, "║ Size    ➤ %s
", sizeStr)
	fmt.Fprintf(&b, "║ Style   ➤ #%d • %s
", spec.ID, spec.Name)
	fmt.Fprintf(&b, "╚════%s═══════════%s════

", spec.BorderGlyph, spec.BorderGlyph)
	fmt.Fprintf(&b, "%s", html.EscapeString(synopsis))

	return b.String()
}

// ResolveOrRenderThumbnail returns the byte slice of the user's custom thumbnail from
// Video Tools Studio if one exists on disk, or renders a lightweight 16:9 (1280x720)
// JPEG poster card matching Template `templateID` (1..11) in memory (< 3.5 MiB transient).
func (p *PosterGenerator) ResolveOrRenderThumbnail(taskID string, templateID int, customPath string) ([]byte, string, error) {
	candidates := []string{}
	if strings.TrimSpace(customPath) != "" {
		candidates = append(candidates, customPath)
	}
	if p.downloadDir != "" && taskID != "" {
		candidates = append(candidates, filepath.Join(p.downloadDir, "thumbnails", taskID+".jpg"))
	}

	for _, path := range candidates {
		if data, err := os.ReadFile(path); err == nil && len(data) > 0 {
			return data, filepath.Base(path), nil
		}
	}

	// Render a fast, memory-safe 16:9 (1280x720) gradient + glass frame card in pure Go.
	spec := GetTemplateSpec(templateID)
	const width, height = 1280, 720
	canvas := image.NewRGBA(image.Rect(0, 0, width, height))

	// Fill obsidian background.
	draw.Draw(canvas, canvas.Bounds(), &image.Uniform{C: spec.PrimaryRGB}, image.Point{}, draw.Src)

	// Draw glassmorphic hero container card (right) and 3D poster frame (left).
	cardRect := image.Rect(460, 80, 1210, 640)
	draw.Draw(canvas, cardRect, &image.Uniform{C: spec.SurfaceRGB}, image.Point{}, draw.Src)

	posterRect := image.Rect(70, 80, 420, 640)
	draw.Draw(canvas, posterRect, &image.Uniform{C: spec.SurfaceRGB}, image.Point{}, draw.Src)

	// Draw accent border bars & simulcast progress bar at bottom.
	accentBar := image.Rect(70, 628, 1210, 640)
	draw.Draw(canvas, accentBar, &image.Uniform{C: spec.AccentRGB}, image.Point{}, draw.Src)

	topPill := image.Rect(460, 80, 472, 640)
	draw.Draw(canvas, topPill, &image.Uniform{C: spec.AccentRGB}, image.Point{}, draw.Src)

	var buf bytes.Buffer
	if err := jpeg.Encode(&buf, canvas, &jpeg.Options{Quality: 90}); err != nil {
		return nil, "", err
	}
	return buf.Bytes(), fmt.Sprintf("poster_tpl_%d.jpg", spec.ID), nil
}

func formatHumanBytes(b int64) string {
	const unit = 1024
	if b < unit {
		return fmt.Sprintf("%d B", b)
	}
	div, exp := int64(unit), 0
	for n := b / unit; n >= unit; n /= unit {
		div *= unit
		exp++
	}
	return fmt.Sprintf("%.2f %ciB", float64(b)/float64(div), "KMGTPE"[exp])
}
