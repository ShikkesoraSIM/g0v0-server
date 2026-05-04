from __future__ import annotations

from datetime import UTC, datetime
from html import escape
from typing import Any

from fastapi import Query
from sqlmodel import col, select

from app.dependencies.database import Database

from .router import router


def _ts(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year=year, month=month, day=day, hour=hour, minute=minute, tzinfo=UTC)


_STREAM_ID = 1001
_STREAM_NAME = "lazer"
_STREAM_DISPLAY_NAME = "Torii"


_RAW_BUILDS: list[dict[str, Any]] = [
    # --------------------------------------------------------------------------
    # May 4, 2026 (late) — Cursor system overhaul (Torii Exclusive)
    # --------------------------------------------------------------------------
    {
        # 504 day, build #1. Intentionally distinct from the 504.0 ops build's id (20260504).
        "id": 202605041,
        "version": "2026.504.1",
        "display_version": "2026.504.1-torii",
        "created_at": _ts(2026, 5, 4, 22, 0),
        "users": 0,
        "entries": [
            ("add", "client", "Cursor size hotkey — Ctrl+Shift+Mouse Wheel adjusts the gameplay cursor size live without opening Settings. A new OSD-style preview pill pops in showing your skin's actual cursor at the current size, with a small TORII EXCLUSIVE header so you can see this is a Torii feature at a glance."),
            ("add", "client", "Three-way menu cursor style picker — Lazer Default (the textured arrow + pink flash), Use Skin's Gameplay Cursor (your skin's cursor.png + cursormiddle.png with the same Expand / Contract click feel the playfield uses), or Use Torii Cursor (a translucent pink ring with a white centre dot that overrides whatever the active skin ships). Lives at Settings → User Interface → General and is mirrored into Settings → Torii → Interface."),
            ("add", "client", "Both gameplay-shaped menu cursor modes get the full skin trail — direct port of the osu! ruleset's CursorTrail pipeline. Skins with cursor.png + cursormiddle.png get the smooth additive trail; skins with cursor.png but no cursormiddle.png get the classic disjoint long-tail dotted look that trail-heavy skins lean on, decision keyed off the same provider that supplied the cursor texture so head and trail can never disagree."),
            ("add", "client", "Menu cursor + trail live-rebuild whenever you change skins — texture, trail, rotation, disjoint mode all swap on the fly. No restart, no reopening any container, the new skin's cursor is on screen the moment the active skin changes."),
            ("fix", "client", "Menu cursor now honours the skin's CursorRotate value from skin.ini instead of always spinning. Asymmetric or directional cursors stay upright as the skin author intended — read through a name-equivalent local enum that resolves to the same skin.ini entry the playfield cursor reads."),
            ("fix", "client", "Menu cursor trail no longer corrupts the gameplay cursor in actual play. The port replicated upstream's `Texture.ScaleAdjust *= 1.6f` line, but LegacySkin hands out the same Texture wrapper across calls — every menu trail rebuild was clobbering the texture's ScaleAdjust while the playfield trail still held the same reference. Plus the trail kept rendering during gameplay because PopOut only faded the cursor head, not the trail. Both fixed; gameplay cursor is back to byte-identical with vanilla."),
        ],
    },
    # --------------------------------------------------------------------------
    # May 4, 2026 — Operations release (admin tooling + score reliability + supporter QoL)
    # --------------------------------------------------------------------------
    {
        "id": 20260504,
        "version": "2026.504.0",
        "display_version": "2026.504.0-torii",
        "created_at": _ts(2026, 5, 4, 12, 0),
        "users": 0,
        "entries": [
            ("fix", "server", "Score-feedback self-heal: after every passed submission we now verify the leaderboard row landed; if it didn't, we retry inline once and queue a background retry. Fixes the recurring 'play submitted but Overall Ranking shows empty' bug."),
            ("add", "gameplay", "Optional double-confirm for Retry / Quit on long pause and fail screens. After 60s of active play, the dangerous buttons need a second click within 5s with a draining countdown bar. Toggle in Settings → Torii → Gameplay."),
            ("add", "client", "Settings sidecar (torii.ini) — every Torii-only setting (custom hue, donator accent hue, alpha-feature unlocks, the new gameplay confirm) now persists in its own file alongside osu.cfg. Sharing the data folder with the official lazer client no longer wipes them when lazer rewrites osu.cfg without those keys."),
            ("add", "admin", "Maintenance Mode — Redis-backed toggle that blocks score submission server-wide for non-admins, with a configurable banner message and a site-wide amber stripe polled every 30 seconds. Auth and admin endpoints stay open so it can never lock the operator out."),
            ("add", "admin", "Changelog Editor — DB-backed admin page for managing builds and entries, including a one-click 'Import from GitHub commit' modal. The public /changelog page now reads from the DB and falls back to the hardcoded list when the table is empty."),
            ("add", "admin", "Per-user PP recalc queue with a searchable user dropdown, single-worker concurrency, and a status panel that polls every 5s while a job is running. Per-task stdout tail surfaces failures without leaving the page."),
            ("add", "admin", "Beatmap Blacklist redesign supporting single-difficulty bans next to whole-set bans, with stats tiles and a scope filter. Daily Challenges get a 🎲 random-pick modal. Global Announcements gain a 'Show as popup' toggle that piggy-backs on the medal-unlock overlay so the message actually interrupts gameplay."),
            ("add", "web", "Top Plays page at /rankings/top-plays — server-wide PP scoreboard, paginated, mode-aware. Profiles now show join date + last-seen, a Daily Challenge stats card when relevant, and a playtime-hours hover on the Play Time stat. Admin viewers also get a Suspicious Activity banner on flagged users."),
            ("add", "web", "Discord feed integration — title grants and new-account registrations both post embeds to the operator's configured channel for live operations visibility."),
            ("fix", "ui", "'Show more' on top plays no longer clobbers loaded scores back to the first six. Navbar quick-search reliably closes on a result click, the Close button, and any URL change. Locked supporter accent rows let actual supporters interact with the controls instead of swallowing every click."),
            ("misc", "client", "Removed the unsupported osu! Space ruleset (dragging dead build weight). Removed the misleading 'This is not an official build' notification. The 'Current server' notification now fires exactly once per session."),
            ("misc", "server", "Newtonsoft.Json bumped to 13.0.4 to clear an alembic startup error, plus the usual round of small bugfixes and refactors across the stack."),
        ],
    },
    # --------------------------------------------------------------------------
    # April 29, 2026 — Aura visual quality overhaul (final release)
    # --------------------------------------------------------------------------
    {
        "id": 20260429,
        "version": "2026.429.1",
        "display_version": "2026.429.1-torii",
        "created_at": _ts(2026, 4, 29, 3, 0),
        "users": 0,
        "entries": [
            ("add", "auras", "User auras now render in EVERY context the username appears: slanted song-select leaderboard, V2 leaderboard, in-game gameplay HUD, chat lines, all four user panels (Brick / Grid / List / Rank), and the profile header."),
            ("fix", "auras", "Glow halo now hugs the actual letter shapes of the username (Photoshop-style outer glow) instead of rendering as a rectangular box behind the bounding box. Implemented via a buffered text mirror with gaussian blur."),
            ("fix", "auras", "Particle spawn area now bound to the rendered text bounds instead of the wrapper width — fixes particles drifting visibly to the right of the username in TruncatingSpriteText contexts (slanted leaderboard, gameplay HUD)."),
            ("fix", "auras", "Slanted song-select leaderboard alignment fixed — UserAuraContainer.Wrap now preserves the wrapped target's Shear so the wrapper renders upright together with the text + glow + emitter inside it."),
            ("fix", "auras", "Particle sizes now scale with the username's font size — chat-density rows (~13px) get proportionally tiny particles, profile-header usernames (~32-40px) get larger ones."),
            ("fix", "auras", "Admin glow softened from saturated cherry red to a coral-leaning pink-red so it reads as a glowy halo behind chat names instead of a wall of saturation."),
            ("fix", "auras", "Anchor.Centre right-drift bug fixed for direct-add particles (Admin sparkles, Dev bits/brackets) — the entire Dev aura was visibly shifted right of the name."),
            ("add", "auras", "Dev aura adds two new particle types: small 0/1 binary digits and operator glyphs (slash / asterisk / equals / plus). Cadence bumped to 170ms and MaxAlive to 12 so the variety actually shows."),
            ("fix", "auras", "Goof leaves now spread in a wider envelope around the username instead of bunching on top of the letters — they read as a halo of leaves drifting nearby."),
            ("add", "auras", "AuraPreset gains GlowColour property; six default presets (Admin / Dev / Mod / QAT / Supporter / Goof) all set their own halo tint matching their particle palette."),
            ("misc", "tests", "Added TestSceneUserAuras (synthetic grid for tuning) and TestSceneAurasInRealUI (real production drawables — leaderboards, chat, panels, profile header — wired up with fake users carrying each preset's group, so reviewers can see every aura against every UI surface in one place)."),
        ],
    },
    # --------------------------------------------------------------------------
    # April 28, 2026 — Donations + Supporter aura
    # --------------------------------------------------------------------------
    {
        "id": 20260428,
        "version": "2026.428.0",
        "display_version": "2026.428.0-torii",
        "created_at": _ts(2026, 4, 28, 18, 0),
        "users": 0,
        "entries": [
            ("add", "donations", "Donation pipeline live end-to-end via Ko-fi: donations grant the donor a Supporter title + pink hearts aura while the donor is currently supporting, plus a permanent Donator badge for everyone who has ever donated. Lapsed donors lose the active aura but keep the Donator badge."),
            ("add", "donations", "Discord forwarding for every donation event — matched donations post a celebratory embed, unmatched donations post an amber embed prompting admin review, duplicate webhook deliveries post a grey 'no-op' embed."),
            ("add", "donations", "Atomic webhook handler with idempotency on (provider, provider_transaction_id) so Ko-fi retries can't double-credit a donor."),
            ("add", "auras", "Single Supporter Aura (pink hearts rising slowly) replaces the previous bronze / silver / gold tier system. No tiers, no premium-tier optics — donations cover server costs proportionally and there is no gameplay advantage."),
            ("add", "web", "Heart 'Support Torii' button in the navbar (next to search and bell) opens a small popover with a CTA to Ko-fi. Mirrors osu!'s 'support the game' affordance with Torii's pink palette."),
            ("add", "web", "Matching heart link in the home footer for users scrolling to the bottom."),
            ("add", "admin", "New 'Donations' tab in the admin panel: queue of unmatched donations with an inline 'Torii username' input and a Match button per row. Calls the same apply_supporter_grant the webhook uses, so manual and automatic matches produce byte-identical state."),
            ("add", "admin", "Donations stats card: total raised by currency, total donations, currently active supporters, lifetime donators, and pending unmatched count."),
            ("misc", "server", "Schema migration: donations table + total_supporter_months counter on lazer_users + kofi_display_name field for auto-matching future donations from the same donor."),
        ],
    },
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
    # When the raw dict came from the DB it carries _stream_*_override
    # keys so the rendered build references its actual stream rather
    # than always echoing the hardcoded "lazer / Torii". Hardcoded
    # builds don't have these keys, so the helper still returns the
    # legacy stub for them.
    if "_stream_name_override" in raw:
        update_stream = {
            "id": _STREAM_ID,
            "name": raw["_stream_name_override"],
            "display_name": raw["_stream_display_override"],
            "is_featured": True,
            "user_count": 0,
        }
    else:
        update_stream = _stream_stub()
    return {
        "id": raw["id"],
        "version": raw["version"],
        "display_version": raw["display_version"],
        "users": raw["users"],
        "created_at": raw["created_at"],
        "update_stream": update_stream,
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


# ─── DB-backed read paths ────────────────────────────────────────────────
#
# When the admin Changelog Editor (app/router/private/changelog.py) has
# any builds in the `changelog_builds` table, the read endpoints below
# serve from DB. When the table is empty (first deploy of the editor,
# fresh dev DB, etc.), they fall back to the historical hardcoded
# `_RAW_BUILDS` list above so the changelog page never appears blank.
#
# Both code paths return the same JSON contract — same key set, same
# nested shapes — so the frontend can't tell them apart. That keeps the
# cutover from "all hardcoded" to "all editor-managed" boring.


def _db_build_to_raw_dict(
    build: Any, entries: list[Any], stream_name: str, stream_display: str
) -> dict[str, Any]:
    """Adapt a (ChangelogBuild + entries + stream) ORM trio into the same
    dict shape that the existing _full_build_payload helpers consume.
    Avoids duplicating the rendering logic per code path."""
    return {
        "id": build.id,
        "version": build.version,
        "display_version": build.display_version,
        "created_at": build.created_at,
        "users": build.users,
        "github_url": build.github_url,
        "_stream_name_override": stream_name,
        "_stream_display_override": stream_display,
        "entries": [(e.type, e.category, e.title) for e in entries],
    }


@router.get("/changelog", tags=["Misc"], name="Changelog index")
async def changelog_index(
    session: Database,
    stream: str | None = Query(default=None),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
):
    del from_, to

    # ── DB path ──────────────────────────────────────────────────────
    # Avoids importing app.database at module top because that drags in
    # the full SQLModel registry; lazy import keeps changelog.py cheap
    # to import for tests / scripts.
    from app.database import ChangelogBuild, ChangelogEntry, ChangelogStream

    db_streams = (await session.exec(select(ChangelogStream))).all()
    if db_streams:
        # Pick the stream the request asked for, defaulting to the
        # featured one (or the first row if none flagged).
        target_stream = next((s for s in db_streams if s.name == stream), None)
        if target_stream is None and stream is None:
            target_stream = next((s for s in db_streams if s.is_featured), db_streams[0])
        if target_stream is None:
            return {
                "streams": [],
                "builds": [],
                "search": {"stream": stream, "from": None, "to": None, "limit": 21},
                "cursor_string": None,
            }

        builds = (
            await session.exec(
                select(ChangelogBuild)
                .where(ChangelogBuild.stream_id == target_stream.id)
                .order_by(col(ChangelogBuild.created_at).desc())
            )
        ).all()

        if builds:
            # Eager-fetch entries in one query keyed by build_id.
            build_ids = [b.id for b in builds]
            entry_rows = (
                await session.exec(
                    select(ChangelogEntry)
                    .where(col(ChangelogEntry.build_id).in_(build_ids))
                    .order_by(col(ChangelogEntry.id))
                )
            ).all()
            entries_by_build: dict[int, list[Any]] = {}
            for e in entry_rows:
                entries_by_build.setdefault(e.build_id, []).append(e)

            raw_builds = [
                _db_build_to_raw_dict(
                    b, entries_by_build.get(b.id, []), target_stream.name, target_stream.display_name
                )
                for b in builds
            ]

            stream_payload = {
                "id": target_stream.id,
                "name": target_stream.name,
                "display_name": target_stream.display_name,
                "is_featured": target_stream.is_featured,
                "user_count": target_stream.user_count,
                "latest_build": _build_ref(raw_builds[0]),
            }

            full_builds: list[dict[str, Any]] = []
            for i, build in enumerate(raw_builds):
                previous_raw = raw_builds[i + 1] if i + 1 < len(raw_builds) else None
                next_raw = raw_builds[i - 1] if i - 1 >= 0 else None
                full_builds.append(_full_build_payload(build, previous_raw, next_raw))

            return {
                "streams": [stream_payload],
                "builds": full_builds,
                "search": {"stream": stream or target_stream.name, "from": None, "to": None, "limit": 21},
                "cursor_string": None,
            }
        # Fall through to the hardcoded path when there are no builds yet.

    # ── Hardcoded fallback ───────────────────────────────────────────
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
async def changelog_build(session: Database, stream: str, version: str):
    # ── DB path ──────────────────────────────────────────────────────
    from app.database import ChangelogBuild, ChangelogEntry, ChangelogStream

    db_stream = (await session.exec(select(ChangelogStream).where(ChangelogStream.name == stream))).first()
    if db_stream is not None:
        all_builds = (
            await session.exec(
                select(ChangelogBuild)
                .where(ChangelogBuild.stream_id == db_stream.id)
                .order_by(col(ChangelogBuild.created_at).desc())
            )
        ).all()
        if all_builds:
            for i, b in enumerate(all_builds):
                if b.version != version:
                    continue
                entries = (
                    await session.exec(
                        select(ChangelogEntry)
                        .where(ChangelogEntry.build_id == b.id)
                        .order_by(col(ChangelogEntry.id))
                    )
                ).all()
                # Build the dict pseudo-version of the prev/next neighbours
                # too so _full_build_payload can navigate.
                def _to_raw(bb, ee):
                    return _db_build_to_raw_dict(bb, ee, db_stream.name, db_stream.display_name)

                prev_b = all_builds[i + 1] if i + 1 < len(all_builds) else None
                next_b = all_builds[i - 1] if i - 1 >= 0 else None
                # We only need the prev/next ref shape (no entries) — keep
                # the second arg to _to_raw as an empty list.
                this_raw = _to_raw(b, entries)
                prev_raw = _to_raw(prev_b, []) if prev_b else None
                next_raw = _to_raw(next_b, []) if next_b else None
                return _full_build_payload(this_raw, prev_raw, next_raw)
            # Stream exists in DB but version not found — fall through
            # to hardcoded only if the stream name matches the legacy one,
            # otherwise it's a real 404.
            if stream != _STREAM_NAME:
                return {"detail": "build not found"}

    # ── Hardcoded fallback ───────────────────────────────────────────
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
