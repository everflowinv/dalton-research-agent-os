"""Quarantine for discoveries whose candidate selection fails deterministically.

2026-09-29: one sell-side search page whose ``companies`` tags ran to 24-193
issuers made ``candidate_view`` refuse the page, the coordinator answered
``selection_pending`` and took the same discovery again next tick, and every
AlphaEngine acquisition behind it waited -- 783 documents on legacy and 1,600
on ws-7d, for days, while the lane reported ``idle``.

A refusal that is a pure function of stored bytes, plan and mission is the
same refusal on every tick.  The coordinator now records it here and moves
on: the one discovery is set aside, everything behind it runs.

The record is for an auditor and is reversible:

* **one file per discovery**, ``<state>/discovery-selections/quarantine/
  <sha256(discovery_ref)[:24]>.json``, holding every quarantine and release as
  an append-only ``events`` list and a ``content_hash`` over the record;
* **bound to the rules it failed under** (``SELECTION_RULES_VERSION``).  A
  release that fixes the rule bumps that version and every quarantine taken
  under the old rules lapses by itself -- the discovery is simply tried again;
* **an owner release** (``release``, ``human:`` actor, a reason, dry run until
  ``--apply``) puts one discovery back without a code change.  If it fails the
  same way again it is quarantined again, as a new event.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .store import canonical_json, content_hash

SCHEMA_VERSION = "discovery-selection-quarantine:0.1"
DIRECTORY = "quarantine"
QUARANTINED, RELEASED = "quarantined", "released"
_HUMAN_ACTOR_RE = re.compile(r"human:[A-Za-z0-9._-]+\Z")
_FILE_RE = re.compile(r"[0-9a-f]{24}\.json\Z")
_MAX_REASON = 500


class QuarantineError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any, name: str, *, maximum: int = 300) -> str:
    if not isinstance(value, str) or not value.strip():
        raise QuarantineError(f"{name} is required")
    return value.strip()[:maximum]


class DiscoverySelectionQuarantine:
    """Owner-only files under the selection launcher's own state directory."""

    def __init__(self, directory: str | Path, *, clock: Any = None) -> None:
        self.directory = Path(directory)
        self._clock = clock or _now

    def _ensure(self) -> Path:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        return self.directory

    def path(self, discovery_ref: str) -> Path:
        digest = hashlib.sha256(_text(discovery_ref, "discovery_ref").encode()).hexdigest()[:24]
        return self.directory / f"{digest}.json"

    def record(self, discovery_ref: str) -> dict[str, Any] | None:
        path = self.path(discovery_ref)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise QuarantineError(f"quarantine record is unreadable: {path.name}") from exc
        body = {key: item for key, item in value.items() if key != "content_hash"}
        if value.get("content_hash") != content_hash(body) or value.get("discovery_ref") != discovery_ref:
            raise QuarantineError(f"quarantine record drifted: {path.name}")
        return value

    def records(self) -> list[dict[str, Any]]:
        found = []
        if not self.directory.is_dir():
            return found
        for path in sorted(self.directory.iterdir()):
            if not _FILE_RE.fullmatch(path.name):
                continue
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                found.append(self.record(value["discovery_ref"]) or value)
            except (OSError, ValueError, KeyError, TypeError, QuarantineError):
                continue
        return found

    def active(self, *, rules_version: str) -> list[str]:
        """Discoveries set aside under the rules that are current now."""

        return sorted(
            row["discovery_ref"] for row in self.records()
            if row.get("status") == QUARANTINED and row.get("rules_version") == rules_version)

    def _write(self, value: Mapping[str, Any]) -> dict[str, Any]:
        body = {key: item for key, item in value.items() if key != "content_hash"}
        wire = {**body, "content_hash": content_hash(body)}
        path = self.path(wire["discovery_ref"])
        self._ensure()
        temporary = path.with_name("." + path.name + ".tmp")
        temporary.write_text(canonical_json(wire) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        return wire

    def quarantine(self, *, discovery_ref: str, reason: str, error_type: str,
                   rules_version: str, detail: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Set one discovery aside; idempotent under unchanged rules."""

        discovery_ref = _text(discovery_ref, "discovery_ref")
        rules_version = _text(rules_version, "rules_version")
        existing = self.record(discovery_ref)
        if (existing is not None and existing.get("status") == QUARANTINED
                and existing.get("rules_version") == rules_version):
            return existing
        event = {"event": QUARANTINED, "at": self._clock(),
                 "reason": _text(reason, "reason", maximum=_MAX_REASON),
                 "error_type": _text(error_type, "error_type", maximum=100),
                 "rules_version": rules_version}
        return self._write({
            "schema_version": SCHEMA_VERSION, "discovery_ref": discovery_ref,
            "status": QUARANTINED, "rules_version": rules_version,
            "reason": event["reason"], "error_type": event["error_type"],
            "quarantined_at": event["at"],
            "detail": dict(detail or {}),
            "events": [*(existing or {}).get("events", []), event],
        })

    def release(self, discovery_ref: str, *, actor_ref: str, reason: str) -> dict[str, Any]:
        """Put one quarantined discovery back in the queue (owner decision)."""

        if not isinstance(actor_ref, str) or not _HUMAN_ACTOR_RE.fullmatch(actor_ref):
            raise QuarantineError("a release is a human decision (actor human:...)")
        existing = self.record(discovery_ref)
        if existing is None:
            raise QuarantineError("discovery is not quarantined")
        if existing.get("status") != QUARANTINED:
            raise QuarantineError(f"discovery is {existing.get('status')}, not quarantined")
        event = {"event": RELEASED, "at": self._clock(), "actor_ref": actor_ref,
                 "reason": _text(reason, "reason", maximum=_MAX_REASON)}
        return self._write({**existing, "status": RELEASED, "released_at": event["at"],
                            "events": [*existing.get("events", []), event]})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m dalton_core.discovery_selection_quarantine",
        description="List or release discoveries quarantined by candidate selection.")
    parser.add_argument("--state-dir", required=True,
                        help="the environment's state/dalton-core directory")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="every quarantine record, active or released")
    release = commands.add_parser("release", help="put one discovery back (dry run by default)")
    release.add_argument("--discovery-ref", required=True)
    release.add_argument("--actor", required=True, help="human:<name>")
    release.add_argument("--reason", required=True)
    release.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    from .discovery_candidate_selection import SELECTION_RULES_VERSION

    store = DiscoverySelectionQuarantine(
        Path(args.state_dir).expanduser() / "discovery-selections" / DIRECTORY)
    if args.command == "list":
        rows = store.records()
        print(json.dumps({"rules_version": SELECTION_RULES_VERSION,
                          "active": store.active(rules_version=SELECTION_RULES_VERSION),
                          "records": rows}, indent=1, ensure_ascii=False))
        return 0
    try:
        existing = store.record(args.discovery_ref)
        if existing is None or existing.get("status") != QUARANTINED:
            raise QuarantineError("discovery is not quarantined")
        if not _HUMAN_ACTOR_RE.fullmatch(args.actor):
            raise QuarantineError("a release is a human decision (actor human:...)")
        if not args.apply:
            print(json.dumps({"dry_run": True, "would_release": existing},
                             indent=1, ensure_ascii=False))
            return 0
        print(json.dumps(store.release(args.discovery_ref, actor_ref=args.actor,
                                       reason=args.reason), indent=1, ensure_ascii=False))
    except QuarantineError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
