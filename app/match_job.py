#!/usr/bin/env python3
"""Groq-powered ML 3 match engine: score a resume against each new job.

Pipeline:
    python app/match_job.py                  # score all jobs with no match yet
    python app/match_job.py --limit 20       # cap jobs scored this run
    python app/match_job.py --model qwen/qwen3.8-27b

`score_match(resume_text, job_description)` -> dict
    {"score": int 0-100, "missing_skills": list[str], "reasoning": str}

Only jobs with NO existing row in `matches` are scored, and each result is
inserted into `matches` with status='new'. Notifications (Milestone 4) read
from `matches` and filter by the preference threshold.

Requires GROQ_API_KEY and GROQ_MODEL in .env (free tier at console.groq.com).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os

from app.db import get_client

MODEL = os.environ.get("GROQ_MODEL", "qwen/qwen3.8-27b")

SYSTEM_PROMPT = (
    "You are a technical recruiter matching a candidate's resume against a job "
    "description. Respond with ONLY a JSON object, no markdown fences, exactly "
    "in this shape: "
    '{"score": <int 0-100>, "missing_skills": [<str up to 10>], '
    '"reasoning": "<1-2 concise sentences>"}. '
    "score = overall fit of the candidate for this exact role. "
    "missing_skills = the most important skills/technologies the job requires "
    "that are absent or weak in the resume (leave the list empty if none). "
    "reasoning = what the score is based on."
)

MAX_ATTEMPTS = 4


def _is_retryable(exc: Exception) -> bool:
    text = str(exc)
    return ("429" in text or "rate" in text.lower()
            or "500" in text or "503" in text or "timed out" in text.lower())


def _coerce_score(value) -> int:
    try:
        score = int(round(float(value)))
    except (TypeError, ValueError):
        score = 0
    return max(0, min(100, score))


def _parse_json(text: str) -> dict:
    text = (text or "").strip()
    if not text:
        raise ValueError("Empty model response.")
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    raise ValueError(f"Could not parse model output as JSON: {text[:200]!r}")


class ScoreError(RuntimeError):
    """Raised when Groq fails or returns an unusable response for a job."""


def score_match(
    resume_text: str,
    job_description: str,
    model: str | None = None,
    timeout: float = 60.0,
) -> dict:
    """Score candidate fit. Resume text first = faster, cheaper scoring."""
    from groq import Groq

    model = model or MODEL
    client = Groq(api_key=os.environ.get("GROQ_API_KEY", "") or None,
                  timeout=timeout)
    resume_slice = (resume_text or "")[:12000]
    job_slice = (job_description or "")[:12000]
    user_prompt = (
        f"RESUME:\n{resume_slice}\n\nJOB DESCRIPTION:\n{job_slice}"
    )
    try:
        last_exc: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                resp = client.chat.completions.create(
                    model=model,
                    temperature=0.1,
                    max_tokens=400,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                )
                break
            except Exception as exc:  # network, auth, rate limit
                last_exc = exc
                if not _is_retryable(exc) or attempt == MAX_ATTEMPTS - 1:
                    raise
                time.sleep(min(2 ** attempt, 20))
        if resp is None:
            raise last_exc  # type: ignore[misc]
    except Exception as exc:  # network, auth, rate limit
        raise ScoreError(f"Groq API error: {exc}") from exc

    content = resp.choices[0].message.content or ""
    try:
        data = _parse_json(content)
    except ValueError as exc:
        raise ScoreError(str(exc)) from exc

    skills = data.get("missing_skills") or []
    if not isinstance(skills, list):
        skills = [str(skills)]
    skills = [str(s).strip() for s in skills if str(s).strip()][:10]

    return {
        "score": _coerce_score(data.get("score")),
        "missing_skills": skills,
        "reasoning": str(data.get("reasoning") or "").strip(),
    }


# --------------------------------------------------------------------------
# Batch runner (Milestone 3)
# --------------------------------------------------------------------------
def load_latest_resume(client) -> tuple[str, list[str]] | None:
    rows = (
        client.table("resume")
        .select("raw_text, parsed_skills")
        .order("uploaded_at", desc=True)
        .limit(1)
        .execute()
        .data
    )
    if not rows:
        return None
    skills = rows[0].get("parsed_skills") or []
    if not isinstance(skills, list):
        skills = []
    return rows[0].get("raw_text") or "", skills


def unmatched_jobs(client, limit: int) -> list[dict]:
    matched_rows = client.table("matches").select("job_id").execute().data
    matched_ids = {r["job_id"] for r in matched_rows}
    if not matched_ids:
        return (
            client.table("jobs_seen")
            .select("*")
            .order("fetched_at", desc=True)
            .limit(limit)
            .execute()
            .data
        )
    ids_literal = "(" + ",".join(str(i) for i in matched_ids) + ")"
    return (
        client.table("jobs_seen")
        .select("*")
        .filter("id", "not.in", ids_literal)
        .order("fetched_at", desc=True)
        .limit(limit)
        .execute()
        .data
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Score new (unmatched) jobs against the resume via Groq.")
    parser.add_argument("--limit", type=int, default=50,
                        help="Max jobs to score this run")
    parser.add_argument("--model", default=MODEL,
                        help="Groq model id (default from GROQ_MODEL)")
    args = parser.parse_args()

    client = get_client()
    resume = load_latest_resume(client)
    if resume is None:
        sys.exit("Error: no resume in DB. Run: python app/ingest_resume.py <file>")
    resume_text, _skills = resume

    jobs = unmatched_jobs(client, args.limit)
    print(f"Found {len(jobs)} job(s) with no match yet (limit={args.limit}).")

    scored = 0
    for job in jobs:
        title = job.get("title") or "(untitled)"
        print(f"  scoring: {title[:60]}", flush=True)
        try:
            result = score_match(resume_text, job.get("description") or "",
                                model=args.model)
        except ScoreError as exc:
            print(f"    ! skipped ({exc})")
            continue

        row = {
            "job_id": job["id"],
            "match_score": result["score"],
            "missing_skills": result["missing_skills"],
            "reasoning": result["reasoning"],
            "status": "new",
        }
        try:
            client.table("matches").insert(row).execute()
        except Exception as exc:
            print(f"    ! insert failed ({exc})")
            continue
        scored += 1
        print(f"    -> score={result['score']} "
              f"missing={result['missing_skills'] or []} | {result['reasoning']}")

    print(f"\nDone: scored & stored {scored} match(es) as status='new'.")


if __name__ == "__main__":
    main()