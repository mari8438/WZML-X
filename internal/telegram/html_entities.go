// Package telegram provides a UTF-16 accurate HTML-to-MTProto entity parser supporting
// `<b>`, `<i>`, `<code>`, `<pre>`, `<u>`, `<s>`, `<tg-spoiler>`, `<a href="...">`,
// and `<blockquote expandable>` around multi-byte Unicode symbols (`🄰🅂`, `🥰`).
package telegram

import (
	"html"
	"strings"
	"unicode/utf16"

	"github.com/gotd/td/tg"
)

type openTag struct {
	name       string
	href       string
	expandable bool
	utf16Start int
}

// ParseHTMLMessage converts an HTML-formatted Telegram string into plain UTF-8 text
// and UTF-16 offset/length `[]tg.MessageEntityClass` entities for MTProto `SendMessage`/`SendMedia`.
func ParseHTMLMessage(input string) (string, []tg.MessageEntityClass) {
	if !strings.Contains(input, "<") {
		return html.UnescapeString(input), nil
	}

	var plain strings.Builder
	utf16Pos := 0
	var stack []openTag
	var entities []tg.MessageEntityClass

	appendText := func(rawChunk string) {
		if rawChunk == "" {
			return
		}
		unescaped := html.UnescapeString(rawChunk)
		plain.WriteString(unescaped)
		for _, r := range unescaped {
			utf16Pos += utf16.RuneLen(r)
		}
	}

	i := 0
	n := len(input)
	for i < n {
		lt := strings.IndexByte(input[i:], '<')
		if lt == -1 {
			appendText(input[i:])
			break
		}
		if lt > 0 {
			appendText(input[i : i+lt])
		}
		gt := strings.IndexByte(input[i+lt:], '>')
		if gt == -1 {
			appendText(input[i+lt:])
			break
		}

		tagContent := strings.TrimSpace(input[i+lt+1 : i+lt+gt])
		i = i + lt + gt + 1

		if tagContent == "" {
			continue
		}

		// Closing tag `</b>`, `</blockquote>`, etc.
		if strings.HasPrefix(tagContent, "/") {
			closeName := canonicalTagName(strings.TrimSpace(tagContent[1:]))
			// Pop matching tag from top of stack
			matchIdx := -1
			for sIdx := len(stack) - 1; sIdx >= 0; sIdx-- {
				if stack[sIdx].name == closeName {
					matchIdx = sIdx
					break
				}
			}
			if matchIdx != -1 {
				op := stack[matchIdx]
				stack = append(stack[:matchIdx], stack[matchIdx+1:]...)
				length := utf16Pos - op.utf16Start
				if length > 0 {
					if ent := buildMTProtoEntity(op, length); ent != nil {
						entities = append(entities, ent)
					}
				}
			}
			continue
		}

		// Self-closing `<br>` / `<br/>`
		lowTag := strings.ToLower(tagContent)
		if lowTag == "br" || lowTag == "br/" || lowTag == "br /" {
			appendText("\n")
			continue
		}

		// Opening tag
		fields := strings.Fields(tagContent)
		rawName := canonicalTagName(fields[0])
		switch rawName {
		case "b", "i", "u", "s", "code", "pre", "spoiler", "blockquote", "a":
			op := openTag{
				name:       rawName,
				utf16Start: utf16Pos,
			}
			if rawName == "blockquote" && strings.Contains(lowTag, "expandable") {
				op.expandable = true
			}
			if rawName == "a" {
				op.href = extractHrefAttr(tagContent)
			}
			stack = append(stack, op)
		default:
			// Unknown tag: treat literally so user text like `<1080p>` isn't swallowed
			appendText("<" + tagContent + ">")
		}
	}

	return plain.String(), entities
}

func canonicalTagName(name string) string {
	switch strings.ToLower(strings.TrimSpace(name)) {
	case "b", "strong":
		return "b"
	case "i", "em":
		return "i"
	case "u", "ins":
		return "u"
	case "s", "strike", "del":
		return "s"
	case "code":
		return "code"
	case "pre":
		return "pre"
	case "tg-spoiler", "spoiler":
		return "spoiler"
	case "blockquote":
		return "blockquote"
	case "a":
		return "a"
	default:
		return strings.ToLower(name)
	}
}

func extractHrefAttr(tag string) string {
	low := strings.ToLower(tag)
	idx := strings.Index(low, "href=")
	if idx == -1 {
		return ""
	}
	rest := strings.TrimSpace(tag[idx+5:])
	if len(rest) == 0 {
		return ""
	}
	quote := rest[0]
	if quote == '"' || quote == '\'' {
		end := strings.IndexByte(rest[1:], quote)
		if end != -1 {
			return html.UnescapeString(rest[1 : 1+end])
		}
	}
	fields := strings.Fields(rest)
	if len(fields) > 0 {
		return html.UnescapeString(fields[0])
	}
	return ""
}

func buildMTProtoEntity(op openTag, length int) tg.MessageEntityClass {
	switch op.name {
	case "b":
		return &tg.MessageEntityBold{Offset: op.utf16Start, Length: length}
	case "i":
		return &tg.MessageEntityItalic{Offset: op.utf16Start, Length: length}
	case "u":
		return &tg.MessageEntityUnderline{Offset: op.utf16Start, Length: length}
	case "s":
		return &tg.MessageEntityStrike{Offset: op.utf16Start, Length: length}
	case "code":
		return &tg.MessageEntityCode{Offset: op.utf16Start, Length: length}
	case "pre":
		return &tg.MessageEntityPre{Offset: op.utf16Start, Length: length}
	case "spoiler":
		return &tg.MessageEntitySpoiler{Offset: op.utf16Start, Length: length}
	case "blockquote":
		bq := &tg.MessageEntityBlockquote{
			Collapsed: op.expandable,
			Offset:    op.utf16Start,
			Length:    length,
		}
		bq.SetFlags()
		return bq
	case "a":
		if op.href != "" {
			return &tg.MessageEntityTextURL{
				Offset: op.utf16Start,
				Length: length,
				URL:    op.href,
			}
		}
	}
	return nil
}
