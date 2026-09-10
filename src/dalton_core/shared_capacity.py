"""Host-wide model capacity reservations shared by isolated workspaces."""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .store import content_hash

LEGACY_POLICY_SCHEMA_VERSION = "shared-model-capacity-policy-0.1"
POLICY_SCHEMA_VERSION = "shared-model-capacity-policy-0.2"
_SHA = re.compile(r"^[0-9a-f]{64}$")
_REF = re.compile(r"^[A-Za-z][A-Za-z0-9._-]*:[^\s]+$")


class SharedCapacityError(RuntimeError):
    pass


class SharedCapacityUnavailable(SharedCapacityError):
    pass


class SharedCapacityExceeded(SharedCapacityError):
    pass


class SharedCapacityConflict(SharedCapacityError):
    pass


def validate_policy(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SharedCapacityError("shared capacity policy has an invalid closed shape")
    version = value.get("schema_version")
    legacy = version == LEGACY_POLICY_SCHEMA_VERSION
    fields = {"schema_version", "id", "status", "provider",
              "max_daily_calls", "max_daily_cost_micros", "max_concurrency",
              "approved_by", "created_at", "content_hash"}
    fields |= ({"credential_slot_ref"} if legacy else {
        "account_ref", "allowed_credential_slot_refs"})
    if version not in {LEGACY_POLICY_SCHEMA_VERSION, POLICY_SCHEMA_VERSION} \
            or set(value) != fields:
        raise SharedCapacityError("shared capacity policy has an invalid closed shape")
    wire = dict(value)
    asserted = wire.pop("content_hash")
    if asserted != content_hash(wire) or not isinstance(asserted, str) \
            or _SHA.fullmatch(asserted) is None:
        raise SharedCapacityError("shared capacity policy content hash does not match")
    if wire["status"] != "approved":
        raise SharedCapacityError("shared capacity policy is not an approved supported policy")
    for key in ("id", "approved_by"):
        if not isinstance(wire[key], str) or _REF.fullmatch(wire[key]) is None:
            raise SharedCapacityError(f"shared capacity policy {key} is invalid")
    if not isinstance(wire["provider"], str) or not wire["provider"] \
            or any(char.isspace() for char in wire["provider"]):
        raise SharedCapacityError("shared capacity policy provider is invalid")
    if legacy:
        slots = [wire["credential_slot_ref"]]
        account_ref = wire["credential_slot_ref"]
    else:
        account_ref = wire["account_ref"]
        if not isinstance(account_ref, str) or _REF.fullmatch(account_ref) is None:
            raise SharedCapacityError("shared capacity policy account_ref is invalid")
        slots = wire["allowed_credential_slot_refs"]
        if (not isinstance(slots, list) or not slots or slots != sorted(slots)
                or len(slots) != len(set(slots))
                or any(not isinstance(slot, str) or _REF.fullmatch(slot) is None
                       for slot in slots)):
            raise SharedCapacityError(
                "shared capacity policy allowed credential slots are invalid")
    try:
        created = datetime.fromisoformat(wire["created_at"].replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise SharedCapacityError("shared capacity policy created_at is invalid") from exc
    if created.tzinfo is None:
        raise SharedCapacityError("shared capacity policy created_at needs a timezone")
    for key in ("max_daily_calls", "max_daily_cost_micros", "max_concurrency"):
        if isinstance(wire[key], bool) or not isinstance(wire[key], int) or wire[key] < 1:
            raise SharedCapacityError(f"shared capacity policy {key} must be positive")
    # Version 0.1 used the credential slot as its explicit account identity.
    # Keep those signed bytes valid while deriving one stable aggregation key
    # that does not change when the owner publishes a new policy ref.
    scope_ref = "shared-model-capacity-scope:" + content_hash({
        "provider": wire["provider"], "account_ref": account_ref,
    })[:32]
    return {**wire, "content_hash": asserted,
            "account_ref": account_ref, "allowed_credential_slot_refs": slots,
            "scope_ref": scope_ref}


def _signed_policy_record(policy: Mapping[str, Any]) -> dict[str, Any]:
    derived = {"scope_ref"}
    if policy["schema_version"] == LEGACY_POLICY_SCHEMA_VERSION:
        derived |= {"account_ref", "allowed_credential_slot_refs"}
    return {key: value for key, value in policy.items() if key not in derived}


_SCHEMA = """
CREATE TABLE IF NOT EXISTS shared_capacity_policies(
 policy_ref TEXT PRIMARY KEY, policy_hash TEXT NOT NULL UNIQUE,
 scope_ref TEXT NOT NULL, account_ref TEXT NOT NULL, record_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS shared_capacity_policy_heads(
 scope_ref TEXT PRIMARY KEY, account_ref TEXT NOT NULL,
 policy_ref TEXT NOT NULL UNIQUE, policy_hash TEXT NOT NULL UNIQUE,
 activated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS shared_capacity_reservations(
 reservation_ref TEXT PRIMARY KEY, identity_hash TEXT NOT NULL UNIQUE,
 workspace_id TEXT NOT NULL, invocation_ref TEXT NOT NULL,
 policy_ref TEXT NOT NULL, policy_hash TEXT NOT NULL,
 scope_ref TEXT NOT NULL, account_ref TEXT NOT NULL, day TEXT NOT NULL,
 reserved_micros INTEGER NOT NULL, status TEXT NOT NULL,
 expires_at TEXT NOT NULL, created_at TEXT NOT NULL,
 dispatched_at TEXT, settled_at TEXT, charged_micros INTEGER, outcome TEXT,
 UNIQUE(workspace_id,invocation_ref));
CREATE TABLE IF NOT EXISTS shared_capacity_events(
 event_id INTEGER PRIMARY KEY AUTOINCREMENT, reservation_ref TEXT NOT NULL,
 event_kind TEXT NOT NULL, created_at TEXT NOT NULL, record_json TEXT NOT NULL);
"""


class SharedCapacityAuthority:
    def __init__(
        self, database: str | Path, *, policy_ref: str, policy_hash: str,
        scope_ref: str | None = None, account_ref: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        path = Path(database).expanduser().resolve()
        if not path.is_file():
            raise SharedCapacityUnavailable("declared shared capacity database is unavailable")
        try:
            info = Path(database).expanduser().lstat()
        except OSError as exc:
            raise SharedCapacityUnavailable("declared shared capacity database is unavailable") from exc
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            raise SharedCapacityUnavailable(
                "shared capacity database must be an owner-only regular file")
        self.path = path
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.connection = sqlite3.connect(path, timeout=10, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA busy_timeout=10000")
        try:
            row = self.connection.execute(
                "SELECT record_json,policy_hash,scope_ref,account_ref "
                "FROM shared_capacity_policies WHERE policy_ref=?",
                (policy_ref,),
            ).fetchone()
        except sqlite3.Error as exc:
            self.connection.close()
            raise SharedCapacityUnavailable("shared capacity authority schema is unavailable") from exc
        if row is None or row["policy_hash"] != policy_hash:
            self.connection.close()
            raise SharedCapacityUnavailable("declared shared capacity policy is unavailable")
        policy = validate_policy(json.loads(row["record_json"]))
        if policy["id"] != policy_ref or policy["content_hash"] != policy_hash:
            self.connection.close()
            raise SharedCapacityUnavailable("declared shared capacity policy binding differs")
        if ((scope_ref is not None and scope_ref != policy["scope_ref"])
                or (account_ref is not None and account_ref != policy["account_ref"])):
            self.connection.close()
            raise SharedCapacityUnavailable(
                "declared shared capacity scope/account binding differs")
        self.policy = policy

    @classmethod
    def initialize(cls, database: str | Path, policy: Mapping[str, Any]) -> None:
        """Explicit owner/setup operation; runtime construction never creates policy."""
        wire = validate_policy(policy)
        path = Path(database).expanduser().resolve()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        connection = sqlite3.connect(path)
        try:
            connection.executescript(_SCHEMA)
            connection.execute("PRAGMA journal_mode=WAL")
            existing = connection.execute(
                "SELECT policy_hash FROM shared_capacity_policies WHERE policy_ref=?",
                (wire["id"],),
            ).fetchone()
            if existing is not None and existing[0] != wire["content_hash"]:
                raise SharedCapacityConflict("policy ref already binds different bytes")
            connection.execute(
                "INSERT OR IGNORE INTO shared_capacity_policies VALUES(?,?,?,?,?)",
                (wire["id"], wire["content_hash"], wire["scope_ref"],
                 wire["account_ref"],
                 json.dumps(_signed_policy_record(wire), ensure_ascii=False,
                            sort_keys=True, separators=(",", ":"))),
            )
            head = connection.execute(
                "SELECT policy_ref,policy_hash FROM shared_capacity_policy_heads "
                "WHERE scope_ref=?", (wire["scope_ref"],)).fetchone()
            if head is None:
                connection.execute(
                    "INSERT INTO shared_capacity_policy_heads VALUES(?,?,?,?,?)",
                    (wire["scope_ref"], wire["account_ref"], wire["id"],
                     wire["content_hash"], wire["created_at"]),
                )
            elif tuple(head) != (wire["id"], wire["content_hash"]):
                raise SharedCapacityConflict(
                    "scope already has an active policy; activate replacement explicitly")
            connection.commit()
            os.chmod(path, 0o600)
        finally:
            connection.close()

    @classmethod
    def activate(
        cls, database: str | Path, policy: Mapping[str, Any], *,
        expected_policy_ref: str, expected_policy_hash: str,
    ) -> None:
        """Publish and CAS-activate one replacement without resetting usage."""
        wire = validate_policy(policy)
        path = Path(database).expanduser().resolve()
        connection = sqlite3.connect(path, isolation_level=None)
        try:
            connection.execute("BEGIN IMMEDIATE")
            head = connection.execute(
                "SELECT policy_ref,policy_hash,account_ref FROM shared_capacity_policy_heads "
                "WHERE scope_ref=?", (wire["scope_ref"],)).fetchone()
            if head is None or head[0] != expected_policy_ref \
                    or head[1] != expected_policy_hash:
                raise SharedCapacityConflict("active shared capacity policy moved")
            if head[2] != wire["account_ref"]:
                raise SharedCapacityConflict("replacement changes the governed account")
            existing = connection.execute(
                "SELECT policy_hash FROM shared_capacity_policies WHERE policy_ref=?",
                (wire["id"],)).fetchone()
            if existing is not None and existing[0] != wire["content_hash"]:
                raise SharedCapacityConflict("policy ref already binds different bytes")
            connection.execute(
                "INSERT OR IGNORE INTO shared_capacity_policies VALUES(?,?,?,?,?)",
                (wire["id"], wire["content_hash"], wire["scope_ref"],
                 wire["account_ref"], json.dumps(
                     _signed_policy_record(wire), ensure_ascii=False,
                     sort_keys=True, separators=(",", ":"))),
            )
            connection.execute(
                "UPDATE shared_capacity_policy_heads SET policy_ref=?,policy_hash=?,"
                "activated_at=? WHERE scope_ref=?",
                (wire["id"], wire["content_hash"], wire["created_at"], wire["scope_ref"]),
            )
            connection.commit()
            os.chmod(path, 0o600)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "SharedCapacityAuthority":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _now(self) -> datetime:
        now = self.clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise SharedCapacityError("shared capacity clock must be timezone-aware")
        return now.astimezone(timezone.utc)

    def _event(self, ref: str, kind: str, now: str, detail: Mapping[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO shared_capacity_events(reservation_ref,event_kind,created_at,record_json) VALUES(?,?,?,?)",
            (ref, kind, now, json.dumps(dict(detail), sort_keys=True, separators=(",", ":"))),
        )

    def _require_active(self) -> None:
        head = self.connection.execute(
            "SELECT policy_ref,policy_hash,account_ref FROM shared_capacity_policy_heads "
            "WHERE scope_ref=?", (self.policy["scope_ref"],)).fetchone()
        if head is None or head["policy_ref"] != self.policy["id"] \
                or head["policy_hash"] != self.policy["content_hash"] \
                or head["account_ref"] != self.policy["account_ref"]:
            raise SharedCapacityUnavailable(
                "declared shared capacity policy is not the active scope head")

    def reserve(
        self, *, workspace_id: str, invocation_ref: str, provider: str,
        credential_slot_ref: str, maximum_cost_micros: int, expires_at: datetime,
    ) -> dict[str, Any]:
        if provider != self.policy["provider"] \
                or credential_slot_ref not in self.policy["allowed_credential_slot_refs"]:
            raise SharedCapacityUnavailable("selected model is outside shared capacity policy scope")
        if isinstance(maximum_cost_micros, bool) or not isinstance(maximum_cost_micros, int) \
                or maximum_cost_micros < 1:
            raise SharedCapacityError("maximum shared reservation cost must be positive")
        now_dt, expiry = self._now(), expires_at.astimezone(timezone.utc)
        if expiry <= now_dt:
            raise SharedCapacityError("shared reservation expiry must be in the future")
        identity = {"workspace_id": workspace_id, "invocation_ref": invocation_ref,
                    "policy_ref": self.policy["id"], "policy_hash": self.policy["content_hash"],
                    "scope_ref": self.policy["scope_ref"],
                    "maximum_cost_micros": maximum_cost_micros}
        identity_hash = content_hash(identity)
        ref = "shared-capacity-reservation:" + identity_hash[:32]
        now, day = now_dt.isoformat(timespec="microseconds"), now_dt.date().isoformat()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self.connection.execute(
                "SELECT * FROM shared_capacity_reservations WHERE workspace_id=? AND invocation_ref=?",
                (workspace_id, invocation_ref),
            ).fetchone()
            if existing is not None:
                if existing["identity_hash"] != identity_hash:
                    raise SharedCapacityConflict("invocation already has a different reservation")
                self.connection.commit()
                return dict(existing)
            self._require_active()
            # Only undispatched reservations expire. A dispatched call keeps its
            # conservative charge until an exact replay settles it.
            stale = self.connection.execute(
                "SELECT reservation_ref FROM shared_capacity_reservations "
                "WHERE status='reserved' AND expires_at<=?", (now,)).fetchall()
            for row in stale:
                self.connection.execute(
                    "UPDATE shared_capacity_reservations SET status='expired',settled_at=?,outcome='undispatched_expired',charged_micros=0 WHERE reservation_ref=?",
                    (now, row[0]))
                self._event(row[0], "expired", now, {"reason": "undispatched_expired"})
            totals = self.connection.execute(
                "SELECT COUNT(*) calls,COALESCE(SUM(CASE WHEN status='settled' THEN charged_micros ELSE reserved_micros END),0) cost "
                "FROM shared_capacity_reservations WHERE scope_ref=? AND day=? AND status IN ('reserved','dispatched','settled')",
                (self.policy["scope_ref"], day),
            ).fetchone()
            open_count = self.connection.execute(
                "SELECT COUNT(*) FROM shared_capacity_reservations WHERE scope_ref=? AND status IN ('reserved','dispatched')",
                (self.policy["scope_ref"],),
            ).fetchone()[0]
            if totals["calls"] + 1 > self.policy["max_daily_calls"]:
                raise SharedCapacityExceeded("shared daily call capacity is exhausted")
            if totals["cost"] + maximum_cost_micros > self.policy["max_daily_cost_micros"]:
                raise SharedCapacityExceeded("shared daily cost capacity is exhausted")
            if open_count + 1 > self.policy["max_concurrency"]:
                raise SharedCapacityExceeded("shared concurrent model capacity is exhausted")
            self.connection.execute(
                "INSERT INTO shared_capacity_reservations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ref, identity_hash, workspace_id, invocation_ref, self.policy["id"],
                 self.policy["content_hash"], self.policy["scope_ref"],
                 self.policy["account_ref"], day, maximum_cost_micros, "reserved",
                 expiry.isoformat(timespec="microseconds"), now, None, None, None, None))
            self._event(ref, "reserved", now, identity)
            self.connection.commit()
            return dict(self.connection.execute(
                "SELECT * FROM shared_capacity_reservations WHERE reservation_ref=?", (ref,)).fetchone())
        except Exception:
            self.connection.rollback()
            raise

    def mark_dispatched(self, reservation_ref: str) -> dict[str, Any]:
        now = self._now().isoformat(timespec="microseconds")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT * FROM shared_capacity_reservations WHERE reservation_ref=?", (reservation_ref,)).fetchone()
            if row is None:
                raise SharedCapacityConflict("shared reservation is missing")
            if row["status"] == "reserved":
                self._require_active()
                if row["expires_at"] <= now:
                    raise SharedCapacityConflict("undispatched shared reservation has expired")
                self.connection.execute(
                    "UPDATE shared_capacity_reservations SET status='dispatched',dispatched_at=? WHERE reservation_ref=?",
                    (now, reservation_ref))
                self._event(reservation_ref, "dispatched", now, {})
            elif row["status"] not in {"dispatched", "settled"}:
                raise SharedCapacityConflict("shared reservation is not dispatchable")
            self.connection.commit()
            return dict(self.connection.execute(
                "SELECT * FROM shared_capacity_reservations WHERE reservation_ref=?", (reservation_ref,)).fetchone())
        except Exception:
            self.connection.rollback()
            raise

    def settle(
        self, reservation_ref: str, *, actual_cost_micros: int | None,
        outcome: str,
    ) -> dict[str, Any]:
        now = self._now().isoformat(timespec="microseconds")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT * FROM shared_capacity_reservations WHERE reservation_ref=?", (reservation_ref,)).fetchone()
            if row is None or row["status"] not in {"dispatched", "settled"}:
                raise SharedCapacityConflict("only a dispatched reservation can settle")
            charged = row["reserved_micros"] if actual_cost_micros is None else actual_cost_micros
            if isinstance(charged, bool) or not isinstance(charged, int) or charged < 0:
                raise SharedCapacityConflict("settlement cost is invalid")
            if actual_cost_micros is None and outcome == "transport_or_protocol_unknown":
                if row["status"] == "settled":
                    # A later duplicate caller's timeout cannot overwrite an
                    # already measured completion from the same invocation.
                    self.connection.commit()
                    return dict(row)
                if row["outcome"] != outcome:
                    self.connection.execute(
                        "UPDATE shared_capacity_reservations SET charged_micros=?,outcome=? WHERE reservation_ref=?",
                        (charged, outcome, reservation_ref))
                    self._event(reservation_ref, "completion_unknown", now,
                                {"charged_micros": charged, "outcome": outcome})
                # The provider may still be running after a socket timeout.
                # Retain BOTH the maximum cost and the concurrency slot.
                self.connection.commit()
                return dict(self.connection.execute(
                    "SELECT * FROM shared_capacity_reservations WHERE reservation_ref=?",
                    (reservation_ref,)).fetchone())
            if row["status"] == "settled":
                if (row["outcome"] == "transport_or_protocol_unknown"
                        and actual_cost_micros is not None):
                    self.connection.execute(
                        "UPDATE shared_capacity_reservations SET settled_at=?,charged_micros=?,outcome=? WHERE reservation_ref=?",
                        (now, charged, outcome, reservation_ref))
                    self._event(reservation_ref, "reconciled", now,
                                {"charged_micros": charged, "outcome": outcome})
                    self.connection.commit()
                    return dict(self.connection.execute(
                        "SELECT * FROM shared_capacity_reservations WHERE reservation_ref=?",
                        (reservation_ref,)).fetchone())
                if row["charged_micros"] != charged or row["outcome"] != outcome:
                    raise SharedCapacityConflict("shared reservation already settled differently")
                self.connection.commit()
                return dict(row)
            self.connection.execute(
                "UPDATE shared_capacity_reservations SET status='settled',settled_at=?,charged_micros=?,outcome=? WHERE reservation_ref=?",
                (now, charged, outcome, reservation_ref))
            self._event(reservation_ref, "settled", now,
                        {"charged_micros": charged, "outcome": outcome})
            self.connection.commit()
            return dict(self.connection.execute(
                "SELECT * FROM shared_capacity_reservations WHERE reservation_ref=?", (reservation_ref,)).fetchone())
        except Exception:
            self.connection.rollback()
            raise
