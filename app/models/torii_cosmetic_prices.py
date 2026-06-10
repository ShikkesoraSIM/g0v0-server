"""Server-side authoritative cosmetic prices.

The cosmetics store is points-only (earned, never bought with money). This map is
the SERVER's source of truth for a purchase: the client sends a cosmetic id, the
server charges THIS price and ignores whatever price the client claimed. It mirrors
the client catalog:
  - osu.Game/Cosmetics/CosmeticCatalog.cs            (cursor trails)
  - osu.Game/Cosmetics/CosmeticNameColourCatalog.cs  (name colours, buyable list)
  - osu.Game/Overlays/Cosmetics/BuyableAuraCatalog.cs (auras on sale)
  - osu.Game/Cosmetics/CosmeticEconomy.cs            (customisation unlock)

Keep it in sync when cosmetics are added there. An id missing here is treated as
not-for-sale (the purchase is rejected), so a new cosmetic must get a server price
before it can be bought. (Longer term this should be admin-configured store data
rather than a hardcoded mirror.)
"""

from __future__ import annotations

import re

COSMETIC_PRICES: dict[str, int] = {
    # ── Cursor trails: solid colours ──────────────────────────────────────────
    "trail-pearl": 300,
    "trail-crimson": 300,
    "trail-ocean": 300,
    "trail-mint": 300,
    "trail-gold": 300,
    "trail-violet": 300,
    # ── trails: gradients ─────────────────────────────────────────────────────
    "trail-sunset": 900,
    "trail-ember": 900,
    "trail-frost": 900,
    # ── trails: premium smooth ────────────────────────────────────────────────
    "trail-aurora": 2500,
    "trail-rainbow-engined": 4000,
    # ── trails: particles ─────────────────────────────────────────────────────
    "trail-bubbles": 400,
    "trail-starlight": 1000,
    "trail-lovestruck": 1000,
    "trail-sakura": 1200,
    "trail-frostfall": 1000,
    "trail-melody": 1100,
    "trail-inferno": 2200,
    "trail-stardust": 2800,
    "trail-confetti": 1000,
    "trail-smoke": 500,
    "trail-prism": 1200,
    "trail-galaxy": 2600,
    "trail-arcade": 900,
    "trail-storm": 2400,
    # ── trails: ribbons ───────────────────────────────────────────────────────
    "trail-comet": 2600,
    "trail-serpent": 1300,
    "trail-rainbow-ribbon": 3200,
    "trail-neon-flux": 3000,
    "trail-comet-prime": 3200,
    "trail-spectrum": 3600,
    "trail-neon-surge": 3400,
    "trail-nebula": 3400,
    "trail-glitch": 2600,
    "trail-wisp": 1400,
    "trail-heartbeat": 1300,
    # ── Name colours: solids ──────────────────────────────────────────────────
    "name-crimson": 200,
    "name-ocean": 200,
    "name-mint": 200,
    "name-gold": 200,
    "name-violet": 200,
    "name-coral": 200,
    # ── Name colours: gradients ───────────────────────────────────────────────
    "name-sunset": 800,
    "name-tide": 800,
    "name-forest": 800,
    "name-berry": 800,
    # ── Auras on sale ─────────────────────────────────────────────────────────
    "summer-2026": 3000,
    # ── Account-wide customisation unlock (length / density / size sliders) ────
    # Cheap, one-time tweak unlock — keep in sync with CosmeticEconomy.AdjustableLengthUnlock.
    "customisation-unlock": 100,
}


def price_for(cosmetic_id: str) -> int | None:
    """Authoritative price for a sellable cosmetic id, or None if it's not for sale."""
    return COSMETIC_PRICES.get(cosmetic_id)


_VALID_COSMETIC_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


def clean_cosmetic_ids(ids) -> list[str]:
    """Filter a list to well-formed cosmetic ids (alphanumeric, dash/underscore, up to
    128 chars). Used to sanitise admin-supplied grant lists before storing them."""
    return [s for s in (str(x).strip() for x in (ids or [])) if _VALID_COSMETIC_ID.fullmatch(s)]
