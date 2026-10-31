# ==============================================================================
# Starfall-Go Debrid v1.2 — Python / Legacy Compatibility Configuration Sample
# Note: In Starfall-Go, configuration is loaded primarily via `.env` or `docker-compose.yml`.
# You can copy `.env.example` to `.env` or edit this file as a reference.
# ==============================================================================

# REQUIRED TELEGRAM BOT CONFIG
BOT_TOKEN = ""                      # Primary bot token from @BotFather
OWNER_ID = 8814651149               # Telegram user ID of bot owner
TELEGRAM_API = 0                    # Telegram API ID from https://my.telegram.org
TELEGRAM_HASH = ""                  # Telegram API Hash from https://my.telegram.org

# HELPER UPLOAD BOTS (Space-separated tokens for 4GB MTProto multi-session uploads)
HELPER_TOKENS = ""

# PUBLIC BASE URL & VPS IP
BASE_URL = "http://your_vps_ip_here:8080"
BASE_URL_PORT = 8080

# USER SESSION (Optional Pyrogram string for 4GB Premium uploads)
USER_SESSION_STRING = ""

# DEFAULT BRANDING & POSTER SETTINGS
POST_TEMPLATE_ID = 11               # 1..11 (Template 11 = Starfall AniList Neo Glass)
POST_BRAND_NAME = "Anime Starfall"
LEECH_DUMP_CHAT = "-1003887117402"
AUTO_POSTER_ENABLED = True
AUTO_POSTER_USE_AS_THUMBNAIL = True

# LOSSLESS AUDIO / SUBTITLE AUTOMATION
AUTO_KEEP_AUDIO_LANGS = "Tamil"
AUTO_KEEP_SUBTITLE_LANGS = "English"
AUTO_INTRO_SUBTITLE = True
INTRO_SUBTITLE_TEXT = "[@Anime_Starfall🥰]"
AUTORENAME = True
LEECH_FILENAME_REMNAME_AUTO = "🄰🅂- [S{season}E{episode}] {name} {year} {resolution} {bit} {ott} {quality} {codec} [{languages}-{audio_codec} {audio_channels}] {shortsub} ~ [@Anime_Starfall🥰]"
