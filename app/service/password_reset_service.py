"""Password reset service."""

from __future__ import annotations

import hmac
import json
import secrets
import string

from app.auth import get_password_hash, invalidate_user_tokens, validate_password
from app.database import User
from app.dependencies.database import with_db
from app.log import logger
from app.service.email_queue import email_queue
from app.utils import utcnow

from redis.asyncio import Redis
from sqlmodel import select


class PasswordResetService:
    """Handles password reset request and confirmation flows."""

    RESET_CODE_PREFIX = "password_reset:code:"
    RESET_RATE_LIMIT_PREFIX = "password_reset:rate_limit:"
    RESET_RATE_LIMIT_IP_PREFIX = "password_reset:rate_limit_ip:"
    RESET_ATTEMPT_PREFIX = "password_reset:attempts:"

    RESET_CODE_TTL_SECONDS = 600
    REQUEST_RATE_LIMIT_SECONDS = 60
    REQUEST_IP_RATE_LIMIT_SECONDS = 30
    MAX_RESET_ATTEMPTS = 5

    SUCCESS_REQUEST_MESSAGE = "If this email exists, a verification code has been sent."
    TOO_MANY_REQUESTS_MESSAGE = "Too many requests. Please try again later."
    INVALID_CODE_MESSAGE = "Invalid or expired verification code."
    INVALID_EMAIL_MESSAGE = "Invalid email for this verification code."
    TOO_MANY_ATTEMPTS_MESSAGE = "Too many incorrect attempts. Please request a new code."

    def generate_reset_code(self) -> str:
        """Generate an 8-digit verification code."""
        return "".join(secrets.choice(string.digits) for _ in range(8))

    def _get_reset_code_key(self, email: str) -> str:
        return f"{self.RESET_CODE_PREFIX}{email.lower()}"

    def _get_rate_limit_key(self, email: str) -> str:
        return f"{self.RESET_RATE_LIMIT_PREFIX}{email.lower()}"

    def _get_ip_rate_limit_key(self, ip_address: str) -> str:
        return f"{self.RESET_RATE_LIMIT_IP_PREFIX}{ip_address}"

    def _get_attempts_key(self, email: str) -> str:
        return f"{self.RESET_ATTEMPT_PREFIX}{email.lower()}"

    async def request_password_reset(
        self,
        email: str,
        ip_address: str,
        user_agent: str,
        redis: Redis,
    ) -> tuple[bool, str]:
        """Request password reset and enqueue verification email."""
        normalized_email = email.lower().strip()
        rate_limit_key = self._get_rate_limit_key(normalized_email)
        ip_rate_limit_key = self._get_ip_rate_limit_key(ip_address)

        # Keep this check before user lookup to reduce abuse and user enumeration.
        if await redis.get(rate_limit_key) or await redis.get(ip_rate_limit_key):
            return False, self.TOO_MANY_REQUESTS_MESSAGE

        # Set request limits up-front for both existing and unknown emails.
        await redis.setex(rate_limit_key, self.REQUEST_RATE_LIMIT_SECONDS, "1")
        await redis.setex(ip_rate_limit_key, self.REQUEST_IP_RATE_LIMIT_SECONDS, "1")

        async with with_db() as session:
            result = await session.exec(select(User).where(User.email == normalized_email))
            user = result.first()

            if not user:
                logger.info(
                    "Password reset requested for unknown email %s from IP %s",
                    normalized_email,
                    ip_address,
                )
                return True, self.SUCCESS_REQUEST_MESSAGE

            reset_code = self.generate_reset_code()
            reset_data = {
                "user_id": user.id,
                "email": normalized_email,
                "reset_code": reset_code,
                "created_at": utcnow().isoformat(),
                "ip_address": ip_address,
                "user_agent": user_agent,
                "used": False,
            }

            reset_code_key = self._get_reset_code_key(normalized_email)
            await redis.setex(reset_code_key, self.RESET_CODE_TTL_SECONDS, json.dumps(reset_data))
            await redis.delete(self._get_attempts_key(normalized_email))

            email_sent = await self.send_password_reset_email(
                email=normalized_email,
                code=reset_code,
                username=user.username,
            )

            if not email_sent:
                await redis.delete(reset_code_key)
                logger.warning("Password reset email enqueue failed for %s", normalized_email)
                return False, "Failed to send reset email. Please try again."

            logger.info(
                "Password reset code generated for user_id=%s email=%s ip=%s",
                user.id,
                normalized_email,
                ip_address,
            )
            return True, self.SUCCESS_REQUEST_MESSAGE

    async def send_password_reset_email(self, email: str, code: str, username: str) -> bool:
        """Queue password reset email."""
        try:
            # NOTE: This template is intentionally light-themed with inline
            # styles on every element. Gmail Web aggressively strips <style>
            # blocks; if the email is dark-themed and the user is in light
            # mode, the body text (which Gmail keeps) becomes light-on-light
            # and is invisible. Light theme works in both modes because
            # Gmail auto-darkens light emails for dark-mode users, but does
            # NOT auto-lighten dark emails for light-mode users.
            html_content = f"""\
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="color-scheme" content="light only">
    <meta name="supported-color-schemes" content="light only">
    <title>Torii Password Reset</title>
</head>
<body style="margin:0;padding:0;background-color:#f5f5f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;color:#333333;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#f5f5f5;">
        <tr>
            <td align="center" style="padding:24px 12px;">
                <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="max-width:600px;width:100%;background-color:#ffffff;border-radius:12px;overflow:hidden;border:1px solid #ececec;">
                    <tr>
                        <td style="background:linear-gradient(135deg,#ED8EA6 0%,#FF6B9D 100%);background-color:#ED8EA6;color:#ffffff;padding:32px 20px;text-align:center;">
                            <h1 style="margin:0;font-size:26px;font-weight:600;color:#ffffff;">Torii Password Reset</h1>
                            <p style="margin:8px 0 0 0;font-size:15px;color:#ffffff;opacity:0.92;">Reset your account password</p>
                        </td>
                    </tr>
                    <tr>
                        <td style="padding:36px 30px;background-color:#ffffff;color:#333333;">
                            <p style="margin:0 0 16px 0;font-size:17px;font-weight:600;color:#222222;">Hello {username},</p>
                            <p style="margin:0 0 24px 0;font-size:15px;line-height:1.6;color:#555555;">
                                Use the verification code below to reset your password:
                            </p>
                            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#fafafa;border:2px solid #ED8EA6;border-radius:12px;margin:8px 0 24px 0;">
                                <tr>
                                    <td align="center" style="padding:24px 16px;">
                                        <div style="font-family:'Courier New',Consolas,monospace;font-size:34px;font-weight:bold;letter-spacing:6px;color:#ED8EA6;">{code}</div>
                                        <div style="margin-top:12px;font-size:13px;color:#666666;">Expires in <strong style="color:#333333;">10 minutes</strong> &middot; one-time use</div>
                                    </td>
                                </tr>
                            </table>
                            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#fff3cd;border-left:4px solid #ffc107;border-radius:6px;margin:0 0 24px 0;">
                                <tr>
                                    <td style="padding:14px 18px;font-size:14px;line-height:1.5;color:#856404;">
                                        If you did not request this, ignore this email and your password will remain unchanged.
                                    </td>
                                </tr>
                            </table>
                            <p style="margin:0;font-size:12px;color:#999999;text-align:center;">
                                This message was sent automatically by Torii. Please do not reply.
                            </p>
                        </td>
                    </tr>
                </table>
            </td>
        </tr>
    </table>
</body>
</html>
"""

            plain_content = (
                f"Hello {username},\n\n"
                "Use this verification code to reset your password:\n\n"
                f"{code}\n\n"
                "This code expires in 10 minutes and can only be used once.\n\n"
                "If you did not request this, ignore this email.\n"
            )

            await email_queue.enqueue_email(
                to_email=email,
                subject="Torii password reset code",
                content=plain_content,
                html_content=html_content,
                metadata={"type": "password_reset", "email": email},
            )
            logger.info("Password reset email queued for %s", email)
            return True
        except Exception:
            logger.exception("Failed to queue password reset email")
            return False

    async def reset_password(
        self,
        email: str,
        reset_code: str,
        new_password: str,
        ip_address: str,
        redis: Redis,
        user_agent: str = "",
    ) -> tuple[bool, str]:
        """Validate code and set a new password."""
        normalized_email = email.lower().strip()
        submitted_code = reset_code.strip()

        reset_code_key = self._get_reset_code_key(normalized_email)
        attempts_key = self._get_attempts_key(normalized_email)

        reset_data_str = await redis.get(reset_code_key)
        if not reset_data_str:
            return False, self.INVALID_CODE_MESSAGE

        try:
            reset_data = json.loads(reset_data_str)
        except json.JSONDecodeError:
            await redis.delete(reset_code_key)
            await redis.delete(attempts_key)
            return False, self.INVALID_CODE_MESSAGE

        if reset_data.get("used", False):
            return False, self.INVALID_CODE_MESSAGE

        if reset_data.get("email") != normalized_email:
            return False, self.INVALID_EMAIL_MESSAGE

        current_attempts_raw = await redis.get(attempts_key)
        current_attempts = int(current_attempts_raw or 0)
        if current_attempts >= self.MAX_RESET_ATTEMPTS:
            return False, self.TOO_MANY_ATTEMPTS_MESSAGE

        expected_code = str(reset_data.get("reset_code", ""))
        if not hmac.compare_digest(expected_code, submitted_code):
            new_attempts = current_attempts + 1
            code_ttl = await redis.ttl(reset_code_key)
            attempts_ttl = code_ttl if code_ttl > 0 else self.RESET_CODE_TTL_SECONDS

            await redis.setex(attempts_key, attempts_ttl, str(new_attempts))

            if new_attempts >= self.MAX_RESET_ATTEMPTS:
                await redis.delete(reset_code_key)
                await redis.delete(attempts_key)
                return False, self.TOO_MANY_ATTEMPTS_MESSAGE

            return False, self.INVALID_CODE_MESSAGE

        password_errors = validate_password(new_password)
        if password_errors:
            return False, password_errors[0]

        async with with_db() as session:
            result = await session.exec(select(User).where(User.email == normalized_email))
            user = result.first()
            if not user or user.id is None:
                return False, "User not found."

            if reset_data.get("user_id") != user.id:
                return False, "Invalid verification request."

            try:
                # Cache user.id BEFORE commit. SQLAlchemy expires loaded
                # objects on commit by default, so reading user.id afterwards
                # triggers a lazy-reload that fails with MissingGreenlet
                # (sync IO inside an awaited path) on async sessions.
                user_id = user.id
                user.pw_bcrypt = get_password_hash(new_password)
                await session.commit()

                tokens_deleted = await invalidate_user_tokens(session, user_id)

                await redis.delete(reset_code_key)
                await redis.delete(attempts_key)

                logger.info(
                    "Password reset success for user_id=%s email=%s ip=%s ua=%s invalidated_tokens=%s",
                    user_id,
                    normalized_email,
                    ip_address,
                    user_agent[:200],
                    tokens_deleted,
                )
                return True, "Password reset successful. All active sessions were signed out."
            except Exception:
                await session.rollback()
                logger.exception("Failed to reset password for %s", normalized_email)
                return False, "Failed to reset password. Please try again."

    async def get_reset_attempts_count(self, email: str, redis: Redis) -> int:
        """Return failed reset attempts count for an email/code pair."""
        attempts_raw = await redis.get(self._get_attempts_key(email.lower().strip()))
        return int(attempts_raw or 0)


password_reset_service = PasswordResetService()
