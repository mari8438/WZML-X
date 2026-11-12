#!/usr/bin/env python3
import asyncio
import logging
import sys

sys.path.insert(0, "/home/administrator/starfall")

from bot.modules.starfall_pipeline.autonomous_agent import StarfallAutonomousAgent, PHASE1_COMPLETED_CATALOGUE

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(name)s] [%(levelname)s] - %(message)s",
)

async def main():
    agent = StarfallAutonomousAgent()
    aot_s3 = [item for item in PHASE1_COMPLETED_CATALOGUE if item["title"] == "Attack on Titan" and item["season"] == 3][0]
    print(f"Starting processing for {aot_s3['title']} Season {aot_s3['season']} ({aot_s3['total_eps']} eps)...")
    
    # Run pipeline twice to do chunked ingestion under 39GB free disk buffer
    print("=== PASS 1: Sourcing missing episodes ===")
    await agent.process_anime(aot_s3)
    
    # If not all 22 complete, run pass 2 for remaining
    import os, re
    clean_t = re.sub(r"[^\w]", "", aot_s3["title"]).lower()
    muxes = [f for f in os.listdir(agent.mux_engine.workspace_dir if hasattr(agent.mux_engine, "output_dir") else "/home/administrator/starfall/downloads/muxed_output") if f.lower().endswith(".mkv") and "s03" in f.lower() and clean_t in re.sub(r"[^\w]", "", f).lower()]
    
    if len(muxes) < aot_s3["total_eps"]:
        print(f"=== PASS 2: Sourcing remaining ({len(muxes)}/{aot_s3['total_eps']} present) ===")
        await agent.process_anime(aot_s3)
        
    muxes_final = [f for f in os.listdir("/home/administrator/starfall/downloads/muxed_output") if f.lower().endswith(".mkv") and "s03" in f.lower() and clean_t in re.sub(r"[^\w]", "", f).lower()]
    print(f"Final S03 Muxed Count: {len(muxes_final)}/{aot_s3['total_eps']}")
    if len(muxes_final) < aot_s3["total_eps"]:
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
