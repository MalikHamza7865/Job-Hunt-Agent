#!/usr/bin/env python3
"""Fetch new job postings from a configured source and dedupe against Supabase.

Sources (see --source / FETCH_SOURCE env):
  adzuna   (default)  official Adzuna Job Search API - free, no partner approval
  indeed   official Indeed OAuth + GraphQL API (partner-gated needs approval)
  sample   offline demo from a local JSON file

Usage:
    python fetch_jobs.py                                   # live Adzuna (default)
    python fetch_jobs.py --source indeed                   # live Indeed
    python fetch_jobs.py --source sample --sample-data sample_jobs.json  # demo/dedupe

Reads keywords + locations from the `preferences` table, queries the source for
each combo, skips postings whose external_job_id already exists in jobs_seen,
and inserts the rest.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.adzuna_client import client_from_env as adzuna_from_env
from app.db import get_client
from app.indeed_client import IndeedClient, client_from_env as indeed_from_env, normalize_job

SOURCES = ("adzuna", "indeed", "sample")


def load_preferences(client) -> dict:
    rows = client.table("preferences").select("*").eq("id", 1).execute().data
    if not rows:
        sys.exit("Error: no preferences row (id=1). Run set_preferences.py first.")
    prefs = rows[0]
    if not prefs.get("keywords"):
        sys.exit("Error: no keywords set. Run: python app/set_preferences.py --keywords ...")
    return prefs


def known_external_ids(client, ids: list[str]) -> set[str]:
    """For a batch of ids, fetch the subset that already exists in jobs_seen."""
    if not ids:
        return set()
    found: set[str] = set()
    # PostgREST limits URL length; chunk by 100 ids per request.
    for i in range(0, len(ids), 100):
        chunk = ids[i : i + 100]
        rows = (
            client.table("jobs_seen")
            .select("external_job_id")
            .in_("external_job_id", chunk)
            .execute()
            .data
        )
        found.update(r["external_job_id"] for r in rows)
    return found


def fetch_from_sample(path: Path) -> list[dict]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        sys.exit("Error: sample-data file must be a JSON array of job objects.")
    # Sample fixtures are Indeed-shaped job records; keep the canonical contract.
    return [normalize_job(job) for job in raw if isinstance(job, dict)]


def fetch_from_api(client, source: str, prefs: dict, limit: int) -> list[dict]:
    jobs: list[dict] = []
    for keyword in prefs["keywords"]:
        for location in prefs["locations"] or [""]:
            print(f"  querying {source} for: {keyword} @ {location or 'anywhere'}")
            try:
                jobs.extend(client.search(keyword=keyword, location=location, limit=limit))
            except Exception as exc:
                print(f"  ! {source} query failed for '{keyword} @ {location}': {exc}")
                if source != "adzuna":
                    raise
    return jobs


def build_client(source: str, verbose: bool = False) -> object:
    if source == "adzuna":
        return adzuna_from_env(verbose=verbose)
    if source == "indeed":
        return indeed_from_env()
    return IndeedClient()


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch and dedupe jobs.")
    parser.add_argument("--source", choices=SOURCES,
                        default=None, help=f"Job source (default: FETCH_SOURCE env or '{SOURCES[0]}')")
    parser.add_argument("--sample-data", type=Path,
                        help="Load jobs from a local JSON file instead of a live API")
    parser.add_argument("--limit", type=int, default=25,
                        help="Max results per keyword/location query")
    parser.add_argument("--country", default=None,
                        help="Adzuna/Indeed country code (e.g. us, gb); overrides env")
    parser.add_argument("--verbose", action="store_true",
                        help="Print the exact API URL/params sent to Adzuna and response status")
    args = parser.parse_args()

    import os
    source = args.source or os.environ.get("FETCH_SOURCE") or "adzuna"
    if args.sample_data:
        source = "sample"
    if source not in SOURCES:
        sys.exit(f"Error: unknown source '{source}'. Choose from {', '.join(SOURCES)}")

    client_db = get_client()
    prefs = load_preferences(client_db)

    if source == "sample":
        if not args.sample_data:
            sys.exit("Error: --source sample requires --sample-data <file>")
        if not args.sample_data.exists():
            sys.exit(f"Error: sample file not found: {args.sample_data}")
        jobs = fetch_from_sample(args.sample_data)
    else:
        client_api = build_client(source, verbose=args.verbose)
        if args.country and hasattr(client_api, "country"):
            client_api.country = args.country
        jobs = fetch_from_api(client_api, source, prefs, args.limit)

    print(f"Fetched {len(jobs)} raw job posting(s).")

    candidates = [j for j in jobs if j.get("external_job_id")]
    existing = known_external_ids(client_db, [j["external_job_id"] for j in candidates])

    new_jobs = []
    seen_this_run: set[str] = set()
    for job in candidates:
        key = job["external_job_id"]
        if key in existing or key in seen_this_run:
            continue
        seen_this_run.add(key)
        new_jobs.append(job)

    print(f"  duplicated/skipped: {len(candidates) - len(new_jobs)}")
    print(f"  new to insert:      {len(new_jobs)}")

    if new_jobs:
        client_db.table("jobs_seen").insert(new_jobs).execute()
        print("Inserted new jobs into jobs_seen:")
        for job in new_jobs:
            print(f"  - [{job['external_job_id']}] {job['title']} @ {job['company']} ({job['location']})")
    else:
        print("No new jobs this run (dedupe OK).")


if __name__ == "__main__":
    main()