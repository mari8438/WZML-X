package telegram

import (
	"github.com/gotd/td/tg"
)

// InlineMarkupBuilder provides a fluent API for assembling inline keyboards.
type InlineMarkupBuilder struct {
	rows [][]tg.KeyboardButtonClass
}

// NewInlineMarkupBuilder initializes an empty inline markup builder.
func NewInlineMarkupBuilder() *InlineMarkupBuilder {
	return &InlineMarkupBuilder{
		rows: make([][]tg.KeyboardButtonClass, 0),
	}
}

// Row appends a new empty row to the markup builder.
func (b *InlineMarkupBuilder) Row(buttons ...tg.KeyboardButtonClass) *InlineMarkupBuilder {
	if len(buttons) > 0 {
		b.rows = append(b.rows, buttons)
	}
	return b
}

// ButtonData creates a callback data button.
func ButtonData(text, data string) tg.KeyboardButtonClass {
	return &tg.KeyboardButtonCallback{
		Text: text,
		Data: []byte(data),
	}
}

// ButtonURL creates a URL link button.
func ButtonURL(text, url string) tg.KeyboardButtonClass {
	return &tg.KeyboardButtonURL{
		Text: text,
		URL:  url,
	}
}

// Build compiles the configured rows into a tg.ReplyInlineMarkup.
func (b *InlineMarkupBuilder) Build() *tg.ReplyInlineMarkup {
	markupRows := make([]tg.KeyboardButtonRow, 0, len(b.rows))
	for _, row := range b.rows {
		if len(row) > 0 {
			markupRows = append(markupRows, tg.KeyboardButtonRow{
				Buttons: row,
			})
		}
	}
	return &tg.ReplyInlineMarkup{
		Rows: markupRows,
	}
}
