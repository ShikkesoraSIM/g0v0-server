"""
Torii server-side catalog for per-user "auras" — the particle effect rendered
behind a user's name in the lazer client (and, eventually, in the web frontend).

This module is the single source of truth for:
  - Which auras exist
  - Which group(s) grant access to each aura
  - Default-priority order when a multi-group user has not picked one explicitly
  - The sentinel values stored in `lazer_users.equipped_aura` and how to
    resolve them into a concrete aura id

Adding a new aura: append one entry to TORII_AURAS. The settings picker in
the client / web reads `GET /api/v2/me/aura-catalog`, which is built from
this dict, so no other server file needs to change. Visual implementations
(particle shapes, colours, animations) live in the client and the web —
this module only cares about IDENTITY and ENTITLEMENT.

Group identifiers used here are the short keys that match
`torii_groups.FLAG_GROUPS` values and the strings stored inside
`User.torii_titles` JSON ("admin", "dev", "mod", "qat", "supporter",
"goof", ...). The "torii-" prefix used by the public API
`APIUserGroup.identifier` is NOT used here — keep this file aligned with
the storage vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.torii_groups import FLAG_GROUPS, is_currently_supporting


# Sentinel values stored in `lazer_users.equipped_aura`.
#
# - DEFAULT: user has no preference; the highest-priority aura among their
#   groups is rendered. Same as None — both are accepted on read.
# - NONE: user explicitly opted OUT; render no aura even if eligible.
# - any concrete aura id (e.g. "admin-embers"): exact pick, render that
#   one provided the user is still entitled to it; otherwise fall back to
#   the default-priority resolution.
AURA_SENTINEL_DEFAULT: str = "default"
AURA_SENTINEL_NONE: str = "none"


@dataclass(frozen=True)
class AuraDefinition:
    """A single aura entry in the catalog. Frozen so consumers can hash /
    compare instances safely."""

    aura_id: str
    """Stable identifier — what gets stored in `equipped_aura` and sent over
    the API. Visual implementations on the client/web are keyed by this."""

    display_name: str
    """User-facing name, shown in the settings picker / preview cards."""

    description: str
    """One-line blurb shown as a tooltip / subtitle in the picker."""

    owning_groups: tuple[str, ...]
    """Group keys that grant access to this aura. A user is entitled to
    equip the aura iff they own AT LEAST one of these groups."""

    default_priority: int = 100
    """Lower wins. When a user with no equipped pick has multiple eligible
    auras, the one with the smallest `default_priority` is rendered. Tied
    values fall back to dict insertion order."""


# ---------------------------------------------------------------------------
# The catalog. Insertion order is the tiebreaker when two auras share a
# default_priority. Keep priorities reasonably spaced so future auras can be
# slotted in without renumbering.
# ---------------------------------------------------------------------------
TORII_AURAS: dict[str, AuraDefinition] = {
    "admin-embers": AuraDefinition(
        aura_id="admin-embers",
        display_name="Admin Embers",
        description="Rising sparks with occasional star flashes — authority + heat.",
        owning_groups=("admin",),
        default_priority=0,
    ),
    "dev-bits": AuraDefinition(
        aura_id="dev-bits",
        display_name="Dev Bits",
        description="Cyan data bits and angle-bracket glyphs floating up.",
        owning_groups=("dev",),
        default_priority=10,
    ),
    "mod-shields": AuraDefinition(
        aura_id="mod-shields",
        display_name="Mod Shields",
        description="Gold shields orbiting the name with a steady pulse.",
        owning_groups=("mod",),
        default_priority=20,
    ),
    "qat-notes": AuraDefinition(
        aura_id="qat-notes",
        display_name="QAT Notes",
        description="Music notes drifting up with the occasional approval check.",
        owning_groups=("qat",),
        default_priority=30,
    ),
    # Supporter aura — single, no tiers, no colour variants (yet).
    # Granted only while a user is currently in the active supporter
    # window (server-side, the "supporter" group in build_groups appears
    # iff is_currently_supporting; lapsed donors lose the aura but keep
    # their permanent "donator" badge).
    "supporter-aura": AuraDefinition(
        aura_id="supporter-aura",
        display_name="Supporter Aura",
        description="Pink hearts, granted while you're an active Torii Supporter.",
        owning_groups=("supporter",),
        default_priority=40,
    ),
    "goof-leaves": AuraDefinition(
        aura_id="goof-leaves",
        display_name="Goof Leaves",
        description="Cute green leaves drifting around with a gentle hover.",
        owning_groups=("goof",),
        default_priority=50,
    ),
    # Recognition tier for community bug-finders — free to receive and
    # therefore expected to be common. Highest default_priority among the
    # group fallbacks so it never wins the tiebreak when the same user
    # also owns a flashier group (admin / dev / mod / qat / supporter /
    # goof). The visual itself (client side) is intentionally subdued —
    # mint bug glyphs crawling slowly along the baseline of the username,
    # never more than a few alive at once. Aligns with
    # BugFinderAuraPreset.cs in torii-osu.
    "bug-finder-bugs": AuraDefinition(
        aura_id="bug-finder-bugs",
        display_name="Bug Finder",
        description="Tiny mint bugs slowly crawling along your name — thanks for the bug report!",
        owning_groups=("bug-finder",),
        default_priority=80,
    ),
}


# ---------------------------------------------------------------------------
# Helpers — every consumer (API endpoints, response builders, validators)
# should go through these so the sentinel + entitlement logic stays in one
# place.
# ---------------------------------------------------------------------------


def user_group_keys(user: object) -> set[str]:
    """Resolve the set of short group keys a user holds.

    Mirrors the logic in `torii_groups.build_groups` but returns the bare
    keys (no API dict) so we can do entitlement checks cheaply. MUST stay
    aligned with build_groups so the catalog endpoint and the public
    /me/groups response agree on what a user owns.
    """
    keys: set[str] = {key for flag, key in FLAG_GROUPS.items() if getattr(user, flag, False)}
    custom: list[str] | None = getattr(user, "torii_titles", None)
    if custom:
        keys.update(custom)
    # "supporter" is granted live based on donor_end_at, not stored as a
    # flag/title — pull it in here so an active supporter's catalog
    # actually surfaces the supporter aura.
    if is_currently_supporting(user):
        keys.add("supporter")
    # "donator" is permanent once has_supported flips. Doesn't grant any
    # aura (no aura's owning_groups include it) but kept here for
    # symmetry with build_groups in case future auras want to.
    if getattr(user, "has_supported", False):
        keys.add("donator")
    return keys


def is_aura_id_known(aura_id: str | None) -> bool:
    """True iff aura_id is a sentinel or a real catalog entry."""
    if aura_id is None:
        return True
    if aura_id in (AURA_SENTINEL_DEFAULT, AURA_SENTINEL_NONE):
        return True
    return aura_id in TORII_AURAS


def is_aura_allowed_for_user(user: object, aura_id: str | None) -> bool:
    """Does this user's group set grant them the right to equip `aura_id`?

    Sentinels (None / "default" / "none") are always allowed — they're not
    locked behind groups.
    """
    if aura_id is None or aura_id in (AURA_SENTINEL_DEFAULT, AURA_SENTINEL_NONE):
        return True
    aura = TORII_AURAS.get(aura_id)
    if aura is None:
        return False
    user_keys = user_group_keys(user)
    return any(g in user_keys for g in aura.owning_groups)


def available_auras_for_user(user: object) -> list[AuraDefinition]:
    """All auras the user is entitled to equip, ordered by `default_priority`
    ascending then by catalog insertion order. Returned in the order the
    settings picker should display them."""
    user_keys = user_group_keys(user)
    eligible = [a for a in TORII_AURAS.values() if any(g in user_keys for g in a.owning_groups)]
    eligible.sort(key=lambda a: a.default_priority)
    return eligible


def resolve_default_aura_id(user: object) -> str | None:
    """The aura id this user gets when they have no explicit pick. None if
    the user has no eligible auras at all."""
    eligible = available_auras_for_user(user)
    return eligible[0].aura_id if eligible else None


def resolve_effective_aura_id(user: object, equipped: str | None) -> str | None:
    """Translate a stored `equipped_aura` value into the aura id that should
    actually render on this user's name. Encapsulates the sentinel logic so
    every API response and consumer treats it identically.

    Returns None when no aura should render (either explicit opt-out, or
    user has no eligible groups).
    """
    if equipped == AURA_SENTINEL_NONE:
        return None
    if equipped is None or equipped == AURA_SENTINEL_DEFAULT:
        return resolve_default_aura_id(user)
    # Explicit pick — only honour it if the user still owns the relevant
    # group, otherwise fall back to default. Demoting a user implicitly
    # demotes their aura too without server intervention.
    if is_aura_allowed_for_user(user, equipped):
        return equipped
    return resolve_default_aura_id(user)


def aura_to_api_dict(aura: AuraDefinition) -> dict:
    """Serialisable representation used by the catalog endpoint."""
    return {
        "id": aura.aura_id,
        "display_name": aura.display_name,
        "description": aura.description,
        "owning_groups": list(aura.owning_groups),
    }
