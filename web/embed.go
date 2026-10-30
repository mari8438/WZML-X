// Package web embeds all HTML templates (`video_studio.html`, `bot_settings.html`)
// and static CSS/JS assets (`anime_themes.css`) directly into the single static
// `starfall-debrid` binary using Go 1.23+ embed.FS.
package web

import "embed"

// Assets bundles all templates and static files at compile time.
//
//go:embed all:templates all:static
var Assets embed.FS
