"""Client for the Adzuna Job Search API (free tier, official, no scraping).

Adzuna is the LIVE job source used by default in this project because Indeed's
official job data requires a partner-program app (see README "Why Adzuna?").
Sign up at https://developer.adzuna.com -> Applications to get app_id + app_key.

Docs: https://developer.adzuna.com/docs/search

QUERY BEHAVIOUR (learned the hard way, tested live 2026-09-14):
  - `where` is country-local. An unsupported location (e.g. "Lahore" when there
    is no Pakistan feed) is NOT an error: the US feed silently ignores it and
    searches the whole county, producing irrelevant jobs. The GB feed returns 0.
  - Adzuna has NO exact "remote" filter. `remote_1`/`remote_2` params return
    HTTP 400. `what=remote` alone scans title+description -> lots of noise.
    Best effort = `what_and=remote` (Andrew matches title/description too).
  - Valid parameters: what, what_and, what_exclude, where, distance, category,
    sort_by (default|date|salary), salary_min/max, full_time, permanent.

STRATEGY (mirrors fetch preferences):
  - ADZUNA_COUNTRY is validated against the supported feed list; if unset or
    unknown, fall back to "us" and say so.
  - Location "remote"-like (remote/anywhere/wfh/...) -> drop `where`, add
    `what_and=remote` (approximate; logged as such).
  - Location in the known-unsupported list (Pakistan cities etc.) -> drop
    `where` (nationwide search) and log why. No silent garbage.
  - Only genuinely "where-able" locations are sent as `where` + `distance`.

The output of `normalize_job` matches the `jobs_seen` table columns exactly
(external_job_id, title, company, location, description, url) - the same
contract as `indeed_client.normalize_job`, so fetch_jobs.py is source-agnostic.
"""
from __future__ import annotations

import os
from typing import Any

import httpx

ADZUNA_BASE_URL = os.environ.get(
    "ADZUNA_BASE_URL", "https://api.adzuna.com/v1/api/jobs")
# Feeds Adzuna actually publishes (see https://developer.adzuna.com/activedocs).
ADZUNA_SUPPORTED_COUNTRIES = {
    "at", "au", "be", "ca", "de", "es", "eu", "fr", "gb", "ie",
    "in", "it", "mx", "nl", "nz", "pl", "ro", "sg", "us",
}
REMOTE_KEYWORDS = {
    "remote", "remote-eu", "remote us", "work from home", "wfh",
    "telework", "anywhere", "worldwide", "from home",
}
# Locations that don't exist on any Adzuna feed (no Pakistan board etc.).
UNSUPPORTED_LOCATION_TOKENS = {
    "lahore", "karachi", "islamabad", "rawalpindi", "faisalabad",
    "multan", "hyderabad", "peshawar", "pakistan",
}
USER_AGENT = "job-hunt-agent/0.1"


def _first(mapping: dict, *keys: str) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if value and str(value).strip():
            return str(value).strip()
    return None


def normalize_job(record: dict) -> dict:
    """Map an Adzuna search result to our canonical fields."""
    location = record.get("location") or {}
    loc_val = _first(location, "display_name", "area")
    job_id = record.get("id")
    return {
        "external_job_id": f"adz-{job_id}" if job_id is not None else None,
        "title": _first(record, "title") or "Untitled role",
        "company": _first((record.get("company") or {}), "display_name")
        or "Unknown company",
        "location": str(loc_val) if loc_val else None,
        "description": _first(record, "description") or "",
        "url": _first(record, "redirect_url") or "",
    }


class AdzunaClient:
    """Queries the free Adzuna Job Search API and returns normalized jobs."""

    def __init__(
        self,
        app_id: str | None = None,
        app_key: str | None = None,
        country: str | None = None,
        base_url: str = ADZUNA_BASE_URL,
        timeout: float = 30.0,
        verbose: bool = False,
    ):
        self.app_id = app_id or os.environ.get("ADZUNA_APP_ID") or ""
        self.app_key = app_key or os.environ.get("ADZUNA_APP_KEY") or ""
        self.country = self._resolve_country(country)
        self.base_url = base_url
        self.timeout = timeout
        self.verbose = verbose or os.environ.get("ADZUNA_VERBOSE") == "1"
        if not self.app_id or not self.app_key:
            raise ValueError(
                "ADZUNA_APP_ID and ADZUNA_APP_KEY must be set "
                "(get them free at https://developer.adzuna.com).")

    # ------------------------------------------------------------------ helpers
    def _resolve_country(self, country: str | None) -> str:
        requested = (country or os.environ.get("ADZUNA_COUNTRY") or "us").strip().lower()
        supported = ADZUNA_SUPPORTED_COUNTRIES
        if requested not in supported:
            print(
                f"  ! ADZUNA_COUNTRY='{requested}' is not a supported Adzuna feed "
                f"({', '.join(sorted(supported))}). Falling back to 'us'.",
                flush=True)
            return "us"
        return requested

    @staticmethod
    def _is_remote_location(location: str) -> bool:
        return (location or "").strip().lower() in REMOTE_KEYWORDS

    @staticmethod
    def _is_unsupported_location(location: str, country: str) -> bool:
        """True when the location cannot exist inside the selected country feed."""
        lowered = (location or "").lower()
        return any(token in lowered for token in UNSUPPORTED_LOCATION_TOKENS)

    def _log(self, message: str) -> None:
        if self.verbose:
            print(f"  [adzuna] {message}", flush=True)

    # ------------------------------------------------------------------ params
    def _build_params(
        self, keyword: str | None, location: str | None,
        limit: int, distance: int, max_days_old: int,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "app_id": self.app_id,
            "app_key": self.app_key,
            "content-type": "application/json",
            "results_per_page": str(max(1, min(int(limit or 25), 50))),
            "max_days_old": str(max_days_old),
            "sort_by": "date",
        }
        if keyword and str(keyword).strip():
            params["what"] = str(keyword).strip()

        where = (location or "").strip()
        if self._is_remote_location(where):
            params["what_and"] = "remote"
            self._log(
                f"'where={where}' treated as REMOTE: dropped 'where', "
                f"added 'what_and=remote' (approximate - Adzuna has no exact "
                f"remote filter).")
        elif where and self._is_unsupported_location(where, self.country):
            print(
                f"  ! '{where}' does not exist on Adzuna's '{self.country}' feed "
                f"(no Pakistan board). Dropping 'where' -> nationwide search.",
                flush=True)
        elif where:
            params["where"] = where
            params["distance"] = str(int(distance or 30))

        if self.verbose:
            url = httpx.URL(f"{self.base_url}/{self.country}/search/1", params=params)
            self._log(f"GET {url}")
        return params

    # ------------------------------------------------------------------ API
    def _search_raw(
        self, what: str | None, where: str | None,
        limit: int = 25, distance: int = 30, max_days_old: int = 30,
    ) -> list[dict]:
        params = self._build_params(what, where, limit, distance, max_days_old)
        resp = httpx.get(
            f"{self.base_url}/{self.country}/search/1", params=params,
            timeout=self.timeout, follow_redirects=True,
            headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        data = resp.json()
        self._log(f"resp {resp.status_code} count={data.get('count')}")
        return data.get("results") or []

    # -- sync public API used by fetch_jobs.py (same shape as IndeedClient) --
    def search(self, keyword: str, location: str, limit: int = 25) -> list[dict]:
        return [
            normalize_job(rec)
            for rec in self._search_raw(what=keyword, where=location, limit=limit)
            if isinstance(rec, dict)
        ]


def client_from_env(verbose: bool = False) -> AdzunaClient:
    return AdzunaClient(verbose=verbose)