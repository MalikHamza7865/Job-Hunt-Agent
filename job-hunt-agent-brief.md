# Project: AI Job-Hunt Agent (WhatsApp-based, Indeed-powered)

## Overview

Build an automated job-hunting agent for me (a backend/AI developer). It periodically fetches new job postings from Indeed, compares each posting against my resume using an LLM, scores the match, and sends me high-match jobs over WhatsApp with tailored application tips. I can reply on WhatsApp with simple commands to get more detail, mark jobs as applied, or check status.

The system should NOT require a server running 24/7. It runs as a scheduled job (GitHub Actions cron) that wakes up, checks for new postings, processes them, and sends WhatsApp messages, then exits. A lightweight webhook endpoint (hosted on Render free tier) is needed only to receive my WhatsApp replies.

## Tech stack

- **Language:** Python 3.11+
- **Job source:** Indeed API (official)
- **LLM:** groq free tier for matching + tailoring suggestions
- **Database:** Supabase (Postgres, free tier)
- **Messaging:** WhatsApp Cloud API (Meta)
- **Scheduler:** GitHub Actions (cron workflow)
- **Webhook host (for replies only):** Render free tier, FastAPI app
- **Resume input:** PDF or plain text, parsed once at setup

## Data model (Postgres tables)

1. `resume` — id, raw_text, parsed_skills (JSON), uploaded_at
2. `preferences` — id, keywords (array), locations (array), min_match_threshold (default 60), salary_min (nullable)
3. `jobs_seen` — id, external_job_id, title, company, location, description, url, fetched_at
4. `matches` — id, job_id (FK), match_score, missing_skills (JSON), tailoring_tips (text, nullable — generated on demand), status (enum: new, sent, applied, dismissed), sent_at
5. `whatsapp_log` — id, direction (in/out), message_text, related_job_id (nullable), timestamp

## Milestones

### Milestone 1 — Project scaffold + resume ingestion
- Set up Python project structure (see below), virtualenv, requirements.txt
- Set up Supabase project, create tables from data model above, write migration/schema SQL
- Build a simple CLI script `ingest_resume.py` that takes a PDF or text file, extracts text (use `pypdf` for PDF), stores it in the `resume` table
- Build a CLI script `set_preferences.py` to set keywords/locations/threshold in `preferences` table
- **Done when:** I can run `python ingest_resume.py my_resume.pdf` and `python set_preferences.py` and see rows in Supabase

### Milestone 2 — Indeed fetch + dedupe
- Write `fetch_jobs.py`: calls Indeed API using keywords/locations from `preferences` table
- For each result, check `jobs_seen` table by `external_job_id` — skip if already seen, insert if new
- Log how many new jobs were found each run
- **Done when:** running the script twice in a row shows 0 new jobs the second time (dedupe works)

### Milestone 3 — LLM matching engine
- Write `match_job.py` (importable function `score_match(resume_text, job_description) -> dict`)
- Prompt Claude to return structured JSON: `{ "score": 0-100, "missing_skills": [...], "reasoning": "short string" }`
- For every new job from Milestone 2, run this function, insert into `matches` table with status `new`
- Only proceed to notification step (Milestone 4) for matches >= `min_match_threshold`
- **Done when:** given a sample resume + job description, the function reliably returns valid JSON with a sensible score

### Milestone 4 — WhatsApp notifications (outbound)
- Set up Meta WhatsApp Cloud API sandbox/business number
- Write `notify.py`: for every unsent match above threshold, format a message (job title, company, score, missing skills, link) and send via WhatsApp Cloud API
- Update `matches.status` to `sent` and `sent_at` after successful send
- Batch multiple matches into one message if more than one in a run (see message format in "WhatsApp message formats" section below)
- **Done when:** I receive a real WhatsApp message on my phone listing new matches

### Milestone 5 — GitHub Actions scheduling
- Write `.github/workflows/job_check.yml` — cron schedule every 6 hours
- Workflow steps: checkout repo, install deps, run `fetch_jobs.py` → `match_job.py` (as a batch step) → `notify.py`
- Store all secrets (Supabase URL/key, Anthropic API key, WhatsApp token) as GitHub Actions secrets — never hardcoded
- **Done when:** the workflow runs successfully on schedule without my laptop being open, and I get a WhatsApp message from an automated run

### Milestone 6 — Webhook for replies (Render deployment)
- Build a small FastAPI app `webhook_app.py` with one POST endpoint for WhatsApp Cloud API webhook verification + incoming messages
- Handle commands:
  - A number (e.g. "2") → look up the corresponding recent match, generate tailoring tips via Claude (only now, on demand, to save API cost), reply with detail
  - `"status"` → query `matches` table, reply with counts (checked / sent / applied this week)
  - `"list"` → reply with all `status = sent` matches not yet marked applied
  - `"applied <n>"` → update that match's status to `applied`
  - `"pause"` / `"resume"` → toggle a flag in `preferences` that `notify.py` checks before sending
- Deploy this app to Render free tier
- Point the WhatsApp Cloud API webhook URL to the deployed Render endpoint
- **Done when:** replying "status" on WhatsApp returns a real answer from the deployed webhook

### Milestone 7 — Polish for portfolio
- Write a clean `README.md`: problem statement, architecture diagram (can be a simple ASCII or Mermaid diagram), tech stack, setup instructions, screenshots/GIF of a real WhatsApp exchange
- Add a `.env.example` file listing required environment variables without real values
- Add basic error handling (Indeed API failures, LLM timeout, WhatsApp send failures should not crash the whole run — log and continue)
- Record a 1-2 minute demo video/GIF showing a real notification and a real reply
- **Done when:** repo is public on GitHub with README, demo GIF, and live proof (my own WhatsApp using it for real)

## Suggested file structure

```
job-hunt-agent/
├── .github/workflows/job_check.yml
├── app/
│   ├── ingest_resume.py
│   ├── set_preferences.py
│   ├── fetch_jobs.py
│   ├── match_job.py
│   ├── notify.py
│   ├── webhook_app.py
│   ├── db.py              # Supabase client setup
│   ├── whatsapp_client.py # send/receive helpers
│   └── prompts.py         # LLM prompt templates
├── schema.sql
├── requirements.txt
├── .env.example
└── README.md
```

## WhatsApp message formats

**Notification (outbound, batched):**
```
🎯 <N> new matches found (last 6 hours)

1. <Title> @ <Company> — <score>% match
   Missing: <skill1>, <skill2>
   🔗 <url>

[... repeat ...]

Reply with a number for tailored resume tips.
```

**On-demand detail (reply to a number):**
```
📋 <Company> — <Title>

Why <score>%: <short reasoning>

Suggested tweaks for this application:
- <tip 1>
- <tip 2>

Apply here: <url>
```

## Environment variables needed

```
ANTHROPIC_API_KEY=
SUPABASE_URL=
SUPABASE_KEY=
INDEED_API_KEY=  (or however Indeed auth is provided)
WHATSAPP_TOKEN=
WHATSAPP_PHONE_NUMBER_ID=
WHATSAPP_VERIFY_TOKEN=
```

## Constraints / notes for the build

- No LinkedIn scraping anywhere in this project — Indeed only, via official API access.
- Keep LLM calls minimal: match scoring runs on every new job (cheap, short prompt), but tailored tips only generate on-demand when I reply with a number (saves cost).
- Everything must run without a persistent server except the small webhook (Milestone 6), which can sleep between replies on Render's free tier — that's acceptable.
- Prioritize getting Milestones 1-5 working end-to-end before starting Milestone 6 — a working notify-only version is already a demo-able project on its own.
