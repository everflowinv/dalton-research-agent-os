"""Owner-approved host capacity shared by workspace connector attempts."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .store import content_hash

POLICY_SCHEMA_VERSION = "shared-connector-capacity-policy-0.2"
_SHA = re.compile(r"^[0-9a-f]{64}$")
_REF = re.compile(r"^[A-Za-z][A-Za-z0-9._-]*:[^\s]+$")


class SharedConnectorCapacityError(RuntimeError): pass
class SharedConnectorCapacityUnavailable(SharedConnectorCapacityError): pass
class SharedConnectorCapacityExceeded(SharedConnectorCapacityError): pass
class SharedConnectorCapacityConflict(SharedConnectorCapacityError): pass


def validate_policy(value: Mapping[str, Any]) -> dict[str, Any]:
    fields = {"schema_version", "id", "status", "scopes", "quota_scope_ref",
              "provider_account_ref", "version", "prior_policy_ref", "rolling_window_seconds",
              "max_calls", "max_cost_micros", "max_cost_micros_per_call",
              "max_concurrency", "approved_by", "created_at", "content_hash"}
    if not isinstance(value, Mapping) or set(value) != fields:
        raise SharedConnectorCapacityError("connector capacity policy has an invalid closed shape")
    wire = dict(value); asserted = wire.pop("content_hash")
    if asserted != content_hash(wire) or not isinstance(asserted, str) or not _SHA.fullmatch(asserted):
        raise SharedConnectorCapacityError("connector capacity policy hash differs")
    if wire["schema_version"] != POLICY_SCHEMA_VERSION or wire["status"] != "approved":
        raise SharedConnectorCapacityError("connector capacity policy is not approved")
    for key in ("id", "quota_scope_ref", "provider_account_ref", "approved_by"):
        if not isinstance(wire[key], str) or not _REF.fullmatch(wire[key]):
            raise SharedConnectorCapacityError(f"connector capacity {key} is invalid")
    scopes = wire["scopes"]
    if not isinstance(scopes, list) or not scopes:
        raise SharedConnectorCapacityError("connector capacity scopes must be explicit")
    seen = set()
    for scope in scopes:
        if not isinstance(scope, Mapping) or set(scope) != {
                "connector_ref", "capability_ref", "credential_slot_refs"}:
            raise SharedConnectorCapacityError("connector capacity scope has invalid shape")
        for key in ("connector_ref", "capability_ref"):
            if not isinstance(scope[key], str) or not _REF.fullmatch(scope[key]):
                raise SharedConnectorCapacityError("connector capacity scope ref is invalid")
        slots = scope["credential_slot_refs"]
        if (not isinstance(slots, list)
                or any(not isinstance(item, str) or not _REF.fullmatch(item) for item in slots)
                or slots != sorted(slots) or len(slots) != len(set(slots))):
            raise SharedConnectorCapacityError("connector capacity credential slots are invalid")
        digest = content_hash(scope)
        if digest in seen:
            raise SharedConnectorCapacityError("connector capacity scopes are duplicated")
        seen.add(digest)
    for key in ("version", "rolling_window_seconds", "max_calls", "max_cost_micros",
                "max_cost_micros_per_call", "max_concurrency"):
        if isinstance(wire[key], bool) or not isinstance(wire[key], int) or wire[key] < 1:
            raise SharedConnectorCapacityError(f"connector capacity {key} must be positive")
    prior = wire["prior_policy_ref"]
    if ((wire["version"] == 1 and prior is not None)
            or (wire["version"] > 1 and (not isinstance(prior, str) or not _REF.fullmatch(prior)))):
        raise SharedConnectorCapacityError("connector capacity policy lineage is invalid")
    if wire["max_cost_micros_per_call"] > wire["max_cost_micros"]:
        raise SharedConnectorCapacityError("per-call capacity exceeds the rolling cost cap")
    try: created = datetime.fromisoformat(wire["created_at"].replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise SharedConnectorCapacityError("connector capacity created_at is invalid") from exc
    if created.tzinfo is None: raise SharedConnectorCapacityError("connector capacity created_at needs timezone")
    return {**wire, "content_hash": asserted}


_SCHEMA = """
CREATE TABLE IF NOT EXISTS shared_connector_capacity_policies(
 policy_ref TEXT PRIMARY KEY, policy_hash TEXT NOT NULL UNIQUE, record_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS shared_connector_capacity_heads(
 quota_scope_ref TEXT PRIMARY KEY, provider_account_ref TEXT NOT NULL UNIQUE,
 policy_ref TEXT NOT NULL UNIQUE, policy_hash TEXT NOT NULL, version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS shared_connector_capacity_reservations(
 reservation_ref TEXT PRIMARY KEY, identity_hash TEXT NOT NULL UNIQUE,
 workspace_id TEXT NOT NULL, invocation_ref TEXT NOT NULL, attempt_number INTEGER NOT NULL,
 policy_ref TEXT NOT NULL, policy_hash TEXT NOT NULL, scope_hash TEXT NOT NULL,
 reserved_micros INTEGER NOT NULL,
 status TEXT NOT NULL, expires_at TEXT NOT NULL, created_at TEXT NOT NULL,
 dispatched_at TEXT, settled_at TEXT, charged_micros INTEGER, outcome TEXT,
 UNIQUE(workspace_id,invocation_ref,attempt_number));
CREATE TABLE IF NOT EXISTS shared_connector_capacity_events(
 event_id INTEGER PRIMARY KEY AUTOINCREMENT,reservation_ref TEXT NOT NULL,
 event_kind TEXT NOT NULL,created_at TEXT NOT NULL,record_json TEXT NOT NULL);
"""


class SharedConnectorCapacityAuthority:
    def __init__(self, database: str | Path, *, policy_ref: str, policy_hash: str,
                 clock: Callable[[], datetime] | None = None):
        path = Path(database).expanduser().resolve()
        try: info = Path(database).expanduser().lstat()
        except OSError as exc: raise SharedConnectorCapacityUnavailable("connector capacity database unavailable") from exc
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise SharedConnectorCapacityUnavailable("connector capacity database must be owner-only")
        self.connection = sqlite3.connect(path, timeout=10, isolation_level=None)
        self.connection.row_factory = sqlite3.Row; self.connection.execute("PRAGMA busy_timeout=10000")
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        try: row = self.connection.execute("SELECT policy_hash,record_json FROM shared_connector_capacity_policies WHERE policy_ref=?", (policy_ref,)).fetchone()
        except sqlite3.Error as exc:
            self.connection.close(); raise SharedConnectorCapacityUnavailable("connector capacity schema unavailable") from exc
        if row is None or row["policy_hash"] != policy_hash:
            self.connection.close(); raise SharedConnectorCapacityUnavailable("connector capacity policy unavailable")
        self.policy = validate_policy(json.loads(row["record_json"]))
        if self.policy["content_hash"] != policy_hash or self.policy["id"] != policy_ref:
            self.connection.close(); raise SharedConnectorCapacityUnavailable("connector capacity binding differs")

    @classmethod
    def initialize(cls, database: str | Path, policy: Mapping[str, Any]) -> None:
        wire = validate_policy(policy); path = Path(database).expanduser().resolve()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        con = sqlite3.connect(path)
        try:
            con.executescript(_SCHEMA)
            con.execute("BEGIN IMMEDIATE")
            old = con.execute("SELECT policy_hash FROM shared_connector_capacity_policies WHERE policy_ref=?", (wire["id"],)).fetchone()
            if old is not None and old[0] != wire["content_hash"]: raise SharedConnectorCapacityConflict("policy ref already binds different bytes")
            if old is not None:
                con.commit()
                return
            head = con.execute(
                "SELECT policy_ref,version,provider_account_ref FROM shared_connector_capacity_heads WHERE quota_scope_ref=?",
                (wire["quota_scope_ref"],)).fetchone()
            if head is None:
                if wire["version"] != 1:
                    raise SharedConnectorCapacityConflict("first account policy must be version one")
                existing_account = con.execute(
                    "SELECT 1 FROM shared_connector_capacity_heads WHERE provider_account_ref=?",
                    (wire["provider_account_ref"],)).fetchone()
                if existing_account:
                    raise SharedConnectorCapacityConflict("provider account already owns a quota scope")
            elif (wire["prior_policy_ref"] != head[0] or wire["version"] != head[1] + 1
                  or wire["provider_account_ref"] != head[2]):
                raise SharedConnectorCapacityConflict("account policy does not extend the active head")
            con.execute("INSERT OR IGNORE INTO shared_connector_capacity_policies VALUES(?,?,?)", (wire["id"], wire["content_hash"], json.dumps(wire,sort_keys=True,separators=(",",":"))))
            con.execute(
                "INSERT INTO shared_connector_capacity_heads VALUES(?,?,?,?,?) ON CONFLICT(quota_scope_ref) DO UPDATE SET policy_ref=excluded.policy_ref,policy_hash=excluded.policy_hash,version=excluded.version",
                (wire["quota_scope_ref"], wire["provider_account_ref"], wire["id"], wire["content_hash"], wire["version"]))
            con.commit(); os.chmod(path, 0o600)
        finally: con.close()

    def close(self): self.connection.close()
    def __enter__(self): return self
    def __exit__(self, *_): self.close()
    def _now(self):
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None: raise SharedConnectorCapacityError("clock must be aware")
        return value.astimezone(timezone.utc)
    def matches(self, *, connector_ref: str, capability_ref: str, credential_slot_refs: Sequence[str]) -> bool:
        return any(connector_ref == scope["connector_ref"] and capability_ref == scope["capability_ref"]
                   and sorted(credential_slot_refs) == scope["credential_slot_refs"]
                   for scope in self.policy["scopes"])
    def _require_active(self):
        head = self.connection.execute(
            "SELECT policy_ref,policy_hash FROM shared_connector_capacity_heads WHERE quota_scope_ref=?",
            (self.policy["quota_scope_ref"],)).fetchone()
        if head is None or tuple(head) != (self.policy["id"], self.policy["content_hash"]):
            raise SharedConnectorCapacityUnavailable("connector capacity policy is superseded; rebind the active policy")
    def _event(self, ref, kind, now, body):
        self.connection.execute("INSERT INTO shared_connector_capacity_events(reservation_ref,event_kind,created_at,record_json) VALUES(?,?,?,?)", (ref,kind,now,json.dumps(body,sort_keys=True,separators=(",",":"))))
    def _require_owned_row(self, row):
        if row is None:
            raise SharedConnectorCapacityConflict("reservation missing")
        if row["policy_ref"] != self.policy["id"] or row["policy_hash"] != self.policy["content_hash"]:
            raise SharedConnectorCapacityConflict("reservation belongs to another capacity policy")
    def reserve(self, *, workspace_id: str, invocation_ref: str, attempt_number: int,
                maximum_cost_micros: int, expires_at: datetime):
        if isinstance(maximum_cost_micros, bool) or not isinstance(maximum_cost_micros, int) or maximum_cost_micros < 1:
            raise SharedConnectorCapacityError("maximum cost must be a positive integer")
        if isinstance(attempt_number, bool) or not isinstance(attempt_number, int) or attempt_number < 1:
            raise SharedConnectorCapacityError("physical attempt must be a positive integer")
        if not isinstance(expires_at, datetime) or expires_at.tzinfo is None:
            raise SharedConnectorCapacityError("reservation expiry must be timezone-aware")
        now_dt=self._now(); now=now_dt.isoformat(timespec="microseconds")
        expiry=expires_at.astimezone(timezone.utc)
        if expiry <= now_dt:
            raise SharedConnectorCapacityError("reservation expiry must be in the future")
        identity={"workspace_id":workspace_id,"invocation_ref":invocation_ref,"attempt_number":attempt_number,
                  "policy_ref":self.policy["id"],"policy_hash":self.policy["content_hash"],"maximum_cost_micros":maximum_cost_micros}
        # Search and document retrieval spend the same provider-account quota.
        # Operation scopes authorize calls; they must not split their budget.
        scope_hash=content_hash({key:self.policy[key] for key in (
            "quota_scope_ref","provider_account_ref")})
        ih=content_hash(identity); ref="shared-connector-capacity-reservation:"+ih[:32]
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            old=self.connection.execute("SELECT * FROM shared_connector_capacity_reservations WHERE workspace_id=? AND invocation_ref=? AND attempt_number=?",(workspace_id,invocation_ref,attempt_number)).fetchone()
            if old:
                if old["identity_hash"] != ih: raise SharedConnectorCapacityConflict("attempt already reserved differently")
                if old["status"] in {"expired", "released"} or (
                        old["status"] == "reserved" and old["expires_at"] <= now):
                    raise SharedConnectorCapacityConflict("attempt reservation ended; a fresh physical attempt is required")
                self.connection.commit(); return dict(old)
            self._require_active()
            if maximum_cost_micros > self.policy["max_cost_micros_per_call"]:
                raise SharedConnectorCapacityExceeded("shared per-call cost capacity exceeded")
            stale=self.connection.execute("SELECT reservation_ref FROM shared_connector_capacity_reservations WHERE status='reserved' AND expires_at<=?",(now,)).fetchall()
            for row in stale:
                self.connection.execute("UPDATE shared_connector_capacity_reservations SET status='expired',settled_at=?,charged_micros=0,outcome='undispatched_expired' WHERE reservation_ref=?",(now,row[0])); self._event(row[0],"expired",now,{})
            cutoff=(now_dt-timedelta(seconds=self.policy["rolling_window_seconds"])).isoformat(timespec="microseconds")
            totals=self.connection.execute("SELECT COUNT(*) calls,COALESCE(SUM(CASE WHEN status='settled' THEN charged_micros ELSE reserved_micros END),0) cost FROM shared_connector_capacity_reservations WHERE scope_hash=? AND created_at>? AND status IN ('reserved','dispatched','settled')",(scope_hash,cutoff)).fetchone()
            concurrent=self.connection.execute("SELECT COUNT(*) FROM shared_connector_capacity_reservations WHERE scope_hash=? AND status IN ('reserved','dispatched')",(scope_hash,)).fetchone()[0]
            if totals["calls"]+1>self.policy["max_calls"] or totals["cost"]+maximum_cost_micros>self.policy["max_cost_micros"] or concurrent+1>self.policy["max_concurrency"]:
                raise SharedConnectorCapacityExceeded("shared connector capacity exhausted")
            self.connection.execute("INSERT INTO shared_connector_capacity_reservations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(ref,ih,workspace_id,invocation_ref,attempt_number,self.policy["id"],self.policy["content_hash"],scope_hash,maximum_cost_micros,"reserved",expiry.isoformat(timespec="microseconds"),now,None,None,None,None))
            self._event(ref,"reserved",now,identity); self.connection.commit()
            return dict(self.connection.execute("SELECT * FROM shared_connector_capacity_reservations WHERE reservation_ref=?",(ref,)).fetchone())
        except Exception: self.connection.rollback(); raise
    def mark_dispatched(self, ref):
        now=self._now().isoformat(timespec="microseconds"); self.connection.execute("BEGIN IMMEDIATE")
        try:
            row=self.connection.execute("SELECT * FROM shared_connector_capacity_reservations WHERE reservation_ref=?",(ref,)).fetchone()
            self._require_owned_row(row)
            if row["status"]=="reserved":
                self._require_active()
                if row["expires_at"] <= now:
                    raise SharedConnectorCapacityConflict("undispatched connector reservation has expired")
                self.connection.execute("UPDATE shared_connector_capacity_reservations SET status='dispatched',dispatched_at=? WHERE reservation_ref=?",(now,ref)); self._event(ref,"dispatched",now,{})
            elif row["status"] not in {"dispatched","settled"}: raise SharedConnectorCapacityConflict("reservation not dispatchable")
            self.connection.commit(); return dict(self.connection.execute("SELECT * FROM shared_connector_capacity_reservations WHERE reservation_ref=?",(ref,)).fetchone())
        except Exception: self.connection.rollback(); raise
    def release_undispatched(self, ref):
        now=self._now().isoformat(timespec="microseconds"); self.connection.execute("BEGIN IMMEDIATE")
        try:
            row=self.connection.execute("SELECT * FROM shared_connector_capacity_reservations WHERE reservation_ref=?",(ref,)).fetchone()
            self._require_owned_row(row)
            if row["status"]=="reserved":
                self.connection.execute("UPDATE shared_connector_capacity_reservations SET status='released',settled_at=?,charged_micros=0,outcome='undispatched_released' WHERE reservation_ref=?",(now,ref)); self._event(ref,"released",now,{})
            elif row["status"] not in {"released","expired"}: raise SharedConnectorCapacityConflict("dispatched reservation cannot be released")
            self.connection.commit(); return dict(self.connection.execute("SELECT * FROM shared_connector_capacity_reservations WHERE reservation_ref=?",(ref,)).fetchone())
        except Exception: self.connection.rollback(); raise
    def settle(self, ref, *, actual_cost_micros: int | None, outcome: str):
        now=self._now().isoformat(timespec="microseconds"); self.connection.execute("BEGIN IMMEDIATE")
        try:
            row=self.connection.execute("SELECT * FROM shared_connector_capacity_reservations WHERE reservation_ref=?",(ref,)).fetchone()
            self._require_owned_row(row)
            if row is None or row["status"] not in {"dispatched","settled"}: raise SharedConnectorCapacityConflict("only dispatched reservations settle")
            if actual_cost_micros is None and outcome=="transport_or_protocol_unknown":
                if row["status"]=="dispatched": self.connection.execute("UPDATE shared_connector_capacity_reservations SET charged_micros=?,outcome=? WHERE reservation_ref=?",(row["reserved_micros"],outcome,ref)); self._event(ref,"completion_unknown",now,{})
                self.connection.commit(); return dict(self.connection.execute("SELECT * FROM shared_connector_capacity_reservations WHERE reservation_ref=?",(ref,)).fetchone())
            charged=row["reserved_micros"] if actual_cost_micros is None else actual_cost_micros
            if isinstance(charged, bool) or not isinstance(charged, int) or charged<0: raise SharedConnectorCapacityConflict("settlement cost invalid")
            if row["status"]=="settled":
                if row["charged_micros"]!=charged or row["outcome"]!=outcome: raise SharedConnectorCapacityConflict("reservation settled differently")
            else:
                self.connection.execute("UPDATE shared_connector_capacity_reservations SET status='settled',settled_at=?,charged_micros=?,outcome=? WHERE reservation_ref=?",(now,charged,outcome,ref)); self._event(ref,"settled",now,{"charged_micros":charged,"outcome":outcome})
            self.connection.commit(); return dict(self.connection.execute("SELECT * FROM shared_connector_capacity_reservations WHERE reservation_ref=?",(ref,)).fetchone())
        except Exception: self.connection.rollback(); raise


def policy_record(**values: Any) -> dict[str, Any]:
    wire={"schema_version":POLICY_SCHEMA_VERSION,**values}; wire["content_hash"]=content_hash(wire); return validate_policy(wire)
