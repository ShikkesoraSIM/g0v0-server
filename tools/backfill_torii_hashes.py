"""One-off backfill: register every historic Torii client hash that's
been seen in user_login_log so the "Playing on Torii client" badge
lights up for users on builds that pre-date the CI hash auto-registration.

Why this exists
---------------
Original CI hashed the .NET HOST bootstrapper (osu-torii.exe), but the
runtime ships the md5 of osu.Game.dll as ``version_hash``. So every
"registered" hash was the wrong file's md5 — no connecting client ever
matched, the badge stayed invisible for everyone, and the server's
unknown_hashes accumulator filled up with thousands of legitimate Torii
hashes that were misclassified as "unknown".

The CI fix lands in build-gu.yml (hash osu.Game.dll going forward),
but it only catches NEW releases. This script handles the back-catalog:
every distinct client_hash seen in user_login_log whose user-agent
matches the lazer pattern ("osu! v…") came from a Torii client (the
server is Torii-only, no other lazer-compatible client connects here),
so it's safe to assume Torii ownership and bulk-register.

Run from the g0v0-server container::

    docker exec osu_api_server python tools/backfill_torii_hashes.py

Idempotent: hashes already in the overrides file are left alone, the
script only ADDS entries that are missing.
"""

from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from dataclasses import dataclass

from sqlmodel import select

from app.database import User
from app.dependencies.database import get_session_factory
from app.service.client_verification_service import (
    get_client_verification_service,
    init_client_verification_service,
)


# Matches the user_agent string the lazer client sends on login —
# "osu! v2026.429.3-lazer" / "osu! v2026.502.0-lazer" / etc.
# The capture group lifts the version so each registration carries the
# build it actually came from instead of a generic "legacy" tag.
LAZER_VERSION_PATTERN = re.compile(r"^osu!\s+v(\S+-lazer)\s*$", re.IGNORECASE)


@dataclass
class CandidateHash:
    client_hash: str
    version: str
    seen_count: int


async def collect_lazer_hashes_from_login_log() -> list[CandidateHash]:
    """Group every distinct (hash, version) pair seen in user_login_log
    where the client_label matches the lazer release pattern."""
    factory = get_session_factory()
    async with factory() as session:
        # Pull the raw rows; sqlmodel/sqlalchemy will type the result
        # tuples for us. We don't need the user info for the registration
        # itself — purely de-duping by hash + extracted version.
        result = await session.exec(
            # noqa: type-ignore — UserLoginLog imported lazily below.
            select_loginlog_rows()
        )
        rows = result.all()

    grouped: dict[tuple[str, str], int] = defaultdict(int)
    for client_hash, client_label in rows:
        if not client_hash or not client_label:
            continue
        m = LAZER_VERSION_PATTERN.match(client_label.strip())
        if not m:
            continue
        version = m.group(1).lower()
        grouped[(client_hash.strip().lower(), version)] += 1

    return [
        CandidateHash(client_hash=h, version=v, seen_count=count)
        for (h, v), count in grouped.items()
    ]


def select_loginlog_rows():
    """Lazy import so the test environment can override UserLoginLog."""
    from sqlmodel import col

    from app.database.user_login_log import UserLoginLog

    # Only successful logins — failures often have garbage hashes.
    return select(UserLoginLog.client_hash, UserLoginLog.client_label).where(
        col(UserLoginLog.login_success) == True,  # noqa: E712 (sqlmodel needs ==True for the comparator)
        col(UserLoginLog.client_hash).is_not(None),
        col(UserLoginLog.client_label).is_not(None),
    )


async def main() -> None:
    print("Initialising client verification service...")
    await init_client_verification_service()
    service = get_client_verification_service()

    print("Scanning user_login_log for lazer-client hashes...")
    candidates = await collect_lazer_hashes_from_login_log()
    print(f"  Found {len(candidates)} distinct (hash, version) pairs.")

    already_registered = sum(
        1 for c in candidates if c.client_hash in service.versions
    )
    to_register = [c for c in candidates if c.client_hash not in service.versions]
    print(
        f"  Already registered: {already_registered}. "
        f"Pending: {len(to_register)}."
    )

    if not to_register:
        print("Nothing to backfill.")
        return

    # Most-seen first so the highest-traffic builds land in the
    # overrides file before the long tail. Helps if the script gets
    # interrupted partway through.
    to_register.sort(key=lambda c: c.seen_count, reverse=True)

    registered = 0
    for c in to_register:
        try:
            # OS unknown — login_log carries user-agent OS hints but the
            # version_hash itself is OS-agnostic and the picker doesn't
            # use the os field for matching, so empty is safe here.
            await service.assign_hash_override(
                c.client_hash,
                client_name="osu! Torii",
                version=c.version,
                os_name="",
                remove_from_unknown=True,
            )
            registered += 1
            print(
                f"  + {c.client_hash[:12]}…  {c.version:<24}  "
                f"(seen {c.seen_count}x)"
            )
        except Exception as exc:
            print(f"  ! {c.client_hash[:12]}…  failed: {exc}")

    print(f"\nDone. Registered {registered}/{len(to_register)} hashes.")


if __name__ == "__main__":
    asyncio.run(main())
