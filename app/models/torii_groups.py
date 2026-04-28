"""
Torii server user groups / titles.

Each group maps to an osu!-compatible APIUserGroup dict that the client
renders via GroupBadgeFlow.  The `identifier` drives the glow effect in the
patched client; the `colour` hex is shown directly in the badge text.
"""

from typing import TypedDict


class ToriiGroupDef(TypedDict, total=False):
    id: int
    identifier: str
    name: str
    short_name: str
    colour: str
    has_listings: bool
    has_playmodes: bool
    is_probationary: bool
    playmodes: list[str]


# ─────────────────────────────────────────────────────────────────────────────
# Canonical group definitions
# key  → used in User.torii_titles  (list of these keys stored in JSON column)
# ─────────────────────────────────────────────────────────────────────────────
TORII_GROUPS: dict[str, ToriiGroupDef] = {
    # ── Staff ────────────────────────────────────────────────────────────────
    "admin": {
        "id": 1001,
        "identifier": "torii-admin",
        "name": "Torii Admin",
        "short_name": "ADM",
        "colour": "#FF3B3B",
        "has_listings": False,
        "has_playmodes": False,
        "is_probationary": False,
    },
    "mod": {
        "id": 1002,
        "identifier": "torii-mod",
        "name": "Moderator",
        "short_name": "MOD",
        "colour": "#4A90E2",
        "has_listings": False,
        "has_playmodes": False,
        "is_probationary": False,
    },
    "dev": {
        "id": 1003,
        "identifier": "torii-dev",
        "name": "Developer",
        "short_name": "DEV",
        "colour": "#00E5FF",
        "has_listings": False,
        "has_playmodes": False,
        "is_probationary": False,
    },
    # ── Competitive / mapping ────────────────────────────────────────────────
    "pooler": {
        "id": 1004,
        "identifier": "torii-pooler",
        "name": "Map Pooler",
        "short_name": "MAP",
        "colour": "#B24BF3",
        "has_listings": False,
        "has_playmodes": False,
        "is_probationary": False,
    },
    "qat": {
        "id": 1005,
        "identifier": "torii-qat",
        "name": "Quality Assurance",
        "short_name": "QAT",
        "colour": "#FFD700",
        "has_listings": False,
        "has_playmodes": False,
        "is_probationary": False,
    },
    "tournament": {
        "id": 1006,
        "identifier": "torii-tournament",
        "name": "Tournament Staff",
        "short_name": "TRN",
        "colour": "#3F51B5",
        "has_listings": False,
        "has_playmodes": False,
        "is_probationary": False,
    },
    # ── Mode advisors (one per gamemode) ─────────────────────────────────────
    "advisor-osu": {
        "id": 1010,
        "identifier": "torii-advisor",
        "name": "osu! Advisor",
        "short_name": "ADV",
        "colour": "#FF66AA",
        "has_listings": False,
        "has_playmodes": True,
        "is_probationary": False,
        "playmodes": ["osu"],
    },
    "advisor-taiko": {
        "id": 1011,
        "identifier": "torii-advisor",
        "name": "Taiko Advisor",
        "short_name": "ADV",
        "colour": "#FF6B35",
        "has_listings": False,
        "has_playmodes": True,
        "is_probationary": False,
        "playmodes": ["taiko"],
    },
    "advisor-catch": {
        "id": 1012,
        "identifier": "torii-advisor",
        "name": "Catch Advisor",
        "short_name": "ADV",
        "colour": "#26C6A6",
        "has_listings": False,
        "has_playmodes": True,
        "is_probationary": False,
        "playmodes": ["fruits"],
    },
    "advisor-mania": {
        "id": 1013,
        "identifier": "torii-advisor",
        "name": "Mania Advisor",
        "short_name": "ADV",
        "colour": "#E91E8C",
        "has_listings": False,
        "has_playmodes": True,
        "is_probationary": False,
        "playmodes": ["mania"],
    },
    # ── Honorary ─────────────────────────────────────────────────────────────
    "alumni": {
        "id": 1020,
        "identifier": "torii-alumni",
        "name": "Alumni",
        "short_name": "ALM",
        "colour": "#9E9E9E",
        "has_listings": False,
        "has_playmodes": False,
        "is_probationary": False,
    },
    "supporter": {
        "id": 1021,
        "identifier": "torii-supporter",
        "name": "Torii Supporter",
        "short_name": "SUP",
        "colour": "#FF7FC8",  # warm pink — matches the supporter-hearts aura
        "has_listings": False,
        "has_playmodes": False,
        "is_probationary": False,
    },
    # ── Supporter loyalty tiers ──────────────────────────────────────────────
    # Auto-assigned by `build_groups` based on User.total_supporter_months.
    # Cumulative — once you cross a threshold, you keep that tier forever.
    "supporter-bronze": {
        "id": 1022,
        "identifier": "torii-supporter-bronze",
        "name": "Bronze Supporter",
        "short_name": "BRZ",
        "colour": "#CD7F32",  # warm copper
        "has_listings": False,
        "has_playmodes": False,
        "is_probationary": False,
    },
    "supporter-silver": {
        "id": 1023,
        "identifier": "torii-supporter-silver",
        "name": "Silver Supporter",
        "short_name": "SLV",
        "colour": "#C0C0C0",  # cool platinum
        "has_listings": False,
        "has_playmodes": False,
        "is_probationary": False,
    },
    "supporter-gold": {
        "id": 1024,
        "identifier": "torii-supporter-gold",
        "name": "Gold Supporter",
        "short_name": "GLD",
        "colour": "#FFD700",  # rich gold
        "has_listings": False,
        "has_playmodes": False,
        "is_probationary": False,
    },
    # ── Special / cute ───────────────────────────────────────────────────────
    "goof": {
        "id": 1030,
        "identifier": "torii-goof",
        "name": "Goofball",
        "short_name": "GOOF",
        "colour": "#9CE5A0",  # soft pastel froggy green
        "has_listings": False,
        "has_playmodes": False,
        "is_probationary": False,
    },
}

# Groups derived automatically from boolean DB flags (is_admin, is_gmt, is_qat, is_bng)
FLAG_GROUPS: dict[str, str] = {
    "is_admin": "admin",
    "is_gmt": "mod",
    "is_qat": "qat",
    "is_bng": "pooler",
}


# Supporter loyalty thresholds.
#
# Cumulative months across ALL donations a user has made → which tier
# group they earn. Crossing a threshold is permanent (we never demote);
# the tier reflects "how long have you supported in total."
#
# This is intentionally TIME-based, not money-based: a 12-month $5 donor
# and a 12-month $50 donor both earn silver. Removes the optics of
# "richer donors get fancier perks" and keeps the framing as "we thank
# you for supporting over time," not "we sell tiered cosmetics."
SUPPORTER_TIER_THRESHOLDS: list[tuple[int, str]] = [
    (36, "supporter-gold"),    # 3+ years
    (12, "supporter-silver"),  # 1-3 years
    (6,  "supporter-bronze"),  # 6-11 months
    (1,  "supporter"),         # 1-5 months
]


def supporter_tier_key_for_months(total_months: int) -> str | None:
    """Pick the highest-priority supporter tier for a cumulative month count.
    Returns None when the user has never donated (0 months).
    """
    for threshold, key in SUPPORTER_TIER_THRESHOLDS:
        if total_months >= threshold:
            return key
    return None


def build_groups(user: object) -> list[dict]:
    """
    Build the `groups` API array for a user.

    Sources (in priority order):
    1. Flag-based roles    (is_admin → admin, is_gmt → mod, …)
    2. Explicit titles     (User.torii_titles JSON list of TORII_GROUPS keys)
    3. Supporter loyalty   (derived from User.total_supporter_months —
                            see SUPPORTER_TIER_THRESHOLDS)
    """
    seen: set[str] = set()
    result: list[dict] = []

    def _add(key: str) -> None:
        if key in seen or key not in TORII_GROUPS:
            return
        seen.add(key)
        g = TORII_GROUPS[key]
        entry: dict = {
            "id": g["id"],
            "identifier": g["identifier"],
            "name": g["name"],
            "short_name": g["short_name"],
            "colour": g.get("colour"),
            "has_listings": g.get("has_listings", False),
            "has_playmodes": g.get("has_playmodes", False),
            "is_probationary": g.get("is_probationary", False),
        }
        if "playmodes" in g:
            entry["playmodes"] = g["playmodes"]
        result.append(entry)

    # 1. Boolean flags
    for flag, group_key in FLAG_GROUPS.items():
        if getattr(user, flag, False):
            _add(group_key)

    # 2. Custom titles stored in torii_titles column
    custom: list[str] = getattr(user, "torii_titles", None) or []
    for key in custom:
        _add(key)

    # 3. Derived supporter tier — only the highest tier the user
    # qualifies for is added (we deliberately don't stack bronze + silver).
    months = getattr(user, "total_supporter_months", 0) or 0
    tier = supporter_tier_key_for_months(months)
    if tier is not None:
        _add(tier)

    return result
