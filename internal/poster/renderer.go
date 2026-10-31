// Package poster renders pure-Go 1280x720 (16:9) Anime/TV/Movie Poster Cards
// for Templates 1 through 11 (including Template 11 Ultra-Pro Flagship Hero Card),
// expandable Telegram HTML captions, and single-frame smart video thumbnails.
package poster

import (
	"bytes"
	"context"
	"fmt"
	"html"
	"image"
	"image/color"
	"image/draw"
	"image/jpeg"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"sync"

	"github.com/starfall-org/starfall-go/internal/models"
	"github.com/starfall-org/starfall-go/internal/safety"
	xdraw "golang.org/x/image/draw"
	"golang.org/x/image/font"
	"golang.org/x/image/font/gofont/gobold"
	"golang.org/x/image/font/gofont/goregular"
	"golang.org/x/image/font/opentype"
	"golang.org/x/image/math/fixed"
)

// Palette defines the color theme for each of Templates 1..11.
type Palette struct {
	ID         int
	Name       string
	BgDark     color.RGBA
	CardFill   color.RGBA
	Accent     color.RGBA
	Secondary  color.RGBA
	TextMain   color.RGBA
	TextMuted  color.RGBA
	GoldBadge  color.RGBA
	RightCover bool // Mirrors poster onto right side for visual variety (e.g. Templates 5, 8)
}

var templatePalettes = map[int]Palette{
	1:  {1, "Classic Amber Hero", color.RGBA{14, 11, 8, 255}, color.RGBA{28, 22, 16, 228}, color.RGBA{234, 179, 8, 255}, color.RGBA{245, 158, 11, 255}, color.RGBA{250, 250, 250, 255}, color.RGBA{196, 188, 176, 255}, color.RGBA{245, 158, 11, 255}, false},
	2:  {2, "Frosted Glass Violet", color.RGBA{12, 15, 26, 255}, color.RGBA{24, 29, 48, 224}, color.RGBA{168, 85, 247, 255}, color.RGBA{217, 70, 239, 255}, color.RGBA{248, 250, 252, 255}, color.RGBA{180, 190, 212, 255}, color.RGBA{250, 204, 21, 255}, false},
	3:  {3, "Minimalist Coral Studio", color.RGBA{20, 24, 32, 255}, color.RGBA{34, 40, 52, 230}, color.RGBA{244, 63, 94, 255}, color.RGBA{251, 113, 133, 255}, color.RGBA{255, 255, 255, 255}, color.RGBA{190, 198, 210, 255}, color.RGBA{251, 191, 36, 255}, false},
	4:  {4, "Neon Cyberpunk Grid", color.RGBA{10, 4, 22, 255}, color.RGBA{24, 12, 48, 228}, color.RGBA{0, 240, 255, 255}, color.RGBA{250, 204, 21, 255}, color.RGBA{245, 252, 255, 255}, color.RGBA{165, 215, 232, 255}, color.RGBA{250, 204, 21, 255}, false},
	5:  {5, "Crimson Split Blade", color.RGBA{16, 7, 8, 255}, color.RGBA{36, 14, 16, 230}, color.RGBA{239, 68, 68, 255}, color.RGBA{248, 113, 113, 255}, color.RGBA{255, 248, 248, 255}, color.RGBA{214, 184, 184, 255}, color.RGBA{251, 191, 36, 255}, true},
	6:  {6, "Emerald Scout Banner", color.RGBA{7, 16, 12, 255}, color.RGBA{16, 36, 26, 228}, color.RGBA{16, 185, 129, 255}, color.RGBA{52, 211, 153, 255}, color.RGBA{244, 255, 250, 255}, color.RGBA{176, 212, 196, 255}, color.RGBA{250, 204, 21, 255}, false},
	7:  {7, "Royal Monochrome Gold", color.RGBA{10, 10, 12, 255}, color.RGBA{24, 24, 28, 234}, color.RGBA{234, 179, 8, 255}, color.RGBA{253, 224, 71, 255}, color.RGBA{252, 252, 252, 255}, color.RGBA{185, 185, 195, 255}, color.RGBA{234, 179, 8, 255}, false},
	8:  {8, "Vaporwave Sunset Card", color.RGBA{19, 8, 34, 255}, color.RGBA{38, 16, 62, 226}, color.RGBA{244, 114, 182, 255}, color.RGBA{56, 189, 248, 255}, color.RGBA{255, 248, 254, 255}, color.RGBA{212, 184, 226, 255}, color.RGBA{250, 204, 21, 255}, true},
	9:  {9, "Obsidian Shadow Monarch", color.RGBA{8, 5, 20, 255}, color.RGBA{20, 13, 44, 232}, color.RGBA{139, 92, 246, 255}, color.RGBA{56, 189, 248, 255}, color.RGBA{248, 250, 255, 255}, color.RGBA{182, 178, 218, 255}, color.RGBA{250, 204, 21, 255}, false},
	10: {10, "Domain Expansion Prism", color.RGBA{9, 13, 24, 255}, color.RGBA{20, 28, 50, 230}, color.RGBA{192, 132, 252, 255}, color.RGBA{96, 165, 250, 255}, color.RGBA{248, 250, 252, 255}, color.RGBA{180, 194, 218, 255}, color.RGBA{250, 204, 21, 255}, false},
	11: {11, "Ultra-Pro Flagship Hero Card", color.RGBA{8, 11, 20, 255}, color.RGBA{18, 25, 42, 228}, color.RGBA{6, 182, 212, 255}, color.RGBA{139, 92, 246, 255}, color.RGBA{248, 250, 252, 255}, color.RGBA{176, 192, 214, 255}, color.RGBA{245, 158, 11, 255}, false},
}

var (
	fontOnce    sync.Once
	parsedBold  *opentype.Font
	parsedReg   *opentype.Font
)

func initFonts() {
	fontOnce.Do(func() {
		// Prefer system DejaVuSans if installed in Alpine container (`font-dejavu`), else built-in Go fonts.
		if b, err := os.ReadFile("/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"); err == nil {
			if f, ferr := opentype.Parse(b); ferr == nil {
				parsedBold = f
			}
		}
		if parsedBold == nil {
			parsedBold, _ = opentype.Parse(gobold.TTF)
		}
		if b, err := os.ReadFile("/usr/share/fonts/dejavu/DejaVuSans.ttf"); err == nil {
			if f, ferr := opentype.Parse(b); ferr == nil {
				parsedReg = f
			}
		}
		if parsedReg == nil {
			parsedReg, _ = opentype.Parse(goregular.TTF)
		}
	})
}

func newFace(bold bool, sizePt float64) font.Face {
	initFonts()
	src := parsedReg
	if bold {
		src = parsedBold
	}
	f, _ := opentype.NewFace(src, &opentype.FaceOptions{
		Size:    sizePt,
		DPI:     72,
		Hinting: font.HintingFull,
	})
	return f
}

// GetPalette returns the color Palette for `templateID` (defaulting to Template 11).
func GetPalette(templateID int) Palette {
	if p, ok := templatePalettes[templateID]; ok {
		return p
	}
	return templatePalettes[11]
}

// RenderPosterJPEG renders a 1280x720 (16:9) JPEG poster card for Templates 1..11
// using AniList/TMDb artwork (when `provider != nil`) and pure-Go typography (< 3.5 MiB RAM).
func RenderPosterJPEG(
	ctx context.Context,
	provider *Provider,
	md models.MediaMetadata,
	settings models.UserSettings,
	templateID int,
) ([]byte, error) {
	if templateID < models.MinPosterTemplate || templateID > models.MaxPosterTemplate {
		templateID = settings.PostTemplateID
	}
	pal := GetPalette(templateID)

	const W, H = 1280, 720
	canvas := image.NewRGBA(image.Rect(0, 0, W, H))
	draw.Draw(canvas, canvas.Bounds(), &image.Uniform{C: pal.BgDark}, image.Point{}, draw.Src)

	var coverImg, bannerImg image.Image
	if provider != nil {
		if md.CoverURL != "" {
			coverImg, _ = provider.FetchImage(ctx, md.CoverURL)
		}
		if md.BannerURL != "" {
			bannerImg, _ = provider.FetchImage(ctx, md.BannerURL)
		}
	}

	// 1. Blurred Full-Bleed Backdrop + Dark Vignette
	backdropSrc := bannerImg
	if backdropSrc == nil {
		backdropSrc = coverImg
	}
	if backdropSrc != nil {
		scaledBg := scaleAspectFill(backdropSrc, W, H)
		blurredBg := fastBoxBlur(scaledBg, 10)
		draw.Draw(canvas, canvas.Bounds(), blurredBg, image.Point{}, draw.Src)
		// Darken backdrop so foreground glass card & typography always pass WCAG AA contrast
		fillAlphaRect(canvas, image.Rect(0, 0, W, H), color.RGBA{pal.BgDark.R, pal.BgDark.G, pal.BgDark.B, 196})
	} else {
		// Stylized diagonal glow bands when offline
		drawAmbientGlow(canvas, pal)
	}

	// Layout coordinates (left vs mirrored right poster)
	posterRect := image.Rect(58, 62, 398, 658)
	cardRect := image.Rect(430, 62, 1222, 658)
	if pal.RightCover {
		cardRect = image.Rect(58, 62, 850, 658)
		posterRect = image.Rect(882, 62, 1222, 658)
	}

	// 2. Drop shadows + Glass Hero Container
	fillAlphaRect(canvas, posterRect.Add(image.Pt(8, 10)), color.RGBA{0, 0, 0, 140})
	fillAlphaRect(canvas, cardRect.Add(image.Pt(8, 10)), color.RGBA{0, 0, 0, 140})
	fillAlphaRect(canvas, cardRect, pal.CardFill)
	drawRectBorder(canvas, cardRect, 2, color.RGBA{pal.Accent.R, pal.Accent.G, pal.Accent.B, 150})

	// Left/Top accent bar on Hero Card
	draw.Draw(canvas, image.Rect(cardRect.Min.X, cardRect.Min.Y, cardRect.Min.X+8, cardRect.Max.Y), &image.Uniform{C: pal.Accent}, image.Point{}, draw.Src)

	// 3. Poster Cover Frame
	if coverImg != nil {
		scaledCover := scaleAspectFill(coverImg, posterRect.Dx(), posterRect.Dy())
		draw.Draw(canvas, posterRect, scaledCover, image.Point{}, draw.Src)
	} else {
		fillAlphaRect(canvas, posterRect, color.RGBA{pal.CardFill.R + 8, pal.CardFill.G + 8, pal.CardFill.B + 12, 255})
		emblemFace := newFace(true, 26)
		if emblemFace != nil {
			drawString(canvas, emblemFace, posterRect.Min.X+42, posterRect.Min.Y+290, "ANIME STARFALL", pal.Accent)
			_ = emblemFace.Close()
		}
	}
	drawRectBorder(canvas, posterRect, 3, pal.Accent)

	// 4. Typography & Badges inside Hero Card
	facePill := newFace(true, 14)
	faceTitle := newFace(true, 34)
	faceSub := newFace(true, 16)
	faceBody := newFace(false, 15)
	faceLabel := newFace(true, 12)
	defer closeFaces(facePill, faceTitle, faceSub, faceBody, faceLabel)

	padX := cardRect.Min.X + 36
	curY := cardRect.Min.Y + 34

	// Category Pill + Rating Pill + Brand Header
	catText := "ANIME RELEASE"
	switch md.Category {
	case "MOVIE":
		catText = "MOVIE RELEASE"
	case "TV":
		catText = "SERIES RELEASE"
	}
	pillW := measureText(facePill, catText) + 28
	fillAlphaRect(canvas, image.Rect(padX, curY, padX+pillW, curY+30), pal.GoldBadge)
	drawString(canvas, facePill, padX+14, curY+21, catText, color.RGBA{12, 12, 14, 255})

	ratingText := md.Rating
	if ratingText == "" {
		ratingText = "HD LOSSLESS"
	} else {
		ratingText = "SCORE " + ratingText
	}
	rPillX := padX + pillW + 12
	rPillW := measureText(facePill, ratingText) + 26
	fillAlphaRect(canvas, image.Rect(rPillX, curY, rPillX+rPillW, curY+30), color.RGBA{pal.Accent.R, pal.Accent.G, pal.Accent.B, 48})
	drawRectBorder(canvas, image.Rect(rPillX, curY, rPillX+rPillW, curY+30), 1, pal.Accent)
	drawString(canvas, facePill, rPillX+13, curY+21, ratingText, pal.Accent)

	brandName := strings.TrimSpace(settings.PostBrandName)
	if brandName == "" {
		brandName = "Anime Starfall"
	}
	brandUpper := strings.ToUpper(brandName)
	brandW := measureText(facePill, brandUpper)
	drawString(canvas, facePill, cardRect.Max.X-brandW-28, curY+21, brandUpper, pal.TextMuted)

	// Optional Brand Logo image if present on disk
	if logoPath := strings.TrimSpace(settings.PostLogo); logoPath != "" {
		if lb, err := os.ReadFile(logoPath); err == nil {
			if limg, _, derr := image.Decode(bytes.NewReader(lb)); derr == nil {
				scaledLogo := scaleAspectFill(limg, 42, 42)
				logoRect := image.Rect(cardRect.Max.X-brandW-78, curY-6, cardRect.Max.X-brandW-36, curY+36)
				draw.Draw(canvas, logoRect, scaledLogo, image.Point{}, draw.Over)
			}
		}
	}

	// Hero Title (up to 2 lines)
	curY += 68
	displayTitle := strings.TrimSpace(md.Title)
	if displayTitle == "" {
		displayTitle = "Starfall Release"
	}
	if md.Year > 0 {
		displayTitle = fmt.Sprintf("%s (%d)", displayTitle, md.Year)
	}
	titleLines := wrapText(faceTitle, sanitizeASCIIFallback(displayTitle), cardRect.Dx()-72, 2)
	for _, line := range titleLines {
		drawString(canvas, faceTitle, padX, curY, line, pal.TextMain)
		curY += 44
	}

	// Episode + Resolution + Codec + Audio Badges Row
	curY += 6
	var badges []string
	if md.Season > 0 || md.Episode > 0 {
		s := md.Season
		if s <= 0 {
			s = 1
		}
		if md.Episode > 0 {
			badges = append(badges, fmt.Sprintf("S%02dE%02d", s, md.Episode))
		} else {
			badges = append(badges, fmt.Sprintf("SEASON %02d", s))
		}
	}
	if md.Resolution != "" {
		badges = append(badges, strings.ToUpper(md.Resolution))
	} else {
		badges = append(badges, "1080P")
	}
	if md.BitDepth != "" {
		badges = append(badges, strings.ToUpper(md.BitDepth))
	}
	if md.VideoCodec != "" {
		badges = append(badges, strings.ToUpper(md.VideoCodec))
	}
	if md.OTT != "" {
		badges = append(badges, md.OTT)
	}
	if md.ShortSub != "" {
		badges = append(badges, strings.ToUpper(md.ShortSub))
	}

	bx := padX
	for _, b := range badges {
		bw := measureText(facePill, b) + 22
		if bx+bw > cardRect.Max.X-28 {
			break
		}
		bRect := image.Rect(bx, curY, bx+bw, curY+28)
		fillAlphaRect(canvas, bRect, color.RGBA{255, 255, 255, 22})
		drawRectBorder(canvas, bRect, 1, color.RGBA{pal.Secondary.R, pal.Secondary.G, pal.Secondary.B, 140})
		drawString(canvas, facePill, bx+11, curY+19, b, pal.TextMain)
		bx += bw + 10
	}

	// Genres line
	curY += 48
	genreLine := "Anime • Action • Fantasy"
	if len(md.Genres) > 0 {
		limit := len(md.Genres)
		if limit > 4 {
			limit = 4
		}
		genreLine = strings.Join(md.Genres[:limit], "  •  ")
	}
	drawString(canvas, faceSub, padX, curY, sanitizeASCIIFallback(genreLine), pal.Accent)

	// Synopsis Paragraph (up to 4 lines)
	curY += 34
	synopsis := strings.TrimSpace(md.Synopsis)
	if synopsis == "" {
		synopsis = fmt.Sprintf("High-speed zero-transcode lossless stream release curated by %s (@Anime_Starfall).", brandName)
	}
	synLines := wrapText(faceBody, sanitizeASCIIFallback(synopsis), cardRect.Dx()-72, 4)
	for _, sl := range synLines {
		drawString(canvas, faceBody, padX, curY, sl, pal.TextMuted)
		curY += 24
	}

	// 3-Column Spec Grid near bottom of Hero Card
	gridY := cardRect.Max.Y - 128
	colW := (cardRect.Dx() - 92) / 3
	specs := [3][2]string{
		{"VIDEO STREAM", nonEmptyJoin(" ", md.Resolution, md.BitDepth, md.VideoCodec, md.Quality)},
		{"AUDIO TRACKS", formatAudioSpec(md)},
		{"SUBTITLES & TEMPLATE", fmt.Sprintf("%s • TPL #%d", firstNonEmpty(md.ShortSub, "ESub"), pal.ID)},
	}
	for i, sp := range specs {
		cx := padX + i*(colW+10)
		box := image.Rect(cx, gridY, cx+colW, gridY+64)
		fillAlphaRect(canvas, box, color.RGBA{8, 12, 22, 165})
		drawRectBorder(canvas, box, 1, color.RGBA{255, 255, 255, 36})
		drawString(canvas, faceLabel, cx+12, gridY+22, sp[0], pal.Accent)
		val := truncateToWidth(facePill, sanitizeASCIIFallback(sp[1]), colW-24)
		drawString(canvas, facePill, cx+12, gridY+46, val, pal.TextMain)
	}

	// Simulcast Footer Bar & Watermark
	barY := cardRect.Max.Y - 36
	fillAlphaRect(canvas, image.Rect(padX, barY, cardRect.Max.X-28, barY+6), color.RGBA{255, 255, 255, 35})
	progW := int(float64(cardRect.Dx()-64) * 0.72)
	draw.Draw(canvas, image.Rect(padX, barY, padX+progW, barY+6), &image.Uniform{C: pal.Accent}, image.Point{}, draw.Src)
	drawString(canvas, faceLabel, padX, barY+24, "DROPPED BY @ANIME_STARFALL  •  ZERO-TRANSCODE LOSSLESS STREAM", pal.TextMuted)

	var out bytes.Buffer
	if err := jpeg.Encode(&out, canvas, &jpeg.Options{Quality: 92}); err != nil {
		return nil, err
	}
	return out.Bytes(), nil
}

// FormatExpandableCaption populates `POST_ANIME_CAPTION`, `POST_TV_CAPTION`, or `POST_MOVIE_CAPTION`
// preserving `<blockquote expandable>` and HTML formatting tags while escaping dynamic text fields.
func FormatExpandableCaption(md models.MediaMetadata, settings models.UserSettings) string {
	defaults := models.DefaultUserSettings()
	var tpl string
	switch md.Category {
	case "MOVIE":
		tpl = settings.PostMovieCaption
		if strings.TrimSpace(tpl) == "" {
			tpl = defaults.PostMovieCaption
		}
	case "TV":
		tpl = settings.PostTVCaption
		if strings.TrimSpace(tpl) == "" {
			tpl = defaults.PostTVCaption
		}
	default:
		tpl = settings.PostAnimeCaption
		if strings.TrimSpace(tpl) == "" {
			tpl = defaults.PostAnimeCaption
		}
	}

	title := html.EscapeString(firstNonEmpty(md.Title, "Starfall Media"))
	yearStr := "2025"
	if md.Year > 0 {
		yearStr = strconv.Itoa(md.Year)
	}
	seasonStr := "01"
	if md.Season > 0 {
		seasonStr = fmt.Sprintf("%02d", md.Season)
	}
	epStr := "01"
	if md.Episode > 0 {
		epStr = fmt.Sprintf("%02d", md.Episode)
	}
	totalEps := "?"
	if md.TotalEpisodes > 0 {
		totalEps = strconv.Itoa(md.TotalEpisodes)
	} else if md.Episode > 0 {
		totalEps = strconv.Itoa(md.Episode)
	}
	rating := html.EscapeString(firstNonEmpty(md.Rating, "8.5 / 10"))
	genres := "Anime, Action"
	if len(md.Genres) > 0 {
		genres = html.EscapeString(strings.Join(md.Genres, ", "))
	}
	langs := "Tamil"
	if len(md.AudioLanguages) > 0 {
		langs = strings.Join(md.AudioLanguages, " + ")
	}
	audioCodec := firstNonEmpty(md.AudioCodec, "AAC")
	audioCh := firstNonEmpty(md.AudioChannels, "2.0")
	shortSub := firstNonEmpty(md.ShortSub, "ESub")
	plot := html.EscapeString(firstNonEmpty(md.Synopsis, "Curated lossless stream release by Anime Starfall."))

	r := strings.NewReplacer(
		"{title}", title,
		"{name}", title,
		"{year}", yearStr,
		"{season}", seasonStr,
		"{episode}", epStr,
		"{episodes}", totalEps,
		"{rating}", rating,
		"{genres}", genres,
		"{resolution}", firstNonEmpty(md.Resolution, "1080p"),
		"{bit}", firstNonEmpty(md.BitDepth, "10bit"),
		"{codec}", firstNonEmpty(md.VideoCodec, "HEVC"),
		"{languages}", langs,
		"{audio}", strings.TrimSpace(audioCodec+" "+audioCh),
		"{audio_codec}", audioCodec,
		"{audio_channels}", audioCh,
		"{shortsub}", shortSub,
		"{plot}", plot,
		"{synopsis}", plot,
	)

	rendered := r.Replace(tpl)
	// Clean up `- {year}` if year was unknown originally and user didn't want placeholder
	return strings.TrimSpace(rendered)
}

// ExtractSmartVideoThumbnail extracts a single 16:9 (1280x720) JPEG frame from `videoPath`
// using `ffmpeg -vframes 1` without decoding the whole stream.
func ExtractSmartVideoThumbnail(ctx context.Context, videoPath string, durationSec float64) ([]byte, error) {
	seekSec := 5
	if durationSec > 20 {
		seekSec = int(durationSec * 0.15)
		if seekSec > 90 {
			seekSec = 90
		}
	}

	tmpOut := videoPath + ".smart_thumb.jpg"
	defer func() { _ = os.Remove(tmpOut) }()

	cmd := safety.NewSupervisedCommand(
		ctx,
		"ffmpeg",
		"-hide_banner",
		"-loglevel", "error",
		"-ss", strconv.Itoa(seekSec),
		"-i", videoPath,
		"-vframes", "1",
		"-vf", "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2",
		"-q:v", "3",
		"-y", tmpOut,
	)
	if err := cmd.Run(); err != nil {
		return nil, err
	}
	return os.ReadFile(tmpOut)
}

func scaleAspectFill(src image.Image, dstW, dstH int) *image.RGBA {
	sb := src.Bounds()
	sw, sh := sb.Dx(), sb.Dy()
	if sw <= 0 || sh <= 0 {
		return image.NewRGBA(image.Rect(0, 0, dstW, dstH))
	}

	scaleX := float64(dstW) / float64(sw)
	scaleY := float64(dstH) / float64(sh)
	scale := scaleX
	if scaleY > scale {
		scale = scaleY
	}

	cropW := int(float64(dstW) / scale)
	cropH := int(float64(dstH) / scale)
	if cropW > sw {
		cropW = sw
	}
	if cropH > sh {
		cropH = sh
	}
	offX := sb.Min.X + (sw-cropW)/2
	offY := sb.Min.Y + (sh-cropH)/2
	cropRect := image.Rect(offX, offY, offX+cropW, offY+cropH)

	dst := image.NewRGBA(image.Rect(0, 0, dstW, dstH))
	xdraw.BiLinear.Scale(dst, dst.Bounds(), src, cropRect, draw.Src, nil)
	return dst
}

// fastBoxBlur performs a lightweight 2-pass separable box blur by downsampling 4x and upsampling,
// keeping CPU and heap allocations tiny (< 1 MiB) on the 2 vCPU VPS.
func fastBoxBlur(src *image.RGBA, radius int) *image.RGBA {
	b := src.Bounds()
	w, h := b.Dx(), b.Dy()
	smallW, smallH := w/6, h/6
	if smallW < 16 {
		smallW = 16
	}
	if smallH < 16 {
		smallH = 16
	}
	small := image.NewRGBA(image.Rect(0, 0, smallW, smallH))
	xdraw.BiLinear.Scale(small, small.Bounds(), src, b, draw.Src, nil)

	// Horizontal & vertical 3-tap smoothing on the tiny image
	tmp := image.NewRGBA(small.Bounds())
	passes := 2
	if radius > 8 {
		passes = 3
	}
	for p := 0; p < passes; p++ {
		for y := 0; y < smallH; y++ {
			for x := 0; x < smallW; x++ {
				var r, g, bl uint32
				var cnt uint32
				for dx := -2; dx <= 2; dx++ {
					nx := x + dx
					if nx >= 0 && nx < smallW {
						c := small.RGBAAt(nx, y)
						r += uint32(c.R)
						g += uint32(c.G)
						bl += uint32(c.B)
						cnt++
					}
				}
				tmp.SetRGBA(x, y, color.RGBA{uint8(r / cnt), uint8(g / cnt), uint8(bl / cnt), 255})
			}
		}
		for y := 0; y < smallH; y++ {
			for x := 0; x < smallW; x++ {
				var r, g, bl uint32
				var cnt uint32
				for dy := -2; dy <= 2; dy++ {
					ny := y + dy
					if ny >= 0 && ny < smallH {
						c := tmp.RGBAAt(x, ny)
						r += uint32(c.R)
						g += uint32(c.G)
						bl += uint32(c.B)
						cnt++
					}
				}
				small.SetRGBA(x, y, color.RGBA{uint8(r / cnt), uint8(g / cnt), uint8(bl / cnt), 255})
			}
		}
	}

	out := image.NewRGBA(b)
	xdraw.BiLinear.Scale(out, b, small, small.Bounds(), draw.Src, nil)
	return out
}

func drawAmbientGlow(canvas *image.RGBA, pal Palette) {
	b := canvas.Bounds()
	for y := 0; y < b.Dy(); y += 2 {
		ratio := float64(y) / float64(b.Dy())
		c := color.RGBA{
			R: uint8(float64(pal.BgDark.R)*(1-ratio*0.3) + float64(pal.Accent.R)*ratio*0.18),
			G: uint8(float64(pal.BgDark.G)*(1-ratio*0.3) + float64(pal.Accent.G)*ratio*0.18),
			B: uint8(float64(pal.BgDark.B)*(1-ratio*0.3) + float64(pal.Secondary.B)*ratio*0.22),
			A: 255,
		}
		draw.Draw(canvas, image.Rect(0, y, b.Dx(), y+2), &image.Uniform{C: c}, image.Point{}, draw.Src)
	}
}

func fillAlphaRect(dst *image.RGBA, r image.Rectangle, c color.RGBA) {
	draw.Draw(dst, r, &image.Uniform{C: c}, image.Point{}, draw.Over)
}

func drawRectBorder(dst *image.RGBA, r image.Rectangle, thickness int, c color.RGBA) {
	u := &image.Uniform{C: c}
	draw.Draw(dst, image.Rect(r.Min.X, r.Min.Y, r.Max.X, r.Min.Y+thickness), u, image.Point{}, draw.Over)
	draw.Draw(dst, image.Rect(r.Min.X, r.Max.Y-thickness, r.Max.X, r.Max.Y), u, image.Point{}, draw.Over)
	draw.Draw(dst, image.Rect(r.Min.X, r.Min.Y, r.Min.X+thickness, r.Max.Y), u, image.Point{}, draw.Over)
	draw.Draw(dst, image.Rect(r.Max.X-thickness, r.Min.Y, r.Max.X, r.Max.Y), u, image.Point{}, draw.Over)
}

func drawString(dst *image.RGBA, face font.Face, x, y int, text string, col color.RGBA) {
	if face == nil || text == "" {
		return
	}
	d := &font.Drawer{
		Dst:  dst,
		Src:  image.NewUniform(col),
		Face: face,
		Dot:  fixed.P(x, y),
	}
	d.DrawString(text)
}

func measureText(face font.Face, text string) int {
	if face == nil || text == "" {
		return 0
	}
	return font.MeasureString(face, text).Ceil()
}

func wrapText(face font.Face, text string, maxWidthPx, maxLines int) []string {
	words := strings.Fields(text)
	if len(words) == 0 {
		return nil
	}
	var lines []string
	cur := words[0]
	for _, w := range words[1:] {
		cand := cur + " " + w
		if measureText(face, cand) <= maxWidthPx {
			cur = cand
		} else {
			lines = append(lines, cur)
			cur = w
			if len(lines) >= maxLines-1 {
				break
			}
		}
	}
	if len(lines) < maxLines && cur != "" {
		// Append remaining words if we broke early on the last allowed line
		usedWords := 0
		for _, l := range lines {
			usedWords += len(strings.Fields(l))
		}
		rem := strings.Join(words[usedWords:], " ")
		lines = append(lines, truncateToWidth(face, rem, maxWidthPx))
	}
	return lines
}

func truncateToWidth(face font.Face, text string, maxWidthPx int) string {
	if measureText(face, text) <= maxWidthPx {
		return text
	}
	runes := []rune(text)
	for len(runes) > 3 {
		runes = runes[:len(runes)-1]
		cand := strings.TrimSpace(string(runes)) + "..."
		if measureText(face, cand) <= maxWidthPx {
			return cand
		}
	}
	return text
}

func sanitizeASCIIFallback(s string) string {
	var b strings.Builder
	for _, r := range s {
		if r <= 0x024F || r == '•' || r == '–' || r == '—' || r == '…' {
			b.WriteRune(r)
		}
	}
	return strings.TrimSpace(b.String())
}

func formatAudioSpec(md models.MediaMetadata) string {
	lang := "DUAL AUDIO"
	if len(md.AudioLanguages) > 0 {
		lang = strings.ToUpper(strings.Join(md.AudioLanguages, "+"))
	}
	return nonEmptyJoin(" ", lang, md.AudioCodec, md.AudioChannels)
}

func nonEmptyJoin(sep string, parts ...string) string {
	var out []string
	for _, p := range parts {
		if strings.TrimSpace(p) != "" {
			out = append(out, strings.TrimSpace(p))
		}
	}
	if len(out) == 0 {
		return "1080p HEVC"
	}
	return strings.Join(out, sep)
}

func firstNonEmpty(vals ...string) string {
	for _, v := range vals {
		if strings.TrimSpace(v) != "" {
			return strings.TrimSpace(v)
		}
	}
	return ""
}

func closeFaces(faces ...font.Face) {
	for _, f := range faces {
		if f != nil {
			_ = f.Close()
		}
	}
}
