from __future__ import annotations

from datetime import datetime

from app.utils import utcnow

from sqlalchemy import Column, DateTime, Text
from sqlmodel import VARCHAR, BigInteger, Boolean, Field, SQLModel

# Which piece of profile media a review row refers to.
MEDIA_AVATAR = "avatar"
MEDIA_COVER = "cover"

# A review row is PENDING while the (NSFW-flagged) media is live and unreviewed.
# REVOKED means an admin reset it to the default. RESOLVED means the user
# replaced it themselves (with SFW media or a newer upload) before anyone acted,
# so it no longer needs attention.
STATUS_PENDING = "pending"
STATUS_REVOKED = "revoked"
STATUS_RESOLVED = "resolved"


class ProfileMediaReview(SQLModel, table=True):
    """An NSFW-flagged avatar/cover upload queued for manual admin review.

    Uploads normally overwrite each other with no history, so this table is the
    record that powers the admin review feed. Only rows that are still
    ``is_current`` point at a file that actually exists in storage.
    """

    __tablename__: str = "profile_media_reviews"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(sa_column=Column(BigInteger, nullable=False, index=True))
    media_type: str = Field(sa_column=Column(VARCHAR(16), nullable=False, index=True))
    url: str = Field(sa_column=Column(Text, nullable=False))
    # Storage key so a revoke can delete the file directly.
    storage_path: str | None = Field(default=None, sa_column=Column(VARCHAR(512), nullable=True))
    filehash: str | None = Field(default=None, sa_column=Column(VARCHAR(128), nullable=True))

    status: str = Field(default=STATUS_PENDING, sa_column=Column(VARCHAR(16), nullable=False, index=True))
    # Whether this row still reflects the user's live media (i.e. the file exists).
    is_current: bool = Field(default=True, sa_column=Column(Boolean, nullable=False, index=True))

    created_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime, nullable=False, index=True))
    reviewed_at: datetime | None = Field(default=None, sa_column=Column(DateTime, nullable=True))
    reviewed_by_id: int | None = Field(default=None, sa_column=Column(BigInteger, nullable=True))
