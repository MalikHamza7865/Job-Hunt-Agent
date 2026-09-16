"""LLM prompt templates (Milestone 6: on-demand tailoring tips).

Kept separate from the webhook so the Groq prompt/retry logic can be tested
and reused without importing FastAPI. `generate_tailoring_tips` is called by
the webhook when the user replies with a match number.
"""
from __future__ import annotations

import os
import time

from app.match_job import _is_retryable

MODEL = os.environ.get("GROQ_MODEL", "qwen/qwen3.8-27b")
MAX_ATTEMPTS = 4

SYSTEM_PROMPT = (
    "You are a senior career coach helping a backend/AI developer write "
    "applications that stand out. Read the candidate's resume and the exact job "
    "description, then produce SHORT, concrete, application-specific tips. "
    "Return only the tips as 3-6 bullet points (each prefixed with '-'), up to "
    "250 words total. No preamble, no markdown fences. Every tip must reference "
    "something real in the resume or the specific job description (skills, "
    "projects, company needs). Do not invent experience."
)


def build_tailoring_prompt(resume_text: str, job_description: str) -> str:
    """Build the user message: resume first, then the job description."""
    resume_slice = (resume_text or "")[:12000]
    job_slice = (job_description or "")[:12000]
    return (
        f"RESUME (candidate):\n{resume_slice}\n\n"
        f"JOB DESCRIPTION (target role):\n{job_slice}"
    )


class TipsError(RuntimeError):
    """Raised when Groq fails or returns an unusable response for tips."""


def generate_tailoring_tips(
    resume_text: str,
    job_description: str,
    model: str | None = None,
    timeout: float = 60.0,
) -> str:
    """Return tailored application tips (3-6 bullets) for one job."""
    from groq import Groq

    model = model or MODEL
    client = Groq(api_key=os.environ.get("GROQ_API_KEY", "") or None,
                  timeout=timeout)
    user_prompt = build_tailoring_prompt(resume_text, job_description)

    last_exc: Exception | None = None
    resp = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            resp = client.chat.completions.create(
                model=model,
                temperature=0.3,
                max_tokens=600,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
            )
            break
        except Exception as exc:  # network, auth, rate limit
            last_exc = exc
            if not _is_retryable(exc) or attempt == MAX_ATTEMPTS - 1:
                raise TipsError(f"Groq API error: {exc}") from exc
            time.sleep(min(2 ** attempt, 20))

    if resp is None:
        raise last_exc  # type: ignore[misc]

    tips = (resp.choices[0].message.content or "").strip()
    if not tips:
        raise TipsError("Groq returned empty tailoring tips.")
    return tips