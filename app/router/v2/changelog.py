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
    # --------------------------------------------------------------------------
    # April 26, 2026
    # --------------------------------------------------------------------------
    {
        "id": 20260426,
        "version": "2026.426.0",
        "display_version": "2026.426.0-torii",
        "created_at": _ts(2026, 4, 26, 21, 0),
        "users": 0,
        "entries": [
            ("fix", "briefing", "Torii Briefing refresh and replay actions are now more reliable and no longer fall over when older local snapshot data is incomplete."),
            ("fix", "briefing", "Repeatedly pressing briefing actions now safely ignores stale async responses instead of opening the wrong card or crashing the overlay."),
            ("fix", "audio", "Experimental WASAPI audio mode is back in settings and reconnected to the upstream lazer audio path for behaviour closer to vanilla lazer."),
            ("fix", "audio", "Removed custom mixer and device handling that could cause duplicated hitsounds or inconsistent device switching on some setups."),
            ("add", "social", "Online users playing through the Torii client now show a small Torii badge in the social panel with a matching tooltip."),
        ],
    },
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # April 23, 2026
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    {
        "id": 20260423,
        "version": "2026.423.0",
        "display_version": "2026.423.0-torii",
        "created_at": _ts(2026, 4, 23, 18, 0),
        "users": 0,
        "entries": [
            ("fix", "audio", "Removed WASAPI experimental toggle from audio settings to prevent shared-config breakage when using the data wizard with vanilla osu!."),
            ("fix", "audio", "Added automatic cleanup of stale 'WASAPI Shared:' / 'WASAPI Exclusive:' device names on startup â€” restores audio for users affected by earlier builds."),
            ("fix", "security", "Restricted user sessions are now immediately invalidated â€” active OAuth tokens revoked on restriction so play cannot continue via spectator server."),
            ("add", "social", "Torii client badge (â›©) now appears next to online users in the social panel who are actively playing via the Torii client."),
            ("add", "social", "Hovering the Torii badge shows a tooltip: 'Playing in Torii client'."),
            ("misc", "client", "Added X-Client-Name: torii header to all SignalR hub connections for server-side client identification."),
        ],
    },
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # April 21â€“22, 2026
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    {
        "id": 20260422,
        "version": "2026.422.0",
        "display_version": "2026.422.0-torii",
        "created_at": _ts(2026, 4, 22, 20, 0),
        "users": 0,
        "entries": [
            ("add", "client", "Score cards now display 'Torii Client' label when a score was submitted from the Torii client (detected from version string)."),
            ("add", "client", "Legacy 'Shigetiro' version strings are mapped to 'Torii Client' for historical score display consistency."),
            ("fix", "client", "Removed unused React import in TitleBadge.tsx that caused TypeScript build error TS6133."),
            ("fix", "web", "Rankings page mode dropdown is no longer hidden behind tab buttons â€” fixed CSS stacking context with explicit z-index."),
            ("fix", "web", "Auto Pilot (osuap) mode removed from rankings since it shares leaderboard space with osu! standard."),
            ("add", "web", "How-to-join page fully redesigned: Torii client prominently featured, feature showcase with 8 cards (pp-dev, Briefing, Custom hue, Performance extras, Mania Sunny rework, Title badges, Zero-loss migration, Multi-server)."),
        ],
    },
    {
        "id": 20260421,
        "version": "2026.421.0",
        "display_version": "2026.421.0-torii",
        "created_at": _ts(2026, 4, 21, 14, 0),
        "users": 0,
        "entries": [
            ("add", "gamemodes", "CTB (fruits) scores with Relax mod now live on a dedicated 'fruitsrx' leaderboard, fully separated from standard CTB."),
            ("add", "gamemodes", "Taiko scores with Relax mod now live on a dedicated 'taikorx' leaderboard, fully separated from standard Taiko."),
            ("misc", "database", "Migration applied: 220 CTB+RX scores and 30 Taiko+RX scores moved to their respective RX modes. Leaderboards recalculated."),
            ("fix", "gamemodes", "Removed backward-compatibility hack that caused CTB scores to appear in both CTB and CTB-RX rankings simultaneously."),
            ("add", "mods", "Freeze Frame and Hidden are no longer mutually incompatible â€” both can be selected together (osu! tools flash calc not used on this server)."),
        ],
    },
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # April 3, 2026  (big client release)
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    {
        "id": 20260403,
        "version": "2026.403.0",
        "display_version": "2026.403.0-torii",
        "created_at": _ts(2026, 4, 3, 7, 30),
        "users": 0,
        "entries": [
            ("add", "client", "Torii Daily Briefing overlay: shows today's rank, pp gain, and active recalculation status on login."),
            ("add", "client", "Briefing displays a radar snapshot of your score history and highlights recent improvements."),
            ("add", "client", "Custom UI hue slider in Torii settings â€” personalise the client accent colour independently of your osu! skin."),
            ("add", "client", "Performance extras: support for NVIDIA Reflex and AMD Anti-Lag 2 for reduced input latency on compatible hardware."),
            ("add", "client", "Unlimited FPS mode available beyond osu!'s standard cap when vsync is disabled."),
            ("add", "client", "Multi-server switching: connect to Torii or return to bancho in one click from Torii Connection settings."),
            ("add", "client", "Zero-loss migration wizard automatically detects your existing osu!lazer data folder and links it â€” no file copying required."),
            ("add", "client", "User title badges displayed in profiles and chat for server groups: admin, dev, mod, qat, pooler, tournament, advisor, alumni, supporter."),
            ("add", "client", "Elite groups (admin/dev) receive a pulsing glow animation on their title badge."),
            ("add", "mania", "Sunny skin rework for osu!mania â€” redesigned note appearance with improved readability."),
            ("misc", "client", "Client rebranded from 'Shigetiro' to 'Torii'. All internal references, app name, and installer updated."),
            ("misc", "packaging", "GitHub Actions workflow set up for automated Windows/macOS/Linux builds and release packaging."),
        ],
    },
    {
        "id": 20260401,
        "version": "2026.401.0",
        "display_version": "2026.401.0-torii",
        "created_at": _ts(2026, 4, 1, 12, 0),
        "users": 0,
        "entries": [
            ("add", "client", "Torii settings section added to the settings overlay with dedicated subsections: Briefing, Interface, Server Connection, Storage, and Experimental."),
            ("add", "client", "Native changelog button added to the game toolbar â€” shows all Torii updates without leaving the client."),
            ("add", "client", "Startup release notes notification shown when a new build is detected."),
            ("fix", "ui", "Torii-specific settings migrated out of general sections into the dedicated Torii section."),
            ("fix", "network", "API endpoint can now be changed at runtime in Torii Connection settings without restarting the client."),
            ("fix", "network", "Stricter hostname validation prevents accidentally entering invalid endpoint URLs."),
        ],
    },
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # March 29â€“31, 2026  (existing entries kept)
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Mid-March 2026
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    {
        "id": 20260320,
        "version": "2026.320.0",
        "display_version": "2026.320.0-torii",
        "created_at": _ts(2026, 3, 20, 15, 0),
        "users": 0,
        "entries": [
            ("add", "pp", "pp-dev system integrated: scores are evaluated against the latest cutting-edge difficulty algorithm, more up-to-date than bancho."),
            ("add", "pp", "Profile pp totals and score pp values reflect pp-dev calculations when enabled."),
            ("add", "rankings", "Rankings page launched at lazer.shikkesora.com/rankings with support for all 8 game modes."),
            ("add", "rankings", "Country filter added to user rankings â€” view top players from any country."),
            ("add", "rankings", "Performance and score-based ranking types available."),
            ("add", "rankings", "Country rankings tab shows total performance by country flag."),
            ("fix", "web", "Pagination controls added to rankings â€” navigate through all ranked players."),
        ],
    },
    {
        "id": 20260315,
        "version": "2026.315.0",
        "display_version": "2026.315.0-torii",
        "created_at": _ts(2026, 3, 15, 10, 0),
        "users": 0,
        "entries": [
            ("add", "server", "CTB Relax (fruitsrx) and Taiko Relax (taikorx) game modes added as distinct leaderboards â€” the first private osu! server to offer these."),
            ("add", "server", "Score submission pipeline extended to route RX mod scores to the correct RX leaderboard."),
            ("add", "server", "Best-score tracking updated for all 8 game modes including the new RX modes."),
            ("add", "web", "Game mode selector on web updated to include CTB-RX and Taiko-RX tabs."),
            ("fix", "server", "Score re-calculation system now correctly handles cross-mode score migration without data loss."),
        ],
    },
    {
        "id": 20260310,
        "version": "2026.310.0",
        "display_version": "2026.310.0-torii",
        "created_at": _ts(2026, 3, 10, 18, 0),
        "users": 0,
        "entries": [
            ("add", "web", "User profile pages live â€” avatar, cover, statistics, recent scores, best scores, first place scores."),
            ("add", "web", "Score detail pages with mod display, grade, accuracy, pp value, and hit statistics."),
            ("add", "web", "Beatmap listing with search, filter by mode/status/category, and pagination."),
            ("add", "web", "Beatmapset pages with embedded audio preview and download button."),
            ("fix", "web", "Avatar NSFW preference respected on all public listing pages."),
            ("misc", "infrastructure", "Frontend deployed to lazer.shikkesora.com via Docker static file serving."),
        ],
    },
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Early March 2026
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    {
        "id": 20260301,
        "version": "2026.301.0",
        "display_version": "2026.301.0-torii",
        "created_at": _ts(2026, 3, 1, 12, 0),
        "users": 0,
        "entries": [
            ("add", "server", "Torii server opened for registrations â€” custom osu! private server built on g0v0-server."),
            ("add", "server", "Score submission, leaderboards, and user statistics working for all standard osu! game modes (osu!, Taiko, CTB, Mania)."),
            ("add", "server", "osu! Relax (osurx) leaderboard added alongside standard osu! mode."),
            ("add", "server", "Beatmap mirroring and download proxy set up â€” fast beatmap downloads for all players."),
            ("add", "server", "Spectator server (m1pp) integrated â€” watch any online player in real time."),
            ("add", "client", "Torii osu! client forked from upstream osu!lazer â€” pre-configured to connect to lazer-api.shikkesora.com."),
            ("add", "client", "First-run data wizard detects existing osu!lazer installation and links data without copying files."),
            ("misc", "server", "Production deployment on EU server (Hetzner, Frankfurt) with automated health checks."),
        ],
    },
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # February 2026  (initial setup)
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    {
        "id": 20260215,
        "version": "2026.215.0",
        "display_version": "2026.215.0-torii",
        "created_at": _ts(2026, 2, 15, 8, 0),
        "users": 0,
        "entries": [
            ("add", "server", "g0v0-server forked and adapted as the Torii API backend â€” full osu!lazer protocol support."),
            ("add", "server", "MySQL database schema set up with Alembic migrations for score, user, and beatmap tables."),
            ("add", "server", "OAuth2 authentication with osu!lazer client token flow."),
            ("add", "server", "Beatmap sync pipeline integrated â€” automatically imports ranked/loved beatmaps from bancho mirrors."),
            ("add", "server", "User registration, login, and profile API endpoints operational."),
            ("misc", "infrastructure", "Docker Compose stack configured (API, MySQL, Redis, Nginx reverse proxy)."),
            ("misc", "infrastructure", "GitHub Actions CI/CD pipeline for automated deployment to production server."),
        ],
    },
    {
        "id": 20260201,
        "version": "2026.201.0",
        "display_version": "2026.201.0-torii",
        "created_at": _ts(2026, 2, 1, 0, 0),
        "users": 0,
        "entries": [
            ("misc", "server", "Torii project started â€” private osu! server for experimental features and cutting-edge pp calculations."),
            ("misc", "server", "Development environment configured. First successful score submission test completed."),
            ("misc", "client", "Initial Torii client build â€” osu!lazer fork with custom server endpoint pointing to Torii API."),
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


def _full_build_payload(
    raw_build: dict[str, Any],
    previous_raw: dict[str, Any] | None,
    next_raw: dict[str, Any] | None,
) -> dict[str, Any]:
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
