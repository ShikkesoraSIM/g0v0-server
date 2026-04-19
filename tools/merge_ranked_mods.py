#!/usr/bin/env python
"""Merge new DEFAULT_RANKED_MODS entries into the existing config/ranked_mods.json.

Only adds mods that are missing — never removes or overwrites existing entries.
Run this INSTEAD of deleting ranked_mods.json when DEFAULT_RANKED_MODS changes.

Usage (from repo root):
    uv run --no-sync tools/merge_ranked_mods.py [--dry-run]
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
RANKED_MODS_FILE = ROOT / "config" / "ranked_mods.json"

# Import DEFAULT_RANKED_MODS directly from the module
sys.path.insert(0, str(ROOT))
from app.models.mods import DEFAULT_RANKED_MODS  # noqa: E402


def merge(dry_run: bool = False) -> None:
    if not RANKED_MODS_FILE.exists():
        print("ranked_mods.json does not exist — run the server once to generate it first, or this script will create it.")
        data: dict = {}
    else:
        data = json.loads(RANKED_MODS_FILE.read_text(encoding="utf-8"))

    checksum = data.pop("$mods_checksum", None)
    added: list[str] = []

    for ruleset_id, mods in DEFAULT_RANKED_MODS.items():
        key = str(ruleset_id)
        if key not in data:
            data[key] = {}
        for acronym, settings in mods.items():
            if acronym not in data[key]:
                data[key][acronym] = settings
                added.append(f"  R{ruleset_id}/{acronym}")

    if checksum is not None:
        data["$mods_checksum"] = checksum

    if not added:
        print("Nothing to add — ranked_mods.json already has all DEFAULT_RANKED_MODS entries.")
        return

    print("Mods that would be added:" if dry_run else "Adding mods:")
    for entry in added:
        print(entry)

    if not dry_run:
        RANKED_MODS_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")
        print(f"\nUpdated {RANKED_MODS_FILE}")
    else:
        print("\n(dry-run — no changes written)")


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    merge(dry_run=dry_run)
