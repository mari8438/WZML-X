import asyncio
import logging
import os
import sys

sys.path.insert(0, "/home/administrator/starfall")

from bot.modules.starfall_pipeline.pipeline_manager import StarfallPipelineManager
from bot.modules.starfall_pipeline.db import get_or_create_season, init_db, update_season_state

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] - %(message)s",
    datefmt="%d-%b-%y %I:%M:%S %p",
)
LOGGER = logging.getLogger("SampleRunner")

async def main():
    await init_db()
    manager = StarfallPipelineManager(
        source_channel_id=-1004335327733,
        target_channel_id=-1004415413346,
    )
    LOGGER.info("Starting sample run for 'My Hero Academia Vigilantes' (Season 1, limit 2 episodes)...")
    res = await manager.process_series_season(
        "My Hero Academia Vigilantes",
        season_number=1,
        max_episodes_to_process=2,
    )
    LOGGER.info(f"Sample run finished with result: {res}")

if __name__ == "__main__":
    asyncio.run(main())
