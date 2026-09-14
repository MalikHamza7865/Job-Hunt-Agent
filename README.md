# AI Job-Hunt Agent (WhatsApp-based)

An automated job-hunting agent for a backend/AI developer. It periodically
fetches new job postings (Adzuna by default, Indeed behind an official OAuth +
GraphQL integration), compares each one against your resume with an LLM, scores
the match, and sends high-match jobs to your phone over WhatsApp with tailored
application tips. You can reply on WhatsApp with simple commands to get more
detail, mark jobs as applied, or check status.

The system does **not** need a server running 24/7. It runs as a scheduled job
(GitHub Actions cron) that wakes up, checks for new postings, processes them,
sends WhatsApp messages, then exits. A lightweight webhook endpooint (Render
free tier) receives your WhatsApp replies.

---

## Problem statement

Manually checking job boards every few hours for roles that actually fit a
narrow skillset is tedious and noisy. This project automates the pipeline:

```
Job source  ->  found matching jobs  ->  WhatsApp notification  ->  reply to refine
```

...with LLM-based scoring so only roles above a configurable match threshold
reach your phone, plus per-role tailoring tips generated only when you ask.

---

## Architecture

```
                        ┌─────────────────────────────────────────────┐
                        │              GitHub Actions (cron)          │
                        │                every 6 hours                │
                        └─────────────────────────────────────────────┘
           ┌──────────────────────┬───────────────────────┬───────────┘
           │                      │                       │
           v                      v                       v
   ┌──────────────┐      ┌───────────────┐       ┌──────────────────┐
   │ fetch_jobs.py│      │  match_job.py │       │     notify.py    │
   │  (Indeed GraphQL,│      │  (Groq/LLM    │       │  (WhatsApp Cloud │
   │   dedupes)   │      │   scoring)    │       │   API - outbound)│
   └──────┬───────┘      └──────┬────────┘       └────────┬─────────┘
          │                     │                         │
          v                     v                         v
     ┌────────────────┐   ┌──────────────┐        ┌─────────────┐
     │  Supabase      │   │              │        │  WhatsApp   │
     │  (jobs_seen,   │◄──┤   matches +  │◄───────┤  Cloud API  │
     │   resume,      │   │   resume     │        │             │
     │   preferences) │   │              │        └──────┬──────┘
     └────────────────┘   └──────────────┘               │
                                                         v   replies ("2",
                          ┌──────────────────────────────┴──────┐   "status")
                          │  Render (FastAPI webhook_app.py)    │
                          │  POST endpoint, WhatsApp verification│
                          └──────────────────────────────────────┘
```

- **Job source**: **Adzuna free API** by default (official, no partner approval,
  no scraping) — see "Why Adzuna?" below. An **Indeed** backend is also built in
  (`--source indeed`): it authenticates with Indeed's official OAuth server and
  calls `https://apis.indeed.com/graphql`, but Indeed only grants live job data
  to **partner-program** apps (their `job-retrieval-service`), so it is usable
  once you obtain partner access.
- **LLM**: Groq free tier (OpenAI-compatible endpoint) for match scoring on every
  new job. Tailoring tips are generated only on demand (cheaper).
- **Database**: Supabase (Postgres, free tier).
- **Scheduler**: GitHub Actions cron (`.github/workflows/job_check.yml`).
- **Webhook (replies only)**: FastAPI app on Render free tier.

---

## Tech stack

| Layer        | Choice                          |
|--------------|---------------------------------|
| Language     | Python 3.11+                    |
| Job source 1 | Adzuna free API (default)       |
| Job source 2 | Indeed OAuth + GraphQL (partner-gated, optional) |
| LLM          | Groq free tier (OpenAI-compatible chat) |
| Database     | Supabase (Postgres)             |
| Messaging    | WhatsApp Cloud API (Meta)       |
| Scheduler    | GitHub Actions (cron)           |
| Webhook host | Render free tier (FastAPI)      |

---

## Repository layout

```
job-hunt-agent/
├── .github/workflows/job_check.yml   # cron: fetch -> match -> notify
├── app/
│   ├── db.py                # Supabase client setup (reads .env)
│   ├── ingest_resume.py     # PDF/text -> resume table
│   ├── set_preferences.py   # keywords / locations / threshold
│   ├── adzuna_client.py     # Adzuna search API + normalization (default source)
│   ├── probe_adzuna.py      # manual CLI probe: what/where/country -> results
│   ├── indeed_client.py     # Indeed OAuth + official GraphQL + normalization
│   ├── auth_indeed.py       # ONE-TIME interactive Indeed OAuth login
│   ├── fetch_jobs.py        # fetch (adzuna|indeed|sample) + dedupe into jobs_seen
│   ├── match_job.py         # Groq-powered score_match() + batch runner (-M3)
│   ├── notify.py            # WhatsApp outbound + status updates
│   ├── webhook_app.py       # FastAPI endpoint for WhatsApp replies
│   ├── whatsapp_client.py   # send/receive helpers
│   └── prompts.py           # LLM prompt templates
├── schema.sql               # Supabase tables (run in SQL Editor)
├── sample_jobs.json         # offline demo fixtures for dedupe testing
├── requirements.txt
├── .env.example
└── README.md
```

---

## Setup

### 1. Prerequisites

- Python 3.11+
- A free [Supabase](https://supabase.com) project
- A free [Groq](https://console.groq.com) API key
- A free [Adzuna](https://developer.adzuna.com) API key (app_id + app_key)
- Meta WhatsApp Cloud API app + a business or sandbox number
- *(Indeed source only)* an Indeed account + partner-program access

### 2. Install

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                 # then fill in real values
```

### 3. Database

Open your Supabase project → **SQL Editor** → paste the contents of
`schema.sql` → run. This creates `resume`, `preferences`, `jobs_seen`,
`matches`, and `whatsapp_log`. Scripts connect with the **service_role** key,
which bypasses Row Level Security, so no RLS policies are required.

### 4. Environment variables

See `.env.example`. The essentials:

```
SUPABASE_URL=                      # https://<project>.supabase.co
SUPABASE_KEY=                      # Project Settings -> API -> service_role
GROQ_API_KEY=
GROQ_MODEL=qwen/qwen3.8-27b
FETCH_SOURCE=adzuna               # adzuna (default) | indeed | sample
ADZUNA_APP_ID=
ADZUNA_APP_KEY=
ADZUNA_COUNTRY=us
WHATSAPP_TOKEN=
WHATSAPP_PHONE_NUMBER_ID=
WHATSAPP_VERIFY_TOKEN=
RECIPIENT_PHONE=                   # your phone, E.164: 15551234567
INDEED_TOKEN_FILE=.indeed_tokens.json   # only needed if FETCH_SOURCE=indeed
```

### 5. Adzuna keys

Get free credentials at https://developer.adzuna.com → **Applications** → create
an app → copy `app_id` and `app_key` into `.env` (`ADZUNA_APP_ID`,
`ADZUNA_APP_KEY`). Adzuna is the default live source: official API, no partner
approval, no scraping. No login/OAuth is required for Adzuna — keys only.

**Location semantics** (important — the API does *not* error on foreign
locations, it silently searches the whole country instead):

- `ADZUNA_COUNTRY` is validated against the real feed list (`us gb ca de fr nl
  ...`); anything else falls back to `us` with a warning.
- `where` is only sent when it can exist inside that country's feed. Locations
  like `lahore`/`karachi`/`pakistan` (no Pakistan board on Adzuna) are dropped
  — the client prints `Dropping 'where' -> nationwide search` and searches the
  whole country.
- `remote` / `anywhere` / `wfh` locations switch to **remote mode**:
  `where` is dropped and `what_and=remote` is added (Adzuna has no exact remote
  filter — `remote_1`/`remote_2` return HTTP 400 — so this is an approximate
  title/description match, logged as such).
- Results are sorted by `sort_by=date` so each cron run starts from the freshest
  postings.

Turn on `ADZUNA_VERBOSE=1` (or pass `--verbose`) to print the exact request URL,
params, and response count for every query — useful for debugging like the
U-turn above.

### 6. One-time Indeed authorization (optional — partner-gated)

> **Why Adzuna?** Indeed's official job data requires the **partner program**:
> tokens are only accepted for clients with access to Indeed's
> `job-retrieval-service` (confirmed 2026-09-13 with the MCP gateway returning
> `invalid_client / Client not allowed` and GraphQL returning `Client is not
> authorized.`; identical symptom upstream at anthropics/claude-code#60940 and
> #47185). HTML scraping is Cloudflare-403'd from datacenter/CI IPs and is
> excluded by this project's no-scraping constraint. Adzuna provides live,
> real, official job data on the free tier instead.

The `--source indeed` backend is implemented and ready; it just needs an
**approved Indeed partner app** to serve data. To use it once you have access:

```bash
python app/auth_indeed.py
```

- Starts a local callback server on `http://127.0.0.1:19876/callback`
- Opens Indeed's sign-in page in your browser
- After you authorize, exchanges the code for tokens (OAuth `refresh_token`
  grant, `offline_access`) and saves them to `INDEED_TOKEN_FILE` (default
  `.indeed_tokens.json`)

In CI, store the token file's contents as a secret and restore it at the start
of the workflow.

---

## Usage

### Ingest your resume

```bash
python app/ingest_resume.py Resume-MUHAMMAD-HAMZA-Petra-Brands.pdf
```

Extracts text (pypdf), detects a starter set of skills, and stores it in the
`resume` table (skills stored as JSON in `parsed_skills`).

### Set preferences

```bash
python app/set_preferences.py --keywords python,fastapi --locations "lahore,remote" --threshold 60
python app/set_preferences.py        # interactive mode
python app/set_preferences.py --pause   # mute notifications (webhook toggles this)
python app/set_preferences.py --resume
```

### Fetch + dedupe jobs

```bash
# live Adzuna API (default; needs ADZUNA_APP_ID/ADZUNA_APP_KEY)
python app/fetch_jobs.py

# explicit source choice / country override / verbose (prints request URL)
python app/fetch_jobs.py --source adzuna --country us --verbose
python app/fetch_jobs.py --source indeed            # needs partner access (step 6)

# manual Adzuna probe - confirm a query returns sensible jobs before relying on it
python app/probe_adzuna.py --what python --where birmingham --country gb
python app/probe_adzuna.py --what fastapi --where remote

# offline demo — validates dedupe without any live API call
python app/fetch_jobs.py --source sample --sample-data sample_jobs.json
python app/fetch_jobs.py --source sample --sample-data sample_jobs.json   # 2nd run => "No new jobs"
```

Run twice in a row: the second run reports `new to insert: 0` — postings are
deduped on `jobs_seen.external_job_id` (prefixed `adz-<id>` for Adzuna) using a
uniqueness constraint in Supabase.

### Match (Milestone 3)

```bash
python app/match_job.py                # score every job with no match yet
python app/match_job.py --limit 20     # cap how many jobs are scored per run
```

`score_match(resume_text, job_description)` calls Groq (`GROQ_MODEL`, default
`qwen/qwen3.8-27b`, a free-tier model — Llama 3.x moved to Enterprise) and
returns `{"score": 0-100, "missing_skills": [...], "reasoning": "..."}`. The
batch runner reads the newest resume from `resume`, scores only jobs with **no**
row in `matches`, and inserts `status='new'` rows into `matches`. (Free-tier
rate limits: 8K tokens/min, 200K tokens/day — pace with `--limit`.)

### Notify (Milestone 4)

```bash
python app/notify.py                # send WhatsApp for new matches >= threshold
python app/notify.py --dry-run      # preview the message(s) without sending
```

Reads `preferences` (`min_match_threshold`, `notifications_enabled` pause flag),
takes `matches` with status='new' and `match_score >= threshold`, sends a
numbered WhatsApp update (title, company, score, missing skills, link), then
marks those matches status='sent' (with `sent_at`) and logs the outbound
message in `whatsapp_log`. Batches keep under WhatsApp's 4096-char limit.
Recipient numbers are auto-converted to E.164 (`03392030660` → `+923392030660`);
the free Meta sandbox number can only message **up to 5 approved recipient
numbers** added in the Meta App Dashboard (error `131030` means the number is
not whitelisted yet).

**24-hour session window:** WhatsApp only allows *free-form* (type=text)
messages while the recipient has messaged the business within the last 24 hours
(errors `131049`/`131026`/`131007`). Outside that window only an **approved
message template** can reach the user, so `notify.py` has a fallback: if the
free-form send fails with a session-window error and `WHATSAPP_TEMPLATE_NAME` is
set, it sends a short template message (count + top match title + score) instead
and still marks the matches `sent`.

### Schedule (Milestone 5 — GitHub Actions cron)

The workflow `.github/workflows/job_check.yml` runs **every 6 hours** and executes
`fetch_jobs.py` → `match_job.py --limit 30` → `notify.py`. It is guarded by a
`concurrency` group (runs never overlap) and a 20-minute timeout.

To enable it:

1. Push this repo to GitHub.
2. Add these **repository Secrets** (Settings → Secrets and variables →
   Actions): `SUPABASE_URL`, `SUPABASE_KEY`, `GROQ_API_KEY`, `ADZUNA_APP_ID`,
   `ADZUNA_APP_KEY`, `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`,
   `RECIPIENT_PHONE`. `GROQ_MODEL` and `ADZUNA_COUNTRY=gb` are already set in
   the workflow as non-secret env vars (override by editing the file).
3. (Recommended) Approve a **message template** in Meta (WhatsApp → Message
   templates → Create) so notifications survive the 24-hour window; then set the
   `WHATSAPP_TEMPLATE_NAME` secret (and uncomment it in the workflow) — see
   `.env.example`. Suggested body:

   ```
   New job matches ready: {{1}} new match(es). Top pick: {{2}} ({{3}}% match).
   Reply in WhatsApp for tailoring tips.
   ```

4. Run once with **workflow_dispatch** (Actions → job-hunt-agent → Run workflow)
   to verify end-to-end before relying on the schedule.

---

## WhatsApp message formats

**Notification (outbound, batched, within 24h window):**

```
🎯 2 new matches found (last 6 hours)

1. Backend Developer @ TechNova Labs — 82% match
   Missing: kubernetes, terraform
   🔗 https://www.indeed.com/viewjob?jk=...

Reply with a number for tailored resume tips.
```

**Notification (template fallback, outside the 24h window):**

```
New job matches ready: 2 new match(es). Top pick: Backend Developer (82% match).
Reply in WhatsApp for tailoring tips.
```

**On-demand detail (reply to a number):**

```
📋 TechNova Labs — Backend Developer

Why 82%: Strong FastAPI/PostgreSQL overlap; missing infra skills.

Suggested tweaks for this application:
- Lead with the hintmint.io launch and async architecture work.
- Add a short "Tauri/Rust" section since the JD mentions desktop tooling.

Apply here: https://www.indeed.com/viewjob?jk=...
```

---

## LinkedIn MCP — evaluation (decision: not integrated)

A LinkedIn MCP server is configured but has **not** been wired into this
project. Verdict: **do not integrate**.

| Question | Answer |
|----------|--------|
| Official LinkedIn-operated, or third-party? | **Third-party.** It is an Apify actor `shahidirfan/linkedin-jobs-mcp-server` by an Apify community author, served through Apify's hosted MCP gateway (`https://mcp.apify.com/`), authenticated with an Apify API token. |
| How does it authenticate? | Via the **Apify platform** (Bearer `APIFY_TOKEN`), which runs remote scrapers. It does not use an official LinkedIn OAuth/API integration. |
| What does it actually do? | Runs LinkedIn **web scrapers** (e.g. the bundled `Fast-LinkedIn-job-Scraper` actor) in the cloud to pull listings. |
| Mentions rate limits / ToS / account risk? | The actor README mentions a rate limit (30 req/s) and pay-per-use pricing, but **no disclaimer about LinkedIn's Terms of Service or account-ban risk**. |
| Risk | LinkedIn's User Agreement prohibits automated scraping; the project brief also bans scraping. Any public portfolio that visibly scrapes LinkedIn invites account/ToS issues. |

If you later want LinkedIn data you can stand behind in a public portfolio, the
safe route is a licensed/official data provider (e.g. an approved API or a
partner-style data agreement) — not a session-scraper MCP.

---

## Constraints honored

- **No scraping** — live data comes from the official Adzuna API (default) and,
  when partner access exists, Indeed's official OAuth + GraphQL API.
- LLM calls kept minimal: matching runs on every new job with a short prompt;
  tailoring tips generate only on demand.
- No persistent server except the small reply webhook (Render free tier).

---

## Roadmap / milestone status

| # | Milestone                              | Status            |
|---|----------------------------------------|-------------------|
| 1 | Project scaffold + resume ingestion    | ✅ Done           |
| 2 | Job fetch + dedupe (Adzuna live default, Indeed GraphQL backend) | ✅ Done (revised ×2) |
| 3 | LLM matching engine (Groq)             | ✅ Done           |
| 4 | WhatsApp notifications (outbound + template fallback) | ✅ Done |
| 5 | GitHub Actions cron (every 6h)         | ✅ Done (enable: push repo + add secrets) |
| 6 | Webhook for replies (Render)           | ⬜ Pending        |
| 7 | Polish for portfolio (README/GIF/demo) | ⬜ Pending        |

---

## License

Private/portfolio project — see repository owner.