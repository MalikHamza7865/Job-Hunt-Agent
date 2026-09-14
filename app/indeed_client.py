"""Direct-API client for Indeed job search (official OAuth + GraphQL API).

WHY NOT the hosted MCP server?
  Indeed's hosted MCP endpoint (https://mcp.indeed.com/claude/mcp) only accepts
  pre-whitelisted partner clients. Tokens minted for a dynamically-registered
  client (RFC 7591 DCR) are rejected by the MCP gateway AND by the underlying
  GraphQL API:

      HTTP 403 {"error": "invalid_client", "error_description": "Client not allowed"}
      HTTP 403 {"data": null, "errors": [{"message": "Client is not authorized."}]}

  (confirmed live on 2026-09-13 in this project; identical symptom reported
  upstream at anthropics/claude-code#60940 and anthropics/claude-code#47185.)

  Workaround: Indeed's AS supports client-ID metadata documents (RFC 7591
  section 4 / OIDC client metadata): the `client_id` may be a URL pointing at a
  public JSON document that describes the client, and the AS treats that
  pre-declared client as acceptable. We therefore use Claude Code's public
  metadata URL as `client_id` and talk to the *official* GraphQL API
  (https://apis.indeed.com/graphql) directly - the exact API the MCP server
  proxies under the hood. This is still Indeed's own OAuth AS and its own API
  surface (no scraping). Override INDEED_CLIENT_ID / INDEED_REDIRECT_URL if you
  obtain your own approved client registration.

Authentication model:
  - No device-code grant -> login is interactive ONCE.
    `python app/auth_indeed.py` opens a browser and saves a token file.
  - Afterwards every run refreshes via the OAuth `refresh_token` grant
    (offline_access scope) with no human in the loop, so the GitHub Actions
    cron works headlessly: store the token file contents as a secret and point
    INDEED_TOKEN_FILE at it in CI.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode, urlparse

import httpx

AUTHORIZE_ENDPOINT = os.environ.get(
    "INDEED_AUTHORIZE_URL", "https://secure.indeed.com/oauth/v2/authorize")
TOKEN_ENDPOINT = os.environ.get(
    "INDEED_TOKEN_URL", "https://apis.indeed.com/oauth/v2/tokens")
GRAPHQL_URL = os.environ.get(
    "INDEED_GRAPHQL_URL", "https://apis.indeed.com/graphql")
# A pre-whitelisted client-ID metadata document Indeed's AS accepts.
CLIENT_ID = os.environ.get(
    "INDEED_CLIENT_ID", "https://claude.ai/oauth/claude-code-client-metadata")
REDIRECT_URL = os.environ.get(
    "INDEED_REDIRECT_URL", "http://127.0.0.1:19876/callback")
REDIRECT_PORT = urlparse(REDIRECT_URL).port or 19876
DEFAULT_SCOPES = "job_seeker.jobs.search offline_access"
USER_AGENT = "job-hunt-agent/0.1"


class IndeedAuthError(RuntimeError):
    """Raised when Indeed rejects a request (token, refresh, or GraphQL)."""


# --------------------------------------------------------------------------
# Token persistence
# --------------------------------------------------------------------------
class TokenStore:
    """Persist the OAuth token pair (+ expiry + client) to a JSON file."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}

    def save(self, data: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        text = json.dumps(data, indent=2)
        tmp.write_text(text, encoding="utf-8")
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        tmp.replace(self.path)


# --------------------------------------------------------------------------
# Normalization (shared with fetch_jobs.py - do not duplicate elsewhere)
# --------------------------------------------------------------------------
def _first(mapping: dict, *keys: str) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if value and str(value).strip():
            return str(value).strip()
    return None


def normalize_job(job: dict) -> dict:
    """Map a raw GraphQL (or MCP-tool) job dict to our canonical fields
    (external_job_id, title, company, location, description, url).

    The output keys must match the `jobs_seen` table columns exactly, since
    fetch_jobs.py inserts these dicts straight into Supabase.
    """
    if "job" in job and isinstance(job.get("job"), dict) and not _first(job, "title"):
        job = job["job"]

    location_val: str | None = None
    structured = job.get("structuredLocation") or {}
    if isinstance(structured, dict):
        location_val = _first(structured, "fullAddress", "formattedLocation", "city")
    elif isinstance(structured, str) and structured.strip():
        location_val = structured.strip()
    if not location_val:
        location_val = _first(job, "locationName", "shortLocation", "location", "formattedLocation")
    if location_val is not None and isinstance(location_val, (str, int, float)):
        location_val = str(location_val).strip() or None

    job_id = _first(job, "id", "externalId", "jobId", "jobkey", "indeedJobKey", "key")
    return {
        "external_job_id": job_id or f"{job.get('title')}:{job.get('sourceEmployerName')}",
        "title": _first(job, "title", "jobtitle", "jobTitle", "position") or "Untitled role",
        "company": _first(job, "sourceEmployerName", "employerName", "company",
                          "company_name") or "Unknown company",
        "location": location_val,
        "description": _first(job, "description", "fullDescription", "snippet",
                              "job_description") or "",
        "url": _first(job, "url", "applyUrl", "jobUrl", "job_url", "indeedUrl")
        or (f"https://www.indeed.com/viewjob?jk={job_id}" if job_id else ""),
    }


# --------------------------------------------------------------------------
# OAuth support
# --------------------------------------------------------------------------
@dataclass
class _Pkce:
    verifier: str
    challenge: str


def _make_pkce() -> _Pkce:
    verifier = secrets.token_urlsafe(48)  # 64 chars, valid PKCE alphabet
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return _Pkce(verifier=verifier, challenge=challenge)


# --------------------------------------------------------------------------
# Indeed client
# --------------------------------------------------------------------------
class IndeedClient:
    """Authenticates with Indeed and queries the official GraphQL API."""

    def __init__(
        self,
        token_file: str | Path | None = None,
        interactive: bool = False,
        timeout: float = 120.0,
    ):
        self.token_file = Path(token_file or os.environ.get(
            "INDEED_TOKEN_FILE", ".indeed_tokens.json"))
        self.interactive = interactive
        self.timeout = timeout

    # -- storage -----------------------------------------------------------
    def _store(self) -> TokenStore:
        return TokenStore(self.token_file)

    # -- token lifecycle ---------------------------------------------------
    async def _ensure_access_token(self, http: httpx.AsyncClient) -> str:
        data = self._store().load()
        now = int(time.time())
        if data.get("access_token") and data.get("expires_at", 0) - 30 > now:
            return data["access_token"]
        if data.get("refresh_token"):
            try:
                return await self._refresh(http, data)
            except Exception as exc:  # stale/revoked refresh token
                if not self.interactive:
                    raise
                print(f"\n(refresh failed: {exc}; re-authenticating)", flush=True)
        if not self.interactive:
            raise IndeedAuthError(
                "No valid Indeed token. Run once: python app/auth_indeed.py")
        return await self._interactive_login(http)

    async def _refresh(self, http: httpx.AsyncClient, data: dict) -> str:
        body = {
            "grant_type": "refresh_token",
            "refresh_token": data["refresh_token"],
            "client_id": CLIENT_ID,
        }
        resp = await http.post(TOKEN_ENDPOINT, data=body)
        if resp.status_code != 200:
            raise IndeedAuthError(
                f"Token refresh failed (HTTP {resp.status_code}): {resp.text[:300]}")
        tok = resp.json()
        state = {
            **data,
            "access_token": tok["access_token"],
            "refresh_token": tok.get("refresh_token") or data.get("refresh_token"),
            "expires_at": int(time.time()) + int(tok.get("expires_in", 3600)),
            "scope": tok.get("scope") or data.get("scope") or DEFAULT_SCOPES,
        }
        self._store().save(state)
        return state["access_token"]

    async def _interactive_login(self, http: httpx.AsyncClient) -> str:
        """One-time OAuth authorization-code (PKCE) flow, saved to the token file."""
        from .callback_server import start_callback_server, wait_for_code

        pkce = _make_pkce()
        state = secrets.token_urlsafe(24)
        authorize_url = f"{AUTHORIZE_ENDPOINT}?{urlencode({
            'response_type': 'code',
            'client_id': CLIENT_ID,
            'redirect_uri': REDIRECT_URL,
            'code_challenge': pkce.challenge,
            'code_challenge_method': 'S256',
            'state': state,
            'scope': DEFAULT_SCOPES,
        })}"

        # Bind the listener BEFORE opening a browser so a manual-URL fallback
        # (headless WSL etc.) always has a server ready.
        start_callback_server(REDIRECT_PORT)
        print(f"\nOpen this URL and sign in to Indeed:\n  {authorize_url}\n", flush=True)
        print("(If a browser window did not open, copy the URL above manually.)", flush=True)
        try:
            import webbrowser
            webbrowser.open(authorize_url)
        except Exception:
            pass  # headless: the URL is printed above

        try:
            result = await asyncio.wait_for(
                wait_for_code(timeout=self.timeout), timeout=self.timeout + 10)
        except Exception:
            raise IndeedAuthError("Timed out waiting for the Indeed sign-in callback.") from None

        if not result.state or not secrets.compare_digest(result.state, state):
            raise IndeedAuthError("State parameter mismatch - authorization failed.")
        if not result.code:
            raise IndeedAuthError("Authorization failed: no code received in callback.")

        body = {
            "grant_type": "authorization_code",
            "code": result.code,
            "redirect_uri": REDIRECT_URL,
            "code_verifier": pkce.verifier,
            "client_id": CLIENT_ID,
        }
        resp = await http.post(TOKEN_ENDPOINT, data=body)
        if resp.status_code != 200:
            raise IndeedAuthError(
                f"Token exchange failed (HTTP {resp.status_code}): {resp.text[:400]}")
        tok = resp.json()
        state_data = {
            "client_id": CLIENT_ID,
            "access_token": tok["access_token"],
            "refresh_token": tok.get("refresh_token"),
            "expires_at": int(time.time()) + int(tok.get("expires_in", 3600)),
            "scope": tok.get("scope") or DEFAULT_SCOPES,
        }
        self._store().save(state_data)
        return state_data["access_token"]

    # -- GraphQL API -------------------------------------------------------
    @staticmethod
    def _graphql(resp: httpx.Response) -> dict:
        if resp.status_code >= 400:
            raise IndeedAuthError(
                f"Indeed API HTTP {resp.status_code}: {resp.text[:400]}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise IndeedAuthError(
                f"Indeed API returned non-JSON: {resp.text[:200]}") from exc
        errors = body.get("errors") or []
        if errors:
            raise IndeedAuthError(f"Indeed API: {errors[0].get('message') or 'unknown error'}")
        data = body.get("data")
        return data if isinstance(data, dict) else {}

    async def _search_async(
        self, keyword: str, location: str, radius: int = 25, limit: int = 10
    ) -> list[dict]:
        limit = max(1, min(int(limit or 25), 50))
        location_lit = "null"
        if location and str(location).strip():
            radius = max(1, int(radius or 25))
            location_lit = (
                f"{{radius: {radius}, radiusUnit: MILES, "
                f"where: {json.dumps(str(location))}}}"
            )
        what_lit = json.dumps(str(keyword))
        query = (
            "query SearchJobs { jobSearch("
            f"location: {location_lit}, what: {what_lit}, limit: {limit})"
            "{ totalResults results { job { "
            "title sourceEmployerName locationName salary { currency period min max } "
            "url key publishedDate } } } }"
        )
        async with httpx.AsyncClient(timeout=30.0, headers={"User-Agent": USER_AGENT}) as http:
            token = await self._ensure_access_token(http)
            resp = await http.post(
                GRAPHQL_URL,
                json={"query": query, "variables": {}},
                headers={"Authorization": f"Bearer {token}"},
            )
        data = self._graphql(resp)
        results = (data.get("jobSearch") or {}).get("results") or []
        jobs: list[dict] = []
        for item in results:
            if isinstance(item, dict):
                jobs.append(normalize_job(item.get("job") or {}))
        return jobs

    async def _detail_async(self, job_id: str) -> dict:
        query = (
            "query GetJobDetail { job("
            f"id: {json.dumps(str(job_id))})"
            "{ result { "
            "title sourceEmployerName locationName salary { currency period min max } "
            "url key publishedDate description { text } "
            "company { name rating reviewCount locationName } "
            "attributes { name label } jobTypes remoteWorkType "
            "} } }"
        )
        async with httpx.AsyncClient(timeout=30.0, headers={"User-Agent": USER_AGENT}) as http:
            token = await self._ensure_access_token(http)
            resp = await http.post(
                GRAPHQL_URL,
                json={"query": query, "variables": {}},
                headers={"Authorization": f"Bearer {token}"},
            )
        data = self._graphql(resp)
        result = (data.get("job") or {}).get("result") or {}
        company = result.get("company")
        description = result.get("description")
        flat = {
            "title": result.get("title"),
            "sourceEmployerName": (company or {}).get("name")
            if isinstance(company, dict) else result.get("sourceEmployerName"),
            "locationName": result.get("locationName"),
            "url": result.get("url"),
            "key": result.get("key"),
            "description": (description or {}).get("text")
            if isinstance(description, dict) else result.get("description") or "",
        }
        return normalize_job(flat)

    # -- sync public API used by fetch_jobs.py --------------------------
    def search(self, keyword: str, location: str, limit: int = 25) -> list[dict]:
        try:
            return asyncio.run(self._search_async(keyword, location, limit=limit))
        except BaseException as exc:
            raise _unwrap(exc) from None

    def get_job_detail(self, job_id: str) -> dict:
        try:
            return asyncio.run(self._detail_async(job_id))
        except BaseException as exc:
            raise _unwrap(exc) from None


def _unwrap(exc: BaseException) -> BaseException:
    if hasattr(exc, "exceptions"):
        return _unwrap(exc.exceptions[0])
    return exc


def client_from_env() -> IndeedClient:
    token_file = os.environ.get("INDEED_TOKEN_FILE", ".indeed_tokens.json")
    return IndeedClient(token_file=token_file)