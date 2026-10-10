"""Connector-level health hold for host-tool sources whose login lapsed.

2026-10-08 (ws-7d and legacy): the AlphaEngine server invalidated the desktop
session ("E_UPSTREAM_API: 用户状态发生变更，刷新 token") while ``auth_status``
still said ready.  Every search failed, each failure was retried per
discovery after ``retry_interval_days`` (one day), so even after the owner
re-logged in nothing was searched until the next day -- and nobody was told.

A login/session refusal (``provider_failure.PROVIDER_AUTH``) is a property of
the *source*, not of one discovery.  So:

* the first such failure puts the source on a short **hold** (``HOLD_MINUTES``);
  while held, the coordinator launches no search and no acquisition for it;
* when the hold lapses the coordinator **probes** the source with its cheapest
  call (AlphaEngine: ``list_filters``).  A failed probe extends the hold; a
  passing probe lifts it, and every discovery that failed with
  ``provider_auth`` is due again on the next tick -- its per-discovery retry
  clock does not apply to a login problem;
* while held, 「需要你处理」, the parity report and the cockpit's data-source
  dialog all say 「登录失效」.

The record is one owner-only JSON file per source under
``<state>/connector-health/``: current state plus a bounded append-only event
list and a ``content_hash``.  Readers (needs_human, parity, cockpit) only read.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .provider_failure import PROVIDER_AUTH
from .store import canonical_json, content_hash

SCHEMA_VERSION = "connector-health:0.1"
DIRECTORY = "connector-health"
HOLD_MINUTES = 15
HELD, RECOVERED = "held", "recovered"
MAX_EVENTS = 50
MAX_BLOCKED_REFS = 20
KINDS = ("search", "acquisition")

#: How each source is named to the owner, and where they fix a lost login.
SOURCE_NAMES: Mapping[str, str] = {
    "source:alphaengine": "AlphaEngine",
    "source:guidepoint": "Guidepoint",
}
SOURCE_APPS: Mapping[str, str] = {
    "source:alphaengine": "AlphaEngine Desktop",
    "source:guidepoint": "Guidepoint 客户端",
}
#: The host-tool sources that share the loopback MCP runner and whose login
#: lives in a desktop app.  A public-web 403 is a website's refusal, not a
#: lapsed login, and never holds a source.
HOST_TOOL_SOURCES = frozenset(SOURCE_NAMES)


def _slug(source_ref: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", source_ref).strip("-") or "source"


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def health_dir(state_dir: str | Path) -> Path:
    return Path(state_dir).expanduser() / DIRECTORY


def read_record(state_dir: str | Path | None, source_ref: str) -> dict[str, Any] | None:
    if state_dir is None:
        return None
    path = health_dir(state_dir) / f"{_slug(source_ref)}.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def read_records(state_dir: str | Path | None) -> list[dict[str, Any]]:
    """Every source's health record, read-only, in source order."""

    if state_dir is None:
        return []
    directory = health_dir(state_dir)
    if not directory.is_dir():
        return []
    out = []
    for path in sorted(directory.glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(value, dict) and value.get("schema_version") == SCHEMA_VERSION:
            out.append(value)
    return out


def held_records(state_dir: str | Path | None) -> list[dict[str, Any]]:
    return [record for record in read_records(state_dir) if record.get("status") == HELD]


class ConnectorHealth:
    """Writer-side owner of the per-source health records."""

    def __init__(self, state_dir: str | Path, *,
                 clock: Callable[[], datetime] | None = None,
                 hold_minutes: int = HOLD_MINUTES) -> None:
        self.state_dir = Path(state_dir).expanduser()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.hold = timedelta(minutes=int(hold_minutes))

    # -- reads -------------------------------------------------------------
    def record(self, source_ref: str) -> dict[str, Any] | None:
        return read_record(self.state_dir, source_ref)

    def held(self, source_ref: str) -> dict[str, Any] | None:
        record = self.record(source_ref)
        return record if record is not None and record.get("status") == HELD else None

    def probe_due(self, source_ref: str) -> bool:
        record = self.held(source_ref)
        if record is None:
            return False
        until = _parse(record.get("hold_until"))
        return until is None or self.clock() >= until

    # -- writes ------------------------------------------------------------
    def _write(self, source_ref: str, value: dict[str, Any]) -> dict[str, Any]:
        directory = self.state_dir / DIRECTORY
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        value = dict(value)
        value["events"] = list(value.get("events") or [])[-MAX_EVENTS:]
        value.pop("content_hash", None)
        value["content_hash"] = content_hash(value)
        path = directory / f"{_slug(source_ref)}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(canonical_json(value) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        return value

    def record_auth_failure(self, source_ref: str, *, kind: str, ref: str | None,
                            detail: Mapping[str, Any] | None,
                            at: str | None = None) -> dict[str, Any]:
        """One ``provider_auth`` failure: open the hold, or count into the open one."""

        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")
        now = self.clock()
        when = at or _iso(now)
        record = self.record(source_ref)
        if record is None or record.get("status") != HELD:
            events = list((record or {}).get("events") or [])
            record = {
                "schema_version": SCHEMA_VERSION,
                "source_ref": source_ref,
                "status": HELD,
                "failure_class": PROVIDER_AUTH,
                "since": when,
                "blocked": {name: 0 for name in KINDS},
                "blocked_refs": [],
                "probes": 0,
                "last_probe": None,
                "recovered_at": None,
                "events": events,
            }
        record["blocked"] = {name: int((record.get("blocked") or {}).get(name) or 0)
                             for name in KINDS}
        record["blocked"][kind] += 1
        if ref:
            refs = [item for item in record.get("blocked_refs") or [] if item != ref]
            record["blocked_refs"] = (refs + [ref])[-MAX_BLOCKED_REFS:]
        record["last_failure_at"] = when
        record["last_detail"] = {
            "code": str((detail or {}).get("code") or ""),
            "message": str((detail or {}).get("message") or "")[:500],
        }
        until = max(filter(None, (_parse(record.get("hold_until")), now + self.hold)))
        record["hold_until"] = _iso(until)
        record["events"] = list(record.get("events") or []) + [{
            "at": _iso(now), "event": "auth_failure", "kind": kind, "ref": ref,
            "message": record["last_detail"]["message"][:200],
        }]
        return self._write(source_ref, record)

    def record_probe(self, source_ref: str, *, ok: bool,
                     detail: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
        """A probe's verdict: lift the hold, or extend it by another hold period."""

        record = self.held(source_ref)
        if record is None:
            return None
        now = self.clock()
        message = str((detail or {}).get("message") or "")[:500]
        record["probes"] = int(record.get("probes") or 0) + 1
        record["last_probe"] = {"at": _iso(now), "ok": bool(ok), "message": message,
                                "method": (detail or {}).get("method")}
        if ok:
            record["status"] = RECOVERED
            record["recovered_at"] = _iso(now)
            record["hold_until"] = None
        else:
            record["hold_until"] = _iso(now + self.hold)
        record["events"] = list(record.get("events") or []) + [{
            "at": _iso(now), "event": "probe_ok" if ok else "probe_failed",
            "message": message[:200],
        }]
        return self._write(source_ref, record)


__all__ = [
    "ConnectorHealth",
    "DIRECTORY",
    "HELD",
    "HOLD_MINUTES",
    "HOST_TOOL_SOURCES",
    "RECOVERED",
    "SCHEMA_VERSION",
    "SOURCE_APPS",
    "SOURCE_NAMES",
    "held_records",
    "read_record",
    "read_records",
]
