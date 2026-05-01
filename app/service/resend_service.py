"""Resend email-sending service.

Mirrors the public surface of `mailersend_service.MailerSendService` so
the email queue / verification service can swap providers without
changes downstream — both expose `send_email(...)` returning
`{"id": "..."}`.

Why Resend (vs MailerSend Trial): MailerSend's free Trial plan caps you
to a tiny pre-approved recipient allowlist (MS42225 — "trial unique
recipients limit reached"), making it unusable for password resets or
email verification on a public service. Resend's free tier (3,000
emails/month, 100/day) has no per-recipient cap — any verified domain
can send to anyone. Same deliverability profile, almost identical API
shape.
"""

from typing import Any

import httpx

from app.config import settings
from app.log import logger


class ResendService:
    """Resend email-sending service."""

    API_URL = "https://api.resend.com/emails"

    def __init__(self):
        if not settings.resend_api_key:
            raise ValueError("Resend API Key is required when email_provider is 'resend'")
        if not settings.resend_from_email:
            raise ValueError("Resend from email is required when email_provider is 'resend'")

        self.api_key = settings.resend_api_key
        self.from_email = settings.resend_from_email
        self.from_name = settings.from_name

    async def send_email(
        self,
        to_email: str,
        subject: str,
        content: str,
        html_content: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        """Send an email via Resend.

        Args:
            to_email: Recipient email address.
            subject: Email subject.
            content: Plain-text body (used as fallback if html_content is None).
            html_content: HTML body (preferred when present).
            metadata: Reserved for future use (logging tags, idempotency
                keys, etc). Currently ignored.

        Returns:
            `{"id": "<resend-message-id>"}` on success, `{"id": ""}` on
            failure (logged via app.log.logger).
        """
        _ = metadata  # reserved

        # Resend accepts `"Name <email@domain>"` — produces a friendly
        # From header. Drops back to bare email if from_name is empty.
        from_field = f"{self.from_name} <{self.from_email}>" if self.from_name else self.from_email

        payload: dict[str, Any] = {
            "from": from_field,
            "to": [to_email],
            "subject": subject,
        }
        # Resend prefers html over text but accepts both. We send only one
        # to keep the payload small and make the rendered body
        # deterministic across mail clients.
        if html_content:
            payload["html"] = html_content
        else:
            payload["text"] = content

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(
                    self.API_URL,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )

            if response.status_code >= 400:
                # Surface Resend's error body verbatim so logs reveal the
                # specific reason (unverified domain, daily quota, etc).
                logger.error(
                    f"Resend send failed for {to_email}: HTTP {response.status_code} {response.text}"
                )
                return {"id": ""}

            data = response.json()
            message_id = str(data.get("id", "")) if isinstance(data, dict) else ""
            logger.info(f"Successfully sent email via Resend to {to_email}, message_id: {message_id}")
            return {"id": message_id}

        except httpx.RequestError as exc:
            logger.error(f"Resend network error sending to {to_email}: {exc!r}")
            return {"id": ""}
        except Exception as exc:
            logger.error(f"Resend unexpected error sending to {to_email}: {exc!r}")
            return {"id": ""}


_resend_service: ResendService | None = None


def get_resend_service() -> ResendService:
    """Lazy singleton — same shape as get_mailersend_service()."""
    global _resend_service
    if _resend_service is None:
        _resend_service = ResendService()
    return _resend_service
