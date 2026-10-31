// Package telegram implements the interactive `/usetting` and `/bsetting` inline
// keyboard menus (`menus_usetting.go`) synced 1:1 with the SQLite UserSettings store.
package telegram

import (
	"fmt"
	"html"
	"strconv"
	"strings"

	"github.com/gotd/td/tg"
	"github.com/starfall-org/starfall-go/internal/models"
)

func boolBadge(v bool) string {
	if v {
		return "✅ ON"
	}
	return "❌ OFF"
}

func valOrUnset(s string) string {
	if strings.TrimSpace(s) == "" {
		return "Not Set"
	}
	return html.EscapeString(truncateRunes(strings.TrimSpace(s), 44))
}

// buildUSettingMenu renders the requested `/usetting` sub-page (`main`, `leech`, `rename`, `video`, `poster`)
// along with a `tg.ReplyInlineMarkup` containing one-tap toggles, text-edit prompts, and Web Studio deep link.
func buildUSettingMenu(u *models.User, page string, webSettingsURL string) (string, *tg.ReplyInlineMarkup) {
	st := u.Settings
	var b strings.Builder

	var rows []tg.KeyboardButtonRow
	btn := func(text, data string) tg.KeyboardButtonClass {
		return &tg.KeyboardButtonCallback{Text: text, Data: []byte(data)}
	}
	urlBtn := func(text, link string) tg.KeyboardButtonClass {
		return &tg.KeyboardButtonURL{Text: text, URL: link}
	}

	switch page {
	case "leech":
		b.WriteString("<b>⚙️ USER SETTINGS — LEECH & DELIVERY</b>\n━━━━━━━━━━━━━━━━━━━━\n")
		fmt.Fprintf(&b, "📡 <b>Dump Chat:</b> <code>%s</code>\n", valOrUnset(st.LeechDumpChat))
		fmt.Fprintf(&b, "📦 <b>Split Size:</b> <code>%d MB</code>\n", st.LeechSplitSize/(1024*1024))
		fmt.Fprintf(&b, "📄 <b>Send As Document:</b> %s\n", boolBadge(st.AsDocument))
		fmt.Fprintf(&b, "🔢 <b>Sequential Leech:</b> %s\n", boolBadge(st.SequentialLeech))
		fmt.Fprintf(&b, "🔔 <b>Complete Msg:</b> %s\n", boolBadge(st.LeechCompleteMsg))
		fmt.Fprintf(&b, "🖋 <b>Caption Font:</b> <code>%s</code>\n", valOrUnset(st.LeechFont))

		rows = append(rows,
			tg.KeyboardButtonRow{Buttons: []tg.KeyboardButtonClass{
				btn("✏️ Set Dump Chat", "us:ask:LEECH_DUMP_CHAT"),
				btn("📄 Doc: "+boolBadge(st.AsDocument), "us:tog:AS_DOCUMENT:leech"),
			}},
			tg.KeyboardButtonRow{Buttons: []tg.KeyboardButtonClass{
				btn("🔢 Seq: "+boolBadge(st.SequentialLeech), "us:tog:SEQUENTIAL_LEECH:leech"),
				btn("🔔 DoneMsg: "+boolBadge(st.LeechCompleteMsg), "us:tog:LEECH_COMPLETE_MSG:leech"),
			}},
			tg.KeyboardButtonRow{Buttons: []tg.KeyboardButtonClass{
				btn("📦 2 GB Split", "us:set:SPLIT_2G:leech"),
				btn("💎 4 GB Split", "us:set:SPLIT_4G:leech"),
			}},
			tg.KeyboardButtonRow{Buttons: []tg.KeyboardButtonClass{btn("⬅️ Back", "us:page:main")}},
		)

	case "rename":
		b.WriteString("<b>🏷 USER SETTINGS — SMART AUTO-RENAME</b>\n━━━━━━━━━━━━━━━━━━━━\n")
		fmt.Fprintf(&b, "✨ <b>Auto-Rename:</b> %s\n", boolBadge(st.AutoRename))
		fmt.Fprintf(&b, "🧩 <b>Template:</b>\n<code>%s</code>\n", valOrUnset(st.LRemnameAuto))
		fmt.Fprintf(&b, "🔄 <b>Name Swap Regex:</b> <code>%s</code>\n", valOrUnset(st.NameSwap))
		fmt.Fprintf(&b, "⬅️ <b>Prefix:</b> <code>%s</code> | ➡️ <b>Suffix:</b> <code>%s</code>\n", valOrUnset(st.LeechPrefix), valOrUnset(st.LeechSuffix))

		rows = append(rows,
			tg.KeyboardButtonRow{Buttons: []tg.KeyboardButtonClass{
				btn("✨ Auto-Rename: "+boolBadge(st.AutoRename), "us:tog:AUTORENAME:rename"),
			}},
			tg.KeyboardButtonRow{Buttons: []tg.KeyboardButtonClass{
				btn("✏️ Edit Template", "us:ask:lremname_auto"),
				btn("✏️ Edit Name Swap", "us:ask:NAME_SWAP"),
			}},
			tg.KeyboardButtonRow{Buttons: []tg.KeyboardButtonClass{
				btn("✏️ Set Prefix", "us:ask:LEECH_PREFIX"),
				btn("✏️ Set Suffix", "us:ask:LEECH_SUFFIX"),
			}},
			tg.KeyboardButtonRow{Buttons: []tg.KeyboardButtonClass{btn("⬅️ Back", "us:page:main")}},
		)

	case "video":
		b.WriteString("<b>🎬 USER SETTINGS — LOSSLESS VIDEO PIPELINE</b>\n━━━━━━━━━━━━━━━━━━━━\n")
		fmt.Fprintf(&b, "⚡ <b>Auto-Process (0%% CPU Copy):</b> %s\n", boolBadge(st.AutoProcess))
		fmt.Fprintf(&b, "📦 <b>Auto-Unzip:</b> %s | 🔗 <b>Auto-Merge:</b> %s\n", boolBadge(st.AutoUnzip), boolBadge(st.AutoMerge))
		fmt.Fprintf(&b, "🔊 <b>Keep Audio Langs:</b> <code>%s</code>\n", valOrUnset(st.AutoKeepAudioLangs))
		fmt.Fprintf(&b, "💬 <b>Keep Sub Langs:</b> <code>%s</code>\n", valOrUnset(st.AutoKeepSubtitleLangs))
		fmt.Fprintf(&b, "🌟 <b>Intro Subtitle:</b> %s (<code>%s</code> • %ds)\n", boolBadge(st.AutoIntroSubtitle), valOrUnset(st.IntroSubtitleText), st.IntroSubtitleDuration)
		fmt.Fprintf(&b, "🏷 <b>Auto-Metadata:</b> %s (<code>%s</code>)\n", boolBadge(st.AutoMetadata), valOrUnset(st.MetadataTitle))

		rows = append(rows,
			tg.KeyboardButtonRow{Buttons: []tg.KeyboardButtonClass{
				btn("⚡ AutoProcess: "+boolBadge(st.AutoProcess), "us:tog:AUTO_PROCESS:video"),
				btn("🏷 Metadata: "+boolBadge(st.AutoMetadata), "us:tog:AUTO_METADATA:video"),
			}},
			tg.KeyboardButtonRow{Buttons: []tg.KeyboardButtonClass{
				btn("📦 Unzip: "+boolBadge(st.AutoUnzip), "us:tog:AUTO_UNZIP:video"),
				btn("🔗 Merge: "+boolBadge(st.AutoMerge), "us:tog:AUTO_MERGE:video"),
			}},
			tg.KeyboardButtonRow{Buttons: []tg.KeyboardButtonClass{
				btn("🌟 Intro Sub: "+boolBadge(st.AutoIntroSubtitle), "us:tog:AUTO_INTRO_SUBTITLE:video"),
				btn("✏️ Intro Text", "us:ask:INTRO_SUBTITLE_TEXT"),
			}},
			tg.KeyboardButtonRow{Buttons: []tg.KeyboardButtonClass{
				btn("🔊 Keep Audio", "us:ask:AUTO_KEEP_AUDIO_LANGS"),
				btn("💬 Keep Subs", "us:ask:AUTO_KEEP_SUBTITLE_LANGS"),
			}},
			tg.KeyboardButtonRow{Buttons: []tg.KeyboardButtonClass{
				btn("✏️ Metadata Brand", "us:ask:METADATA_TITLE"),
				btn("⬅️ Back", "us:page:main"),
			}},
		)

	case "poster":
		b.WriteString("<b>🖼 USER SETTINGS — AUTO-POSTER & THUMBNAILS</b>\n━━━━━━━━━━━━━━━━━━━━\n")
		fmt.Fprintf(&b, "🎨 <b>Auto-Poster:</b> %s\n", boolBadge(st.AutoPosterEnabled))
		fmt.Fprintf(&b, "🖼 <b>Use Poster As Video Thumb:</b> %s\n", boolBadge(st.AutoPosterUseAsThumb))
		fmt.Fprintf(&b, "📨 <b>Send Standalone Poster Msg:</b> %s\n", boolBadge(st.AutoPosterSendMessage))
		fmt.Fprintf(&b, "💎 <b>Template ID (1-11):</b> <code>#%d</code>\n", st.PostTemplateID)
		fmt.Fprintf(&b, "👑 <b>Brand Name:</b> <code>%s</code>\n", valOrUnset(st.PostBrandName))
		fmt.Fprintf(&b, "📸 <b>Smart Video Thumb Fallback:</b> %s\n", boolBadge(st.AutoThumbnail))

		rows = append(rows,
			tg.KeyboardButtonRow{Buttons: []tg.KeyboardButtonClass{
				btn("🎨 Poster: "+boolBadge(st.AutoPosterEnabled), "us:tog:AUTO_POSTER_ENABLED:poster"),
				btn("📨 SendMsg: "+boolBadge(st.AutoPosterSendMessage), "us:tog:AUTO_POSTER_SEND_MESSAGE:poster"),
			}},
			tg.KeyboardButtonRow{Buttons: []tg.KeyboardButtonClass{
				btn("🖼 UseAsThumb: "+boolBadge(st.AutoPosterUseAsThumb), "us:tog:AUTO_POSTER_USE_AS_THUMBNAIL:poster"),
				btn("📸 AutoThumb: "+boolBadge(st.AutoThumbnail), "us:tog:AUTO_THUMBNAIL:poster"),
			}},
			tg.KeyboardButtonRow{Buttons: []tg.KeyboardButtonClass{
				btn("🔢 Set Template (1-11)", "us:ask:POST_TEMPLATE_ID"),
				btn("👑 Set Brand Name", "us:ask:POST_BRAND_NAME"),
			}},
			tg.KeyboardButtonRow{Buttons: []tg.KeyboardButtonClass{btn("⬅️ Back", "us:page:main")}},
		)

	default:
		b.WriteString("<b>⚡ ANIME STARFALL — USER WORKFLOW STUDIO</b>\n━━━━━━━━━━━━━━━━━━━━\n")
		fmt.Fprintf(&b, "👤 <b>User:</b> <code>%s</code>\n", html.EscapeString(u.Name))
		fmt.Fprintf(&b, "📡 <b>Dump Channel:</b> <code>%s</code>\n", valOrUnset(st.LeechDumpChat))
		fmt.Fprintf(&b, "🏷 <b>Auto-Rename:</b> %s  |  🎬 <b>Auto-Process:</b> %s\n", boolBadge(st.AutoRename), boolBadge(st.AutoProcess))
		fmt.Fprintf(&b, "🔊 <b>Audio Keep:</b> <code>%s</code>  |  💬 <b>Sub Keep:</b> <code>%s</code>\n", valOrUnset(st.AutoKeepAudioLangs), valOrUnset(st.AutoKeepSubtitleLangs))
		fmt.Fprintf(&b, "🖼 <b>Poster Template:</b> <code>#%d</code> (%s)  |  🧭 <b>Mode:</b> <code>%s</code>\n", st.PostTemplateID, boolBadge(st.AutoPosterEnabled), strings.ToUpper(st.TaskMode))

		rows = append(rows,
			tg.KeyboardButtonRow{Buttons: []tg.KeyboardButtonClass{
				btn("📡 Leech & Split", "us:page:leech"),
				btn("🏷 Auto-Rename", "us:page:rename"),
			}},
			tg.KeyboardButtonRow{Buttons: []tg.KeyboardButtonClass{
				btn("🎬 Video Pipeline", "us:page:video"),
				btn("🖼 Poster (1-11)", "us:page:poster"),
			}},
			tg.KeyboardButtonRow{Buttons: []tg.KeyboardButtonClass{
				btn("🧭 Toggle Mode ("+strings.ToUpper(st.TaskMode)+")", "us:set:TOGGLE_MODE:main"),
			}},
		)
		if webSettingsURL != "" {
			rows = append(rows, tg.KeyboardButtonRow{Buttons: []tg.KeyboardButtonClass{
				urlBtn("🌐 Open Full Web Settings Studio", webSettingsURL),
			}})
		}
		rows = append(rows, tg.KeyboardButtonRow{Buttons: []tg.KeyboardButtonClass{
			btn("✖️ Close", "us:close"),
		}})
	}

	return b.String(), &tg.ReplyInlineMarkup{Rows: rows}
}

func applySettingToggle(st *models.UserSettings, key string) {
	switch key {
	case "AS_DOCUMENT":
		st.AsDocument = !st.AsDocument
	case "SEQUENTIAL_LEECH":
		st.SequentialLeech = !st.SequentialLeech
	case "LEECH_COMPLETE_MSG":
		st.LeechCompleteMsg = !st.LeechCompleteMsg
	case "AUTORENAME":
		st.AutoRename = !st.AutoRename
	case "AUTO_PROCESS":
		st.AutoProcess = !st.AutoProcess
	case "AUTO_METADATA":
		st.AutoMetadata = !st.AutoMetadata
	case "AUTO_UNZIP":
		st.AutoUnzip = !st.AutoUnzip
	case "AUTO_MERGE":
		st.AutoMerge = !st.AutoMerge
	case "AUTO_INTRO_SUBTITLE":
		st.AutoIntroSubtitle = !st.AutoIntroSubtitle
	case "AUTO_POSTER_ENABLED":
		st.AutoPosterEnabled = !st.AutoPosterEnabled
	case "AUTO_POSTER_SEND_MESSAGE":
		st.AutoPosterSendMessage = !st.AutoPosterSendMessage
	case "AUTO_POSTER_USE_AS_THUMBNAIL":
		st.AutoPosterUseAsThumb = !st.AutoPosterUseAsThumb
	case "AUTO_THUMBNAIL":
		st.AutoThumbnail = !st.AutoThumbnail
	}
}

func applyTextSettingInput(st *models.UserSettings, key, val string) error {
	val = strings.TrimSpace(val)
	if strings.EqualFold(val, "clear") || strings.EqualFold(val, "none") {
		val = ""
	}
	switch key {
	case "LEECH_DUMP_CHAT":
		if err := models.ValidateTelegramDumpChat(val); err != nil {
			return err
		}
		st.LeechDumpChat = val
	case "lremname_auto":
		st.LRemnameAuto = val
	case "NAME_SWAP":
		st.NameSwap = val
	case "LEECH_PREFIX":
		st.LeechPrefix = val
	case "LEECH_SUFFIX":
		st.LeechSuffix = val
	case "AUTO_KEEP_AUDIO_LANGS":
		st.AutoKeepAudioLangs = val
	case "AUTO_KEEP_SUBTITLE_LANGS":
		st.AutoKeepSubtitleLangs = val
	case "INTRO_SUBTITLE_TEXT":
		st.IntroSubtitleText = val
	case "METADATA_TITLE":
		st.MetadataTitle = val
		st.MetadataAudioTitle = val + " - {audiolang}"
		st.MetadataSubtitleTitle = val + " - {sublang}"
	case "POST_TEMPLATE_ID":
		id, err := strconv.Atoi(val)
		if err != nil {
			return models.ErrInvalidPosterTemplate
		}
		if err := models.ValidatePosterTemplate(id); err != nil {
			return err
		}
		st.PostTemplateID = id
	case "POST_BRAND_NAME":
		st.PostBrandName = val
	}
	return nil
}
