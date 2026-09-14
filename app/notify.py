#!/usr/bin/env python3
"""Milestone 4 - send WhatsApp notifications for new high-scoring matches.

Pipeline:
    python app/notify.py                 # send notifications for qualifying matches
    python app/notify.py --dry-run       # preview message(s) without sending

Behaviour:
  - Reads `preferences` (min_match_threshold, notifications_enabled/pause).
  - Selects `matches` with status='new' and match_score >= threshold.
  - Builds a numbered WhatsApp message (score, missing skills, link).
  - Sends via the WhatsApp Cloud API, then marks those matches status='sent'
    (sets sent_at) and writes the outbound message to `whatsapp_log`.
  - Honors the pause flag: when notifications_enabled is false it does nothing.

Message size: WhatsApp text messages cap at 4096 chars, so the batch is split
into multiple messages of up to 8 jobs each.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import get_client
from app.whatsapp_client import (
    WhatsAppClient,
    WhatsAppError,
    client_from_env,
    is_session_window_error,
)

MAX_JOBS_PER_MESSAGE = 8
MAX_CHARS_PER_MESSAGE = 3800
MISSING_SKILLS_CAP = 6


def load_preferences(client) -> dict:
    rows = client.table("preferences").select("*").eq("id", 1).execute().data
    if not rows:
        sys.exit("Error: no preferences row (id=1). Run set_preferences.py first.")
    return rows[0]


def qualifying_matches(client, threshold: int) -> tuple[list[dict], dict[int, dict]]:
    """Return (ordered matches, job-id -> job map) for not-yet-sent, above-threshold."""
    matches = (
        client.table("matches")
        .select("id, job_id, match_score, missing_skills, reasoning, status")
        .eq("status", "new")
        .execute()
        .data
    )
    hits = [m for m in matches if int(m.get("match_score") or 0) >= threshold]
    hits.sort(key=lambda m: int(m["match_score"]), reverse=True)

    job_ids = list({m["job_id"] for m in hits})
    jobs: dict[int, dict] = {}
    for i in range(0, len(job_ids), 100):
        chunk = job_ids[i : i + 100]
        for row in (client.table("jobs_seen")
                    .select("id, title, company, location, url")
                    .in_("id", tuple(chunk)).execute().data):
            jobs[row["id"]] = row
    return hits, jobs


def _missing_skills(match: dict) -> list[str]:
    """Return missing_skills as a list, tolerating stale string-encoded rows."""
    value = match.get("missing_skills") or []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return [value]
    if not isinstance(value, list):
        value = [value]
    return [str(s).strip() for s in value if str(s).strip()]


def format_matches(items: list[tuple[dict, dict]]) -> str:
    """Build the numbered WhatsApp body for a batch of (match, job) pairs."""
    lines = [f"🎯 {len(items)} new job match(es) found"]
    if len(items) > 1:
        lines[0] += f" (top {len(items)})"
    lines.append("")
    for idx, (match, job) in enumerate(items, 1):
        title = job.get("title") or "(untitled)"
        company = job.get("company") or "Unknown"
        score = int(match.get("match_score") or 0)
        missing_txt = ", ".join(_missing_skills(match)[:MISSING_SKILLS_CAP])
        url = job.get("url") or ""
        lines.append(f"{idx}. {title} @ {company} — {score}% match")
        if missing_txt:
            lines.append(f"   Missing: {missing_txt}")
        if url:
            lines.append(f"   🔗 {url}")
        if idx != len(items):
            lines.append("")
    lines.append("")
    lines.append("Reply with a number for tailored resume tips. "
                 "Send 'pause' to mute notifications.")
    return "\n".join(lines)


def _try_template_fallback(wa: WhatsAppClient, to: str, batch, name: str, lang: str) -> bool:
    """Deliver a condensed version of the batch via an approved template.

    Template body to create/approve in Meta (name defaults to job_match_alert):
        "New job matches ready: {{1}} new match(es). Top pick: {{2}}
         ({{3}}% match). Reply in WhatsApp for tailoring tips."
    """
    count = str(len(batch))
    top_match, top_job = (batch[0] if batch else ({}, {}))
    top_title = (top_job.get("title") or "job")[:100]
    top_score = str(int(top_match.get("match_score") or 0))
    try:
        wa.send_template_message(to, name, [count, top_title, top_score], language=lang)
        return True
    except WhatsAppError as exc:
        print(f"  ! template fallback also failed: {exc}")
        return False


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Send WhatsApp notifications for new high-score matches.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview message(s) without sending or marking sent")
    args = parser.parse_args()

    client = get_client()
    prefs = load_preferences(client)

    if not prefs.get("notifications_enabled", True):
        print("Skipped: notifications are paused (preferences.notifications_enabled=false).")
        return

    threshold = int(prefs.get("min_match_threshold") or 60)
    hits, jobs = qualifying_matches(client, threshold)

    if not hits:
        print(f"No new matches above threshold={threshold}. Nothing to send.")
        return

    pairs = [(m, jobs[m["job_id"]]) for m in hits if m["job_id"] in jobs]

    msgs: list[tuple[list[tuple[dict, dict]], str]] = []
    for start in range(0, len(pairs), MAX_JOBS_PER_MESSAGE):
        batch = pairs[start : start + MAX_JOBS_PER_MESSAGE]
        body = format_matches(batch)
        if len(body) > MAX_CHARS_PER_MESSAGE:
            print(f"  ! batch of {len(batch)} exceeds {MAX_CHARS_PER_MESSAGE} chars; "
                  f"splitting further", file=sys.stderr)
            while len(body) > MAX_CHARS_PER_MESSAGE and len(batch) > 1:
                batch = batch[:-1]
                body = format_matches(batch)
        msgs.append((batch, body))

    print(f"{len(hits)} qualifying match(es) -> {len(msgs)} message(s).")

    if args.dry_run:
        for idx, (_, body) in enumerate(msgs, 1):
            print(f"\n----- message {idx} ({len(body)} chars) -----")
            print(body)
        print("\nDry run - nothing sent, matches left as 'new'.")
        return

    try:
        wa = client_from_env()
    except ValueError as exc:
        sys.exit(f"Error: {exc}")

    to = os.environ.get("RECIPIENT_PHONE") or os.environ.get("WHATSAPP_TO") or ""
    if not to:
        sys.exit("Error: RECIPIENT_PHONE not set in .env")

    template_name = os.environ.get("WHATSAPP_TEMPLATE_NAME", "").strip()
    template_lang = os.environ.get("WHATSAPP_TEMPLATE_LANG", "en").strip() or "en"

    for batch, body in msgs:
        try:
            msg_id = wa.send_text_message(to, body)
        except WhatsAppError as exc:
            if template_name and _try_template_fallback(
                    wa, to, batch, template_name, template_lang):
                print(f"  ! free-form blocked ({str(exc)[:160]}); "
                      f"sent via template '{template_name}' instead.")
                msg_id = "template"
            else:
                print(f"  ! send failed: {exc}")
                if is_session_window_error(exc) and not template_name:
                    print("    24-hour window lapsed and no approved template is "
                          "configured. Approve the 'job_match_alert' template "
                          "(WHATSAPP_TEMPLATE_NAME) or reply to the bot on "
                          "WhatsApp to re-open the window.")
                print("    (matches left as 'new'; nothing marked as sent)")
                return
        first_job_id = batch[0][1]["id"] if batch else None
        client.table("whatsapp_log").insert({
            "direction": "out",
            "message_text": body,
            "related_job_id": first_job_id,
        }).execute()
        matched_ids = [int(m["id"]) for m, _ in batch]
        (client.table("matches")
         .update({"status": "sent", "sent_at": "now()"})
         .in_("id", tuple(matched_ids))
         .execute())
        print(f"  sent {len(batch)} match(es) -> status='sent' (msg {msg_id})")


if __name__ == "__main__":
    main()
