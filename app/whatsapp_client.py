"""Minimal WhatsApp Cloud API client (Meta) - outbound text + template messages.

Docs: https://developers.facebook.com/docs/whatsapp/business-management-api/
      message-sending-flow

Config (from .env):
    WHATSAPP_TOKEN             <- token from Meta App Dashboard
    WHATSAPP_PHONE_NUMBER_ID   <- phone number id, not the phone number itself
    RECIPIENT_PHONE            <- E.164 like +923012345678
    WHATSAPP_GRAPH_URL         <- optional override
    WHATSAPP_API_VERSION       <- default v21.0
    WHATSAPP_TEMPLATE_NAME     <- approved template for the 24h-window fallback
    WHATSAPP_TEMPLATE_LANG     <- template language code (default "en")

Numbers are auto-normalized to E.164: a local PK mobile like 03392030660
becomes +923392030660. Recipient must be whitelisted in the Meta sandbox
(sandbox numbers can only message up to 5 approved recipients).

24-HOUR SESSION WINDOW: free-form (type=text) messages only succeed while the
recipient has messaged the business within the last 24 hours. Outside that
window Meta returns a re-engagement error (131049/131026/131007); approved
*message templates* are the ONLY way to reach a user who hasn't replied.
`send_template_message` is the fallback for that case (see notify.py).
"""
from __future__ import annotations

import os
import re

import httpx

USER_AGENT = "job-hunt-agent/0.1"

# Meta error codes that mean the 24-hour session window has lapsed, so a
# free-form text cannot be delivered (an approved template is required instead).
SESSION_WINDOW_ERROR_CODES = {131049, 131026, 131007, 132000}


class WhatsAppError(RuntimeError):
    """Raised when Meta rejects a message.

    Attributes (best-effort, None when not reportable):
        code    Meta error code (e.g. 131030 Recipient not in allowed list)
        subcode Meta error subcode (e.g. 131049 24h window lapsed)
    """

    def __init__(
        self,
        message: str,
        code: int | None = None,
        subcode: int | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.subcode = subcode


def is_session_window_error(exc: Exception) -> bool:
    """True when an approved message template is needed to reach the user."""
    code = getattr(exc, "code", None)
    subcode = getattr(exc, "subcode", None)
    in_codes = code in SESSION_WINDOW_ERROR_CODES or subcode in SESSION_WINDOW_ERROR_CODES
    text = str(exc).lower()
    hinted = (
        any(str(c) in text for c in SESSION_WINDOW_ERROR_CODES)
        or "24 hour" in text or "re-engagement" in text
        or "hasn't messaged" in text or "session" in text)
    return bool(in_codes or hinted)


def normalize_phone(number: str) -> str:
    """Best-effort E.164 conversion. Handles Pakistani local format."""
    value = (number or "").strip()
    if not value:
        raise WhatsAppError("Recipient phone number is empty.")
    if value.startswith("+"):
        if re.fullmatch(r"\+[0-9]{8,15}", value):
            return value
        raise WhatsAppError(f"Invalid E.164 number: {value!r}")
    digits = re.sub(r"\D", "", value)
    if len(digits) == 11 and digits.startswith("0"):  # e.g. 03392030660 (PK)
        digits = "92" + digits[1:]
    elif len(digits) == 10:  # assume US/CA
        digits = "1" + digits
    return "+" + digits


class WhatsAppClient:
    def __init__(
        self,
        token: str | None = None,
        phone_number_id: str | None = None,
        api_version: str = "v21.0",
        timeout: float = 20.0,
    ):
        self.token = token or os.environ.get("WHATSAPP_TOKEN") or ""
        self.phone_number_id = phone_number_id or os.environ.get(
            "WHATSAPP_PHONE_NUMBER_ID") or ""
        base = os.environ.get("WHATSAPP_GRAPH_URL", "https://graph.facebook.com")
        self.api_version = os.environ.get("WHATSAPP_API_VERSION") or api_version
        self.timeout = timeout
        if not self.token or not self.phone_number_id:
            raise ValueError(
                "WHATSAPP_TOKEN and WHATSAPP_PHONE_NUMBER_ID must be set.")
        self.url = f"{base}/{self.api_version}/{self.phone_number_id}/messages"

    def _handle(self, resp: httpx.Response) -> dict:
        if resp.status_code >= 400:
            code = subcode = None
            detail = ""
            try:
                errs = resp.json()
                if "error" in errs:
                    e = errs["error"]
                    code = e.get("code")
                    subcode = e.get("error_subcode")
                    detail = f" (code={code} subcode={subcode} type={e.get('type')}) {e.get('message')}"
            except ValueError:
                pass
            raise WhatsAppError(
                f"WhatsApp API HTTP {resp.status_code}{detail}",
                code=code, subcode=subcode)
        return resp.json()

    def send_text_message(self, to: str, body: str) -> str:
        """Send a free-form text message; returns the Meta message id."""
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": normalize_phone(to),
            "type": "text",
            "text": {"body": body, "preview_url": True},
        }
        return self._post(payload)

    def send_template_message(
        self, to: str, name: str, params: list[str], language: str = "en",
    ) -> str:
        """Send an APPROVED message template (works outside the 24h window).

        `params` become the template body's {{1}}, {{2}}, ... placeholders.
        The template must exist and be approved in the Meta dashboard:
        WhatsApp -> Message templates -> "job_match_alert" (body below).
        """
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": normalize_phone(to),
            "type": "template",
            "template": {
                "name": name,
                "language": {"code": language},
                "components": [{
                    "type": "body",
                    "parameters": [{"type": "text", "text": str(p)}
                                   for p in params],
                }],
            },
        }
        return self._post(payload)

    def _post(self, payload: dict) -> str:
        resp = httpx.post(
            self.url,
            json=payload,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
            timeout=self.timeout,
        )
        data = self._handle(resp)
        try:
            return data["messages"][0]["id"]
        except (KeyError, IndexError, TypeError):
            raise WhatsAppError(
                f"Unexpected Meta response: {str(data)[:200]}") from None


def client_from_env() -> WhatsAppClient:
    return WhatsAppClient()