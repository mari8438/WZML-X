package video

import (
	"slices"
	"testing"

	"github.com/starfall-org/starfall-go/internal/models"
)

func TestBuildMkvmergeArgs_TamilKeepAndZeroMatchGuard(t *testing.T) {
	probe := &MediaProbe{
		Streams: []StreamInfo{
			{Index: 0, TypeIndex: 0, CodecType: "video", CodecName: "hevc"},
			{Index: 1, TypeIndex: 0, CodecType: "audio", CodecName: "eac3", Language: "jpn", Title: "Japanese"},
			{Index: 2, TypeIndex: 1, CodecType: "audio", CodecName: "eac3", Language: "tam", Title: "Tamil"},
			{Index: 3, TypeIndex: 0, CodecType: "subtitle", CodecName: "subrip", Language: "eng", Title: "English"},
			{Index: 4, TypeIndex: 1, CodecType: "subtitle", CodecName: "subrip", Language: "ara", Title: "Arabic"},
		},
	}

	settings := models.DefaultUserSettings()
	settings.AutoProcess = true
	settings.AutoKeepAudioLangs = "Tamil"
	settings.AutoKeepSubtitleLangs = "English"
	settings.IntroSubtitleText = "[@Anime_Starfall🥰]"

	args := BuildMkvmergeArgs("in.mkv", "out.mkv", probe, settings, nil, nil, "/tmp/intro.srt")
	if !slices.Contains(args, "--audio-tracks") || !slices.Contains(args, "2") {
		t.Fatalf("expected --audio-tracks 2 for Tamil stream, got: %v", args)
	}
	if !slices.Contains(args, "--subtitle-tracks") || !slices.Contains(args, "3") {
		t.Fatalf("expected --subtitle-tracks 3 for English stream, got: %v", args)
	}
	if !slices.Contains(args, "/tmp/intro.srt") {
		t.Fatalf("expected intro srt appended, got: %v", args)
	}

	// Zero-match audio guard test: source only has Japanese (`jpn`), user asked for `Tamil` -> must NOT mute!
	jpOnlyProbe := &MediaProbe{
		Streams: []StreamInfo{
			{Index: 0, TypeIndex: 0, CodecType: "video", CodecName: "hevc"},
			{Index: 1, TypeIndex: 0, CodecType: "audio", CodecName: "aac", Language: "jpn"},
		},
	}
	argsGuard := BuildMkvmergeArgs("jp.mkv", "out.mkv", jpOnlyProbe, settings, nil, nil, "")
	if slices.Contains(argsGuard, "--no-audio") || slices.Contains(argsGuard, "--audio-tracks") {
		t.Fatalf("zero-match audio guard failed; should keep original audio instead of muting: %v", argsGuard)
	}
}
