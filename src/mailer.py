"""Send an approved draft as email, via Resend.

Majeure drafts a force majeure notice and a customer email, and a human approves them. This is
the last step: actually putting one in someone's inbox.

Resend rather than SMTP because the deployed service runs on Render's free tier, which commonly
blocks outbound SMTP ports -- an HTTPS call has no such problem, and the failure mode of a wrong
API key is an immediate 401 rather than a connection that hangs until it times out.

Deliberately a seam, not a hard dependency: with no RESEND_API_KEY the module reports itself
unavailable and the API refuses the send with a message saying why, instead of silently recording
a document as sent that never went anywhere. A notice that claims to have been served and was not
is worse than one that was never sent, because a force majeure clause turns on having given notice
inside the window.

Env (see .env.example):
    RESEND_API_KEY      required to send at all
    NOTICE_FROM         the From address; must be on a domain verified with Resend.
                        Defaults to Resend's shared onboarding sender, which works without
                        verifying a domain but can only deliver to the account owner's address.
    NOTICE_RECIPIENT    default To, used when the caller does not name one
"""
from __future__ import annotations

import os
import re
from typing import Optional

import requests

API_URL = "https://api.resend.com/emails"
# Resend's shared sender. It needs no DNS setup, which is what makes it usable in a demo, but it
# will only deliver to the email address that owns the Resend account -- a real deployment sets
# NOTICE_FROM to an address on its own verified domain.
DEFAULT_FROM = "Majeure <onboarding@resend.dev>"
TIMEOUT = 20

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class MailError(RuntimeError):
    """Sending failed, or was never possible. The message is shown to the operator."""


def is_configured() -> bool:
    return bool(os.environ.get("RESEND_API_KEY"))


def default_recipient() -> Optional[str]:
    return os.environ.get("NOTICE_RECIPIENT") or None


def valid_address(address: str) -> bool:
    return bool(_EMAIL_RE.match((address or "").strip()))


def send(to: str, subject: str, body: str, reply_to: Optional[str] = None,
         attachments: Optional[list] = None) -> str:
    """Send one plain-text email. Returns the provider's message id, or raises MailError."""
    key = os.environ.get("RESEND_API_KEY")
    if not key:
        raise MailError("Email is not configured on this deployment (RESEND_API_KEY is not set), "
                        "so nothing was sent. The notice is approved and ready to send by hand.")
    to = (to or "").strip()
    if not valid_address(to):
        raise MailError(f"{to!r} is not a valid email address.")

    payload = {
        "from": os.environ.get("NOTICE_FROM") or DEFAULT_FROM,
        "to": [to],
        "subject": subject,
        # Plain text on purpose: these are legal notices, and a text/plain body is what gets quoted
        # back in a dispute without a mail client's HTML rewriting anything.
        "text": body,
    }
    if reply_to:
        payload["reply_to"] = reply_to
    if attachments:
        # Resend takes base64 in `content`; the signable PDF is the reason to send a document
        # rather than just a message.
        payload["attachments"] = attachments

    try:
        resp = requests.post(API_URL, json=payload, timeout=TIMEOUT,
                             headers={"Authorization": f"Bearer {key}"})
    except requests.RequestException as exc:
        raise MailError(f"Could not reach the mail provider: {type(exc).__name__}") from exc

    if resp.status_code >= 400:
        # Surface the provider's own words: "domain is not verified" and "invalid api key" are the
        # two things that actually go wrong, and both are fixable only if the operator is told which.
        detail = ""
        try:
            detail = (resp.json().get("message") or resp.json().get("error") or "")
        except ValueError:
            detail = resp.text[:200]
        raise MailError(f"Mail provider refused the send ({resp.status_code}): {detail}")

    try:
        return str(resp.json().get("id") or "")
    except ValueError:
        return ""
