#!/usr/bin/env python3
"""
Update vps_audio_bank.py:
Automatically delete duplicate files when saving or scanning the audio bank.
"""
import re

bank_file = "/home/administrator/starfall/bot/helper/video_utils/vps_audio_bank.py"
with open(bank_file, "r", encoding="utf-8") as f:
    code = f.read()

target = """async def save_to_audio_bank(user_id: int | str, src_path: str, custom_name: str = "") -> str:
    \"\"\"Saves an extracted audio file into the persistent VPS audio bank.\"\"\"
    bank_dir = get_audio_bank_dir(user_id)
    await aiomakedirs(bank_dir, exist_ok=True)
    target_name = custom_name or os.path.basename(src_path)
    target_path = os.path.join(bank_dir, target_name)
    if os.path.abspath(src_path) != os.path.abspath(target_path):
        if await aiopath.exists(target_path):
            await aioremove(target_path)
        await aiocopyfile(src_path, target_path)
    return target_path"""

replacement = """async def save_to_audio_bank(user_id: int | str, src_path: str, custom_name: str = "") -> str:
    \"\"\"Saves an extracted audio file into the persistent VPS audio bank, automatically deleting duplicates.\"\"\"
    bank_dir = get_audio_bank_dir(user_id)
    await aiomakedirs(bank_dir, exist_ok=True)
    target_name = custom_name or os.path.basename(src_path)
    target_path = os.path.join(bank_dir, target_name)

    # 1. Direct filename overwrite / duplicate removal
    if os.path.abspath(src_path) != os.path.abspath(target_path):
        if await aiopath.exists(target_path):
            await aioremove(target_path)
        await aiocopyfile(src_path, target_path)

    # 2. Rule: Automatically delete any duplicate audio files (same episode & lang, or identical size)
    try:
        new_size = os.path.getsize(target_path)
        new_s, new_e = parse_season_episode(target_name)
        new_lang, _ = resolve_full_language_name("", target_name)

        for fname in os.listdir(bank_dir):
            fpath = os.path.join(bank_dir, fname)
            if fpath == target_path or not os.path.isfile(fpath):
                continue
            
            f_size = os.path.getsize(fpath)
            f_lang, _ = resolve_full_language_name("", fname)

            # Duplicate condition A: Identical file size + same audio language
            if f_size == new_size and f_lang == new_lang:
                LOGGER.info(f"Duplicate audio found in bank (identical size {f_size}B): auto-deleting {fname}")
                try:
                    os.remove(fpath)
                except Exception:
                    pass
                continue

            # Duplicate condition B: Same Season + Episode + same audio language
            if new_s is not None and new_e is not None:
                fs, fe = parse_season_episode(fname)
                if fs == new_s and fe == new_e and f_lang == new_lang:
                    LOGGER.info(f"Duplicate audio found in bank (same S{fs}E{fe} {f_lang}): auto-deleting older {fname}")
                    try:
                        os.remove(fpath)
                    except Exception:
                        pass
    except Exception as e:
        LOGGER.warning(f"Audio bank duplicate check warning: {e}")

    return target_path"""

if target in code:
    code = code.replace(target, replacement)
    with open(bank_file, "w", encoding="utf-8") as f:
        f.write(code)
    print("SUCCESS: Updated vps_audio_bank.py with automatic duplicate deletion rule!")
else:
    print("WARNING: Target block not found in vps_audio_bank.py")
