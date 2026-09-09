#!/usr/bin/env python3
"""Regenerate the deterministic connector inventory package data.

The packaged JSON under ``src/dalton_core/connector_inventory`` is not a source
of truth. It is a rendering of ``PROFILE_DEFINITIONS``, and
``load_packaged_connector_inventory`` refuses to load it if the two disagree.
So a hash in ``index.json`` is never typed by a person: it is regenerated here,
which is why adding a connector is a change to the definitions and a re-run,
not a change to twelve files.

Run with no arguments to regenerate. Run with ``--check`` to be told what would
change without changing it, which is what CI and a merge want.

**Why this matters when several connectors land at once.** Every profile's
hashes are its own, but ``index.json`` carries all of them plus one top-level
``content_hash`` over the whole list, so two branches that each add a connector
both rewrite that one line and both are right. The merge is not a text merge:
take either side, re-run this, and the result is correct by construction. The
summary below exists so that whoever does that can see, in one screen, that
what moved is exactly the new connector and nothing else -- an unexplained hash
in that list is the whole reason this file prints anything at all.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dalton_core.connector_inventory import build_connector_inventory


ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "src" / "dalton_core" / "connector_inventory"
# ``kind`` in the built inventory to the directory it is written under.
DIRECTORY_BY_KIND = {
    "templates": "profiles", "fixtures": "fixtures", "proposals": "proposals",
}


def render(value: object) -> str:
    """The exact bytes a packaged file holds, for any of them."""

    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def planned_files(inventory: dict) -> dict[Path, str]:
    """Every packaged path and the text it should contain."""

    files: dict[Path, str] = {TARGET / "index.json": render(inventory["index"])}
    for kind, directory in DIRECTORY_BY_KIND.items():
        for slug, value in inventory[kind].items():
            files[TARGET / directory / f"{slug}.json"] = render(value)
    return files


def existing_files() -> set[Path]:
    return {
        path
        for directory in ("", *DIRECTORY_BY_KIND.values())
        for path in (TARGET / directory).glob("*.json")
    }


def index_hashes(text: str) -> dict[str, dict[str, str]]:
    """The per-connector hashes an index file asserts, keyed by connector ref."""

    index = json.loads(text)
    return {
        entry["connector_ref"]: {
            field: entry[field] for field in sorted(entry) if field.endswith("_hash")
        }
        for entry in index["profiles"]
    }


def summarise(files: dict[Path, str]) -> list[str]:
    """What a regeneration would change, in the order a reader wants it.

    Named rather than counted: "three files changed" is not something anyone
    can check, and the point of printing at all is that an unexplained moved
    hash gets noticed.
    """

    lines: list[str] = []
    stale = sorted(existing_files() - set(files))
    for path in stale:
        lines.append(f"remove  {path.relative_to(ROOT)} (no definition renders it)")
    for path in sorted(files):
        wanted = files[path]
        if not path.exists():
            lines.append(f"add     {path.relative_to(ROOT)}")
            continue
        current = path.read_text(encoding="utf-8")
        if current == wanted:
            continue
        if path.name != "index.json":
            lines.append(f"update  {path.relative_to(ROOT)}")
            continue
        before, after = index_hashes(current), index_hashes(wanted)
        lines.append(f"update  {path.relative_to(ROOT)}")
        for ref in sorted(set(before) | set(after)):
            if ref not in before:
                lines.append(f"          + {ref}")
            elif ref not in after:
                lines.append(f"          - {ref}")
            elif before[ref] != after[ref]:
                moved = sorted(
                    field for field, value in after[ref].items()
                    if before[ref].get(field) != value
                )
                lines.append(f"          ~ {ref}: {', '.join(moved)}")
        # The top-level hash covers the whole list, so it moves whenever any
        # entry does. Saying so keeps it from looking like a finding of its own.
        lines.append("          ~ index content_hash (covers the whole list)")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check", action="store_true",
        help="report what would change and exit non-zero, writing nothing",
    )
    args = parser.parse_args(argv)

    inventory = build_connector_inventory()
    files = planned_files(inventory)
    changes = summarise(files)

    if args.check:
        if changes:
            print("packaged connector inventory is stale:")
            for line in changes:
                print(f"  {line}")
            return 1
        print("packaged connector inventory matches the frozen definitions")
        return 0

    for path in sorted(existing_files() - set(files)):
        path.unlink()
    for path, text in sorted(files.items()):
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
    if changes:
        print("regenerated the packaged connector inventory:")
        for line in changes:
            print(f"  {line}")
    else:
        print("packaged connector inventory was already current; nothing written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
