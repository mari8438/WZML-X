# ruff: noqa: F403, F405
mirror = """<b>Send link along with command line or </b>

/cmd link

<b>By replying to link/file</b>:

/cmd -n new name -e -up upload destination

<b>NOTE:</b>
1. Commands that start with <b>qb</b> are ONLY for torrents."""

yt = """<b>Send link along with command line</b>:

/cmd link
<b>By replying to link</b>:
/cmd -n new name -z password -opt x:y|x1:y1

Check here all supported <a href='https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md'>SITES</a>
Check all yt-dlp api options from this <a href='https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/YoutubeDL.py#L212'>FILE</a> or use this <a href='https://t.me/mltb_official_channel/177'>script</a> to convert cli arguments to api options."""

clone = """Send Gdrive|Gdot|Filepress|Filebee|Appdrive|Gdflix link or rclone path along with command or by replying to the link/rc_path by command.
Use -sync to use sync method in rclone. Example: /cmd rcl/rclone_path -up rcl/rclone_path/rc -sync"""

new_name = """<b>New Name</b>: -n

/cmd link -n new name
Note: Doesn't work with torrents"""

multi_link = """<b>Multi links only by replying to first link/file</b>: -i

/cmd -i 10(number of links/files)"""

same_dir = """<b>Move file(s)/folder(s) to new folder</b>: -m

You can use this arg also to move multiple links/torrents contents to the same directory, so all links will be uploaded together as one task

/cmd link -m new folder (only one link inside new folder)
/cmd -i 10(number of links/files) -m folder name (all links contents in one folder)
/cmd -b -m folder name (reply to batch of message/file(each link on new line))

Bare <code>-m</code> has media-aware behavior:
/cmd link -m (one video: open Manual Merge Tracks)
/cmd -i 10 -m (multiple videos: concatenate in order)

While using bulk you can also use this arg with different folder name along with the links in message or file batch
Example:
link1 -m folder1
link2 -m folder1
link3 -m folder2
link4 -m folder2
link5 -m folder3
link6
so link1 and link2 content will be uploaded from same folder which is folder1
link3 and link4 content will be uploaded from same folder also which is folder2
link5 will be uploaded alone inside new folder named folder3
link6 will get uploaded normally alone
"""

thumb = """<b>Thumbnail for current task</b>: -t

/cmd link -t image-url or tg-message-link (doc or photo) or none (file without thumb)
Reply to a photo/image document with <code>-t</code> to save it as your persistent custom thumbnail.
Supports any direct image URL (jpg, png, webp, etc.) or a Telegram message link containing a photo/document."""

split_size = """<b>Split size for current task</b>: -sp

/cmd link -sp (500mb or 2gb or 4000000000)
Note: Only mb and gb are supported or write in bytes without unit!"""

upload = """<b>Upload Destination</b>: -up

/cmd link -up rcl/gdl (rcl: to select rclone config, remote & path | gdl: To select token.pickle, gdrive id) using buttons
You can directly add the upload path: -up remote:dir/subdir or -up Gdrive_id or -up id/username (telegram) or -up id/username|topic_id (telegram)
If DEFAULT_UPLOAD is `rc` then you can pass up: `gd` to upload using gdrive tools to GDRIVE_ID.
If DEFAULT_UPLOAD is `gd` then you can pass up: `rc` to upload to RCLONE_PATH.

If you want to add path or gdrive manually from your config/token (UPLOADED FROM USETTING) add mrcc: for rclone and mtp: before the path/gdrive_id without space.
/cmd link -up mrcc:main:dump or -up mtp:gdrive_id <strong>or you can simply edit upload using owner/user token/config from usetting without adding mtp: or mrcc: before the upload path/id</strong>

To add leech destination:
-up id/@username/pm
-up b:id/@username/pm (b: means leech by bot) (id or username of the chat or write pm means private message so bot will send the files in private to you)
when you should use b:(leech by bot)? When your default settings is leech by user and you want to leech by bot for specific task.
-up u:id/@username(u: means leech by user) This in case OWNER added USER_STRING_SESSION.
-up h:id/@username(hybrid leech) h: to upload files by bot and user based on file size.
-up id/@username|topic_id(leech in specific chat and topic) add | without space and write topic id after chat id or username.

In case you want to specify whether using token.pickle or service accounts you can add tp:gdrive_id (using token.pickle) or sa:gdrive_id (using service accounts) or mtp:gdrive_id (using token.pickle uploaded from usetting).
DEFAULT_UPLOAD doesn't affect on leech cmds.
"""

user_download = """<b>User Download</b>: link

/cmd tp:link to download using owner token.pickle in case service account enabled.
/cmd sa:link to download using service account in case service account disabled.
/cmd tp:gdrive_id to download using token.pickle and file_id in case service account enabled.
/cmd sa:gdrive_id to download using service account and file_id in case service account disabled.
/cmd mtp:gdrive_id or mtp:link to download using user token.pickle uploaded from usetting
/cmd mrcc:remote:path to download using user rclone config uploaded from usetting
you can simply edit upload using owner/user token/config from usetting without adding mtp: or mrcc: before the path/id"""

rcf = """<b>Rclone Flags</b>: -rcf

/cmd link|path|rcl -up path|rcl -rcf --buffer-size:8M|--drive-starred-only|key|key:value
This will override all other flags except --exclude
Check here all <a href='https://rclone.org/flags/'>RcloneFlags</a>."""

bulk = """<b>Bulk Download</b>: -b

Bulk can be used only by replying to text message or text file contains links separated by new line.
Example:
link1 -n new name -up remote1:path1 -rcf |key:value|key:value
link2 -z -n new name -up remote2:path2
link3 -e -n new name -up remote2:path2
Reply to this example by this cmd -> /cmd -b(bulk)

Note: Any arg along with the cmd will be set to all links
/cmd -b -up remote: -z -m folder name (all links contents in one zipped folder uploaded to one destination)
so you can't set different upload destinations along with link in case you have added -m along with cmd
You can set start and end of the links from the bulk like seed, with -b start:end or only end by -b :end or only start by -b start.
The default start is from zero(first link) to inf."""

rclone_dl = """<b>Rclone Download</b>:

Treat rclone paths exactly like links
/cmd main:dump/ubuntu.iso or rcl(To select config, remote and path)
Users can add their own rclone from user settings
If you want to add path manually from your config add mrcc: before the path without space
/cmd mrcc:main:dump/ubuntu.iso
You can simply edit using owner/user config from usetting without adding mrcc: before the path"""

extract_zip = """<b>Extract/Zip</b>: -e -z

/cmd link -e password (extract password protected)
/cmd link -z password (zip password protected)
/cmd link -z password -e (extract and zip password protected)
Note: When both extract and zip added with cmd it will extract first and then zip, so always extract first"""

join = """<b>Join Splitted Files</b>: -j

This option will only work before extract and zip, so mostly it will be used with -m argument (samedir)
By Reply:
/cmd -i 3 -j -m folder name
/cmd -b -j -m folder name
if u have link(folder) have splitted files:
/cmd link -j"""

tg_links = """<b>TG Links</b>:

Treat links like any direct link
Some links need user access so you must add USER_SESSION_STRING for it.
Three types of links:
Public: https://t.me/channel_name/message_id
Private: tg://openmessage?user_id=xxxxxx&message_id=xxxxx
Super: https://t.me/c/channel_id/message_id
Range: https://t.me/channel_name/first_message_id-last_message_id
Range Example: tg://openmessage?user_id=xxxxxx&message_id=555-560 or https://t.me/channel_name/100-150
Note: Range link will work only by replying cmd to it"""

sample_video = """<b>Sample Video</b>: -sv

Create sample video for one video or folder of videos.
/cmd -sv (it will take the default values which 60sec sample duration and part duration is 4sec).
You can control those values. Example: /cmd -sv 70:5(sample-duration:part-duration) or /cmd -sv :5 or /cmd -sv 70."""

screenshot = """<b>ScreenShots</b>: -ss

Create screenshots for one video or folder of videos.
/cmd -ss (it will take the default values which is 10 photos).
You can control this value. Example: /cmd -ss 6."""

seed = """<b>Bittorrent seed</b>: -d

/cmd link -d ratio:seed_time or by replying to file/link
To specify ratio and seed time add -d ratio:time.
Example: -d 0.7:10 (ratio and time) or -d 0.7 (only ratio) or -d :10 (only time) where time in minutes"""

zip_arg = """<b>Zip</b>: -z password

/cmd link -z (zip)
/cmd link -z password (zip password protected)"""

qual = """<b>Quality Buttons</b>: -s

In case default quality added from yt-dlp options using format option and you need to select quality for specific link or links with multi links feature.
/cmd link -s"""

yt_opt = """<b>Options</b>: -opt

/cmd link -opt {"format": "bv*+mergeall[vcodec=none]", "nocheckcertificate": True, "playliststart": 10, "fragment_retries": float("inf"), "matchtitle": "S13", "writesubtitles": True, "live_from_start": True, "postprocessor_args": {"ffmpeg": ["-threads", "4"]}, "wait_for_video": (5, 100), "download_ranges": [{"start_time": 0, "end_time": 10}]}

Check all yt-dlp api options from this <a href='https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/YoutubeDL.py#L184'>FILE</a> or use this <a href='https://t.me/mltb_official_channel/177'>script</a> to convert cli arguments to api options."""

convert_media = """<b>Convert Media</b>: -ca -cv
/cmd link -ca mp3 -cv mp4 (convert all audios to mp3 and all videos to mp4)
/cmd link -ca mp3 (convert all audios to mp3)
/cmd link -cv mp4 (convert all videos to mp4)
/cmd link -ca mp3 + flac ogg (convert only flac and ogg audios to mp3)
/cmd link -cv mkv - webm flv (convert all videos to mp4 except webm and flv)"""

force_start = """<b>Force Start</b>: -f -fd -fu
/cmd link -f (force download and upload)
/cmd link -fd (force download only)
/cmd link -fu (force upload directly after download finish)"""

gdrive = """<b>Gdrive</b>: link
If DEFAULT_UPLOAD is `rc` then you can pass up: `gd` to upload using gdrive tools to GDRIVE_ID.
/cmd gdriveLink or gdl or gdriveId -up gdl or gdriveId or gd
/cmd tp:gdriveLink or tp:gdriveId -up tp:gdriveId or gdl or gd (to use token.pickle if service account enabled)
/cmd sa:gdriveLink or sa:gdriveId -p sa:gdriveId or gdl or gd (to use service account if service account disabled)
/cmd mtp:gdriveLink or mtp:gdriveId -up mtp:gdriveId or gdl or gd(if you have added upload gdriveId from usetting) (to use user token.pickle that uploaded by usetting)
You can simply edit using owner/user token from usetting without adding mtp: before the id"""

rclone_cl = """<b>Rclone</b>: path
If DEFAULT_UPLOAD is `gd` then you can pass up: `rc` to upload to RCLONE_PATH.
/cmd rcl/rclone_path -up rcl/rclone_path/rc -rcf flagkey:flagvalue|flagkey|flagkey:flagvalue
/cmd rcl or rclone_path -up rclone_path or rc or rcl
/cmd mrcc:rclone_path -up rcl or rc(if you have add rclone path from usetting) (to use user config)
You can simply edit using owner/user config from usetting without adding mrcc: before the path"""

name_swap = r"""<b>Name Substitution</b>: -ns
/cmd link -ns script/code/s | mirror/leech | tea/ /s | clone | cpu/ | \[mltb\]/mltb | \\text\\/text/s
This will affect on all files. Format: wordToReplace/wordToReplaceWith/sensitiveCase
Word Substitutions. You can add pattern instead of normal text. Timeout: 60 sec
NOTE: You must add \ before any character, those are the characters: \^$.|?*+()[]{}-
1. script will get replaced by code with sensitive case
2. mirror will get replaced by leech
4. tea will get replaced by space with sensitive case
5. clone will get removed
6. cpu will get replaced by space
7. [mltb] will get replaced by mltb
8. \text\ will get replaced by text with sensitive case
"""

transmission = """<b>Tg transmission</b>: -hl -ut -bt
/cmd link -hl (both: user for >2GB, bot for ≤2GB)
/cmd link -bt (bot only)
/cmd link -ut (user only)"""

thumbnail_layout = """Thumbnail Layout: -tl
/cmd link -tl 3x3 (widthxheight) 3 photos in row and 3 photos in column"""

leech_as = """<b>Leech as</b>: -doc -med
/cmd link -doc (Leech as document)
/cmd link -med (Leech as media)"""

ffmpeg_cmds = """<b>FFmpeg Commands</b>: -ff
list of lists of ffmpeg commands. You can set multiple ffmpeg commands for all files before upload. Don't write ffmpeg at beginning, start directly with the arguments.
Notes:
1. Add <code>-del</code> to the list(s) which you want from the bot to delete the original files after command run complete!
3. To execute one of pre-added lists in bot like: ({"subtitle": ["-i mltb.mkv -c copy -c:s srt mltb.mkv"]}), you must use -ff subtitle (list key)
Examples: ["-i mltb.mkv -c copy -c:s srt mltb.mkv", "-i mltb.video -c copy -c:s srt mltb", "-i mltb.m4a -c:a libmp3lame -q:a 2 mltb.mp3", "-i mltb.audio -c:a libmp3lame -q:a 2 mltb.mp3", "-i mltb -map 0:a -c copy mltb.mka -map 0:s -c copy mltb.srt"]
Here I will explain how to use mltb.* which is reference to files you want to work on.
1. First cmd: the input is mltb.mkv so this cmd will work only on mkv videos and the output is mltb.mkv also so all outputs are mkv. -del will delete the original media after complete run of the cmd.
2. Second cmd: the input is mltb.video so this cmd will work on all videos and the output is only mltb so the extension is the same as input files.
3. Third cmd: the input is mltb.m4a so this cmd will work only on m4a audios and the output is mltb.mp3 so the output extension is mp3.
4. Fourth cmd: the input is mltb.audio so this cmd will work on all audios and the output is mltb.mp3 so the output extension is mp3."""

metadata = """<b>Metadata</b>: -meta

Apply custom metadata to media files using pipe (|) separator.

<b>Format:</b> key=value|key2=value2|key3=value3

<b>Dynamic Variables:</b>
• <code>{filename}</code> - Original filename
• <code>{basename}</code> - Filename without extension  
• <code>{extension}</code> - File extension
• <code>{audiolang}</code> - Audio language (auto-detected or English)
• <code>{sublang}</code> - Subtitle language (auto-detected or none)
• <code>{year}</code> - Year extracted from filename

<b>Per-Stream Metadata:</b>
Set different metadata for audio/video/subtitle streams in User Settings > FFmpeg Settings:
• <b>Audio Metadata:</b> Applied to each audio stream
• <b>Video Metadata:</b> Applied to video streams  
• <b>Subtitle Metadata:</b> Applied to subtitle streams

<b>Examples:</b>
<code>/mirror link -meta title=My Movie|artist={audiolang} Version</code>
<code>/yt link -meta album={basename}|year={year}|genre=Action</code>

<b>Escape Pipes:</b> Use <code>\\|</code> to include literal pipe in values:
<code>title=Movie \\| Director's Cut</code>

<b>User Settings Example:</b>
• Audio Metadata: <code>language={audiolang}|title=Audio Track</code>
• Video Metadata: <code>title={basename}|year={year}</code>
• Subtitle Metadata: <code>language={sublang}|title=Subtitles</code>"""

auto_process = """<b>Auto Process</b>
Configure in /usettings > Auto Process.

Order:
download -> unzip -> order tracks -> remove streams -> smart merge -> intro subtitle -> metadata -> auto rename -> sequential upload

Notes:
1. Smart merge batches episodes under Telegram max size minus AUTO_MERGE_SAFETY_MB.
2. Range names use <code>[S1-EP(01-06)]</code> and never mix seasons.
3. Subtitle translate uses LIBRE_TRANSLATE_API_URL and optional LIBRE_TRANSLATE_API_KEY from config.py.
4. Intro subtitles use INTRO_SUBTITLE_RANGES and fade in/out as a muxed subtitle track.
5. Auto rename and auto thumbnail use TMDb first, then AniList/MyAnimeList, then filename/video-frame fallback.
6. AutoLeech can start leech tasks from plain links/files for authorized users when AUTO_LEECH is enabled.
7. Caption <code>{filename}</code> keeps the full final name with prefix/suffix; <code>{upload_filename}</code> is the shortened Telegram filename.

AutoRename variables include:
<code>{file_name}</code> <code>{file_size}</code> <code>{file_caption}</code> <code>{languages}</code> <code>{subtitles}</code> <code>{duration}</code> <code>{ott}</code> <code>{resolution}</code> <code>{name}</code> <code>{title}</code> <code>{year}</code> <code>{quality}</code> <code>{DS4K}</code> <code>{season}</code> <code>{episode}</code> <code>{audio}</code> <code>{lib}</code> <code>{extension}</code> <code>{shortsub}</code> <code>{shortlang}</code> <code>{part}</code> <code>{raw_name}</code> <code>{link}</code> <code>{vcodec}</code> <code>{codec}</code> <code>{acodec}</code> <code>{audio_codec}</code> <code>{audio_channels}</code> <code>{audio_bitrate}</code> <code>{hdr}</code> <code>{dynamic_range}</code> <code>{release_group}</code> <code>{group}</code>."""

starfallx_video_tools = """<b>StarFallX Video Tools</b>: -vt

Use <code>/cmd link -vt</code>. If streams are not ready, the bot waits for download/extract to finish and opens the menu later.

Tools: Remove Stream, Extract Stream, Change Order, Audio Order, Subtitle Order, Merge Tracks, Translate Subs, Video + Video.

Extract Stream is exclusive and uploads only extracted audio/subtitle artifacts."""

starfallx_autorename = """<b>StarFallX AutoRename</b>

Task override:
<code>/cmd link -ar custom [S{season}E{episode}] {name} {resolution} {DS4K} {codec}</code>

Reverse create can turn a sample filename into a template. Missing variables render blank.

Useful variables:
<code>{name}</code> <code>{title}</code> <code>{year}</code> <code>{season}</code> <code>{episode}</code> <code>{resolution}</code> <code>{quality}</code> <code>{DS4K}</code> <code>{codec}</code> <code>{audio_codec}</code> <code>{audio_channels}</code> <code>{shortsub}</code> <code>{release_group}</code>."""

starfallx_batch = """<b>StarFallX Batch Leech</b>

<code>/bleech link1 link2 link3</code> runs many links with BLEECH download/upload limits.
Add <code>-m</code> to download every supplied link fully, then merge and upload them as one task.

<code>/bqleech magnet</code> is qB-only huge torrent batching using <code>BQLEECH_BATCH_SIZE_GB</code>.

Restart recovery can resume these plans when <code>BATCH_TASK_RESTART_RESUME</code> is enabled."""

starfallx_thumbnail = """<b>StarFallX Auto Thumbnail</b>

Priority: custom user thumb -> TMDb -> AniList -> MyAnimeList -> FFmpeg frame.

Provider images are saved from HD sources. Document thumbs are resized only when Telegram requires a small thumbnail."""

starfallx_upload = """<b>StarFallX Upload Engine</b>

Helper bot tokens improve parallel multi-file uploads, not one-file speed.

Rules:
1. One helper token = one active upload.
2. Normal users use their own helper tokens.
3. Owner/sudo can use approved helper pool.
4. 2GB+ uploads need premium user session or split fallback.
5. Add helper bots to LEECH_DUMP_CHAT for sequential dump/copy support."""

YT_HELP_DICT = {
    "main": yt,
    "New-Name": f"{new_name}\nNote: Don't add file extension",
    "Zip": zip_arg,
    "Quality": qual,
    "Options": yt_opt,
    "Multi-Link": multi_link,
    "Same-Directory": same_dir,
    "Thumb": thumb,
    "Split-Size": split_size,
    "Upload-Destination": upload,
    "Rclone-Flags": rcf,
    "Bulk": bulk,
    "Sample-Video": sample_video,
    "Screenshot": screenshot,
    "Convert-Media": convert_media,
    "Force-Start": force_start,
    "Name-Swap": name_swap,
    "TG-Transmission": transmission,
    "Thumb-Layout": thumbnail_layout,
    "Leech-Type": leech_as,
    "FFmpeg-Cmds": ffmpeg_cmds,
    "Metadata": metadata,
    "Auto-Process": auto_process,
    "StarFallX-VideoTools": starfallx_video_tools,
    "StarFallX-AutoRename": starfallx_autorename,
    "StarFallX-Batch": starfallx_batch,
    "StarFallX-Thumbnail": starfallx_thumbnail,
    "StarFallX-Upload": starfallx_upload,
}

MIRROR_HELP_DICT = {
    "main": mirror,
    "New-Name": new_name,
    "DL-Auth": "<b>Direct link authorization</b>: -au -ap\n\n/cmd link -au username -ap password",
    "Headers": "<b>Direct link custom headers</b>: -h\n\n/cmd link -h key: value key1: value1",
    "Extract/Zip": extract_zip,
    "Select-Files": "<b>Bittorrent/JDownloader/Sabnzbd File Selection</b>: -s\n\n/cmd link -s or by replying to file/link",
    "Torrent-Seed": seed,
    "Multi-Link": multi_link,
    "Same-Directory": same_dir,
    "Thumb": thumb,
    "Split-Size": split_size,
    "Upload-Destination": upload,
    "Rclone-Flags": rcf,
    "Bulk": bulk,
    "Join": join,
    "Rclone-DL": rclone_dl,
    "Tg-Links": tg_links,
    "Sample-Video": sample_video,
    "Screenshot": screenshot,
    "Convert-Media": convert_media,
    "Force-Start": force_start,
    "User-Download": user_download,
    "Name-Swap": name_swap,
    "TG-Transmission": transmission,
    "Thumb-Layout": thumbnail_layout,
    "Leech-Type": leech_as,
    "FFmpeg-Cmds": ffmpeg_cmds,
    "Metadata": metadata,
    "Auto-Process": auto_process,
    "StarFallX-VideoTools": starfallx_video_tools,
    "StarFallX-AutoRename": starfallx_autorename,
    "StarFallX-Batch": starfallx_batch,
    "StarFallX-Thumbnail": starfallx_thumbnail,
    "StarFallX-Upload": starfallx_upload,
}

CLONE_HELP_DICT = {
    "main": clone,
    "Multi-Link": multi_link,
    "Bulk": bulk,
    "Gdrive": gdrive,
    "Rclone": rclone_cl,
}

RSS_HELP_MESSAGE = """
Use this format to add feed url:
Title1 link (required)
Title2 link -c cmd -inf xx -exf xx
Title3 link -c cmd -d ratio:time -z password

-c command -up mrcc:remote:path/subdir -rcf --buffer-size:8M|key|key:value
-inf For included words filter.
-exf For excluded words filter.
-stv true or false (sensitive filter)
-al true or false (owner/sudo RSS auto-leech into RSS_CHAT)
-lb bot or user (RSS upload client; bot is default and does not need user session)
-ar title, remove_dots, or none (RSS auto-leech filename source)

Example: Title https://www.rss-url.com -al true -lb bot -ar title -inf 1080 or 720 or 144p|mkv or mp4|hevc -exf flv or web|xxx
This filter will parse links that its titles contain `(1080 or 720 or 144p) and (mkv or mp4) and hevc` and doesn't contain (flv or web) and xxx words. You can add whatever you want.

Another example: -inf  1080  or 720p|.web. or .webrip.|hevc or x264. This will parse titles that contain ( 1080  or 720p) and (.web. or .webrip.) and (hevc or x264). I have added space before and after 1080 to avoid wrong matching. If this `10805695` number in title it will match 1080 if added 1080 without spaces after it.

Filter Notes:
1. | means and.
2. Add `or` between similar keys, you can add it between qualities or between extensions, so don't add filter like this f: 1080|mp4 or 720|web because this will parse 1080 and (mp4 or 720) and web ... not (1080 and mp4) or (720 and web).
3. You can add `or` and `|` as much as you want.
4. Take a look at the title if it has a static special character after or before the qualities or extensions or whatever and use them in the filter to avoid wrong match.
Timeout: 60 sec.
"""

PASSWORD_ERROR_MESSAGE = """
<b>This link requires a password!</b>
- Insert <b>::</b> after the link and write the password after the sign.

<b>Example:</b> link::my password
"""


def get_bot_commands():
    from ...core.plugin_manager import get_plugin_manager

    static_commands = {
        "Mirror": "[link/file] Mirror to Upload Destination",
        "QbMirror": "[magnet/torrent] Mirror to Upload Destination using qbit",
        "Ytdl": "[link] Mirror YouTube, m3u8, Social Media and yt-dlp supported urls",
        "UpHoster": "[link/file] Upload to DDL Servers",
        "Leech": "[link/file] Leech files to Upload to Telegram",
        "QbLeech": "[magnet/torrent] Leech files to Upload to Telegram using qbit",
        "BigQLeech": "[magnet/torrent] Leech huge qB torrents in safe 30GB batches",
        "BatchLeech": "[links] Leech multiple links with download/upload batch limits",
        "YtdlLeech": "[link] Leech YouTube, m3u8, Social Media and yt-dlp supported urls",
        "Clone": "[link] Clone files/folders to GDrive",
        "UserSet": "User personal settings",
        "ForceStart": "[gid/reply] Force start from queued task",
        "Count": "[link] Count no. of files/folders in GDrive",
        "List": "[query] Search any Text which is available in GDrive",
        "Search": "[query] Search torrents via Qbit Plugins",
        "CreateTorrent": "[link/reply] Create torrent release pack",
        "MediaInfo": "[reply/link] Get MediaInfo of the Target Media",
        "Select": "[gid/reply] Select files for NZB, Aria2, Qbit Tasks",
        "Ping": "Ping Bot to test Response Speed",
        "Status": "[id/me] Tasks Status of Bot",
        "Stats": "Bot, OS, Repo & System full Statistics",
        "Rss": "User RSS Management Settings",
        "IMDB": "[query] or ttxxxxxx Get IMDB info",
        "Poster": "[query/reply] Search and save edited poster thumbnail",
        "Sites": "Show owner-configured useful website links",
        "CancelAll": "Cancel all Tasks on the Bot",
        "Help": "Detailed help usage of the WZ Bot",
        "BotSet": "[SUDO] Bot Management Settings",
        "Log": "[SUDO] Get Bot Logs for Internal Working",
        "Restart": "[SUDO] Reboot bot",
        "RestartSessions": "[SUDO] Reboot User Sessions",
        "GenPyroSess": "[SUDO] Generate Pyrogram String Session",
    }

    commands = static_commands.copy()

    plugin_manager = get_plugin_manager()
    if plugin_manager:
        for plugin_info in plugin_manager.list_plugins():
            if plugin_info.enabled and plugin_info.commands:
                for cmd in plugin_info.commands:
                    key = cmd.capitalize()
                    if key not in commands:
                        commands[key] = (
                            plugin_info.description or f"Plugin command: {cmd}"
                        )

    return commands


BOT_COMMANDS = get_bot_commands()


def get_help_string():
    from ..telegram_helper.tg_ui_formatter import (
        PREMIUM_BOLT,
        PREMIUM_DOC,
        PREMIUM_GEAR,
        PREMIUM_MOVIE,
        PREMIUM_PACKAGE,
        safe_emoji,
    )

    star_icon = safe_emoji(PREMIUM_BOLT, "🌟")
    leech_icon = safe_emoji(PREMIUM_PACKAGE, "📥")
    mirror_icon = safe_emoji(PREMIUM_DOC, "☁️")
    vt_icon = safe_emoji(PREMIUM_MOVIE, "🎬")
    pref_icon = safe_emoji(PREMIUM_GEAR, "⚙️")

    return (
        f"<b>{star_icon} StarFallX v1.2 — Commands Guide</b>\n"
        "<i>Tap a group to open it.</i>\n\n"
        f"<blockquote expandable><b>{leech_icon} LEECH COMMANDS</b> · download to Telegram\n"
        "• /leech or /l [link] : Leech direct link to Telegram\n"
        "• /bleech or /bl [links] : Batch leech multiple links\n"
        "• /qbleech or /ql [magnet/torrent] : Leech via qBittorrent\n"
        "• /ytdl or /y [link] : Leech YouTube/yt-dlp stream\n"
        "• /leech -vt : Open Video Tools after download\n"
        "• /leech -s : Generate video sample before upload</blockquote>\n\n"
        f"<blockquote expandable><b>{mirror_icon} MIRROR COMMANDS</b> · cloud & rclone uploads\n"
        "• /mirror or /m [link] : Mirror to Google Drive / Cloud\n"
        "• /qbmirror or /qm [magnet] : Mirror torrent to Cloud\n"
        "• /jdmirror or /jm [link] : Mirror via JDownloader\n"
        "• /clone [drive_url] : Copy Google Drive files</blockquote>\n\n"
        f"<blockquote expandable><b>{vt_icon} VIDEO TOOLS</b> · remuxing, streams & rename\n"
        "• Audio Merge : Mux external audio tracks without re-encode\n"
        "• Subtitle Merge : Add external subtitles to video\n"
        "• AutoRename : Template-based file renaming\n"
        "• Track Ordering : Reorder default audio and subtitle tracks</blockquote>\n\n"
        f"<blockquote expandable><b>{pref_icon} USER PREFERENCES</b> · settings & custom thumb\n"
        "• /usetting or /bsetting : Open User Settings menu\n"
        "• /setthumb : Set custom video thumbnail\n"
        "• /delthumb : Remove custom thumbnail\n"
        "• /userbot : Configure Premium helper sessions</blockquote>"
    )


CATEGORY_HELP_DICT = {
    "leech": (
        "<b>📥 StarFallX — Leech Engine Guide</b>\n"
        "<i>Tap a drawer to inspect commands & flags.</i>\n\n"
        "<blockquote expandable><b>📥 CORE LEECH COMMANDS</b> · direct, torrent & stream\n"
        "╭ <code>/leech</code> or <code>/l</code> [link] : Direct link to Telegram\n"
        "├ <code>/qbleech</code> or <code>/ql</code> [magnet] : High-speed qBittorrent leech\n"
        "├ <code>/bleech</code> or <code>/bl</code> [links] : Batch leech with queue control\n"
        "├ <code>/bql</code> [magnet] : Big-Queue torrent batching (30GB+ packs)\n"
        "├ <code>/ytdl</code> or <code>/y</code> [url] : YouTube & 1000+ streaming sites\n"
        "╰ <code>/jdl</code> · <code>/nzbl</code> : JDownloader & Usenet NZB leech</blockquote>\n\n"
        "<blockquote expandable><b>⚡ POWER FLAGS</b> · per-task modifiers\n"
        "╭ <code>-vt</code> : Launch Video Studio after download\n"
        "├ <code>-s</code> : Generate a video sample before upload\n"
        "├ <code>-ar &lt;template&gt;</code> : Custom one-task AutoRename template\n"
        "├ <code>-m &lt;folder&gt;</code> : Merge & mux multi-part videos in order\n"
        "├ <code>-sp 2gb</code> : Override Telegram split threshold\n"
        "╰ <code>-t &lt;url/reply&gt;</code> : Attach custom task thumbnail</blockquote>"
    ),
    "mirror": (
        "<b>☁️ StarFallX — Cloud Mirror & Clone Guide</b>\n"
        "<i>Tap a drawer to inspect cloud commands & routing.</i>\n\n"
        "<blockquote expandable><b>☁️ MIRROR & CLONE</b> · Google Drive, Rclone & DDL\n"
        "╭ <code>/mirror</code> or <code>/m</code> [link] : Mirror direct link to Cloud\n"
        "├ <code>/qbmirror</code> or <code>/qm</code> [magnet] : Mirror torrent via qBittorrent\n"
        "├ <code>/jdmirror</code> or <code>/jm</code> [link] : Mirror via JDownloader\n"
        "├ <code>/clone</code> [drive_url] : High-speed server-side GDrive copy\n"
        "╰ <code>/count</code> [drive_url] : Calculate total folder & file sizes</blockquote>\n\n"
        "<blockquote expandable><b>🧭 DESTINATION ROUTING</b> · target overrides\n"
        "╭ <code>-up rc</code> : Upload via configured Rclone remote\n"
        "├ <code>-up gd</code> : Upload via Google Drive API\n"
        "├ <code>-up remote:path</code> : Route to specific Rclone directory\n"
        "╰ <code>-z [password]</code> : Archive output as encrypted ZIP</blockquote>"
    ),
    "videotools": (
        "<b>🎬 StarFallX — Video Studio & Remux Engine</b>\n"
        "<i>Lossless MKV stream copy · Zero video re-encoding.</i>\n\n"
        "<blockquote expandable><b>🎬 STUDIO CAPABILITIES</b> · interactive & web UI\n"
        "╭ <b>Audio Merge:</b> Mux external audio tracks without re-encode\n"
        "├ <b>Subtitle Merge:</b> Attach ASS/SRT + styled intro subtitles\n"
        "├ <b>Delay Sync:</b> Millisecond audio/subtitle offset + 2m preview\n"
        "├ <b>Track Ordering:</b> Reorder & set default audio/subtitle streams\n"
        "╰ <b>VPS Audio Bank:</b> Save & auto-pair audio across episode packs</blockquote>\n\n"
        "<blockquote expandable><b>🤖 AUTO-PROCESS PIPELINE</b> · single-pass automation\n"
        "╭ <b>Stage 1:</b> Download → Auto-Unzip → Reorder Tracks\n"
        "├ <b>Stage 2:</b> Strip Unwanted Streams → Smart Episode Merge\n"
        "╰ <b>Stage 3:</b> Intro Subtitle → Stream Metadata → AutoRename → Upload</blockquote>"
    ),
    "settings": (
        "<b>⚙️ StarFallX — User Preferences & Controls</b>\n"
        "<i>Personalize captions, posters, thumbnails & helper bots.</i>\n\n"
        "<blockquote expandable><b>⚙️ PREFERENCE COMMANDS</b> · user & admin menus\n"
        "╭ <code>/usetting</code> or <code>/bsetting</code> : Open User / Bot Settings\n"
        "├ <code>/setthumb</code> · <code>/delthumb</code> : Manage custom video thumbnail\n"
        "├ <code>/poster</code> [query] : Search & save 1280×720 HD poster art\n"
        "╰ <code>/userbot</code> : Configure PIN-protected helper bot tokens</blockquote>\n\n"
        "<blockquote expandable><b>🏷️ TEMPLATE VARIABLES</b> · AutoRename & captions\n"
        "╭ <b>Identity:</b> <code>{title}</code> · <code>{year}</code> · <code>{season}</code> · <code>{episode}</code>\n"
        "├ <b>Streams:</b> <code>{resolution}</code> · <code>{codec}</code> · <code>{languages}</code> · <code>{shortsub}</code>\n"
        "╰ <b>Metadata:</b> <code>{rating}</code> · <code>{genres}</code> · <code>{ott}</code> · <code>{quality}</code></blockquote>"
    ),
}


def get_full_telegraph_help_html():
    return (
        "<h2>✦ StarfallX v1.2 — Complete User Manual &amp; Commands Guide ✦</h2>"
        "<p>StarfallX is a high-performance Telegram &amp; Web automation platform featuring multi-threaded mirror/leech engines, lossless video tools, and smart queue management.</p>"
        "<hr/>"
        "<h3>1. 📥 Leech Commands (Telegram Upload)</h3>"
        "<ul>"
        "<li><b>/leech &lt;link/reply&gt;</b>: Download direct links and upload to Telegram.</li>"
        "<li><b>/qbleech &lt;magnet/torrent&gt;</b>: Download torrents using libtorrent v2.0 and upload to Telegram.</li>"
        "<li><b>/bleech &lt;link1 link2 ...&gt;</b>: Batch leech multiple links with BLEECH download/upload queue policies.</li>"
        "<li><b>/bql &lt;magnet&gt;</b>: Big queue torrent leeching for massive releases.</li>"
        "<li><b>/ytdlleech &lt;url&gt;</b>: Extract and leech video or audio from YouTube and 1000+ supported sites.</li>"
        "<li><b>/jdleech &lt;link&gt;</b>: Leech via JDownloader engine.</li>"
        "<li><b>/nzbleech &lt;nzb_link&gt;</b>: Leech Usenet releases via SABnzbd+.</li>"
        "</ul>"
        "<h3>2. ☁️ Mirror &amp; Clone Commands (Cloud Upload)</h3>"
        "<ul>"
        "<li><b>/mirror &lt;link/reply&gt;</b>: Mirror direct links to configured Google Drive or Rclone storage.</li>"
        "<li><b>/qbmirror &lt;magnet&gt;</b>: Mirror torrents to cloud storage.</li>"
        "<li><b>/ytdl &lt;url&gt;</b>: Mirror YouTube videos/playlists directly to cloud.</li>"
        "<li><b>/clone &lt;drive_url&gt;</b>: Copy files or entire folders across Google Drives or Rclone remotes.</li>"
        "<li><b>/count &lt;drive_url&gt;</b>: Count and calculate total file and folder sizes.</li>"
        "<li><b>/delete &lt;drive_url&gt;</b>: Remove files/folders from Google Drive (Owner &amp; Sudo).</li>"
        "</ul>"
        "<h3>3. 🎬 Video Tools &amp; Web Studio Engine</h3>"
        "<ul>"
        "<li><b>-vt Flag</b>: Add <code>-vt</code> to any leech command to launch Video Tools after download.</li>"
        "<li><b>Web Video Studio</b>: Real-time dual player preview running on port 8080.</li>"
        "<li><b>Pure Lossless Mux</b>: Add/remove audio and subtitle tracks using stream copy with zero video re-encoding.</li>"
        "<li><b>Audio Delay Alignment</b>: Apply millisecond-level delay corrections with instant 2-minute preview clips.</li>"
        "<li><b>VPS Audio Bank</b>: Save extracted audio tracks into the VPS bank and batch pair them across entire seasons.</li>"
        "<li><b>Auto Process</b>: Single-pass pipeline: Download &rarr; Unzip &rarr; Order &rarr; Strip Streams &rarr; Mux &rarr; Intro &rarr; Metadata &rarr; Autorename &rarr; Sequential Upload.</li>"
        "</ul>"
        "<h3>4. ⚙️ Command-Line Arguments Reference</h3>"
        "<ul>"
        "<li><b>-n &lt;new_name&gt;</b>: Rename the downloaded file before upload.</li>"
        "<li><b>-m &lt;folder_name&gt;</b>: Place multiple links into one folder or merge video parts in natural order.</li>"
        "<li><b>-sp &lt;split_size&gt;</b>: Set custom split size (e.g. <code>-sp 2gb</code> or <code>-sp 4gb</code>).</li>"
        "<li><b>-t &lt;url/reply&gt;</b>: Attach a custom thumbnail for this task.</li>"
        "<li><b>-up &lt;dest&gt;</b>: Route upload to <code>rc</code> (Rclone), <code>gd</code> (GDrive), or Telegram chat ID.</li>"
        "<li><b>-ar &lt;template&gt;</b>: Apply a one-task custom auto-rename template.</li>"
        "<li><b>-z [password]</b>: Compress output into a password-protected zip archive.</li>"
        "</ul>"
        "<h3>5. 📊 Status &amp; Queue Management</h3>"
        "<ul>"
        "<li><b>/status</b>: Display live active tasks, speeds, progress bars, and ETA.</li>"
        "<li><b>/stats</b>: View VPS RAM, CPU load, network I/O, package versions, and task limits.</li>"
        "<li><b>/cancel &lt;gid&gt;</b>: Cancel an active or queued task.</li>"
        "<li><b>/forcestart &lt;gid&gt;</b>: Force an item from the queue to start immediately.</li>"
        "<li><b>/select</b>: Interactively pick specific files to download from torrents or NZB archives.</li>"
        "</ul>"
        "<h3>6. 🎨 Customization &amp; Templates</h3>"
        "<ul>"
        "<li><b>/usetting</b>: Configure custom prefix, suffix, poster engine, caption templates, and user tokens.</li>"
        "<li><b>Caption Variables</b>: <code>{filename}</code>, <code>{size}</code>, <code>{duration}</code>, <code>{resolution}</code>, <code>{languages}</code>, <code>{year}</code>, <code>{genres}</code>, <code>{rating}</code>, <code>{ott}</code>.</li>"
        "</ul>"
        "<hr/>"
        "<p><i>StarfallX v1.2 — Built for 2-core 4GB RAM VPS environments with memory trimming and adaptive queue management.</i></p>"
    )


help_string = get_help_string()
