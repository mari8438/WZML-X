from pyrogram.enums import ButtonStyle
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .. import LOGGER
from ..core.config_manager import Config
from ..helper.video_utils.video_tools import (
    UI_TIMEOUT,
    finish_merge_track_intake,
    format_lang_tag,
    generate_auto_mux_name,
    generate_merge_preview,
    get_vt_event,
    get_vt_session,
    get_vt_state,
    start_merge_track_intake,
)
from ..helper.video_utils.vps_audio_bank import (
    list_audio_bank,
    clear_audio_bank,
    match_videos_with_bank,
    resolve_full_language_name,
)


def _selected_icon(selected):
    return "🟢 " if selected else "⚪ "


def _state_style(selected):
    return _btn_style(ButtonStyle.SUCCESS) if selected else _btn_style()


def _btn_style(style=None):
    if not Config.COLORED_BTNS:
        return ButtonStyle.DEFAULT
    return style or ButtonStyle.PRIMARY


def _danger_label(label):
    return f"\U0001F534 {label}"


def _has_extract_selection(state):
    return bool(state.get("extract_audio") or state.get("extract_sub"))


def _clear_non_extract_tools(state):
    for key in (
        "remove_audio",
        "remove_sub",
        "merge_audio",
        "merge_sub",
        "translate_sub",
        "keep_audio",
        "keep_sub",
        "audio_order",
        "sub_order",
    ):
        state[key] = []
    state["audio_order_value"] = ""
    state["sub_order_value"] = ""
    state["default_audio"] = None
    state["default_sub"] = None
    state["intro_subtitle"] = False
    state["video_merge"] = False


def _tracks_for(state, action_key):
    if action_key == "merge_audio":
        return state.get("external_audio", [])
    if action_key == "merge_sub":
        return state.get("external_sub", [])
    if "audio" in action_key:
        return state.get("audio_tracks", [])
    return state.get("sub_tracks", [])


def _track_text(track):
    if "name" in track:
        return track["name"]
    return f"Track {track['index'] + 1} - {str(track.get('lang', 'unk')).upper()} ({track.get('codec', '')})"


LANGUAGES = [
    ("🇮🇳 Hindi", "Hindi"),
    ("🇮🇳 Tamil", "Tamil"),
    ("🇮🇳 Telugu", "Telugu"),
    ("🇬🇧 English", "English"),
    ("🇮🇳 Malayalam", "Malayalam"),
    ("🇮🇳 Kannada", "Kannada"),
    ("🇮🇳 Bengali", "Bengali"),
    ("🇮🇳 Marathi", "Marathi"),
    ("🇯🇵 Japanese", "Japanese"),
    ("🇰🇷 Korean", "Korean"),
]


async def render_extract_studio(message, state):
    from ..helper.ext_utils.status_utils import get_readable_file_size

    task_id = state["task_id"]
    filename = state.get("filename", "Video.mkv")
    is_folder = state.get("is_folder", False)
    extract_type = state.get("extract_type", "audio")
    save_to_vps = state.get("save_to_vps", True)
    upload_to_tg = state.get("upload_to_tg", True)

    user_id = state.get("user_id") or (message.from_user.id if getattr(message, "from_user", None) else 0)
    bank_info = await list_audio_bank(user_id)
    bank_files_count = bank_info.get("total_files", 0)
    bank_size_str = get_readable_file_size(bank_info.get("total_size", 0))

    tracks = state.get("audio_tracks", []) if extract_type == "audio" else state.get("sub_tracks", [])
    selected_indices = set(state.get("extract_audio", []) if extract_type == "audio" else state.get("extract_sub", []))

    header_lines = [
        "<b>✦ 𝗘𝗫𝗧𝗥𝗔𝗖𝗧 𝗦𝗧𝗥𝗘𝗔𝗠 𝗦𝗧𝗨𝗗𝗜𝗢 ✦</b>",
        f"📁 <b>Source:</b> <code>{filename}</code>" + (f" <i>(Folder: {state.get('total_episodes', len(state.get('folder_videos', [])))} Videos)</i>" if is_folder else ""),
        "🏷️ <b>Naming:</b> <code>{Language} - {Filename}.{ext}</code>",
        f"💾 <b>VPS Audio Bank:</b> <code>{bank_files_count} file(s) ({bank_size_str})</code>",
        "│",
        f"└ <b>Current View:</b> <b>{'🔊 AUDIO STREAMS' if extract_type == 'audio' else '📝 SUBTITLE STREAMS'}</b>",
    ]

    rows = []
    # Track buttons
    if not tracks:
        header_lines.append("<i>No streams found for this type.</i>")
    else:
        for t in tracks:
            idx = t["index"]
            is_sel = idx in selected_indices
            icon = "🟢 " if is_sel else "⚪ "
            raw_lang = t.get("lang") or ""
            raw_title = t.get("title") or ""
            full_lang, _ = resolve_full_language_name(raw_lang, raw_title)
            codec = t.get("codec") or "Stream"
            label = f"{icon}{full_lang} ({codec})"
            rows.append([
                InlineKeyboardButton(label, callback_data=f"vt_ext_toggle_{idx}_{task_id}", style=_state_style(is_sel))
            ])

    # Presets row
    rows.append([
        InlineKeyboardButton("✅ Select All", callback_data=f"vt_ext_preset_all_{task_id}"),
        InlineKeyboardButton("🇮🇳 Ind Dubs", callback_data=f"vt_ext_preset_indian_{task_id}"),
        InlineKeyboardButton("🔄 Reset", callback_data=f"vt_ext_preset_reset_{task_id}"),
    ])

    # Destination toggles row
    rows.append([
        InlineKeyboardButton(
            f"💾 Save VPS : {'ON' if save_to_vps else 'OFF'}",
            callback_data=f"vt_ext_togglevps_{task_id}",
            style=_state_style(save_to_vps),
        ),
        InlineKeyboardButton(
            f"📤 Upload TG : {'ON' if upload_to_tg else 'OFF'}",
            callback_data=f"vt_ext_toggletg_{task_id}",
            style=_state_style(upload_to_tg),
        ),
    ])

    # Switch type and clear bank row
    switch_label = "📝 Switch to Subtitles" if extract_type == "audio" else "🔊 Switch to Audio"
    bottom_row = [
        InlineKeyboardButton(switch_label, callback_data=f"vt_ext_switch_{task_id}")
    ]
    if bank_files_count > 0:
        bottom_row.append(
            InlineKeyboardButton(f"🧹 Clear VPS Bank ({bank_files_count})", callback_data=f"vt_ext_clearbank_{task_id}", style=_btn_style(ButtonStyle.DANGER))
        )
    rows.append(bottom_row)

    # Action row
    sel_count = len(selected_indices)
    rows.append([
        InlineKeyboardButton("🔙 Back", callback_data=f"vt_main_{task_id}"),
        InlineKeyboardButton(
            f"⚡ Start Extract ({sel_count})" if sel_count else "⚡ Start Extract",
            callback_data=f"vt_ext_start_{task_id}",
            style=_btn_style(ButtonStyle.SUCCESS),
        ),
    ])

    markup = InlineKeyboardMarkup(rows)
    try:
        await message.edit_text("\n".join(header_lines), reply_markup=markup)
    except Exception as e:
        if "MESSAGE_NOT_MODIFIED" not in str(e).upper():
            LOGGER.warning(f"render_extract_studio edit failed: {e}")

async def render_muxing_configuration(vt_msg, state):
    from ..helper.ext_utils.status_utils import get_readable_file_size

    task_id = state["task_id"]
    filename = state.get("filename", "Video.mkv")
    is_folder = state.get("is_folder", False)
    file_size_bytes = state.get("file_size")
    size_str = get_readable_file_size(file_size_bytes) if file_size_bytes else "N/A"
    task_mode = state.get("task_mode", "#Leech")

    user_id = state.get("user_id") or (vt_msg.chat.id if getattr(vt_msg, "chat", None) else 0)
    bank_info = await list_audio_bank(user_id)
    bank_files_count = bank_info.get("total_files", 0)

    # Auto-match video(s) with VPS audio bank
    videos = state.get("folder_videos") or ([state["input_path"]] if state.get("input_path") else [])
    if bank_files_count > 0 and not state.get("matched_bank") and videos:
        sel_langs = state.get("selected_bank_languages")
        if not sel_langs and bank_info.get("languages"):
            state["selected_bank_languages"] = list(bank_info["languages"].keys())
            sel_langs = state["selected_bank_languages"]
        state["matched_bank"] = match_videos_with_bank(videos, bank_info["files"], sel_langs)

    external_audio = state.get("external_audio", [])
    external_sub = state.get("external_sub", [])

    audio_delay_ms = int(state.get("audio_delay_ms") or 0)
    delay_summary = f" ({audio_delay_ms:+d}ms)" if audio_delay_ms != 0 else ""

    auto_sync = f"ON{delay_summary}" if state.get("auto_sync", True) else f"OFF{delay_summary}"
    final_sample = "ON (1 Sample Only)" if state.get("final_sample", True) else "OFF"
    preview_sync = "ON" if state.get("preview_sync", True) else "OFF"
    save_settings = "ON" if state.get("save_settings", True) else "OFF"

    source_audio_count = len(state.get("audio_tracks", []))
    if state.get("remove_original_audio"):
        source_audio_str = "0 stream(s) (Removed)"
    else:
        source_audio_str = f"{source_audio_count} stream(s)"

    output_name = state.get("output_name")
    if not output_name:
        output_name = generate_auto_mux_name(
            filename,
            external_audio,
            state.get("remove_original_audio", False),
            state.get("audio_tracks", []),
        )

    matched_bank = state.get("matched_bank")
    bank_summary_str = f"{bank_files_count} file(s) on VPS" if bank_files_count else "None"
    matched_summary_str = f"{matched_bank.get('matched_episodes', 0)}/{matched_bank.get('total_episodes', 0)} episodes paired" if matched_bank else ""

    video_dur = float(state.get("video_duration") or 0)
    if not video_dur and state.get("input_path"):
        try:
            from ..helper.ext_utils.media_utils import get_media_info
            v_info = await get_media_info(state["input_path"])
            video_dur = float(v_info[0] or 0)
            state["video_duration"] = video_dur
        except Exception:
            pass

    audio_dur = 0.0
    if external_audio:
        first_a = external_audio[0]
        audio_dur = float(first_a.get("duration") or 0)
        if not audio_dur and first_a.get("path"):
            try:
                from ..helper.ext_utils.media_utils import get_media_info
                a_info = await get_media_info(first_a["path"])
                audio_dur = float(a_info[0] or 0)
                first_a["duration"] = audio_dur
            except Exception:
                pass
    elif matched_bank and matched_bank.get("matched"):
        first_m = matched_bank["matched"][0]
        if first_m.get("audio_tracks"):
            trk_path = first_m["audio_tracks"][0].get("audio_path")
            if trk_path:
                try:
                    from ..helper.ext_utils.media_utils import get_media_info
                    a_info = await get_media_info(trk_path)
                    audio_dur = float(a_info[0] or 0)
                except Exception:
                    pass

    length_diff_ms = None
    if video_dur > 0 and audio_dur > 0:
        length_diff_ms = int(round((audio_dur - video_dur) * 1000))
        state["video_duration_ms"] = int(round(video_dur * 1000))
        state["audio_duration_ms"] = int(round(audio_dur * 1000))
        state["length_diff_ms"] = length_diff_ms
        try:
            from ..helper.video_utils.vps_audio_bank import load_web_session, save_web_session
            sess = await load_web_session(task_id) or {}
            sess["video_duration_ms"] = state["video_duration_ms"]
            sess["audio_duration_ms"] = state["audio_duration_ms"]
            sess["length_diff_ms"] = length_diff_ms
            await save_web_session(task_id, sess)
        except Exception:
            pass

    lines = [
        "<b>⬡ Muxing Configuration :</b>",
        f"├ Base Video → <code>{filename}</code>" + (f" <i>(Season Pack: {len(state.get('folder_videos', []))} Episodes)</i>" if is_folder else ""),
        f"├ File Size → <code>{size_str}</code>",
        f"├ Task Mode → <code>{task_mode}</code>",
        f"├ VPS Audio Bank → <code>{bank_summary_str}</code>" + (f" <i>({matched_summary_str})</i>" if matched_summary_str else ""),
        f"├ Audio Delay → <b>{audio_delay_ms:+d} ms</b>",
    ]
    if length_diff_ms is not None:
        v_min, v_sec = divmod(int(video_dur), 60)
        a_min, a_sec = divmod(int(audio_dur), 60)
        diff_str = f"{length_diff_ms:+d} ms"
        lines.append(f"├ Length Diff → <b>{diff_str}</b> <i>(Audio: {a_min}m {a_sec:02d}s vs Video: {v_min}m {v_sec:02d}s)</i>")

    lines.extend([
        f"├ Auto Sync → <b>{auto_sync}</b>",
        f"├ Final Sample → <b>{final_sample}</b>",
        f"├ Source Audio → <code>{source_audio_str}</code>",
        f"├ Preview Sync → <b>{preview_sync}</b>",
        f"├ Save Settings → <b>{save_settings}</b>",
        f"├ Output Name → <code>{output_name}</code>",
        "│",
    ])

    total_ext = len(external_audio) + len(external_sub)
    downloading_status = state.get("downloading_status")

    if total_ext == 0 and not downloading_status:
        lines.append("└ <i>No external tracks added yet. Send files to chat.</i>")
    else:
        curr = 0
        for i, item in enumerate(external_audio, start=1):
            curr += 1
            is_last = (curr == total_ext and not downloading_status)
            prefix = "└" if is_last else "├"
            lang = (item.get("language") or "und").capitalize()
            codec = item.get("codec") or "AUDIO"
            channels = item.get("channels") or "2.0"
            tag = item.get("tag") or format_lang_tag(lang)
            trk_idx = item.get("stream_index", 0)
            size_mb = item.get("size_mb", 0.0)
            size_display = f"{size_mb:.2f}MB" if size_mb else "N/A"
            delay = item.get("delay_ms", 0)
            sync_info = f" | Sync: {delay:+d}ms"
            if length_diff_ms is not None:
                sync_info += f" | Δ: {length_diff_ms:+d}ms"
            lines.append(
                f"{prefix} Audio {i} → {lang} ({codec} {channels}) [{tag}] [Trk {trk_idx}] - {size_display}{sync_info}"
            )

        for j, s_item in enumerate(external_sub, start=1):
            curr += 1
            is_last = (curr == total_ext and not downloading_status)
            prefix = "└" if is_last else "├"
            s_name = s_item.get("name", f"Sub {j}")
            lines.append(f"{prefix} Subtitle {j} → {s_name}")

        if downloading_status:
            lines.append(f"└ ⏳ <i>{downloading_status}</i>")

    rows = []
    # 1. Prominent Web Studio URL Button
    base_url = Config.BASE_URL.rstrip('/') if Config.BASE_URL and "trycloudflare.com" not in Config.BASE_URL else f"http://172.105.52.160:{Config.BASE_URL_PORT or 8080}"
    web_url = state.get("web_studio_url") or f"{base_url}/app/video-studio?task_id={task_id}&token={state.get('token', '')}"
    if web_url:
        rows.append([
            InlineKeyboardButton("🌐 Open Web Merge Studio (Sync & Confirm)", url=web_url)
        ])

    # 2. Intake Audio & Subtitles
    rows.append([
        InlineKeyboardButton("➕ Add Audio", callback_data=f"vt_mux_addaudio_{task_id}"),
        InlineKeyboardButton("➕ Add Subtitle", callback_data=f"vt_mux_addsub_{task_id}"),
    ])

    # 3. VPS Audio Bank Integration
    bank_btn_label = f"📥 Use VPS Audio ({bank_files_count})" if bank_files_count else "📥 VPS Audio Bank (Empty)"
    rows.append([
        InlineKeyboardButton(bank_btn_label, callback_data=f"vt_mux_usebank_{task_id}"),
        InlineKeyboardButton(f"⏱️ Delay ({audio_delay_ms:+d}ms)", callback_data=f"vt_mux_setdelay_{task_id}"),
    ])

    # 4. Audio Language & Remove
    rows.append([
        InlineKeyboardButton("🌐 Audio Lang", callback_data=f"vt_mux_audiolang_{task_id}"),
        InlineKeyboardButton("🗑️ Remove Audio", callback_data=f"vt_mux_removeaudio_{task_id}"),
    ])

    # 5. Rename & Delay Preview
    rows.append([
        InlineKeyboardButton("✏️ Rename", callback_data=f"vt_mux_rename_{task_id}"),
        InlineKeyboardButton("🎬 Delay Preview", callback_data=f"vt_mux_preview_{task_id}"),
    ])

    # 6. Auto Sync & Final Sample (1 Sample Only)
    rows.append([
        InlineKeyboardButton(
            f"Auto Sync : {'ON' if state.get('auto_sync', True) else 'OFF'}",
            callback_data=f"vt_mux_toggle_auto_sync_{task_id}",
            style=_state_style(state.get("auto_sync", True)),
        ),
        InlineKeyboardButton(
            f"1 Sample Only : {'ON' if state.get('final_sample', True) else 'OFF'}",
            callback_data=f"vt_mux_toggle_final_sample_{task_id}",
            style=_state_style(state.get("final_sample", True)),
        ),
    ])

    # 7. Dedicated Merge VPS Audio Bank Button (Primary Action when Bank Audio exists)
    if bank_files_count > 0:
        matched_cnt = matched_bank.get("matched_episodes", 0) if matched_bank else 0
        tot_cnt = matched_bank.get("total_episodes", 0) if matched_bank else 0
        if matched_cnt > 0:
            merge_label = f"⚡ Merge VPS Audio into Video ({matched_cnt}/{tot_cnt} Paired)" if tot_cnt > 1 else f"⚡ Merge VPS Audio into Video (1 Paired)"
        else:
            merge_label = f"⚡ Merge VPS Audio Bank into Video ({bank_files_count} Files)"
        rows.append([
            InlineKeyboardButton(
                merge_label,
                callback_data=f"vt_mux_mergebank_{task_id}",
                style=_btn_style(ButtonStyle.SUCCESS),
            )
        ])

    # 8. Clear VPS Audio Bank button (if files exist)
    if bank_files_count > 0:
        rows.append([
            InlineKeyboardButton(f"🧹 Clear Extracted Files ({bank_files_count})", callback_data=f"vt_mux_clearbank_{task_id}", style=_btn_style(ButtonStyle.DANGER))
        ])

    # 9. Cancel & Start Mux
    rows.append([
        InlineKeyboardButton(
            "❌ Cancel", callback_data=f"vt_mux_cancel_{task_id}", style=_btn_style(ButtonStyle.DANGER)
        ),
        InlineKeyboardButton(
            "🚀 Start Mux", callback_data=f"vt_mux_start_{task_id}", style=_btn_style(ButtonStyle.SUCCESS)
        ),
    ])

    markup = InlineKeyboardMarkup(rows)
    try:
        await vt_msg.edit_text("\n".join(lines), reply_markup=markup)
    except Exception as e:
        if "MESSAGE_NOT_MODIFIED" not in str(e).upper():
            LOGGER.warning(f"render_muxing_configuration edit failed: {e}")


render_merge_intake = render_muxing_configuration


async def render_vps_bank_picker(message, state, bank_info):
    task_id = state["task_id"]
    langs = bank_info.get("languages", {})
    sel_langs = set(state.get("selected_bank_languages", []))
    matched_bank = state.get("matched_bank")
    matched_ep = matched_bank.get("matched_episodes", 0) if matched_bank else 0
    total_ep = matched_bank.get("total_episodes", 0) if matched_bank else 0
    matched_str = f" ({matched_ep}/{total_ep} paired)" if total_ep > 0 else ""

    text = (
        "<b>📦 VPS Extracted Audio Bank</b>\n\n"
        f"Select audio languages to merge into target video(s){matched_str}:\n"
    )
    rows = []
    for lang, files in langs.items():
        is_sel = lang in sel_langs
        icon = "🟢 " if is_sel else "⚪ "
        rows.append([
            InlineKeyboardButton(
                f"{icon}{lang} ({len(files)} files)",
                callback_data=f"vt_mux_togglebanklang_{lang}_{task_id}",
                style=_state_style(is_sel),
            )
        ])

    # Direct 1-click merge button from within the bank picker
    if sel_langs or matched_ep > 0:
        merge_btn_txt = f"⚡ Confirm & Merge Audio into Video ({matched_ep if matched_ep else len(sel_langs)} Paired)"
        rows.append([
            InlineKeyboardButton(
                merge_btn_txt,
                callback_data=f"vt_mux_mergebank_{task_id}",
                style=_btn_style(ButtonStyle.SUCCESS),
            )
        ])

    rows.append([
        InlineKeyboardButton("🔙 Back to Mux Config", callback_data=f"vt_mux_back_{task_id}"),
        InlineKeyboardButton("🧹 Clear VPS Bank", callback_data=f"vt_mux_clearbank_{task_id}", style=_btn_style(ButtonStyle.DANGER)),
    ])
    try:
        await message.edit_text(text, reply_markup=InlineKeyboardMarkup(rows))
    except Exception as e:
        if "MESSAGE_NOT_MODIFIED" not in str(e).upper():
            LOGGER.warning(f"render_vps_bank_picker error: {e}")


async def render_audio_delay_menu(message, state):
    task_id = state["task_id"]
    curr_ms = int(state.get("audio_delay_ms") or 0)
    text = (
        "<b>⏱️ Set Audio Delay Offset:</b>\n\n"
        f"<b>Current Delay:</b> <code>{curr_ms:+d} ms</code>\n\n"
        "Choose a preset below or type a custom millisecond value:"
    )
    presets = [-250, -150, -100, 0, 100, 150, 250]
    rows = []
    r1 = [InlineKeyboardButton(f"{ms:+d}ms" if ms != 0 else "0ms", callback_data=f"vt_mux_applydelay_{ms}_{task_id}") for ms in presets[:4]]
    r2 = [InlineKeyboardButton(f"{ms:+d}ms", callback_data=f"vt_mux_applydelay_{ms}_{task_id}") for ms in presets[4:]]
    rows.append(r1)
    rows.append(r2)
    rows.append([
        InlineKeyboardButton("✏️ Custom Delay (ms)", callback_data=f"vt_mux_customdelay_{task_id}"),
        InlineKeyboardButton("🔙 Back", callback_data=f"vt_mux_back_{task_id}"),
    ])
    try:
        await message.edit_text(text, reply_markup=InlineKeyboardMarkup(rows))
    except Exception as e:
        if "MESSAGE_NOT_MODIFIED" not in str(e).upper():
            LOGGER.warning(f"render_audio_delay_menu error: {e}")

async def render_audio_lang_menu(message, state):
    task_id = state["task_id"]
    ext_audio = state.get("external_audio", [])
    if not ext_audio:
        await render_muxing_configuration(message, state)
        return
    if len(ext_audio) == 1:
        await render_audio_lang_picker(message, state, 0)
        return

    text = "<b>🌐 Select Audio Track to Change Language:</b>\n\nChoose which track to configure:"
    rows = []
    for item in ext_audio:
        idx = item["index"]
        lang = (item.get("language") or "und").capitalize()
        codec = item.get("codec") or "AUDIO"
        rows.append(
            [
                InlineKeyboardButton(
                    f"Audio {idx + 1}: {lang} ({codec})",
                    callback_data=f"vt_mux_picktrack_{idx}_{task_id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton("🔙 Back to Muxing Config", callback_data=f"vt_mux_back_{task_id}")])
    try:
        await message.edit_text(text, reply_markup=InlineKeyboardMarkup(rows))
    except Exception as e:
        if "MESSAGE_NOT_MODIFIED" not in str(e).upper():
            LOGGER.warning(f"render_audio_lang_menu edit failed: {e}")


async def render_audio_lang_picker(message, state, track_index):
    task_id = state["task_id"]
    ext_audio = state.get("external_audio", [])
    item = next((a for a in ext_audio if a["index"] == track_index), None)
    if not item:
        await render_muxing_configuration(message, state)
        return

    curr_lang = (item.get("language") or "und").capitalize()
    text = (
        f"<b>🌐 Select Language for Audio {track_index + 1}:</b>\n\n"
        f"<b>Track:</b> <code>{item.get('name', 'Audio')}</code>\n"
        f"<b>Current Language:</b> <b>{curr_lang}</b>\n\n"
        "Choose a language below or type custom:"
    )

    rows = []
    for i in range(0, len(LANGUAGES), 2):
        row = []
        for label, val in LANGUAGES[i : i + 2]:
            row.append(
                InlineKeyboardButton(
                    label,
                    callback_data=f"vt_mux_setlang_{track_index}_{val}_{task_id}",
                )
            )
        rows.append(row)

    rows.append(
        [
            InlineKeyboardButton(
                "✏️ Custom Language",
                callback_data=f"vt_mux_customlang_{track_index}_{task_id}",
            )
        ]
    )
    rows.append([InlineKeyboardButton("🔙 Back to Muxing Config", callback_data=f"vt_mux_back_{task_id}")])
    try:
        await message.edit_text(text, reply_markup=InlineKeyboardMarkup(rows))
    except Exception as e:
        if "MESSAGE_NOT_MODIFIED" not in str(e).upper():
            LOGGER.warning(f"render_audio_lang_picker edit failed: {e}")


async def render_remove_audio_menu(message, state):
    task_id = state["task_id"]
    remove_orig = state.get("remove_original_audio", False)
    ext_audio = state.get("external_audio", [])

    text = (
        "<b>🗑️ Remove Audio Options:</b>\n\n"
        "• <b>Source Video Audio:</b> Strip or keep original audio tracks from the base video.\n"
        "• <b>External Audio:</b> Remove individual added external dubs.\n"
    )

    rows = [
        [
            InlineKeyboardButton(
                "🟢 Source Audio: KEEP" if not remove_orig else "🔴 Source Audio: REMOVE",
                callback_data=f"vt_mux_toggle_remove_orig_{task_id}",
                style=_btn_style(ButtonStyle.DANGER if remove_orig else ButtonStyle.SUCCESS),
            )
        ]
    ]

    for item in ext_audio:
        idx = item["index"]
        lang = (item.get("language") or "und").capitalize()
        codec = item.get("codec") or "AUDIO"
        rows.append(
            [
                InlineKeyboardButton(
                    f"❌ Delete Audio {idx + 1} ({lang} - {codec})",
                    callback_data=f"vt_mux_delextaudio_{idx}_{task_id}",
                    style=_btn_style(ButtonStyle.DANGER),
                )
            ]
        )

    rows.append([InlineKeyboardButton("🔙 Back to Muxing Config", callback_data=f"vt_mux_back_{task_id}")])
    try:
        await message.edit_text(text, reply_markup=InlineKeyboardMarkup(rows))
    except Exception as e:
        if "MESSAGE_NOT_MODIFIED" not in str(e).upper():
            LOGGER.warning(f"render_remove_audio_menu edit failed: {e}")


async def render_rename_menu(message, state):
    task_id = state["task_id"]
    output_name = state.get("output_name")
    if not output_name:
        output_name = generate_auto_mux_name(
            state.get("filename") or "",
            state.get("external_audio", []),
            state.get("remove_original_audio", False),
            state.get("audio_tracks", []),
        )

    text = (
        "<b>✏️ Rename Output File:</b>\n\n"
        f"<b>Current Output Name:</b>\n<code>{output_name}</code>\n\n"
        "Choose an option below:"
    )

    rows = [
        [
            InlineKeyboardButton(
                "🪄 Auto-Generate Name", callback_data=f"vt_mux_autorename_{task_id}"
            ),
        ],
        [
            InlineKeyboardButton(
                "✍️ Type Custom Name", callback_data=f"vt_mux_typecustomname_{task_id}"
            ),
        ],
        [
            InlineKeyboardButton("🔙 Back to Muxing Config", callback_data=f"vt_mux_back_{task_id}"),
        ],
    ]
    try:
        await message.edit_text(text, reply_markup=InlineKeyboardMarkup(rows))
    except Exception as e:
        if "MESSAGE_NOT_MODIFIED" not in str(e).upper():
            LOGGER.warning(f"render_rename_menu edit failed: {e}")


async def render_merge_audio_config(query, state, index):
    await render_audio_lang_picker(query.message, state, index)


async def render_merge_audio_config_message(message, state, index):
    await render_muxing_configuration(message, state)


async def render_video_tools_main(vt_msg, state):
    extract_selected = _has_extract_selection(state)
    audio_count = len(state.get("audio_tracks", []))
    sub_count = len(state.get("sub_tracks", []))
    ext_audio = len(state.get("external_audio", []))
    ext_sub = len(state.get("external_sub", []))
    audio_info = f"{audio_count}" + (f" (+{ext_audio} ext)" if ext_audio else "")
    sub_info = f"{sub_count}" + (f" (+{ext_sub} ext)" if ext_sub else "")
    timeout_str = f"{UI_TIMEOUT // 60}m" if UI_TIMEOUT % 60 == 0 else f"{UI_TIMEOUT}s"

    text = (
        "<b>✦ 𝗦𝗧𝗔𝗥𝗙𝗔𝗟𝗟𝗫 • 𝗠𝗞𝗩 𝗦𝗧𝗨𝗗𝗜𝗢 ✦</b>\n\n"
        f"🎬 <code>{state['filename']}</code>\n"
        "╭ <b>Engine</b>    : ⚡ <i>mkvmerge (Zero-Reencode)</i>\n"
        f"├ <b>Audio</b>     : <code>{audio_info}</code>\n"
        f"├ <b>Subtitles</b> : <code>{sub_info}</code>\n"
        f"╰ <b>Timeout</b>   : <code>{timeout_str}</code>"
    )
    task_id = state["task_id"]
    if extract_selected:
        text += "\n\n<b>Extract mode:</b> enabled. Other Video Tools actions are disabled."
        rows = [
            [InlineKeyboardButton(_selected_icon(True) + "Extract Stream", callback_data=f"vt_extract_{task_id}", style=_btn_style(ButtonStyle.SUCCESS))],
            [
                InlineKeyboardButton("\U0001F7E2 Done", callback_data=f"vt_done_{task_id}", style=_btn_style(ButtonStyle.SUCCESS)),
                InlineKeyboardButton(_danger_label("Close"), callback_data=f"vt_close_{task_id}", style=_btn_style(ButtonStyle.DANGER)),
            ],
        ]
    else:
        rows = [
            [
                InlineKeyboardButton(_danger_label("Remove Stream"), callback_data=f"vt_remove_{task_id}", style=_btn_style(ButtonStyle.DANGER)),
                InlineKeyboardButton("Extract Stream", callback_data=f"vt_extract_{task_id}"),
            ],
            [InlineKeyboardButton("Change Order", callback_data=f"vt_order_{task_id}")],
            [
                InlineKeyboardButton("Merge Tracks", callback_data=f"vt_merge_{task_id}"),
                InlineKeyboardButton("Translate Subs", callback_data=f"vt_translate_{task_id}"),
            ],
            [InlineKeyboardButton(_selected_icon(state.get("video_merge")) + "Video + Video", callback_data=f"vt_video_{task_id}", style=_state_style(state.get("video_merge")))],
            [
                InlineKeyboardButton("\U0001F7E2 Done", callback_data=f"vt_done_{task_id}", style=_btn_style(ButtonStyle.SUCCESS)),
                InlineKeyboardButton(_danger_label("Close"), callback_data=f"vt_close_{task_id}", style=_btn_style(ButtonStyle.DANGER)),
            ],
        ]
    markup = InlineKeyboardMarkup(rows)
    try:
        await vt_msg.edit(text, reply_markup=markup)
    except Exception as e:
        LOGGER.error(f"render_video_tools_main error: {e}")


async def render_change_order(query, state):
    task_id = state["task_id"]
    await query.message.edit_text(
        "<b>Change Order</b>\n\nChoose the stream type to reorder.",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Audio Order", callback_data=f"vt_orderaudio_{task_id}"),
                    InlineKeyboardButton("Subtitle Order", callback_data=f"vt_ordersub_{task_id}"),
                ],
                [InlineKeyboardButton("Back", callback_data=f"vt_main_{task_id}")],
            ]
        ),
    )


async def render_stream_type_menu(query, task_id, mode, title):
    await query.message.edit_text(
        f"<b>{title}</b>\n\nChoose which stream type to configure:",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Audio", callback_data=f"vt_list_{mode}_audio_{task_id}"),
                    InlineKeyboardButton("Subtitle", callback_data=f"vt_list_{mode}_sub_{task_id}"),
                ],
                [InlineKeyboardButton("Back", callback_data=f"vt_main_{task_id}")],
            ]
        ),
    )


async def render_stream_list(query, state, action_key, title):
    task_id = state["task_id"]
    tracks = _tracks_for(state, action_key)
    if action_key in ("default_audio", "default_sub"):
        remove_key = "remove_audio" if "audio" in action_key else "remove_sub"
        tracks = [track for track in tracks if track["index"] not in state.get(remove_key, [])]

    if not tracks:
        await query.message.edit_text(
            "<b>No tracks found</b> for this type.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Back", callback_data=f"vt_main_{task_id}")]]
            ),
        )
        return

    markup = []
    selected = state.get(action_key, None)
    is_order = action_key in ("audio_order", "sub_order")
    for track in tracks:
        idx = track["index"]
        selected_row = False
        if action_key in ("default_audio", "default_sub"):
            selected_row = selected == idx
            icon = _selected_icon(selected_row)
        elif is_order:
            order = state.get(action_key, [])
            selected_row = idx in order
            icon = f"{order.index(idx) + 1}. {_selected_icon(True)}" if selected_row else ""
        else:
            selected_row = idx in state.get(action_key, [])
            icon = _selected_icon(selected_row)
        markup.append(
            [
                InlineKeyboardButton(
                    icon + _track_text(track),
                    callback_data=f"vt_toggle_{action_key}_{idx}_{task_id}",
                    style=_state_style(selected_row),
                )
            ]
        )

    if action_key not in ("default_audio", "default_sub"):
        all_indices = [track["index"] for track in tracks]
        all_selected = len(state.get(action_key, [])) == len(all_indices)
        markup.append(
            [
                InlineKeyboardButton(
                    "\U0001F7E2 Clear Multi" if all_selected else "Multi Select",
                    callback_data=f"vt_toggle_{action_key}_all_{task_id}",
                    style=_btn_style(ButtonStyle.SUCCESS) if all_selected else _btn_style(),
                )
            ]
        )

    if action_key in ("remove_audio", "remove_sub"):
        markup.append(
            [
                InlineKeyboardButton("Reset", callback_data=f"vt_reset_{action_key}_{task_id}"),
                InlineKeyboardButton("Reverse", callback_data=f"vt_reverse_{action_key}_{task_id}"),
            ]
        )
        markup.append(
            [
                InlineKeyboardButton(_danger_label("Remove"), callback_data=f"vt_removego_{task_id}", style=_btn_style(ButtonStyle.DANGER)),
                InlineKeyboardButton("\U0001F7E2 Continue", callback_data=f"vt_main_{task_id}", style=_btn_style(ButtonStyle.SUCCESS)),
            ]
        )
    elif action_key in ("extract_audio", "extract_sub"):
        markup.append(
            [
                InlineKeyboardButton("⚡ Extract Selected", callback_data=f"vt_done_{task_id}", style=_btn_style(ButtonStyle.SUCCESS)),
                InlineKeyboardButton("Back", callback_data=f"vt_main_{task_id}"),
            ]
        )
    else:
        back = "vt_order_" + task_id if is_order else "vt_main_" + task_id
        markup.append([InlineKeyboardButton("Back", callback_data=back)])

    note = ""
    if action_key in ("keep_audio", "keep_sub") and state.get(action_key):
        note = f"\n\n<b>Keep only:</b> {len(state[action_key])} selected stream(s)."
    elif action_key in ("extract_audio", "extract_sub") and state.get(action_key):
        note = f"\n\n<b>Selected to extract:</b> {len(state[action_key])} stream(s)."
    elif is_order and state.get(action_key):
        note = f"\n\n<b>Order selected:</b> {len(state[action_key])} stream(s)."
    elif action_key in ("remove_audio", "remove_sub") and state.get(action_key):
        note = f"\n\n<b>Selected to remove:</b> {len(state[action_key])} stream(s)."

    await query.message.edit_text(
        f"<b>{title}</b>\n\nSelect streams. Changes are saved automatically.{note}",
        reply_markup=InlineKeyboardMarkup(markup),
    )


async def video_tools_callback(_, query):
    data = query.data
    try:
        parts = data.split("_")
        if len(parts) < 3:
            raise ValueError("callback data too short")
        task_id = parts[-1]
        action = parts[1]
    except (ValueError, IndexError) as e:
        LOGGER.error(f"VT callback bad data: {data} - {e}")
        await query.answer(f"Bad callback: {e}", show_alert=True)
        return

    event = get_vt_event(task_id)
    if event is None:
        await query.answer("Session expired or already processed!", show_alert=True)
        return
    state = get_vt_state(task_id)
    if state is None:
        await query.answer("Session state not found!", show_alert=True)
        return
    if state.get("completed"):
        await query.answer("This task is already executed!", show_alert=True)
        return

    if state.get("busy"):
        await query.answer("Please wait for the current probe/preview/mux step.", show_alert=True)
        return

    await query.answer()

    try:
        if action == "mux":
            mux_action = parts[2]
            if mux_action == "cancel":
                state["completed"] = True
                state["cancelled"] = True
                event.set()
                await query.message.edit_text("<b>Muxing cancelled.</b> Proceeding normally...")
                return

            if mux_action == "back":
                await render_muxing_configuration(query.message, state)
                return

            if mux_action == "clearbank":
                user_id = query.from_user.id if getattr(query, "from_user", None) else (state.get("user_id") or 0)
                del_count, freed = await clear_audio_bank(user_id)
                state["matched_bank"] = None
                await query.answer(f"🧹 Cleared {del_count} extracted files from VPS!", show_alert=True)
                await render_muxing_configuration(query.message, state)
                return

            if mux_action == "usebank":
                user_id = query.from_user.id if getattr(query, "from_user", None) else (state.get("user_id") or 0)
                bank_info = await list_audio_bank(user_id)
                if bank_info["total_files"] == 0:
                    await query.answer("VPS Audio Bank is currently empty. Extract audio streams first!", show_alert=True)
                    return
                await render_vps_bank_picker(query.message, state, bank_info)
                return

            if mux_action == "togglebanklang":
                lang = parts[3]
                sel = state.setdefault("selected_bank_languages", [])
                if lang in sel:
                    sel.remove(lang)
                else:
                    sel.append(lang)
                user_id = query.from_user.id if getattr(query, "from_user", None) else (state.get("user_id") or 0)
                bank_info = await list_audio_bank(user_id)
                videos = state.get("folder_videos") or ([state["input_path"]] if state.get("input_path") else [])
                if videos:
                    state["matched_bank"] = match_videos_with_bank(videos, bank_info["files"], sel)
                await render_vps_bank_picker(query.message, state, bank_info)
                return

            if mux_action == "mergebank":
                user_id = query.from_user.id if getattr(query, "from_user", None) else (state.get("user_id") or 0)
                bank_info = await list_audio_bank(user_id)
                if bank_info.get("total_files", 0) == 0:
                    await query.answer("VPS Audio Bank is empty! Extract audio streams first.", show_alert=True)
                    return

                if not state.get("selected_bank_languages") and bank_info.get("languages"):
                    state["selected_bank_languages"] = list(bank_info["languages"].keys())

                videos = state.get("folder_videos") or ([state["input_path"]] if state.get("input_path") else [])
                if videos:
                    state["matched_bank"] = match_videos_with_bank(videos, bank_info["files"], state.get("selected_bank_languages"))

                matched_bank = state.get("matched_bank")
                matched_episodes = matched_bank.get("matched_episodes", 0) if matched_bank else 0
                total_episodes = matched_bank.get("total_episodes", 0) if matched_bank else len(videos)

                if matched_episodes == 0:
                    await query.answer("⚠️ No audio files could be matched with the video(s)!", show_alert=True)
                    return

                # For single video file: populate external_audio so _execute_vt_pipeline finds it
                if not state.get("is_folder") and not state.get("folder_videos"):
                    matched_list = matched_bank.get("matched", [])
                    if matched_list:
                        ext_items = []
                        for a_idx, trk in enumerate(matched_list[0].get("audio_tracks", [])):
                            ext_items.append({
                                "index": a_idx,
                                "name": trk.get("audio_name", ""),
                                "path": trk.get("audio_path", ""),
                                "stream_index": 0,
                                "language": trk.get("language", "und"),
                                "title": trk.get("audio_name", ""),
                                "delay_ms": state.get("audio_delay_ms", 0),
                                "duration": 0,
                            })
                        if ext_items:
                            state["external_audio"] = ext_items
                            state["merge_audio"] = list(range(len(ext_items)))

                state["merge_intake"] = True
                state["completed"] = True
                event.set()

                langs_str = ", ".join(state.get("selected_bank_languages", [])) or "Matched Audio"
                await query.answer(f"⚡ Merging {matched_episodes} audio file(s) into video source!", show_alert=False)
                try:
                    await query.message.edit_text(
                        f"🚀 <b>Lossless Muxing Dispatched!</b>\n\n"
                        f"<b>Target:</b> <code>{state.get('filename')}</code>\n"
                        f"<b>Matched:</b> <code>{matched_episodes}/{total_episodes}</code> video(s) paired with VPS audio\n"
                        f"<b>Languages:</b> {langs_str}\n"
                        f"<b>Engine:</b> <code>mkvmerge</code> stream-copy (Lossless, 0% CPU)\n\n"
                        f"<i>Output will be automatically uploaded to Telegram.</i>"
                    )
                except Exception as e:
                    LOGGER.warning(f"Error editing message after mergebank: {e}")
                return

            if mux_action == "setdelay":
                await render_audio_delay_menu(query.message, state)
                return

            if mux_action == "applydelay":
                ms = int(parts[3])
                state["audio_delay_ms"] = ms
                for a in state.get("external_audio", []):
                    a["delay_ms"] = ms
                await query.answer(f"Audio Delay set to {ms:+d}ms!")
                await render_muxing_configuration(query.message, state)
                return

            if mux_action == "customdelay":
                state["awaiting_input"] = ("global_delay_ms", 0)
                await query.message.reply_text("Send the signed integer delay in milliseconds (e.g. <code>-250</code> or <code>1200</code>):")
                return

            if mux_action == "addaudio":
                await query.answer(
                    "Send audio (.mka, .aac, .ac3, .eac3, .m4a) or video files to this chat to add them.",
                    show_alert=True,
                )
                return

            if mux_action == "addsub":
                await query.answer(
                    "Send subtitle (.srt, .ass, .vtt) files to this chat to add them.",
                    show_alert=True,
                )
                return

            if mux_action == "audiolang":
                if not state.get("external_audio"):
                    await query.answer(
                        "No external audio tracks added yet. Send audio files first!",
                        show_alert=True,
                    )
                    return
                await render_audio_lang_menu(query.message, state)
                return

            if mux_action == "picktrack":
                track_idx = int(parts[3])
                await render_audio_lang_picker(query.message, state, track_idx)
                return

            if mux_action == "setlang":
                track_idx = int(parts[3])
                new_lang = parts[4]
                item = next(
                    (a for a in state.get("external_audio", []) if a["index"] == track_idx),
                    None,
                )
                if item:
                    item["language"] = new_lang
                    item["tag"] = format_lang_tag(new_lang)
                    item["title"] = new_lang
                    state["preview_stale"] = True
                    if not state.get("custom_output_name"):
                        state["output_name"] = generate_auto_mux_name(
                            state.get("filename") or "",
                            state.get("external_audio", []),
                            state.get("remove_original_audio", False),
                            state.get("audio_tracks", []),
                        )
                await query.answer(f"Language set to {new_lang}!")
                await render_muxing_configuration(query.message, state)
                return

            if mux_action == "customlang":
                track_idx = int(parts[3])
                state["awaiting_input"] = ("language", track_idx)
                await query.message.reply_text(
                    f"Send the custom language name or ISO code for Audio {track_idx + 1} (e.g. <code>Spanish</code> or <code>spa</code>):"
                )
                return

            if mux_action == "removeaudio":
                await render_remove_audio_menu(query.message, state)
                return

            if mux_action == "toggle" and len(parts) >= 5 and parts[3] == "remove" and parts[4] == "orig":
                state["remove_original_audio"] = not state.get("remove_original_audio", False)
                state["preview_stale"] = True
                if not state.get("custom_output_name"):
                    state["output_name"] = generate_auto_mux_name(
                        state.get("filename") or "",
                        state.get("external_audio", []),
                        state.get("remove_original_audio", False),
                        state.get("audio_tracks", []),
                    )
                await render_remove_audio_menu(query.message, state)
                return

            if mux_action == "delextaudio":
                idx = int(parts[3])
                from aiofiles.os import path as aiopath, remove
                from contextlib import suppress
                ext_audio = state.get("external_audio", [])
                item_to_remove = next((a for a in ext_audio if a["index"] == idx), None)
                if item_to_remove:
                    fpath = item_to_remove.get("path")
                    if fpath and await aiopath.exists(fpath):
                        with suppress(Exception):
                            await remove(fpath)
                    state["external_audio"] = [a for a in ext_audio if a["index"] != idx]
                    for new_idx, a in enumerate(state["external_audio"]):
                        a["index"] = new_idx
                    state["merge_audio"] = [a["index"] for a in state["external_audio"]]
                    state["preview_stale"] = True
                    if not state.get("custom_output_name"):
                        state["output_name"] = generate_auto_mux_name(
                            state.get("filename") or "",
                            state.get("external_audio", []),
                            state.get("remove_original_audio", False),
                            state.get("audio_tracks", []),
                        )
                await query.answer("Audio track deleted.")
                await render_remove_audio_menu(query.message, state)
                return

            if mux_action == "rename":
                await render_rename_menu(query.message, state)
                return

            if mux_action == "autorename":
                state["custom_output_name"] = False
                state["output_name"] = generate_auto_mux_name(
                    state.get("filename") or "",
                    state.get("external_audio", []),
                    state.get("remove_original_audio", False),
                    state.get("audio_tracks", []),
                )
                await query.answer("Output filename auto-generated!")
                await render_muxing_configuration(query.message, state)
                return

            if mux_action == "typecustomname":
                state["awaiting_input"] = ("output_name", None)
                await query.message.reply_text(
                    "Send the custom filename for the output video (e.g. <code>Movie (2026) 1080p.mkv</code>):"
                )
                return

            if mux_action == "toggle":
                toggle_key = "_".join(parts[3:-1])
                if toggle_key == "auto_sync":
                    state["auto_sync"] = not state.get("auto_sync", True)
                    if state["auto_sync"]:
                        video_dur = float(state.get("video_duration", 0) or 0)
                        for a in state.get("external_audio", []):
                            track_dur = float(a.get("duration", 0) or 0)
                            if video_dur and track_dur:
                                a["delay_ms"] = int(round((video_dur - track_dur) * 1000))
                    else:
                        for a in state.get("external_audio", []):
                            a["delay_ms"] = 0
                    state["preview_stale"] = True
                elif toggle_key == "preview_sync":
                    state["preview_sync"] = not state.get("preview_sync", True)
                elif toggle_key == "save_settings":
                    state["save_settings"] = not state.get("save_settings", True)
                elif toggle_key == "final_sample":
                    state["final_sample"] = not state.get("final_sample", True)
                await render_muxing_configuration(query.message, state)
                return

            if mux_action == "preview":
                has_audio = bool(state.get("external_audio") or (state.get("matched_bank") and state["matched_bank"].get("matched")))
                if not has_audio:
                    await query.answer(
                        "Please select audio from VPS Bank or send an audio file before previewing delay!",
                        show_alert=True,
                    )
                    return
                await query.answer("🎬 Generating lossless sync preview clip... Please wait.", show_alert=False)
                try:
                    await generate_merge_preview(state)
                    await query.answer("✅ Delay preview sent to chat! Check audio sync.", show_alert=True)
                except Exception as pe:
                    LOGGER.warning(f"Video Tool manual preview generation failed: {pe}")
                    await query.answer(f"Preview failed: {pe}", show_alert=True)
                return

            if mux_action == "start":
                has_bank = bool(state.get("matched_bank") and state["matched_bank"].get("matched_episodes", 0) > 0)
                if not state.get("external_audio") and not state.get("external_sub") and not has_bank:
                    user_id = query.from_user.id if getattr(query, "from_user", None) else (state.get("user_id") or 0)
                    bank_info = await list_audio_bank(user_id)
                    if bank_info.get("total_files", 0) > 0:
                        if not state.get("selected_bank_languages") and bank_info.get("languages"):
                            state["selected_bank_languages"] = list(bank_info["languages"].keys())
                        videos = state.get("folder_videos") or ([state["input_path"]] if state.get("input_path") else [])
                        if videos:
                            state["matched_bank"] = match_videos_with_bank(videos, bank_info["files"], state.get("selected_bank_languages"))
                            has_bank = bool(state["matched_bank"].get("matched_episodes", 0) > 0)

                if not state.get("external_audio") and not state.get("external_sub") and not has_bank:
                    await query.answer(
                        "Please send audio/subtitle files or select audio from VPS Audio Bank before starting mux!",
                        show_alert=True,
                    )
                    return

                # If bank is matched and single video, populate external_audio so _execute_vt_pipeline finds it
                if has_bank and not state.get("external_audio") and not state.get("is_folder") and not state.get("folder_videos"):
                    matched_list = state["matched_bank"].get("matched", [])
                    if matched_list:
                        ext_items = []
                        for a_idx, trk in enumerate(matched_list[0].get("audio_tracks", [])):
                            ext_items.append({
                                "index": a_idx,
                                "name": trk.get("audio_name", ""),
                                "path": trk.get("audio_path", ""),
                                "stream_index": 0,
                                "language": trk.get("language", "und"),
                                "title": trk.get("audio_name", ""),
                                "delay_ms": state.get("audio_delay_ms", 0),
                                "duration": 0,
                            })
                        if ext_items:
                            state["external_audio"] = ext_items
                            state["merge_audio"] = list(range(len(ext_items)))

                state["merge_audio"] = [item["index"] for item in state.get("external_audio", [])]
                state["merge_sub"] = [item["index"] for item in state.get("external_sub", [])]

                if state.get("save_settings", True):
                    from .. import user_data
                    from ..helper.ext_utils.db_handler import database
                    session = get_vt_session(task_id)
                    listener = session.get("listener") if session else None
                    user_id = listener.message.from_user.id if listener and listener.message and listener.message.from_user else None
                    if user_id:
                        u_dict = user_data.setdefault(user_id, {})
                        u_dict["MUX_AUTO_SYNC"] = state.get("auto_sync", True)
                        u_dict["MUX_PREVIEW_SYNC"] = state.get("preview_sync", True)
                        u_dict["MUX_FINAL_SAMPLE"] = state.get("final_sample", True)
                        u_dict["MUX_REMOVE_ORIGINAL"] = state.get("remove_original_audio", False)
                        if database:
                            await database.update_user_data(user_id)

                if state.get("preview_sync", True):
                    if state.get("preview_stale", True):
                        try:
                            await generate_merge_preview(state)
                        except Exception as pe:
                            LOGGER.warning(f"Video Tool preview generation skipped: {pe}")

                try:
                    finish_merge_track_intake(state, event)
                except ValueError as ve:
                    await query.answer(str(ve), show_alert=True)
                    return

                try:
                    await query.message.edit_text("<b>🚀 Muxing configuration applied. Processing with mkvmerge...</b>")
                except Exception as me:
                    if "MESSAGE_ID_INVALID" not in str(me).upper():
                        raise
                return
        if action == "ext":
            ext_action = parts[2]
            if ext_action == "toggle":
                idx = int(parts[3])
                e_type = state.get("extract_type", "audio")
                key = "extract_audio" if e_type == "audio" else "extract_sub"
                curr = state.setdefault(key, [])
                if idx in curr:
                    curr.remove(idx)
                else:
                    curr.append(idx)
                await render_extract_studio(query.message, state)
                return

            if ext_action == "preset":
                preset = parts[3]
                e_type = state.get("extract_type", "audio")
                key = "extract_audio" if e_type == "audio" else "extract_sub"
                tracks = state.get("audio_tracks" if e_type == "audio" else "sub_tracks", [])
                if preset == "all":
                    state[key] = [t["index"] for t in tracks]
                elif preset == "reset":
                    state[key] = []
                elif preset == "indian":
                    ind_langs = {"tamil", "hindi", "telugu", "malayalam", "kannada", "tam", "hin", "tel", "mal", "kan"}
                    sel = []
                    for t in tracks:
                        combined = f"{t.get('lang', '')} {t.get('title', '')}".lower()
                        if any(l in combined for l in ind_langs):
                            sel.append(t["index"])
                    state[key] = sel or [t["index"] for t in tracks]
                await render_extract_studio(query.message, state)
                return

            if ext_action == "switch":
                curr = state.get("extract_type", "audio")
                state["extract_type"] = "sub" if curr == "audio" else "audio"
                await render_extract_studio(query.message, state)
                return

            if ext_action == "togglevps":
                state["save_to_vps"] = not state.get("save_to_vps", True)
                await render_extract_studio(query.message, state)
                return

            if ext_action == "toggletg":
                state["upload_to_tg"] = not state.get("upload_to_tg", True)
                if not state["upload_to_tg"] and not state.get("save_to_vps", True):
                    state["save_to_vps"] = True
                await render_extract_studio(query.message, state)
                return

            if ext_action == "clearbank":
                user_id = query.from_user.id if getattr(query, "from_user", None) else (state.get("user_id") or 0)
                await clear_audio_bank(user_id)
                await query.answer("🧹 VPS Extracted Audio Bank cleared!", show_alert=True)
                await render_extract_studio(query.message, state)
                return

            if ext_action == "start":
                audio_sel = state.get("extract_audio", [])
                sub_sel = state.get("extract_sub", [])
                if not audio_sel and not sub_sel:
                    await query.answer("Please select at least one stream to extract!", show_alert=True)
                    return
                _clear_non_extract_tools(state)
                state["completed"] = True
                event.set()
                await query.message.edit_text("<b>⚡ Starting stream extraction...</b>")
                return

        if action == "close":
            state["completed"] = True
            state["cancelled"] = True
            event.set()
            await query.message.edit_text("<b>Video Tools cancelled.</b> Proceeding normally...")
            return

        if action == "done":
            if state.get("merge_intake"):
                if state.get("preview_stale") and state.get("merge_audio"):
                    await query.message.edit_text(
                        "<b>Preview required.</b> Generate the 120-second preview "
                        "after your latest audio settings, then press Done.",
                        reply_markup=InlineKeyboardMarkup(
                            [[InlineKeyboardButton("Back", callback_data=f"vt_mergeback_{task_id}")]]
                        ),
                    )
                    return
                try:
                    finish_merge_track_intake(state, event)
                except ValueError as error:
                    await query.answer(str(error), show_alert=True)
                    return
            else:
                state["completed"] = True
                event.set()
            try:
                await query.message.edit_text("<b>Video Tools configuration saved.</b> Processing...")
            except Exception as error:
                if "MESSAGE_ID_INVALID" not in str(error).upper():
                    raise
            return

        if action == "removego":
            if _has_extract_selection(state):
                await query.answer("Extract Stream cannot be combined with other tools.", show_alert=True)
                return
            state["completed"] = True
            event.set()
            await query.message.edit_text("<b>Video Tools:</b> removing selected streams...")
            return

        if action == "video":
            if _has_extract_selection(state):
                await query.answer("Extract Stream cannot be combined with Video + Video.", show_alert=True)
                return
            state["video_merge"] = True
            state["completed"] = True
            event.set()
            await query.message.edit_text("<b>Video + Video:</b> merge planner will start after download/extract.")
            return

        if action == "main":
            await render_video_tools_main(query.message, state)
            return

        if action == "remove":
            if _has_extract_selection(state):
                await query.answer("Extract Stream cannot be combined with Remove Stream.", show_alert=True)
                return
            await render_stream_type_menu(query, task_id, "remove", "Remove Stream")
            return

        if action == "extract":
            _clear_non_extract_tools(state)
            state["extract_type"] = "audio"
            await render_extract_studio(query.message, state)
            return

        if action == "merge":
            if _has_extract_selection(state):
                await query.answer("Extract Stream cannot be combined with Merge Tracks.", show_alert=True)
                return
            await start_merge_track_intake(query.message, state)
            await render_merge_intake(query.message, state)
            return

        if action == "mergeback":
            await render_merge_intake(query.message, state)
            return

        if action == "mergecfg":
            await render_merge_audio_config(query, state, int(parts[2]))
            return

        if action == "mergetoggle":
            index = int(parts[2])
            selected = state.setdefault("merge_audio", [])
            if index in selected:
                selected.remove(index)
            else:
                selected.append(index)
            state["preview_stale"] = True
            await render_merge_audio_config(query, state, index)
            return

        if action == "mergeoriginal":
            state["remove_original_audio"] = not state.get("remove_original_audio", False)
            state["preview_stale"] = True
            await render_merge_intake(query.message, state)
            return

        if action == "mergefield":
            index = int(parts[-2])
            field = "_".join(parts[2:-2])
            if field not in {"language", "delay_ms", "title"}:
                await query.answer("Invalid merge setting.", show_alert=True)
                return
            state["awaiting_input"] = (field, index)
            prompt = {
                "language": "Send the language name or ISO code, for example <code>Tamil</code> or <code>tam</code>.",
                "delay_ms": "Send a signed integer delay in milliseconds, for example <code>-250</code> or <code>1200</code>.",
                "title": "Send the Telegram/MKV audio track title.",
            }[field]
            await query.message.reply_text(prompt)
            return

        if action == "preview":
            await query.message.edit_text("<b>Generating the 120-second middle preview...</b>")
            try:
                await generate_merge_preview(state)
                selected = state.get("merge_audio", [])
                if selected:
                    await render_merge_audio_config_message(query.message, state, selected[0])
                else:
                    await render_merge_intake(query.message, state)
            except Exception as error:
                await query.message.edit_text(
                    f"<b>Preview failed:</b> <code>{str(error)[:400]}</code>",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("Back", callback_data=f"vt_mergeback_{task_id}")]]
                    ),
                )
            return

        if action == "translate":
            if _has_extract_selection(state):
                await query.answer("Extract Stream cannot be combined with Translate Subs.", show_alert=True)
                return
            await render_stream_list(query, state, "translate_sub", "Translate Subtitles")
            return

        if action == "keepaudio":
            if _has_extract_selection(state):
                await query.answer("Extract Stream cannot be combined with Keep Audios.", show_alert=True)
                return
            await render_stream_list(query, state, "keep_audio", "Keep Audios")
            return

        if action == "keepsub":
            if _has_extract_selection(state):
                await query.answer("Extract Stream cannot be combined with Keep Subtitles.", show_alert=True)
                return
            await render_stream_list(query, state, "keep_sub", "Keep Subtitles")
            return

        if action == "order":
            if _has_extract_selection(state):
                await query.answer("Extract Stream cannot be combined with Change Order.", show_alert=True)
                return
            await render_change_order(query, state)
            return

        if action == "orderaudio":
            if _has_extract_selection(state):
                await query.answer("Extract Stream cannot be combined with Audio Order.", show_alert=True)
                return
            await render_stream_list(query, state, "audio_order", "Audio Order")
            return

        if action == "ordersub":
            if _has_extract_selection(state):
                await query.answer("Extract Stream cannot be combined with Subtitle Order.", show_alert=True)
                return
            await render_stream_list(query, state, "sub_order", "Subtitle Order")
            return

        if action == "defa":
            if _has_extract_selection(state):
                await query.answer("Extract Stream cannot be combined with Default Audio.", show_alert=True)
                return
            await render_stream_list(query, state, "default_audio", "Default Audio")
            return

        if action == "defs":
            if _has_extract_selection(state):
                await query.answer("Extract Stream cannot be combined with Default Subtitle.", show_alert=True)
                return
            await render_stream_list(query, state, "default_sub", "Default Subtitle")
            return

        if action == "list":
            mode = parts[2]
            track_type = parts[3]
            action_key = f"{mode}_{track_type}"
            if _has_extract_selection(state) and not action_key.startswith("extract_"):
                await query.answer("Extract Stream cannot be combined with other tools.", show_alert=True)
                return
            title = {
                "remove_audio": "Remove Audio",
                "remove_sub": "Remove Subtitle",
                "extract_audio": "Extract Audio",
                "extract_sub": "Extract Subtitle",
            }.get(action_key, f"{mode.title()} {track_type.title()}")
            await render_stream_list(query, state, action_key, title)
            return

        if action == "toggle":
            action_key = parts[2] + "_" + parts[3]
            idx_str = parts[4]
            if _has_extract_selection(state) and not action_key.startswith("extract_"):
                await query.answer("Extract Stream cannot be combined with other tools.", show_alert=True)
                return
            tracks = _tracks_for(state, action_key)
            all_indices = [track["index"] for track in tracks]

            if idx_str == "all":
                if action_key in ("default_audio", "default_sub"):
                    state[action_key] = None
                else:
                    state[action_key] = [] if len(state.get(action_key, [])) == len(all_indices) else list(all_indices)
            else:
                idx = int(idx_str)
                if action_key in ("default_audio", "default_sub"):
                    state[action_key] = None if state[action_key] == idx else idx
                elif action_key in ("audio_order", "sub_order"):
                    state.setdefault(action_key, [])
                    if idx in state[action_key]:
                        state[action_key].remove(idx)
                    else:
                        state[action_key].append(idx)
                else:
                    state.setdefault(action_key, [])
                    if idx in state[action_key]:
                        state[action_key].remove(idx)
                    else:
                        state[action_key].append(idx)
                    if action_key == "remove_audio" and state.get("default_audio") == idx:
                        state["default_audio"] = None
                    if action_key == "remove_sub" and state.get("default_sub") == idx:
                        state["default_sub"] = None
            if action_key.startswith("extract_") and state.get(action_key):
                _clear_non_extract_tools(state)

            title_map = {
                "remove_audio": "Remove Audio",
                "remove_sub": "Remove Subtitle",
                "extract_audio": "Extract Audio",
                "extract_sub": "Extract Subtitle",
                "merge_audio": "Merge Audio Files",
                "merge_sub": "Merge Subtitle Files",
                "translate_sub": "Translate Subtitles",
                "keep_audio": "Keep Audios",
                "keep_sub": "Keep Subtitles",
                "audio_order": "Audio Order",
                "sub_order": "Subtitle Order",
                "default_audio": "Default Audio",
                "default_sub": "Default Subtitle",
            }
            await render_stream_list(query, state, action_key, title_map[action_key])
            return

        if action in ("reset", "reverse"):
            action_key = parts[2] + "_" + parts[3]
            tracks = _tracks_for(state, action_key)
            if action == "reset":
                state[action_key] = []
            else:
                all_indices = [track["index"] for track in tracks]
                state[action_key] = [
                    idx for idx in all_indices if idx not in state.get(action_key, [])
                ]
            title_map = {
                "remove_audio": "Remove Audio",
                "remove_sub": "Remove Subtitle",
            }
            await render_stream_list(query, state, action_key, title_map.get(action_key, "Remove Stream"))
    except Exception as e:
        if state.get("completed") and "MESSAGE_ID_INVALID" in str(e).upper():
            return
        err_msg = f"{type(e).__name__}: {e}"
        LOGGER.error(f"VT callback error: action={action} data={data} - {e}")
        try:
            await query.answer(err_msg[:200], show_alert=True)
        except Exception:
            pass
