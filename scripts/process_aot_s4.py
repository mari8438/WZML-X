#!/usr/bin/env python3
import asyncio
import logging
import sys
import os

sys.path.insert(0, "/home/administrator/starfall")

from bot.modules.starfall_pipeline.autonomous_agent import StarfallAutonomousAgent, PHASE1_COMPLETED_CATALOGUE

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(name)s] [%(levelname)s] - %(message)s",
)

async def main():
    agent = StarfallAutonomousAgent()
    aot_s4 = [item for item in PHASE1_COMPLETED_CATALOGUE if item["title"] == "Attack on Titan" and item["season"] == 4][0]
    print(f"Starting processing for {aot_s4['title']} Season {aot_s4['season']} ({aot_s4['total_eps']} eps)...")
    ok = await agent.process_anime(aot_s4)
    print(f"Result for Attack on Titan S04: {ok}")
    if not ok:
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
