#!/usr/bin/env python3
"""Remove obvious test donation rows left over from webhook integration testing.

Targets rows that match ANY of:
  - donor_display_name in {"Jo Example", "Test Donor", "Test Donor 3"}
    (the names hard-coded in the smoke-test webhook curl invocations)
  - provider_transaction_id starts with "00000000-"
    (the sentinel txn id used by the test harness)

Real donations (e.g. the actual Ko-fi event from Mash → Mash39) are
preserved. Run dry-run first; nothing is mutated until --apply is passed.

Usage:
    docker compose exec app python tools/cleanup_test_donations.py            # dry-run
    docker compose exec app python tools/cleanup_test_donations.py --apply    # actually delete

Notes on what is NOT undone:
  - Supporter grants applied to whichever user the test donations were
    matched to (Shikkesora in our case) STAY. donor_end_at, support_level,
    is_supporter / has_supported and total_supporter_months are not
    rewound. Reversing those cleanly is fiddly (later real donations may
    have stacked on top of the test grants) and the admin can adjust
    their own state directly via SQL if they want a clean slate.
  - The unique (provider, provider_transaction_id) constraint guarantees
    re-running the script after a manual replay won't double-delete
    anything — the script is a no-op once the rows are gone.
"""

from __future__ import annotations

import asyncio
import os
import sys
from argparse import ArgumentParser

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.database.donation import Donation  # noqa: E402  (sys.path tweak above)
from app.dependencies.database import engine  # noqa: E402

from sqlalchemy import or_  # noqa: E402
from sqlmodel import col, select  # noqa: E402
from sqlmodel.ext.asyncio.session import AsyncSession  # noqa: E402


# Exact donor names used in the integration smoke tests. Update this
# list if more test fixtures get added — pattern-match by name rather
# than blanket-deleting all unmatched rows so we never destroy real
# anonymous donations.
TEST_DONOR_NAMES: tuple[str, ...] = ("Jo Example", "Test Donor", "Test Donor 3")

# Sentinel prefix used by the smoke-test webhook script for the txn id.
# Any real Ko-fi transaction id is a UUIDv4, so an all-zeros prefix is
# unambiguous.
TEST_TXN_PREFIX: str = "00000000-"


async def find_test_donations(session: AsyncSession) -> list[Donation]:
    stmt = select(Donation).where(
        or_(
            col(Donation.donor_display_name).in_(TEST_DONOR_NAMES),
            col(Donation.provider_transaction_id).startswith(TEST_TXN_PREFIX),
        )
    )
    return list((await session.exec(stmt)).all())


def fmt_amount(amount_cents: int, currency: str) -> str:
    return f"{amount_cents / 100:.2f} {currency}"


async def main() -> int:
    parser = ArgumentParser(description="Delete test donations from the donations table.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete. Without this flag the script only prints what it would do.",
    )
    args = parser.parse_args()

    async with AsyncSession(engine) as session:
        rows = await find_test_donations(session)

        if not rows:
            print("No test donations found — nothing to clean.")
            return 0

        print(f"Found {len(rows)} test donation row(s):\n")
        for d in rows:
            link = f"user_id={d.user_id}" if d.user_id else "unmatched"
            print(
                f"  id={d.id:<5} "
                f"{fmt_amount(d.amount_cents, d.currency):<12} "
                f"from {d.donor_display_name!s:<14} "
                f"({link})  "
                f"txn={d.provider_transaction_id}"
            )

        if not args.apply:
            print("\nDry run only. Re-run with --apply to delete these rows.")
            return 0

        for d in rows:
            await session.delete(d)
        await session.commit()
        print(f"\nDeleted {len(rows)} test donation row(s).")
        print(
            "\nReminder: matched rows' supporter grants on the linked users were NOT "
            "rewound (donor_end_at, total_supporter_months, support_level stay as-is). "
            "Adjust manually via SQL if a clean slate is wanted."
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
