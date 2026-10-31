package media

import (
	"testing"

	"github.com/starfall-org/starfall-go/internal/models"
)

func TestParseAndFormatAutoRename_StarfallTemplate(t *testing.T) {
	rawName := "[Judas] Solo.Leveling.S02E05.2025.1080p.10bit.CR.WEB-DL.x265.Tamil.Japanese.EAC3.2.0.ESub.mkv"
	md := ParseFilename(rawName)

	if md.Season != 2 || md.Episode != 5 {
		t.Fatalf("expected S02E05, got S%02dE%02d", md.Season, md.Episode)
	}
	if md.Title != "Solo Leveling" {
		t.Fatalf("expected Title 'Solo Leveling', got %q", md.Title)
	}
	if md.Year != 2025 || md.Resolution != "1080p" || md.BitDepth != "10bit" || md.OTT != "CR" || md.Quality != "WEB-DL" || md.VideoCodec != "HEVC" {
		t.Fatalf("unexpected parsed video metadata: %+v", md)
	}

	settings := models.DefaultUserSettings()
	settings.AutoRename = true
	settings.NameSwap = `^\[Judas\]\s*`
	settings.LRemnameAuto = "🄰🅂- [S{season}E{episode}] {name} {year} {resolution} {bit} {ott} {quality} {codec} [{languages}-{audio_codec} {audio_channels}] {shortsub} ~ [@Anime_Starfall🥰]"

	got := FormatAutoRename(rawName, md, settings)
	want := "🄰🅂- [S02E05] Solo Leveling 2025 1080p 10bit CR WEB-DL HEVC [Tam+Jap-EAC3 2.0] ESub ~ [@Anime_Starfall🥰].mkv"
	if got != want {
		t.Fatalf("FormatAutoRename mismatch:\n got: %q\nwant: %q", got, want)
	}

	caption := FormatUploadCaption(got, "b")
	if caption != "<b>"+want+"</b>" {
		t.Fatalf("unexpected caption: %q", caption)
	}
}

func TestParseAnimeDashEpisode(t *testing.T) {
	rawName := "[SubsPlease] Dandadan - 09 (1080p) [A1B2C3D4].mkv"
	md := ParseFilename(rawName)
	if md.Title != "Dandadan" || md.Season != 1 || md.Episode != 9 || md.Resolution != "1080p" {
		t.Fatalf("unexpected anime dash parse: %+v", md)
	}
}
