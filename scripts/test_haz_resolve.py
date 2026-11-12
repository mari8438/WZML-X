#!/usr/bin/env python3
import sys
import os
sys.path.insert(0, '/home/administrator/starfall')
os.chdir('/home/administrator/starfall')

import asyncio
from bot.helper.ext_utils.hindianime_resolver import HindiAnimeResolver

async def main():
    gateway_url = "https://002.hindianimeszone.com/download1.php?code=0YVFP&q=720p"
    solver_url = "http://127.0.0.1:8191/v1"
    print(f"Resolving gateway URL: {gateway_url} via {solver_url}")
    result = await HindiAnimeResolver.resolve_via_solver(gateway_url, solver_url)
    print("Resolver Result:", result)

if __name__ == "__main__":
    asyncio.run(main())
