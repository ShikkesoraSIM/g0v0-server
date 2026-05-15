"""HTTP client stub for the external anti-cheat service (torii-slitwrist).

The implementation of detection logic lives in a SEPARATE PRIVATE REPO —
this file intentionally contains zero detection rules, only the
fire-and-forget transport.

Anyone forking g0v0-server gets this stub for free; if they don't run a
matching service on the configured URL, every call returns None silently
and the rest of the scoring pipeline is unaffected. This is by design:
the public repo describes WHERE the hook is, but not WHAT the detection
looks like, so cheat developers can't read the public code and learn
which patterns are being scanned for.

Contract (versioned via the `version` field in the payload):

REQUEST  POST /check
{
  "version": 1,
  "score": { ...all the user-visible score data... },
  "user":  { ...trust factor + history summary... },
  "beatmap": { ...static map characteristics that bound what's possible... },
  "replay": { "available": bool, "data_b64": str | None }
}

RESPONSE
{
  "verdict": "ok" | "low_concern" | "suspicious" | "critical" | "inconclusive",
  "confidence": float 0..1,
  "detectors_fired": [str, ...],            # IDs of detectors that flagged
  "reasons": [
    {"detector": str, "code": str, "severity": str, "evidence": dict}
  ],
  "trust_factor_applied": float 0..100,
  "metrics": dict                            # opaque to us, surfaced for admin debug
}

Failure modes (all silently swallowed — anti-cheat MUST NEVER block a
legit score from being saved):

- Service unreachable / timeout         → returns None, logs WARNING
- Service returned non-2xx              → returns None, logs WARNING
- Service returned malformed JSON       → returns None, logs WARNING
- ANTICHEAT_URL is empty / not set      → returns None, no log (feature disabled)

The score has already been committed to the DB by the time we get here.
This call is purely advisory — it produces a `SuspiciousAlert` row that
admins act on manually. There is NO automatic ban path from this client.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from app.config import settings
from app.log import log

if TYPE_CHECKING:
    pass

logger = log("AnticheatClient")


# ─── Module-level HTTP client ───────────────────────────────────────────────
#
# Reused across submissions so we share connection-pool + TLS keepalive.
# Lazily constructed because settings.anticheat_url may not be configured
# until after import time, and we never want this module's import to
# fail just because the feature is off.

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=2.0,
                read=getattr(settings, "anticheat_timeout_sec", 8.0),
                write=2.0,
                pool=2.0,
            ),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _client


async def close_client() -> None:
    """Module shutdown hook — call from app lifespan teardown so connections
    are closed cleanly. Safe to call multiple times."""
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        except Exception:
            pass
        _client = None


async def submit_for_analysis(
    *,
    score_payload: dict[str, Any],
    user_payload: dict[str, Any],
    beatmap_payload: dict[str, Any],
    replay_b64: str | None = None,
) -> dict[str, Any] | None:
    """Send a score + user + beatmap + (optional) replay snapshot to the
    private detection service and return its verdict.

    Returns None when the feature is disabled or the call fails. Callers
    MUST handle None as "no signal" and NOT treat it as either pass or
    fail — the score has already been accepted by the time we get here.

    The replay blob is base64-encoded if present so the JSON payload stays
    a single document. The detection service decodes lazily — most
    detectors only need the score+user payload to fire, and decoding ~MB
    of LZMA-compressed frames per score would dominate the request budget
    if we did it eagerly.
    """

    url = (getattr(settings, "anticheat_url", "") or "").strip()
    if not url:
        # Feature disabled. No log — this is the default state for forks
        # / for environments where the private service isn't deployed.
        return None

    payload = {
        "version": 1,
        "score": score_payload,
        "user": user_payload,
        "beatmap": beatmap_payload,
        "replay": {
            "available": replay_b64 is not None,
            "data_b64": replay_b64,
        },
    }

    token = (getattr(settings, "anticheat_token", "") or "").strip()
    headers: dict[str, str] = {"User-Agent": "g0v0-server-anticheat-client/1"}
    if token:
        headers["X-AC-Token"] = token

    endpoint = f"{url.rstrip('/')}/check"

    try:
        client = _get_client()
        resp = await client.post(endpoint, json=payload, headers=headers)
    except httpx.TimeoutException:
        logger.warning(
            "Anticheat service timeout for score_id={}",
            score_payload.get("score_id"),
        )
        return None
    except httpx.HTTPError as exc:
        logger.warning(
            "Anticheat service HTTP error for score_id={}: {}",
            score_payload.get("score_id"), exc,
        )
        return None
    except Exception as exc:
        # Belt-and-suspenders: even unanticipated failures (DNS, SSL, etc)
        # must NOT escape from this function and break the score pipeline.
        logger.warning(
            "Anticheat service unexpected error for score_id={}: {}",
            score_payload.get("score_id"), exc,
        )
        return None

    if resp.status_code != 200:
        logger.warning(
            "Anticheat service returned {} for score_id={}",
            resp.status_code, score_payload.get("score_id"),
        )
        return None

    try:
        result = resp.json()
    except Exception as exc:
        logger.warning(
            "Anticheat service returned non-JSON for score_id={}: {}",
            score_payload.get("score_id"), exc,
        )
        return None

    if not isinstance(result, dict):
        logger.warning(
            "Anticheat service returned non-object for score_id={}",
            score_payload.get("score_id"),
        )
        return None

    return result
