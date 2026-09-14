#!/usr/bin/env python3
"""Set job-hunting preferences in Supabase `preferences` table.

Usage:
    python set_preferences.py                      # interactive
    python set_preferences.py --keywords python,fastapi --locations "remote,new york" --threshold 70
    python set_preferences.py --pause              # stop notifications
    python set_preferences.py --resume             # resume notifications
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import get_client

PREF_ID = 1


def current_prefs(client) -> dict:
    res = client.table("preferences").select("*").eq("id", PREF_ID).execute()
    data = res.data
    return data[0] if data else {}


def parse_list(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def prompt_optional(label: str, current: str) -> str | None:
    """Prompt; return None when the user leaves the field empty (no change)."""
    value = input(f"{label} [{current}]: ").strip()
    return value or None


def main() -> None:
    parser = argparse.ArgumentParser(description="Set job-hunt preferences.")
    parser.add_argument("--keywords", help="Comma-separated keywords (e.g. python,fastapi)")
    parser.add_argument("--locations", help="Comma-separated locations (e.g. remote,new york)")
    parser.add_argument("--threshold", type=int, help="Min match threshold (0-100)")
    parser.add_argument("--salary-min", type=int, help="Minimum salary (nullable)")
    parser.add_argument("--pause", action="store_true", help="Disable notifications (M6 flag)")
    parser.add_argument("--resume", action="store_true", help="Enable notifications (M6 flag)")
    args = parser.parse_args()

    client = get_client()
    prefs = current_prefs(client)
    interactive = not any([args.keywords, args.locations, args.threshold,
                           args.salary_min is not None, args.pause, args.resume])

    keywords: list[str] | None
    locations: list[str] | None

    if args.keywords:
        keywords = parse_list(args.keywords)
    elif interactive:
        cur = ", ".join(prefs.get("keywords") or [])
        val = prompt_optional("Keywords", cur)
        keywords = parse_list(val) if val else None
    else:
        keywords = None

    if args.locations:
        locations = parse_list(args.locations)
    elif interactive:
        cur = ", ".join(prefs.get("locations") or [])
        val = prompt_optional("Locations", cur)
        locations = parse_list(val) if val else None
    else:
        locations = None

    threshold = args.threshold if args.threshold is not None else None
    salary_min = args.salary_min
    if interactive:
        raw = prompt_optional("Min match threshold", str(prefs.get("min_match_threshold", 60)))
        if raw:
            threshold = int(raw)
        raw = prompt_optional("Min salary (blank to clear)", str(prefs.get("salary_min") or "none"))
        if raw:
            salary_min = int(raw)

    payload: dict = {}
    if keywords is not None:
        payload["keywords"] = keywords
    if locations is not None:
        payload["locations"] = locations
    if threshold is not None:
        if not 0 <= threshold <= 100:
            sys.exit("Error: threshold must be between 0 and 100.")
        payload["min_match_threshold"] = threshold
    if salary_min is not None:
        payload["salary_min"] = salary_min
    if args.pause:
        payload["notifications_enabled"] = False
    if args.resume:
        payload["notifications_enabled"] = True

    if not payload:
        print("No changes requested.")
        return

    result = (
        client.table("preferences")
        .upsert({"id": PREF_ID, **payload}, on_conflict="id")
        .execute()
    )
    updated = result.data[0]
    print("OK - preferences updated:")
    for key in ("keywords", "locations", "min_match_threshold", "salary_min",
                "notifications_enabled"):
        print(f"  {key:>20}: {updated.get(key)}")


if __name__ == "__main__":
    main()