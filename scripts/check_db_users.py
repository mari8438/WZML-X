#!/usr/bin/env python3
import asyncio
import sys
import os

sys.path.insert(0, '/home/administrator/starfall')
os.chdir('/home/administrator/starfall')

from bot.core.config_manager import Config
Config.load()
from bot.helper.ext_utils.db_handler import DbManager

async def check_users():
    if DbManager.db is None:
        await DbManager().db_load()
    rows = await DbManager().get_all_users()
    print("Users in DB:")
    for u, data in rows.items():
        print(f"User {u}:")
        for k in ['AUTO_PROCESS', 'AUTO_VT', 'AUTO_EXTRACT_AUDIO_LANGS', 'AUTO_EXTRACT_SAVE_VPS', 'AUTO_EXTRACT_UPLOAD_TG']:
            print(f"  {k}: {data.get(k)}")

if __name__ == '__main__':
    asyncio.run(check_users())
