package telegram

import (
	"testing"
	"unicode/utf16"

	"github.com/gotd/td/tg"
)

func TestParseHTMLMessage_ExpandableBlockquoteAndEmojiOffsets(t *testing.T) {
	in := "<b>「 Solo Leveling - 2025 」</b>\n<blockquote expandable>Synopsis :\n   Shadow Monarch</blockquote>\nDropped By ➤ [@Anime_Starfall🥰]"
	plain, entities := ParseHTMLMessage(in)

	if len(entities) != 2 {
		t.Fatalf("expected 2 entities (bold + expandable blockquote), got %d", len(entities))
	}
	bold, ok := entities[0].(*tg.MessageEntityBold)
	if !ok {
		t.Fatalf("expected first entity to be bold, got %T", entities[0])
	}
	bq, ok := entities[1].(*tg.MessageEntityBlockquote)
	if !ok || !bq.Collapsed {
		t.Fatalf("expected second entity to be collapsed (expandable) blockquote, got %#v", entities[1])
	}

	u16 := utf16.Encode([]rune(plain))
	boldSlice := string(utf16.Decode(u16[bold.Offset : bold.Offset+bold.Length]))
	if boldSlice != "「 Solo Leveling - 2025 」" {
		t.Fatalf("bold UTF-16 slice mismatch: %q", boldSlice)
	}
	bqSlice := string(utf16.Decode(u16[bq.Offset : bq.Offset+bq.Length]))
	if bqSlice != "Synopsis :\n   Shadow Monarch" {
		t.Fatalf("blockquote UTF-16 slice mismatch: %q", bqSlice)
	}
}
