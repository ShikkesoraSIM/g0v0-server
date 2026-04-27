"""CI webhook endpoint for auto-registering Torii client build hashes.

After each release build, the CI computes MD5 hashes of the shipped executables
and POSTs them here. The server immediately registers them as overrides so that
players on the new build are recognised in scores/login logs without any manual
steps.

Authentication: static Bearer token stored in ``settings.client_version_webhook_secret``.
"""

from __future__ import annotations

import secrets
from typing import Annotated

from app.config import settings
from app.dependencies.client_verification import ClientVerificationService
from app.log import log

from fastapi import Depends, Header, HTTPException, status
from pydantic import BaseModel, field_validator

logger = log("ClientVersionWebhook")


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------

def _require_webhook_auth(authorization: Annotated[str, Header()] = "") -> None:
    """Validate the static Bearer token from the CI."""
    secret = (settings.client_version_webhook_secret or "").strip()
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Client version webhook is not configured on this server.",
        )

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header must be 'Bearer <secret>'.",
        )

    if not secrets.compare_digest(token.strip(), secret):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook secret.",
        )


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class RegisterHashesRequest(BaseModel):
    """Payload sent by the CI after a successful release build."""

    version: str
    """Semver version string, e.g. '2026.427.1-lazer'."""

    client_name: str = "osu! Torii"
    """Human-readable client name shown in the admin panel and score history."""

    hashes: dict[str, str]
    """Mapping of lowercase MD5 hex digest → OS label, e.g. ``{'abc123…': 'Windows'}``."""

    @field_validator("version")
    @classmethod
    def _version_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("version must not be empty")
        return v.strip()

    @field_validator("client_name")
    @classmethod
    def _name_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("client_name must not be empty")
        return v.strip()

    @field_validator("hashes")
    @classmethod
    def _hashes_not_empty(cls, v: dict[str, str]) -> dict[str, str]:
        if not v:
            raise ValueError("hashes must contain at least one entry")
        return {k.strip().lower(): val.strip() for k, val in v.items() if k.strip()}


class RegisterHashesResponse(BaseModel):
    registered: int
    version: str
    client_name: str


# ---------------------------------------------------------------------------
# Route (imported by router.py)
# ---------------------------------------------------------------------------

from .router import router  # noqa: E402  (local import avoids circular deps)


@router.post(
    "/client-versions/register",
    name="Register Torii client build hashes",
    tags=["Client Versions", "CI Webhook"],
    response_model=RegisterHashesResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(_require_webhook_auth)],
)
async def register_client_hashes(
    payload: RegisterHashesRequest,
    verification_service: ClientVerificationService,
) -> RegisterHashesResponse:
    """Called by the CI after each release build.

    Registers the MD5 hash of every shipped executable as a known client override
    so that players on the new build are immediately recognised.
    """
    registered = 0
    for md5_hash, os_label in payload.hashes.items():
        try:
            await verification_service.assign_hash_override(
                md5_hash,
                client_name=payload.client_name,
                version=payload.version,
                os_name=os_label,
                remove_from_unknown=True,
            )
            registered += 1
        except Exception as exc:
            logger.warning(
                f"Failed to register hash {md5_hash[:12]}… "
                f"(version={payload.version}, os={os_label}): {exc}"
            )

    logger.info(
        f"CI webhook registered {registered}/{len(payload.hashes)} hashes "
        f"for {payload.client_name} {payload.version}"
    )

    return RegisterHashesResponse(
        registered=registered,
        version=payload.version,
        client_name=payload.client_name,
    )
