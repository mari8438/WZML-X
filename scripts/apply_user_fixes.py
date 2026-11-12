#!/usr/bin/env python3
"""
1. Fix Batch Leech: If any link fails, skip it instead of cancelling entire batch task
2. Ultra-shorten Audio Bank log: Only show how many files were saved
"""

# 1. Update task_listener.py
listener_path = "/home/administrator/starfall/bot/helper/listeners/task_listener.py"
with open(listener_path, "r", encoding="utf-8") as f:
    listener_code = f.read()

old_listener_snippet = """        controller_gid = getattr(self, "batch_controller_gid", "")
        if controller_gid and result != "complete":
            with suppress(Exception):
                from ...modules.batch_task_registry import mark_controller_cancelled

                mark_controller_cancelled(controller_gid, str(result))"""

new_listener_snippet = """        controller_gid = getattr(self, "batch_controller_gid", "")
        if controller_gid and result != "complete":
            # Link failed: skip it without cancelling the entire batch task
            pass"""

if old_listener_snippet in listener_code:
    listener_code = listener_code.replace(old_listener_snippet, new_listener_snippet)
    with open(listener_path, "w", encoding="utf-8") as f:
        f.write(listener_code)
    print("SUCCESS: Updated task_listener.py (skip failed links in batch)")
else:
    print("WARNING: old_listener_snippet not found in task_listener.py")


# 2. Update auto_process.py for ultra-short message
ap_path = "/home/administrator/starfall/bot/helper/video_utils/auto_process.py"
with open(ap_path, "r", encoding="utf-8") as f:
    ap_code = f.read()

# Look for the audio bank notification block
import re
pattern = r"await send_message\(\s*listener\.message,\s*f?\"💾 <b>\[Auto Process\].*?\n\s*\)"
match = re.search(pattern, ap_code, re.DOTALL)
if match:
    short_msg = """await send_message(
            listener.message,
            f"💾 <b>Saved {len(all_extracted_paths)} audio file(s) to Audio Bank.</b>",
        )"""
    ap_code = ap_code[:match.start()] + short_msg + ap_code[match.end():]
    with open(ap_path, "w", encoding="utf-8") as f:
        f.write(ap_code)
    print("SUCCESS: Updated auto_process.py (ultra-short audio bank log)")
else:
    print("WARNING: audio bank message pattern not found in auto_process.py")
