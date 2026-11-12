#!/usr/bin/env python3
path = '/home/administrator/starfall/bot/helper/video_utils/auto_process.py'
with open(path, 'r', encoding='utf-8') as f:
    c = f.read()

target = """        file_list_str = "\\n".join(f"• <code>{ospath.basename(p)}</code>" for p in all_extracted_paths[:10])
        if len(all_extracted_paths) > 10:
            file_list_str += f"\\n<i>...and {len(all_extracted_paths) - 10} more</i>"
        await send_message(
            listener.message,
            f"💾 <b>[Auto Process] Extracted {len(all_extracted_paths)} stream(s) directly to VPS Audio Bank!</b>\\n"
            f"<b>Target:</b> <code>{raw_extract_langs}</code>\\n"
            f"<b>Folder:</b> <code>{ospath.basename(up_path.rstrip('/'))}</code>\\n\\n"
            f"{file_list_str}\\n\\n"
            "<i>Saved in VPS (Upload to Telegram skipped). Files are ready for merging.</i>",
        )"""

replacement = """        await send_message(
            listener.message,
            f"💾 <b>[Auto Process] Extracted {len(all_extracted_paths)} stream(s) to Audio Bank!</b>\\n"
            f"<b>Folder:</b> <code>{ospath.basename(up_path.rstrip('/'))}</code> | <b>Target:</b> <code>{raw_extract_langs}</code>\\n"
            "<i>Saved in VPS (Upload to Telegram skipped). Ready for merging.</i>",
        )"""

if target in c:
    c = c.replace(target, replacement)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(c)
    print("SUCCESS_REPLACED")
else:
    print("TARGET_NOT_FOUND")
