"""SQLModel + Pydantic schemas for the changelog editor.

Three tables form a strict tree:

    changelog_streams 1 ── N changelog_builds 1 ── N changelog_entries

A *stream* is a release channel (we ship one: "lazer" / "Torii"). A *build*
is a tagged release on that stream (e.g. ``2026.503.8-torii``). An *entry*
is a single bullet point inside a build's notes (type + category + text).

Why this lives in its own module rather than inside the existing v2
changelog router: the router previously had only hardcoded ``_RAW_BUILDS``
data shaped as plain dicts, no ORM at all. Putting the table definitions
here keeps SQLModel imports out of the read path's hot module and matches
the pattern used by donation.py / suspicious_alert.py / etc.

Pydantic *Create / *Update schemas live here too so the admin router and
any future CLI consumers share one schema definition (no field drift).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.utils import utcnow

from sqlalchemy import Column, DateTime, ForeignKey, Text
from sqlmodel import JSON, Field, SQLModel, VARCHAR


# ─── Streams ─────────────────────────────────────────────────────────────


class ChangelogStreamBase(SQLModel):
    """Shared columns between the table model and the Pydantic schemas."""

    name: str = Field(sa_column=Column(VARCHAR(50), unique=True, nullable=False, index=True))
    display_name: str = Field(sa_column=Column(VARCHAR(100), nullable=False))
    is_featured: bool = Field(default=False)
    user_count: int = Field(default=0)


class ChangelogStream(ChangelogStreamBase, table=True):
    __tablename__: str = "changelog_streams"

    id: int | None = Field(default=None, primary_key=True, index=True)
    created_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime, nullable=False))
    updated_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime, nullable=False))


class ChangelogStreamCreate(ChangelogStreamBase):
    pass


class ChangelogStreamUpdate(SQLModel):
    name: str | None = None
    display_name: str | None = None
    is_featured: bool | None = None
    user_count: int | None = None


class ChangelogStreamResponse(ChangelogStreamBase):
    id: int
    created_at: datetime
    updated_at: datetime


# ─── Builds ──────────────────────────────────────────────────────────────


class ChangelogBuildBase(SQLModel):
    stream_id: int = Field(
        sa_column=Column(ForeignKey("changelog_streams.id"), nullable=False, index=True)
    )
    version: str = Field(sa_column=Column(VARCHAR(50), nullable=False))
    display_version: str = Field(sa_column=Column(VARCHAR(100), nullable=False))
    users: int = Field(default=0)
    # Optional pointer to the GitHub release / tag that this build maps to.
    # Useful for the editor's "open build on GitHub" link; nullable because
    # historical builds were authored before the editor existed.
    github_url: str | None = Field(default=None, sa_column=Column(VARCHAR(500), nullable=True))


class ChangelogBuild(ChangelogBuildBase, table=True):
    __tablename__: str = "changelog_builds"

    id: int | None = Field(default=None, primary_key=True, index=True)
    # created_at carries the *publish* date the admin chose, not the row
    # insertion timestamp. Indexed because the read endpoints sort by it.
    created_at: datetime = Field(
        default_factory=utcnow, sa_column=Column(DateTime, nullable=False, index=True)
    )
    updated_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime, nullable=False))


class ChangelogBuildCreate(ChangelogBuildBase):
    # Allow the admin form to override the timestamp. Defaults to now if
    # omitted so a fresh "create build" with no date does the right thing.
    created_at: datetime | None = None


class ChangelogBuildUpdate(SQLModel):
    version: str | None = None
    display_version: str | None = None
    users: int | None = None
    github_url: str | None = None
    created_at: datetime | None = None


class ChangelogBuildResponse(ChangelogBuildBase):
    id: int
    created_at: datetime
    updated_at: datetime


# ─── Entries ─────────────────────────────────────────────────────────────


class ChangelogEntryBase(SQLModel):
    build_id: int = Field(
        sa_column=Column(ForeignKey("changelog_builds.id"), nullable=False, index=True)
    )
    repository: str = Field(default="torii-osu", sa_column=Column(VARCHAR(100), nullable=False))
    github_pull_request_id: int | None = Field(default=None)
    github_url: str | None = Field(
        default="https://github.com/shikkesora", sa_column=Column(VARCHAR(500), nullable=True)
    )
    url: str | None = Field(default=None, sa_column=Column(VARCHAR(500), nullable=True))
    # Free-form so we don't bottleneck on enum migrations every time we
    # invent a new category. The editor UI constrains the choices.
    type: str = Field(default="misc", sa_column=Column(VARCHAR(20), nullable=False, index=True))
    category: str = Field(default="other", sa_column=Column(VARCHAR(20), nullable=False, index=True))
    title: str = Field(sa_column=Column(VARCHAR(500), nullable=False))
    message_html: str = Field(default="", sa_column=Column(Text, nullable=False))
    # Major changes get the bold / featured treatment in the rendered
    # changelog. Most entries are not major.
    major: bool = Field(default=False)
    # GitHub user blob (display_name, github_url, osu_username, user_id,
    # user_url). Optional because not every entry comes from a commit.
    github_user: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON, nullable=True))


class ChangelogEntry(ChangelogEntryBase, table=True):
    __tablename__: str = "changelog_entries"

    id: int | None = Field(default=None, primary_key=True, index=True)
    created_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime, nullable=False))
    updated_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime, nullable=False))


class ChangelogEntryCreate(ChangelogEntryBase):
    pass


class ChangelogEntryUpdate(SQLModel):
    repository: str | None = None
    github_pull_request_id: int | None = None
    github_url: str | None = None
    url: str | None = None
    type: str | None = None
    category: str | None = None
    title: str | None = None
    message_html: str | None = None
    major: bool | None = None
    github_user: dict[str, Any] | None = None


class ChangelogEntryResponse(ChangelogEntryBase):
    id: int
    created_at: datetime
    updated_at: datetime
