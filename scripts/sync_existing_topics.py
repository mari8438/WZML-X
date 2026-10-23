import asyncio
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pyrogram import Client
from bot.helper.ext_utils.missav_topic_archive import MissAVTopicArchive
import config

SUPERGROUPS = [
    (-1004486729792, "Tags - Esub"),
    (-1003967556547, "Actress - Esub"),
    (-1004345896581, "Codes - Esub"),
]


async def main():
    print("=== Syncing Existing Forum Topics into Database ===")
    await MissAVTopicArchive.init()

    # Use first active helper bot token
    tokens = config.GLOBAL_UPLOAD_BOT_TOKENS.split()
    tok = tokens[0] if tokens else getattr(config, "BOT_TOKEN", "")
    api_id = config.TELEGRAM_API
    api_hash = config.TELEGRAM_HASH

    app = Client("topic_syncer", api_id=api_id, api_hash=api_hash, bot_token=tok, in_memory=True)
    await app.start()
    me = await app.get_me()
    print(f"Logged in as @{me.username} ({me.id})")

    total_synced = 0
    for chat_id, group_name in SUPERGROUPS:
        print(f"\nScanning topics in {group_name} ({chat_id})...")
        count = 0
        try:
            async for topic in app.get_forum_topics(chat_id):
                t_id = getattr(topic, "message_thread_id", None) or getattr(topic, "id", None)
                t_title = getattr(topic, "title", "") or getattr(topic, "name", "")
                if t_id and t_title:
                    await MissAVTopicArchive.set(chat_id, t_title, t_id, display_name=t_title)
                    count += 1
            print(f"✅ Indexed {count} topics for {group_name}")
            total_synced += count
        except Exception as e:
            print(f"❌ Error scanning {group_name}: {e}")

    await app.stop()
    print(f"\n🎉 Finished! Total topics indexed into MongoDB & configs/missav_topics.json: {total_synced}")


if __name__ == "__main__":
    asyncio.run(main())
