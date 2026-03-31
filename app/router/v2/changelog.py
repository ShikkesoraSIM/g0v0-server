from __future__ import annotations

from datetime import UTC, datetime
from html import escape
from typing import Any

from fastapi import Query

from .router import router


def _ts(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year=year, month=month, day=day, hour=hour, minute=minute, tzinfo=UTC)


_STREAM_ID = 1001
_STREAM_NAME = "lazer"
_STREAM_DISPLAY_NAME = "Torii"


_RAW_BUILDS: list[dict[str, Any]] = [
    {
        "id": 20260331,
        "version": "2026.331.0",
        "display_version": "2026.331.0-torii",
        "created_at": _ts(2026, 3, 31, 9, 30),
        "users": 0,
        "entries": [
            ("add", "torii", "Torii settings section now includes Appearance and Connection groups."),
            ("add", "torii", "Added native changelog toolbar button and startup release notes notification."),
            ("add", "pp", "Added pp-dev unlock alias code: luv-weird-pp."),
            ("fix", "ui", "Moved custom UI hue controls into Torii section for cleaner settings layout."),
            ("fix", "network", "Added runtime API endpoint apply and safer host validation in Torii Connection."),
        ],
    },
    {
        "id": 20260330,
        "version": "2026.330.1",
        "display_version": "2026.330.1-torii",
        "created_at": _ts(2026, 3, 30, 22, 10),
        "users": 0,
        "entries": [
            ("fix", "pp", "Stabilised pp-variant request flow for profile/top-play refresh."),
            ("fix", "toolbar", "Aligned toolbar indicator spacing and visibility transitions."),
            ("misc", "client", "Improved local diagnostics around API endpoint and online mode checks."),
        ],
    },
    {
        "id": 20260329,
        "version": "2026.329.0",
        "display_version": "2026.329.0-torii",
        "created_at": _ts(2026, 3, 29, 19, 0),
        "users": 0,
        "entries": [
            ("add", "pp", "Added pp-dev mode toggle plumbing for Torii local testing."),
            ("add", "ui", "Introduced Torii alpha feature gate via code input."),
            ("fix", "download", "Reduced beatmap download pre-redirect latency in backend mirror probing."),
        ],
    },
]


def _stream_stub() -> dict[str, Any]:
    return {
        "id": _STREAM_ID,
        "name": _STREAM_NAME,
        "display_name": _STREAM_DISPLAY_NAME,
        "is_featured": True,
        "user_count": 0,
    }


def _build_ref(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": raw["id"],
        "version": raw["version"],
        "display_version": raw["display_version"],
        "users": raw["users"],
        "created_at": raw["created_at"],
        "update_stream": _stream_stub(),
    }


def _entry_payload(raw_build: dict[str, Any], idx: int, entry: tuple[str, str, str]) -> dict[str, Any]:
    change_type, category, title = entry
    entry_id = int(f"{raw_build['id']}{idx + 1:02d}")
    return {
        "id": entry_id,
        "repository": "torii-osu",
        "github_pull_request_id": None,
        "github_url": "https://github.com/shikkesora",
        "url": f"https://lazer.shikkesora.com/changelog/{raw_build['version']}",
        "type": change_type,
        "category": category,
        "title": title,
        "message_html": f"<p>{escape(title)}</p>",
        "major": change_type == "add",
        "created_at": raw_build["created_at"],
        "github_user": {
            "id": 1,
            "display_name": "Shikkesora",
            "github_url": "https://github.com/shikkesora",
            "osu_username": "Shikkesora",
            "user_id": 19,
            "user_url": "https://lazer.shikkesora.com/users/19",
        },
    }


def _full_build_payload(raw_build: dict[str, Any], previous_raw: dict[str, Any] | None, next_raw: dict[str, Any] | None) -> dict[str, Any]:
    payload = _build_ref(raw_build)
    payload["changelog_entries"] = [
        _entry_payload(raw_build, idx, entry)
        for idx, entry in enumerate(raw_build["entries"])
    ]
    payload["versions"] = {
        "previous": _build_ref(previous_raw) if previous_raw else None,
        "next": _build_ref(next_raw) if next_raw else None,
    }
    return payload


@router.get("/changelog", tags=["Misc"], name="Changelog index")
async def changelog_index(
    stream: str | None = Query(default=None),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
):
    del from_, to

    if stream and stream != _STREAM_NAME:
        return {
            "streams": [],
            "builds": [],
            "search": {"stream": stream, "from": None, "to": None, "limit": 21},
            "cursor_string": None,
        }

    raw_builds = sorted(_RAW_BUILDS, key=lambda b: b["created_at"], reverse=True)
    stream_payload = _stream_stub()
    stream_payload["latest_build"] = _build_ref(raw_builds[0])

    full_builds: list[dict[str, Any]] = []
    for i, build in enumerate(raw_builds):
        previous_raw = raw_builds[i + 1] if i + 1 < len(raw_builds) else None
        next_raw = raw_builds[i - 1] if i - 1 >= 0 else None
        full_builds.append(_full_build_payload(build, previous_raw, next_raw))

    return {
        "streams": [stream_payload],
        "builds": full_builds,
        "search": {"stream": stream or _STREAM_NAME, "from": None, "to": None, "limit": 21},
        "cursor_string": None,
    }


@router.get("/changelog/{stream}/{version}", tags=["Misc"], name="Changelog build")
async def changelog_build(stream: str, version: str):
    if stream != _STREAM_NAME:
        return {"detail": "build not found"}

    raw_builds = sorted(_RAW_BUILDS, key=lambda b: b["created_at"], reverse=True)

    for i, build in enumerate(raw_builds):
        if build["version"] != version:
            continue

        previous_raw = raw_builds[i + 1] if i + 1 < len(raw_builds) else None
        next_raw = raw_builds[i - 1] if i - 1 >= 0 else None
        return _full_build_payload(build, previous_raw, next_raw)

    return {"detail": "build not found"}
