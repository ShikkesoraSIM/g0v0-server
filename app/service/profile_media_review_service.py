from __future__ import annotations

from app.database.profile_media_review import (
    STATUS_PENDING,
    STATUS_RESOLVED,
    ProfileMediaReview,
)

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select


async def record_media_upload(
    session: AsyncSession,
    *,
    user_id: int,
    media_type: str,
    url: str,
    storage_path: str,
    filehash: str | None,
    is_nsfw: bool,
) -> None:
    """Keep the NSFW review queue in sync with a fresh avatar/cover upload.

    A previously-current pending row for this media type is no longer live once
    the file is overwritten, so it is marked resolved and dropped from the
    queue. When the new upload is NSFW-flagged we enqueue a fresh pending row so
    it shows up in the admin review feed. The caller is responsible for
    committing the surrounding transaction.
    """
    existing = (
        await session.exec(
            select(ProfileMediaReview).where(
                col(ProfileMediaReview.user_id) == user_id,
                col(ProfileMediaReview.media_type) == media_type,
                col(ProfileMediaReview.is_current).is_(True),
            )
        )
    ).all()
    for row in existing:
        row.is_current = False
        if row.status == STATUS_PENDING:
            row.status = STATUS_RESOLVED
        session.add(row)

    if is_nsfw:
        session.add(
            ProfileMediaReview(
                user_id=user_id,
                media_type=media_type,
                url=url,
                storage_path=storage_path,
                filehash=filehash,
                status=STATUS_PENDING,
                is_current=True,
            )
        )
