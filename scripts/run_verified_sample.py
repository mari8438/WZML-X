import asyncio
import logging
import os
import sqlite3
import sys

sys.path.insert(0, "/home/administrator/starfall")

from bot.modules.starfall_pipeline.pipeline_manager import StarfallPipelineManager
from bot.modules.starfall_pipeline.db import init_db

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] - %(message)s",
    datefmt="%d-%b-%y %I:%M:%S %p",
)
LOGGER = logging.getLogger("VerifiedSampleRunner")

async def main():
    await init_db()

    # Clear previous test state for Cuckoos in DB so it runs cleanly
    conn = sqlite3.connect("/home/administrator/starfall/data/starfall_state.db")
    c = conn.cursor()
    c.execute("UPDATE anime_seasons SET season_state = 'PENDING' WHERE title_romaji LIKE '%Cuckoo%' OR title_romaji LIKE '%Kakkou%'")
    c.execute("DELETE FROM episode_jobs WHERE season_id IN (SELECT season_id FROM anime_seasons WHERE title_romaji LIKE '%Cuckoo%' OR title_romaji LIKE '%Kakkou%')")
    conn.commit()
    conn.close()

    manager = StarfallPipelineManager(
        source_channel_id=-1004335327733,
        target_channel_id=-1004415413346,
    )
    LOGGER.info("Starting Verified Sample Run for 'A Couple of Cuckoos' S01 (2 episodes)...")
    res = await manager.process_series_season(
        "A Couple of Cuckoos",
        season_number=1,
        max_episodes_to_process=2,
    )
    LOGGER.info(f"Verified Sample Run Finished: {res}")

if __name__ == "__main__":
    asyncio.run(main())
