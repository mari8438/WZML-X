#!/usr/bin/env python3
"""
Ensure Auto Process is NEVER blocked by self.video_tool in task_listener.py.
When auto_enabled(self) is True, run process_auto_pipeline directly and suppress interactive Video Tool UI.
"""
file_path = "/home/administrator/starfall/bot/helper/listeners/task_listener.py"

with open(file_path, "r", encoding="utf-8") as f:
    content = f.read()

old_block = """        if auto_enabled(self) and not self.video_tool and not self.is_cancelled:
            up_path = await process_auto_pipeline(self, up_path, gid)"""

new_block = """        if auto_enabled(self) and not self.is_cancelled:
            self.video_tool = False
            up_path = await process_auto_pipeline(self, up_path, gid)"""

if old_block in content:
    content = content.replace(old_block, new_block)
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)
    print("SUCCESS: Updated task_listener.py to prioritize auto_enabled over interactive video_tool!")
else:
    print("WARNING: old_block not found in task_listener.py")
