"""Milestone 6 - FastAPI webhook for WhatsApp replies.

Receives incoming WhatsApp messages (Meta Cloud API), interprets text commands,
and replies with generated content. Deployable to Render free tier.

Endpoints:
    GET  /webhook   Meta webhook verification (echoes hub.challenge)
    POST /webhook   message delivery; validates X-Hub-Signature-256
    GET  /health    Render health check

Commands (send on WhatsApp):
    <number>         -> tailored application tips for that match (read the number
                        from the latest notification or from 'list')
    list             -> numbered list of your recent matches
    status           -> summary counts + pause state
    applied <n>      -> mark match #N as applied
    pause / resume   -> mute / unmute notifications (cron respects this)
    help             -> this help

Outbound replies are free-form (type=text), which is fine because the user just
messaged us (the 24-hour session window is open).

Config (env): SUPABASE_URL, SUPABASE_KEY, GROQ_API_KEY (+GROQ_MODEL),
WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID, RECIPIENT_PHONE (optional guard),
WHATSAPP_VERIFY_TOKEN, WHATSAPP_APP_SECRET (recommended: signature check).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re

from fastapi import FastAPI, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from app.db import get_client
from app.prompts import TipsError, generate_tailoring_tips
from app.whatsapp_client import (
    WhatsAppClient,
    WhatsAppError,
    client_from_env,
    normalize_phone,
)

app = FastAPI(title="Job-Hunt Agent webhook")

VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN", "").strip()
APP_SECRET = os.environ.get("WHATSAPP_APP_SECRET", "").strip()
RECIPIENT = (
    os.environ.get("RECIPIENT_PHONE", "") or os.environ.get("WHATSAPP_TO", "")
).strip()
BACKEND_WA_MAX_BATCH = 8  # mirrors notify.MAX_JOBS_PER_MESSAGE (last-batch size)

HELP_TEXT = (
    "🤖 Job-Hunt Agent commands:\n"
    "• 'list' – view your recent matches\n"
    "• 'status' – summary counts and pause state\n"
    "• a number (e.g. '1') – tailored tips for that match\n"
    "• 'applied 1' – mark match #1 as applied\n"
    "• 'pause' / 'resume' – mute / unmute notifications"
)


# --------------------------------------------------------------------------
# Webhook entrypoints
# --------------------------------------------------------------------------
@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/webhook")
def verify_webhook(
    mode: str = Query("", alias="hub.mode"),
    token: str = Query("", alias="hub.verify_token"),
    challenge: str = Query("", alias="hub.challenge"),
) -> PlainTextResponse:
    if mode == "subscribe" and token and token == VERIFY_TOKEN:
        return PlainTextResponse(challenge)
    raise HTTPException(status_code=403, detail="Webhook verification failed.")


@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks) -> dict:
    raw = await request.body()

    if APP_SECRET and not _valid_signature(APP_SECRET, request.headers.get("X-Hub-Signature-256", ""), raw):
        print("[webhook] rejected: bad X-Hub-Signature-256", flush=True)
        raise HTTPException(status_code=403, detail="Bad signature.")

    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid JSON body.")

    messages = _extract_messages(payload)
    if not messages:
        return {"status": "ok", "messages": 0}

    client = get_client()
    for sender, text, msg_id in messages:
        try:
            client.table("whatsapp_log").insert({
                "direction": "in",
                "message_text": text,
                "related_job_id": None,
            }).execute()
        except Exception as exc:  # log failures must not break the flow
            print(f"[webhook] failed to log inbound: {exc}", flush=True)
        background_tasks.add_task(_handle_message, sender, text, msg_id)
    print(f"[webhook] queued {len(messages)} inbound message(s)", flush=True)
    return {"status": "ok", "messages": len(messages)}


# --------------------------------------------------------------------------
# Background handling
# --------------------------------------------------------------------------
def _handle_message(sender: str, text: str, msg_id: str) -> None:
    """Process one text message and reply (runs in a worker thread)."""
    try:
        sender = normalize_phone(sender)
    except WhatsAppError:
        print(f"[webhook] could not normalize sender {sender!r}; ignoring", flush=True)
        return

    if RECIPIENT and sender != normalize_phone(RECIPIENT):
        print(f"[webhook] message from unknown sender {sender}; ignoring", flush=True)
        return

    body = (text or "").strip()
    print(f"[webhook] < {sender}: {body[:120]!r} (msg {msg_id})", flush=True)

    if not body:
        return

    try:
        reply, related_job_id = _process_command(sender, body)
    except Exception as exc:  # never crash the worker; reply with a friendly error
        print(f"[webhook] error processing {body!r}: {exc}", flush=True)
        reply = "⚠️ Sorry, something went wrong. Try 'help' or try again in a minute."
        related_job_id = None

    if not reply:
        return

    try:
        wa = client_from_env()
        wa.send_text_message(sender, reply)
    except WhatsAppError as exc:
        print(f"[webhook] reply send failed: {exc}", flush=True)
        return

    try:
        client = get_client()
        client.table("whatsapp_log").insert({
            "direction": "out",
            "message_text": reply,
            "related_job_id": related_job_id,
        }).execute()
    except Exception as exc:
        print(f"[webhook] failed to log outbound: {exc}", flush=True)
    print(f"[webhook] > replied ({len(reply)} chars)", flush=True)


# --------------------------------------------------------------------------
# Command dispatch
# --------------------------------------------------------------------------
def _process_command(sender: str, body: str) -> tuple[str | None, int | None]:
    """Return (reply body or None, related job id or None)."""
    cmd = body.lower().strip()

    applied = re.fullmatch(r"applied\s+(\d+)", cmd)
    if applied:
        return _applied(int(applied.group(1)))

    if cmd in ("list", "l"):
        return _list(), None

    if cmd in ("status", "s"):
        return _status(), None

    if cmd in ("pause", "mute", "stop"):
        return _set_pause(False), None

    if cmd in ("resume", "unmute", "unpause"):
        return _set_pause(True), None

    if cmd in ("help", "h", "?", "menu"):
        return HELP_TEXT, None

    number = re.fullmatch(r"(\d+)", cmd)
    if number:
        return _tips(int(number.group(1)))

    return (
        "🤖 Didn't understand that. Commands: 'list', 'status', a match number "
        "for tailored tips, 'applied <n>', 'pause'/'resume'. Or 'help'.",
        None,
    )


# --------------------------------------------------------------------------
# Command implementations
# --------------------------------------------------------------------------
def _numbered_matches(client, limit: int = 8) -> list[tuple[int, dict, dict]]:
    """Resolve the numbering from the MOST RECENT notification.

    notify.py numbers each sent batch 1..N ordered by score (desc); the last
    message the user saw is the last batch of the most recent send (the tail of
    the score-desc group, capped at `BACKEND_WA_MAX_BATCH`). Return
    [(number, match, job)] matching that numbering.
    """
    rows = (
        client.table("matches")
        .select("id, job_id, match_score, status, sent_at")
        .in_("status", ("sent", "applied"))
        .not_.is_("sent_at", "null")
        .order("sent_at", desc=True)
        .limit(100)
        .execute()
        .data
    )
    if not rows:
        return []

    by_sent_at: dict[str, list[dict]] = {}
    for r in rows:
        by_sent_at.setdefault(r["sent_at"], []).append(r)
    latest = max(by_sent_at)
    group = by_sent_at[latest]
    group.sort(key=lambda r: (-int(r.get("match_score") or 0), -int(r["id"])))
    tail = group[-limit:] if len(group) > limit else group

    jobs: dict[int, dict] = {}
    ids = [r["job_id"] for r in tail]
    for row in (client.table("jobs_seen")
                .select("id, title, company, url, description")
                .in_("id", tuple(ids))
                .execute().data):
        jobs[row["id"]] = row
    return [(i, m, jobs.get(m["job_id"], {})) for i, m in enumerate(tail, 1)]


def _tips(number: int) -> tuple[str | None, int | None]:
    if number < 1:
        return "Please send a match number from the list (e.g. '1').", None
    client = get_client()
    numbered = _numbered_matches(client)
    entry = next((e for e in numbered if e[0] == number), None)
    if not entry:
        return (
            f"Sorry, match #{number} is not in your latest notification. "
            "Send 'list' to see your current matches.", None)

    _, match, job = entry
    if not job:
        return f"Could not load details for match #{number}.", None

    resume_rows = (client.table("resume")
                   .select("raw_text").order("uploaded_at", desc=True).limit(1)
                   .execute().data)
    if not resume_rows:
        return "No resume found - ingest your resume first.", None

    try:
        tips = generate_tailoring_tips(
            resume_rows[0]["raw_text"], job.get("description") or "")
    except TipsError as exc:
        return f"⚠️ Could not generate tips right now: {exc}", None

    try:
        (client.table("matches")
         .update({"tailoring_tips": tips})
         .eq("id", match["id"]).execute())
    except Exception as exc:
        print(f"[webhook] tips save failed: {exc}", flush=True)

    title = job.get("title") or "(untitled)"
    company = job.get("company") or "Unknown"
    score = int(match.get("match_score") or 0)
    lines = [
        f"📋 {company} — {title}",
        "",
        f"Why {score}%: {match.get('reasoning') or 'see score.'}",
        "",
        "Suggested tweaks for this application:",
        tips,
    ]
    if job.get("url"):
        lines += ["", f"Apply here: {job['url']}"]
    return "\n".join(lines), match.get("job_id")


def _applied(number: int) -> tuple[str | None, int | None]:
    client = get_client()
    numbered = _numbered_matches(client)
    entry = next((e for e in numbered if e[0] == number), None)
    if not entry:
        return (
            f"Sorry, match #{number} is not in your latest notification. "
            "Send 'list' to see your current matches.", None)

    _, match, job = entry
    (client.table("matches")
     .update({"status": "applied"})
     .eq("id", match["id"]).execute())
    title = job.get("title") or "(untitled)"
    company = job.get("company") or "Unknown"
    return f"✅ Marked '{title} @ {company}' as applied. Good luck! 🤞", match.get("job_id")


def _list() -> str:
    client = get_client()
    rows = (
        client.table("matches")
        .select("id, job_id, match_score, status")
        .not_.is_("sent_at", "null")
        .order("sent_at", desc=True)
        .limit(12)
        .execute()
        .data
    )
    if not rows:
        return "No matches notified yet. New high-score matches will be sent when the cron runs."

    jobs: dict[int, dict] = {}
    ids = [r["job_id"] for r in rows]
    for row in (client.table("jobs_seen")
                .select("id, title, company")
                .in_("id", tuple(ids)).execute().data):
        jobs[row["id"]] = row

    lines = ["📋 Your recent matches:"]
    for i, m in enumerate(rows, 1):
        job = jobs.get(m["job_id"], {})
        title = job.get("title") or "(untitled)"
        company = job.get("company") or "Unknown"
        score = int(m.get("match_score") or 0)
        mark = "✓" if m["status"] == "applied" else "·"
        lines.append(f"{i}. {title} @ {company} — {score}% {mark}")
    lines.append("")
    lines.append("Reply with a number for tailored tips, or 'applied N' once you apply.")
    return "\n".join(lines)


def _status() -> str:
    client = get_client()
    prefs = None
    try:
        rows = client.table("preferences").select("*").eq("id", 1).execute().data
        prefs = rows[0] if rows else None
    except Exception as exc:
        print(f"[webhook] prefs read failed: {exc}", flush=True)

    threshold = int((prefs or {}).get("min_match_threshold") or 60)
    paused = not bool((prefs or {}).get("notifications_enabled", True))

    counts = {"total": None, "new": 0, "pending": 0, "sent": 0, "applied": 0, "dismissed": 0}
    try:
        counts["total"] = client.table("jobs_seen").select("id").execute().data
        for r in client.table("matches").select("status, match_score").execute().data:
            status = r["status"]
            if status in counts:
                counts[status] += 1
            if status == "new" and int(r.get("match_score") or 0) >= threshold:
                counts["pending"] += 1
    except Exception as exc:
        print(f"[webhook] status counts failed: {exc}", flush=True)

    lines = [
        "📊 Job-hunt status",
        f"• Jobs in feed: {len(counts['total']) if counts['total'] is not None else 'n/a'}",
        f"• New matches above {threshold}% not yet sent: {counts['pending']}",
        f"• Sent to WhatsApp: {counts['sent']}",
        f"• Applied: {counts['applied']}",
        f"• Dismissed: {counts['dismissed']}",
        f"• Notifications: {'PAUSED' if paused else 'ON'}",
        "",
        "Automated check runs every 6 hours.",
    ]
    return "\n".join(lines)


def _set_pause(enabled: bool) -> str:
    client = get_client()
    client.table("preferences").upsert(
        {"id": 1, "notifications_enabled": enabled},
        on_conflict="id").execute()
    if enabled:
        return "🔔 Notifications resumed. You'll keep getting them."
    return "🔕 Notifications paused. Cron runs will skip WhatsApp."


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _valid_signature(secret: str, provided: str, raw: bytes) -> bool:
    if not provided:
        return False
    try:
        digest = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(f"sha256={digest}", provided)


def _extract_messages(payload: dict) -> list[tuple[str, str, str]]:
    """Pull (from, text, id) for inbound text messages from the webhook payload."""
    found: list[tuple[str, str, str]] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for msg in value.get("messages", []):
                if msg.get("type") != "text":
                    continue
                text = ((msg.get("text") or {}).get("body") or "").strip()
                if not text:
                    continue
                found.append((msg.get("from", ""), text, msg.get("id", "")))
    return found


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("app.webhook_app:app", host="0.0.0.0", port=port)