package user

import (
	"context"
	"path/filepath"
	"testing"
)

func TestSeedFromJSONDir_Owner8814651149(t *testing.T) {
	ctx := context.Background()
	store, err := NewStore(":memory:")
	if err != nil {
		t.Fatalf("NewStore(:memory:) error: %v", err)
	}
	defer store.Close()

	cfgDir := filepath.Join("..", "..", "configs")
	n, err := store.SeedFromJSONDir(ctx, cfgDir)
	if err != nil {
		t.Fatalf("SeedFromJSONDir error: %v", err)
	}
	if n < 1 {
		t.Fatalf("expected at least 1 seeded user from %s, got %d", cfgDir, n)
	}

	u, err := store.GetByTelegramID(ctx, 8814651149)
	if err != nil {
		t.Fatalf("GetByTelegramID(8814651149) error: %v", err)
	}
	if u.ID != "tg:8814651149" {
		t.Errorf("expected ID tg:8814651149, got %q", u.ID)
	}
	if u.TelegramDumpChat != "-1003887117402" {
		t.Errorf("expected TelegramDumpChat -1003887117402, got %q", u.TelegramDumpChat)
	}
	if u.Settings.LeechSplitSize != 4194304000 {
		t.Errorf("expected LeechSplitSize 4194304000, got %d", u.Settings.LeechSplitSize)
	}
	if u.Settings.PostTemplateID != 11 {
		t.Errorf("expected PostTemplateID 11, got %d", u.Settings.PostTemplateID)
	}
	if !u.Settings.AutoRename || !u.Settings.AutoProcess || !u.Settings.AutoIntroSubtitle || !u.Settings.AutoMetadata {
		t.Errorf("expected AutoRename/AutoProcess/AutoIntroSubtitle/AutoMetadata all true: %+v", u.Settings)
	}
	if u.Settings.AutoKeepAudioLangs != "Tamil" {
		t.Errorf("expected AutoKeepAudioLangs Tamil, got %q", u.Settings.AutoKeepAudioLangs)
	}
	if u.Settings.AutoKeepSubtitleLangs != "English" {
		t.Errorf("expected AutoKeepSubtitleLangs English, got %q", u.Settings.AutoKeepSubtitleLangs)
	}
	if u.Settings.AudioMetadata["title"] != "[@Anime_Starfall🥰] - {audiolang}" {
		t.Errorf("unexpected AudioMetadata title: %q", u.Settings.AudioMetadata["title"])
	}
}
