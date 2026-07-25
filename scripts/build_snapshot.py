"""Run the pipeline once and write a snapshot the API can serve instantly.

Run this before recording the demo, and on a schedule in production. It
decouples the slow part (model calls) from the fast part (serving), which is
what lets USSD answer in milliseconds and lets a free-tier host cold-start
without a two-minute stall.
"""
import asyncio, logging, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')

from hatua.api.app import STATE, SNAPSHOT, refresh

async def main():
    await refresh(top_n=int(sys.argv[1]) if len(sys.argv) > 1 else 4)
    print(f"\ndistricts assessed : {len(STATE.results)}")
    print(f"advisories total   : {len(STATE.all_advisories)}")
    print(f"  dispatchable     : {len(STATE.advisories)}")
    print(f"  blocked          : {len(STATE.blocked)}")
    print(f"snapshot           : {SNAPSHOT}")

asyncio.run(main())
