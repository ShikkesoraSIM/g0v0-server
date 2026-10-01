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
    # ── Cursor trails: basic ────────────────────────────────────────────────────
    "trail-pearl": 300,
    "trail-crimson": 300,
    "trail-ocean": 300,
    "trail-mint": 300,
    "trail-gold": 300,
    "trail-violet": 300,
    "trail-bubbles": 400,
    "trail-smoke": 500,
    "trail-origami": 500,
    "trail-maple": 300,
    "trail-amber": 300,
    "trail-copper": 300,
    "trail-moss": 300,
    "trail-mist": 500,
    # ── Cursor trails: special ──────────────────────────────────────────────────
    "trail-sunset": 900,
    "trail-ember": 900,
    "trail-frost": 900,
    "trail-starlight": 1000,
    "trail-lovestruck": 1200,
    "trail-sakura": 1200,
    "trail-frostfall": 1000,
    "trail-melody": 1100,
    "trail-serpent": 1300,
    "trail-wisp": 1800,
    "trail-heartbeat": 1300,
    "trail-confetti": 1000,
    "trail-prism": 1200,
    "trail-arcade": 900,
    "trail-ink-flow": 900,
    "trail-comet-tail": 1000,
    "trail-circuit-line": 1100,
    "trail-jellyfish": 1200,
    "trail-moths": 1100,
    "trail-harvest": 900,
    "trail-cider": 900,
    "trail-dusk-fog": 900,
    "trail-leaf-fall": 1200,
    "trail-acorns": 1000,
    "trail-pumpkin-patch": 1100,
    "trail-ember-rise": 1500,
    "trail-autumn-rain": 1000,
    "trail-lanterns": 1200,
    "trail-cinnamon": 1300,
    "trail-first-frost": 1300,
    # ── Cursor trails: premium ──────────────────────────────────────────────────
    "trail-aurora": 2500,
    "trail-rainbow-engined": 5500,
    "trail-inferno": 2200,
    "trail-stardust": 2800,
    "trail-comet": 2600,
    "trail-rainbow-ribbon": 3200,
    "trail-neon-flux": 3000,
    "trail-comet-prime": 3200,
    "trail-spectrum": 3600,
    "trail-neon-surge": 3400,
    "trail-nebula": 3400,
    "trail-glitch": 2600,
    "trail-galaxy": 4500,
    "trail-storm": 2400,
    "trail-vhs": 2200,
    "trail-bloodmoon": 2400,
    "trail-mycelium": 2300,
    "trail-embersteel": 2100,
    "trail-golden-hour": 2600,
    "trail-maple-storm": 3000,
    "trail-bonfire": 2800,
    "trail-harvest-moon": 3000,
    # ── Name colours ────────────────────────────────────────────────────────────
    "name-crimson": 200,
    "name-ocean": 200,
    "name-mint": 200,
    "name-gold": 200,
    "name-violet": 200,
    "name-coral": 200,
    "name-sunset": 800,
    "name-tide": 800,
    "name-forest": 800,
    "name-berry": 800,
    "name-maple": 200,
    "name-amber": 200,
    "name-moss": 200,
    "name-harvest": 800,
    "name-dusk": 800,
    "name-ember": 800,
    "name-cider": 800,
    "name-twilight": 800,
    "name-candlelight": 1500,
    "name-smoulder": 1500,
    # ── Auras on sale ───────────────────────────────────────────────────────────
    "summer-2026": 3000,
    "autumn-leaffall": 2500,
    "autumn-leaffall-gust": 2500,
    "autumn-leaffall-dusk": 2500,
    "autumn-maple-wind": 4500,
    "stardust": 10000,
    # ── Account-wide unlocks ───────────────────────────────────────────────────
    "customisation-unlock": 100,
    "accent-hue-unlock": 5000,
}


def price_for(cosmetic_id: str) -> int | None:
    """Authoritative price for a sellable cosmetic id, or None if it's not for sale."""
    return COSMETIC_PRICES.get(cosmetic_id)


_VALID_COSMETIC_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


def clean_cosmetic_ids(ids) -> list[str]:
    """Filter a list to well-formed cosmetic ids (alphanumeric, dash/underscore, up to
    128 chars). Used to sanitise admin-supplied grant lists before storing them."""
    return [s for s in (str(x).strip() for x in (ids or [])) if _VALID_COSMETIC_ID.fullmatch(s)]
