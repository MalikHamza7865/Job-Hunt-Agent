-- ============================================================
-- Job-Hunt Agent schema (run in Supabase -> SQL Editor)
-- Tables follow the data model in job-hunt-agent-brief.md
-- ============================================================

-- 1. Resume ----------------------------------------------------
-- There is effectively one active resume; insert a new row on
-- each ingestion and always read the most recent (max uploaded_at).
create table if not exists resume (
    id            bigserial primary key,
    raw_text      text not null,
    parsed_skills jsonb,
    uploaded_at   timestamptz not null default now()
);

-- 2. Preferences ------------------------------------------------
create table if not exists preferences (
    id                     bigserial primary key,
    keywords               text[] not null default '{}',
    locations              text[] not null default '{}',
    min_match_threshold    int not null default 60,
    salary_min             numeric,
    notifications_enabled  boolean not null default true
);

-- Seed a single defaults row so fetch/match scripts always have one
insert into preferences (id)
select 1
where not exists (select 1 from preferences where id = 1);

-- 3. Jobs seen (dedupe source) ----------------------------------
create table if not exists jobs_seen (
    id               bigserial primary key,
    external_job_id  text not null unique,
    title            text not null,
    company          text,
    location         text,
    description      text,
    url              text,
    fetched_at       timestamptz not null default now()
);

create index if not exists idx_jobs_seen_external_id on jobs_seen (external_job_id);

-- 4. Matches -----------------------------------------------------
create table if not exists matches (
    id                    bigserial primary key,
    job_id                bigint not null references jobs_seen (id) on delete cascade,
    match_score           int not null,
    missing_skills        jsonb,
    reasoning             text,        -- short why-the-score explanation
    tailoring_tips        text,        -- generated on demand (M6)
    status                text not null default 'new',
    sent_at               timestamptz,
    created_at            timestamptz not null default now(),
    constraint matches_status_ok check (status in ('new', 'sent', 'applied', 'dismissed'))
);

create index if not exists idx_matches_status on matches (status);
create index if not exists idx_matches_job on matches (job_id);

-- 5. WhatsApp log -----------------------------------------------
create table if not exists whatsapp_log (
    id              bigserial primary key,
    direction       text not null check (direction in ('in', 'out')),
    message_text    text not null,
    related_job_id  bigint null references jobs_seen (id) on delete set null,
    timestamp       timestamptz not null default now()
);

-- ----------------------------------------------------------------
-- Notes:
--  * Scripts connect with the SERVICE_ROLE key, which bypasses
--    Row Level Security, so no RLS policies are defined here.
--  * If you ever enable RLS, add policies for service_role (or just
--    keep using the service role key, which is already exempt).
-- ----------------------------------------------------------------