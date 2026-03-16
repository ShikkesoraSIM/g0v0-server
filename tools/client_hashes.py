#!/usr/bin/env python3
"""Manage local client hash overrides and unknown hash registry."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
OVERRIDES_PATH = CONFIG_DIR / "client_versions_overrides.json"
UNKNOWN_PATH = CONFIG_DIR / "client_versions_unknown.json"


def normalize_hash(raw_value: str) -> str:
    return (raw_value or "").strip().lower()


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8") or "{}")
    except Exception:
        return {}


def save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")


def cmd_list_unknown(args: argparse.Namespace) -> int:
    unknown = load_json(UNKNOWN_PATH)
    rows: list[tuple[str, int, str, str]] = []
    for client_hash, value in unknown.items():
        if not isinstance(value, dict):
            continue
        rows.append(
            (
                str(client_hash),
                int(value.get("count", 0)),
                str(value.get("last_source", "")),
                str(value.get("last_user_agent", "")),
            )
        )

    rows.sort(key=lambda item: item[1], reverse=True)
    if args.limit is not None:
        rows = rows[: args.limit]

    if not rows:
        print("No unknown client hashes recorded.")
        return 0

    print(f"{'hash':<34} {'count':>8}  {'source':<24} user-agent")
    print("-" * 100)
    for client_hash, count, source, user_agent in rows:
        print(f"{client_hash:<34} {count:>8}  {source:<24} {user_agent}")
    return 0


def cmd_add_override(args: argparse.Namespace) -> int:
    client_hash = normalize_hash(args.hash)
    if not client_hash:
        raise SystemExit("hash cannot be empty")

    overrides = load_json(OVERRIDES_PATH)
    if not isinstance(overrides, dict):
        overrides = {}

    overrides[client_hash] = {
        "client_name": args.name,
        "version": args.version,
        "os": args.os,
    }
    save_json(OVERRIDES_PATH, overrides)

    if args.clean_unknown:
        unknown = load_json(UNKNOWN_PATH)
        if isinstance(unknown, dict) and client_hash in unknown:
            del unknown[client_hash]
            save_json(UNKNOWN_PATH, unknown)

    print(f"Saved override for {client_hash}: {args.name} {args.version} ({args.os})")
    return 0


def cmd_remove_override(args: argparse.Namespace) -> int:
    client_hash = normalize_hash(args.hash)
    overrides = load_json(OVERRIDES_PATH)
    if not isinstance(overrides, dict):
        print("No overrides found.")
        return 0

    if client_hash not in overrides:
        print(f"Override {client_hash} not found.")
        return 0

    del overrides[client_hash]
    save_json(OVERRIDES_PATH, overrides)
    print(f"Removed override {client_hash}.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Client hash registry helper")
    sub = parser.add_subparsers(dest="command", required=True)

    list_unknown = sub.add_parser("list-unknown", help="List unknown client hashes")
    list_unknown.add_argument("--limit", type=int, default=30, help="Max rows to print")
    list_unknown.set_defaults(func=cmd_list_unknown)

    add_override = sub.add_parser("add-override", help="Add or update an override for one hash")
    add_override.add_argument("--hash", required=True, help="Client hash")
    add_override.add_argument("--name", required=True, help="Client name")
    add_override.add_argument("--version", required=True, help="Version label")
    add_override.add_argument("--os", default="", help="OS label")
    add_override.add_argument(
        "--clean-unknown",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Remove this hash from unknown registry",
    )
    add_override.set_defaults(func=cmd_add_override)

    remove_override = sub.add_parser("remove-override", help="Delete one override hash")
    remove_override.add_argument("--hash", required=True, help="Client hash")
    remove_override.set_defaults(func=cmd_remove_override)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
