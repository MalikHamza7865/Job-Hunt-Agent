#!/usr/bin/env python3
"""Manual Adzuna probe - confirm what/where/remote produce sensible results.

Usage (reads ADZUNA_APP_ID / ADZUNA_APP_KEY / ADZUNA_COUNTRY from .env):
    python app/probe_adzuna.py --what python --where birmingham --country gb
    python app/probe_adzuna.py --what "fastapi" --where remote
    python app/probe_adzuna.py --what python --where "lahore"   # see handled-unsupported
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

from app.adzuna_client import AdzunaClient


def main() -> None:
    parser = argparse.ArgumentParser(description="Manual Adzuna query probe.")
    parser.add_argument("--what", default="python", help="keyword (what)")
    parser.add_argument("--where", default="", help="location (where); 'remote' for remote mode")
    parser.add_argument("--country", default=None, help="feed country (overrides env)")
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()

    client = AdzunaClient(country=args.country, verbose=True)

    print(f"\n== Probe: what={args.what!r} where={args.where!r} "
          f"country={client.country} ==")
    results = client.search(keyword=args.what, location=args.where, limit=args.limit)

    print(f"\nReturned {len(results)} normalized jobs:")
    for job in results[:args.limit]:
        loc = job["location"] or "n/a"
        print(f"  - [{job['external_job_id']}] {job['title'][:60]} @ {loc[:35]}")


if __name__ == "__main__":
    main()