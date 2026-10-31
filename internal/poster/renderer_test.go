package poster

import (
	"bytes"
	"context"
	"image/jpeg"
	"strings"
	"testing"

	"github.com/starfall-org/starfall-go/internal/models"
)

func TestRenderPosterTemplates1To11AndExpandableCaption(t *testing.T) {
	ctx := context.Background()
	md := models.MediaMetadata{
		Title:          "Solo Leveling: Arise from the Shadow",
		Year:           2025,
		Season:         2,
		Episode:        5,
		TotalEpisodes:  13,
		Resolution:     "1080p",
		BitDepth:       "10bit",
		VideoCodec:     "HEVC",
		AudioCodec:     "EAC3",
		AudioChannels:  "2.0",
		AudioLanguages: []string{"Tam", "Jap"},
		ShortSub:       "ESub",
		Rating:         "8.8 / 10",
		Genres:         []string{"Action", "Adventure", "Fantasy"},
		Synopsis:       "Sung Jinwoo faces the High Orc dungeon boss Kargalgan in an intense shadow monarch battle.",
		Category:       "ANIME",
	}
	settings := models.DefaultUserSettings()

	for tplID := 1; tplID <= 11; tplID++ {
		jpgBytes, err := RenderPosterJPEG(ctx, nil, md, settings, tplID)
		if err != nil {
			t.Fatalf("RenderPosterJPEG(tpl=%d) error: %v", tplID, err)
		}
		img, err := jpeg.Decode(bytes.NewReader(jpgBytes))
		if err != nil {
			t.Fatalf("jpeg.Decode(tpl=%d) error: %v", tplID, err)
		}
		if img.Bounds().Dx() != 1280 || img.Bounds().Dy() != 720 {
			t.Fatalf("tpl %d expected 1280x720, got %dx%d", tplID, img.Bounds().Dx(), img.Bounds().Dy())
		}
	}

	caption := FormatExpandableCaption(md, settings)
	if !strings.Contains(caption, "<blockquote expandable>") || !strings.Contains(caption, "[@Anime_Starfall🥰]") {
		t.Fatalf("expected expandable blockquote and Anime_Starfall branding in caption:\n%s", caption)
	}
}
