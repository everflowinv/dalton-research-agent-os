"""Vision-gap #13: tell the owner, unprompted, on Feishu -- governed and capped.

Until now everything the owner had to know lived on a page they had to open:
the weekly brief, a conviction call waiting for a yes or no, a memo at the last
human gate, a model that vanished from the gateway, the emergency stop they
pressed.  This module pushes a short Chinese message for each of those into one
Feishu chat the owner names, and nothing else.

**The contract.**  ``connector-governance/owner-delivery-feishu-v1.json`` is the
approval, seeded ``proposed`` by every install and approved once per
environment by the owner, exactly like a data connector.  It differs from a
data connector's record in one way that is the point of it: it carries the
*recipient* -- the Feishu chat id -- and that id comes from the owner, never
from code.  The record binds a hash of the contract's terms (channel, host
tool, kinds of message, permissions, content policy); a code change to any of
them invalidates the approval rather than silently widening it.  The approved
record is the switch: no approval, no chat id, a hand-edited file whose
content hash no longer matches -- each is "nothing is sent", reported with the
reason, never guessed around.

**The ledger.**  ``owner-delivery.sqlite`` beside the Core holds one
append-only table, ``delivery_receipts``: what (kind and the exact artifact
version refs the message announced), to whom, when, the host's message id and
the outcome.  Nothing is updated or deleted -- triggers refuse it.  Whether an
artifact version has been delivered on a channel is *folded* from the rows,
and a partial unique index makes "delivered twice" impossible to write even if
two passes raced.  A pass writes an ``attempting`` row before it hands the
message to the host and a terminal row after; an ``attempting`` row a crashed
pass left behind becomes ``indeterminate`` and is treated as delivered,
because to the owner a missing duplicate is cheaper than a repeated alarm.

**Who sends.**  Never the writer's store thread.  The writer's lane
(:mod:`dalton_core.mission_owner_delivery_lane`) only decides *whether a pass
is due* and spawns this module as a child process; the child reads the Core,
the model router and the governance record read-only, writes only its own
ledger, and reaches Feishu only through the OpenClaw host tool
(``openclaw message send --channel feishu``) with an environment it builds
itself.  The host holds the Feishu credential; this process never sees it.

**Producers and their caps** (defaults are code; an environment may override
them in its ``service.json`` under ``owner_delivery``):

* weekly brief publication -> one message per issue version: the change summary
  and a cockpit link;
* conviction call proposals and Investment Memos awaiting the owner -> one
  「需要你处理」 digest at most every ``digest_interval_hours``, never one message
  per item;
* model fallback notices -> as soon as they are written, but at most one
  message per ``fallback_min_interval_minutes`` (several notices in that window
  ride together);
* the owner's own emergency stop / resume-all receipts -> immediately;
* across all of them, at most ``daily_message_cap`` messages per day in
  ``day_timezone``, spent in that priority order.

Everything announced is announced *since the approval*: approving the contract
does not replay last month's weekly briefs into the chat.  The digest is the
exception by nature -- it describes what is waiting now.

A failed send is a receipt with the reason; the next pass retries it (an event
after ``retry_after_minutes``, a digest at its next window).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .store import canonical_json, content_hash
from .terminology import label as _term
from .terminology import scrub

CHANNEL = "feishu"
CONTRACT_KIND = "owner-delivery-feishu"
CONTRACT_VERSION = 1
CONTRACT_FILE_NAME = f"{CONTRACT_KIND}-v{CONTRACT_VERSION}.json"
GOVERNANCE_DIR_NAME = "connector-governance"
CONTRACT_SCHEMA_VERSION = "owner-delivery-contract-0.1"
CONTRACT_ID = f"connector-governance:{CONTRACT_KIND}:v{CONTRACT_VERSION}"
CAPABILITY_ID = f"capability:dalton:connector:{CONTRACT_KIND}"
LEDGER_FILE_NAME = "owner-delivery.sqlite"
RECEIPT_SCHEMA_VERSION = "owner-delivery-receipt-0.1"
SETTINGS_KEY = "owner_delivery"

ARTIFACT_KINDS: tuple[str, ...] = (
    "owner_lever", "model_fallback_notice", "weekly_brief", "needs_human_digest",
)
# Spend order under the day cap: the owner's own stop first, then a stage that
# lost its model, then the brief, then the digest (which comes back anyway).
PRIORITY = {kind: index for index, kind in enumerate(ARTIFACT_KINDS)}
OUTCOMES = ("attempting", "delivered", "failed", "capped", "indeterminate")
# Outcomes that count as "the owner has (or may have) been told".
SETTLED_OUTCOMES = ("delivered", "indeterminate")

# The terms an approval covers.  Hashed into the record; a change here is a new
# contract the owner has not approved.
CONTRACT_TERMS: dict[str, Any] = {
    "channel": CHANNEL,
    "host_tool": {
        "transport": "host_tool",
        "program": "openclaw",
        "argv": ["message", "send", "--channel", CHANNEL],
        "openclaw_tool": "message",
        "action": "send",
        "runs_as": "lane child process, never the writer store thread",
    },
    "artifact_kinds": list(ARTIFACT_KINDS),
    "allowed_permissions": {
        "core_db": "read_only",
        "credential_slot_refs": [],
        "filesystem_write": ["state:owner-delivery.sqlite"],
        # This process opens no socket: the OpenClaw host reaches Feishu with
        # its own credential.  The side effect is the message itself.
        "network": False,
        "risk_class": "medium",
        "side_effects": ["write:owner-chat-message"],
    },
    "content_policy": (
        "short Chinese summaries, counts, titles and cockpit links only; never "
        "document text, evidence bodies, credentials, prompts or file paths"
    ),
    "caps": "daily message cap, digest interval and fallback rate limit enforced "
            "in code with defaults; an environment may only override the numbers",
}
CONTRACT_HASH = content_hash(CONTRACT_TERMS)

CONTRACT_FIELDS = frozenset({
    "schema_version", "id", "capability_id", "status", "terms", "contract_hash",
    "recipient", "approved_by", "approved_at", "content_hash",
})
RECIPIENT_FIELDS = frozenset({"chat_id", "account"})
CONTRACT_STATUSES = frozenset({"proposed", "approved"})
# A Feishu group chat id.  The owner copies it from the chat's settings or from
# the host's `feishu_chat info`; the shape check catches a pasted name or URL.
CHAT_ID_RE = re.compile(r"^oc_[0-9A-Za-z]{8,64}$")
ACCOUNT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")

DEFAULT_SETTINGS: dict[str, Any] = {
    "digest_interval_hours": 6,
    "daily_message_cap": 12,
    "fallback_min_interval_minutes": 60,
    "retry_after_minutes": 30,
    "poll_interval_minutes": 10,
    "day_timezone": "Asia/Shanghai",
    "cockpit_base_url": None,
    "openclaw_executable": None,
    "send_timeout_seconds": 60,
}
_INT_BOUNDS: dict[str, tuple[int, int]] = {
    "digest_interval_hours": (1, 168),
    "daily_message_cap": (1, 200),
    "fallback_min_interval_minutes": (1, 24 * 60),
    "retry_after_minutes": (1, 24 * 60),
    "poll_interval_minutes": (1, 24 * 60),
    "send_timeout_seconds": (5, 600),
}
MAX_MESSAGE_CHARS = 3500
MAX_DIGEST_LINES = 12
MAX_COMMAND_OUTPUT_BYTES = 512 * 1024
# What the host-tool child inherits.  Execution variables only, plus the host's
# own OPENCLAW_* settings so the CLI finds the same gateway the owner runs.
_PASSTHROUGH_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR",
                    "USER", "LOGNAME", "PYTHONPATH", "VIRTUAL_ENV")
_EXECUTABLE_CANDIDATES = ("/opt/homebrew/bin/openclaw", "/usr/local/bin/openclaw")


class OwnerDeliveryError(RuntimeError):
    """The delivery cannot proceed as configured."""


class DeliverySendError(OwnerDeliveryError):
    """One send failed; ``code`` is the short reason recorded in the receipt."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# time
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return moment.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# the contract
# ---------------------------------------------------------------------------


def contract_path(state_dir: str | Path) -> Path:
    return Path(state_dir) / GOVERNANCE_DIR_NAME / CONTRACT_FILE_NAME


def _seal(body: Mapping[str, Any]) -> dict[str, Any]:
    body = {key: value for key, value in body.items() if key != "content_hash"}
    return {**body, "content_hash": content_hash(body)}


def build_contract_record() -> dict[str, Any]:
    """The ``proposed`` record every install seeds.  Deterministic: no clock."""

    return _seal({
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "id": CONTRACT_ID,
        "capability_id": CAPABILITY_ID,
        "status": "proposed",
        "terms": json.loads(canonical_json(CONTRACT_TERMS)),
        "contract_hash": CONTRACT_HASH,
        "recipient": {"chat_id": None, "account": None},
        "approved_by": None,
        "approved_at": None,
    })


def approve_contract(
    record: Mapping[str, Any] | None, *, approved_by: str, chat_id: str,
    account: str | None = None, now: datetime | None = None,
) -> dict[str, Any]:
    """The approved record for this environment.  Pure: the caller writes it.

    Re-derives from the code's terms rather than editing the record it was
    given, so approving an old or hand-edited proposal approves *this* code's
    contract and nothing else.
    """

    if not isinstance(approved_by, str) or not approved_by.startswith("human:") \
            or not approved_by.removeprefix("human:").strip():
        raise OwnerDeliveryError("approved_by must be a human: principal")
    if not isinstance(chat_id, str) or CHAT_ID_RE.fullmatch(chat_id.strip()) is None:
        raise OwnerDeliveryError("chat_id must be a Feishu chat id (oc_...)")
    if account is not None and (not isinstance(account, str)
                                or ACCOUNT_RE.fullmatch(account) is None):
        raise OwnerDeliveryError("account must be an OpenClaw account id")
    if record is not None and record.get("id") not in (None, CONTRACT_ID):
        raise OwnerDeliveryError("the record is not the owner delivery contract")
    moment = now or _utc_now()
    base = build_contract_record()
    base.pop("content_hash")
    return _seal({
        **base,
        "status": "approved",
        "recipient": {"chat_id": chat_id.strip(), "account": account},
        "approved_by": approved_by,
        "approved_at": _iso(moment),
    })


def revoke_contract(record: Mapping[str, Any] | None) -> dict[str, Any]:
    """Back to ``proposed`` (and no recipient).  The off switch."""

    del record
    return build_contract_record()


@dataclass(frozen=True)
class ContractState:
    present: bool
    status: str | None
    approved: bool
    reason: str | None
    chat_id: str | None = None
    account: str | None = None
    approved_at: datetime | None = None
    approved_by: str | None = None
    content_hash: str | None = None

    def summary(self) -> dict[str, Any]:
        return {"present": self.present, "status": self.status,
                "approved": self.approved, "reason": self.reason,
                "approved_at": None if self.approved_at is None else _iso(self.approved_at),
                "recipient_set": bool(self.chat_id)}


def evaluate_contract(record: Any) -> ContractState:
    """Whether this record lets anything be sent, and if not, why (in Chinese)."""

    def refuse(reason: str, status: str | None = None) -> ContractState:
        return ContractState(True, status, False, reason)

    if not isinstance(record, Mapping):
        return refuse("飞书推送的审批文件不是一个 JSON 对象")
    status = record.get("status") if isinstance(record.get("status"), str) else None
    if set(record) != CONTRACT_FIELDS:
        return refuse("飞书推送的审批文件字段不对（多了或少了），请用开通脚本重新生成", status)
    if record.get("schema_version") != CONTRACT_SCHEMA_VERSION \
            or record.get("id") != CONTRACT_ID or record.get("capability_id") != CAPABILITY_ID:
        return refuse("飞书推送的审批文件不是这一版合同", status)
    body = {key: value for key, value in record.items() if key != "content_hash"}
    if record.get("content_hash") != content_hash(body):
        return refuse("飞书推送的审批文件被手工改过（数字指纹对不上），请用开通脚本重新签署", status)
    if record.get("contract_hash") != CONTRACT_HASH or record.get("terms") != json.loads(
            canonical_json(CONTRACT_TERMS)):
        return refuse("代码里的推送合同已经变了，原来的批准不覆盖新合同，请重新批准", status)
    if status not in CONTRACT_STATUSES:
        return refuse("飞书推送的审批状态无法识别", status)
    if status != "approved":
        return refuse("飞书推送还没有批准：请运行开通脚本，填上群聊 chat id 并批准", status)
    recipient = record.get("recipient")
    if not isinstance(recipient, Mapping) or set(recipient) != RECIPIENT_FIELDS:
        return refuse("飞书推送的接收人字段不对", status)
    chat_id = recipient.get("chat_id")
    if not isinstance(chat_id, str) or CHAT_ID_RE.fullmatch(chat_id) is None:
        return refuse("飞书推送已批准但没有有效的群聊 chat id（oc_ 开头）", status)
    account = recipient.get("account")
    if account is not None and (not isinstance(account, str)
                                or ACCOUNT_RE.fullmatch(account) is None):
        return refuse("飞书推送的 OpenClaw 账号字段无效", status)
    approved_by = record.get("approved_by")
    if not isinstance(approved_by, str) or not approved_by.startswith("human:"):
        return refuse("飞书推送的批准人必须是一个人（human:）", status)
    approved_at = parse_time(record.get("approved_at"))
    if approved_at is None:
        return refuse("飞书推送的批准时间无效", status)
    return ContractState(True, status, True, None, chat_id=chat_id, account=account,
                         approved_at=approved_at, approved_by=approved_by,
                         content_hash=record["content_hash"])


def load_contract(state_dir: str | Path) -> ContractState:
    path = contract_path(state_dir)
    if not path.is_file():
        return ContractState(False, None, False,
                             "这个环境还没有开通飞书推送（没有审批文件；用 docs/ops/owner-delivery-enable.sh 开通）")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ContractState(True, None, False, "飞书推送的审批文件读不出来")
    return evaluate_contract(record)


# ---------------------------------------------------------------------------
# settings: defaults are code, an environment may override the numbers
# ---------------------------------------------------------------------------


def validate_settings(raw: Any) -> dict[str, Any]:
    """The ``owner_delivery`` block of a service config, checked and closed."""

    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise OwnerDeliveryError("owner_delivery must be an object")
    unknown = set(raw) - set(DEFAULT_SETTINGS)
    if unknown:
        raise OwnerDeliveryError(
            "owner_delivery has unknown keys: " + ", ".join(sorted(unknown)))
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if key in _INT_BOUNDS:
            low, high = _INT_BOUNDS[key]
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise OwnerDeliveryError(f"owner_delivery.{key} must be an integer {low}..{high}")
        elif key == "day_timezone":
            try:
                ZoneInfo(str(value))
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise OwnerDeliveryError("owner_delivery.day_timezone is not a time zone") from exc
        elif key == "cockpit_base_url":
            if value is not None and (not isinstance(value, str)
                                      or re.fullmatch(r"https?://[^\s]{1,300}", value) is None):
                raise OwnerDeliveryError("owner_delivery.cockpit_base_url must be an http(s) URL")
        elif key == "openclaw_executable":
            if value is not None and (not isinstance(value, str) or not Path(value).is_absolute()):
                raise OwnerDeliveryError("owner_delivery.openclaw_executable must be an absolute path")
        out[key] = value
    return out


def _read_service_config(path: Path | None) -> Mapping[str, Any]:
    if path is None or not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, Mapping) else {}


def load_settings(
    state_dir: str | Path, *, service_config_path: str | Path | None = None,
) -> dict[str, Any]:
    """Code defaults, then the environment's ``service.json`` overrides.

    Two values are derived when not set, from blocks the service config already
    carries: the cockpit's public URL (``control``) and the OpenClaw executable
    the legacy Discord bridge was configured with (``outbox``).
    """

    if service_config_path is None:
        from .service_config_location import service_config_path as locate

        service_config_path = locate(state_dir)
    raw = _read_service_config(Path(service_config_path))
    settings = {**DEFAULT_SETTINGS, **validate_settings(raw.get(SETTINGS_KEY))}
    if settings["cockpit_base_url"] is None:
        control = raw.get("control")
        config = control.get("config") if isinstance(control, Mapping) else None
        if isinstance(config, Mapping) and isinstance(config.get("tailscale_host"), str) \
                and isinstance(config.get("port"), int):
            settings["cockpit_base_url"] = (
                f"https://{config['tailscale_host']}:{config['port']}/")
    if settings["openclaw_executable"] is None:
        outbox = raw.get("outbox")
        config = outbox.get("config") if isinstance(outbox, Mapping) else None
        value = config.get("openclaw_executable") if isinstance(config, Mapping) else None
        if isinstance(value, str) and Path(value).is_absolute():
            settings["openclaw_executable"] = value
    settings["model_router_db"] = raw.get("model_router_db") \
        if isinstance(raw.get("model_router_db"), str) else None
    return settings


# ---------------------------------------------------------------------------
# the ledger
# ---------------------------------------------------------------------------

LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS delivery_receipts (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_id TEXT NOT NULL UNIQUE,
    attempt_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    artifact_kind TEXT NOT NULL CHECK(artifact_kind IN (
        'owner_lever', 'model_fallback_notice', 'weekly_brief', 'needs_human_digest')),
    artifact_refs_json TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    recipient_ref TEXT,
    outcome TEXT NOT NULL CHECK(outcome IN (
        'attempting', 'delivered', 'failed', 'capped', 'indeterminate')),
    reason TEXT,
    message_id TEXT,
    body_sha256 TEXT,
    body TEXT,
    contract_hash TEXT,
    record_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- Delivered (or possibly delivered) at most once per exact message identity:
-- the artifact versions it announced, on this channel.
CREATE UNIQUE INDEX IF NOT EXISTS delivery_receipts_settled_once
ON delivery_receipts(idempotency_key) WHERE outcome IN ('delivered', 'indeterminate');
CREATE INDEX IF NOT EXISTS delivery_receipts_by_kind
ON delivery_receipts(artifact_kind, sequence);
CREATE INDEX IF NOT EXISTS delivery_receipts_by_key
ON delivery_receipts(idempotency_key, sequence);
CREATE TRIGGER IF NOT EXISTS delivery_receipts_no_update
BEFORE UPDATE ON delivery_receipts BEGIN
    SELECT RAISE(ABORT, 'delivery receipts are append-only');
END;
CREATE TRIGGER IF NOT EXISTS delivery_receipts_no_delete
BEFORE DELETE ON delivery_receipts BEGIN
    SELECT RAISE(ABORT, 'delivery receipts are append-only');
END;
"""


def idempotency_key(kind: str, refs: Iterable[str], channel: str = CHANNEL) -> str:
    """One message identity: what it announced (exact versions) and where."""

    ordered = sorted(set(refs))
    return f"{kind}:{content_hash(ordered)[:32]}:{channel}"


class DeliveryLedger:
    """``delivery_receipts``: append-only, owned by the delivery child alone."""

    def __init__(self, path: str | Path, *, read_only: bool = False) -> None:
        self.path = Path(path)
        if read_only:
            self.connection = sqlite3.connect(
                self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(self.path, timeout=30)
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.executescript(LEDGER_SCHEMA)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        self.connection.row_factory = sqlite3.Row

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "DeliveryLedger":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def append(self, *, attempt_id: str, kind: str, refs: Sequence[str], key: str,
               outcome: str, at: datetime, recipient_ref: str | None = None,
               reason: str | None = None, message_id: str | None = None,
               body: str | None = None, contract_hash: str | None = None) -> dict[str, Any]:
        if kind not in ARTIFACT_KINDS:
            raise OwnerDeliveryError(f"unknown artifact kind {kind}")
        if outcome not in OUTCOMES:
            raise OwnerDeliveryError(f"unknown outcome {outcome}")
        import hashlib

        record = {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "receipt_id": "delivery-receipt:" + uuid.uuid4().hex,
            "attempt_id": attempt_id,
            "channel": CHANNEL,
            "artifact_kind": kind,
            "artifact_refs": sorted(set(refs)),
            "idempotency_key": key,
            "recipient_ref": recipient_ref,
            "outcome": outcome,
            "reason": None if reason is None else str(reason)[:500],
            "message_id": message_id,
            "body_sha256": None if body is None
            else hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "contract_hash": contract_hash,
            "created_at": _iso(at),
        }
        record["content_hash"] = content_hash(record)
        with self.connection:
            self.connection.execute(
                "INSERT INTO delivery_receipts(receipt_id, attempt_id, channel, artifact_kind,"
                " artifact_refs_json, idempotency_key, recipient_ref, outcome, reason,"
                " message_id, body_sha256, body, contract_hash, record_json, content_hash,"
                " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (record["receipt_id"], attempt_id, CHANNEL, kind,
                 canonical_json(record["artifact_refs"]), key, recipient_ref, outcome,
                 record["reason"], message_id, record["body_sha256"], body, contract_hash,
                 canonical_json(record), record["content_hash"], record["created_at"]))
        return record

    def rows(self, kind: str | None = None) -> list[sqlite3.Row]:
        if kind is None:
            return self.connection.execute(
                "SELECT * FROM delivery_receipts ORDER BY sequence").fetchall()
        return self.connection.execute(
            "SELECT * FROM delivery_receipts WHERE artifact_kind=? ORDER BY sequence",
            (kind,)).fetchall()

    def settled_refs(self, kind: str) -> set[str]:
        """Artifact versions the owner has (or may have) been told about."""

        out: set[str] = set()
        for row in self.connection.execute(
                "SELECT artifact_refs_json FROM delivery_receipts WHERE artifact_kind=? "
                "AND outcome IN ('delivered','indeterminate')", (kind,)):
            out.update(json.loads(row["artifact_refs_json"]))
        return out

    def last_attempt_at(self, kind: str) -> datetime | None:
        row = self.connection.execute(
            "SELECT created_at FROM delivery_receipts WHERE artifact_kind=? AND outcome IN "
            "('attempting','delivered','failed','indeterminate') "
            "ORDER BY sequence DESC LIMIT 1", (kind,)).fetchone()
        return None if row is None else parse_time(row["created_at"])

    def last_failure_at(self, key: str) -> datetime | None:
        row = self.connection.execute(
            "SELECT created_at FROM delivery_receipts WHERE idempotency_key=? AND "
            "outcome='failed' ORDER BY sequence DESC LIMIT 1", (key,)).fetchone()
        return None if row is None else parse_time(row["created_at"])

    def sent_since(self, start: datetime) -> int:
        """Messages handed to the host since ``start`` (the day cap's count)."""

        count = 0
        for row in self.connection.execute(
                "SELECT created_at FROM delivery_receipts WHERE outcome IN "
                "('delivered','indeterminate')"):
            moment = parse_time(row["created_at"])
            if moment is not None and moment >= start:
                count += 1
        return count

    def capped_since(self, key: str, start: datetime) -> bool:
        for row in self.connection.execute(
                "SELECT created_at FROM delivery_receipts WHERE idempotency_key=? AND "
                "outcome='capped'", (key,)):
            moment = parse_time(row["created_at"])
            if moment is not None and moment >= start:
                return True
        return False

    def settle_interrupted(self, now: datetime) -> list[str]:
        """An ``attempting`` row with no terminal row: the pass died mid-send."""

        rows = self.connection.execute(
            "SELECT a.* FROM delivery_receipts a WHERE a.outcome='attempting' AND NOT EXISTS "
            "(SELECT 1 FROM delivery_receipts b WHERE b.attempt_id=a.attempt_id "
            "AND b.outcome!='attempting')").fetchall()
        settled: list[str] = []
        for row in rows:
            key = row["idempotency_key"]
            already = self.connection.execute(
                "SELECT 1 FROM delivery_receipts WHERE idempotency_key=? AND outcome IN "
                "('delivered','indeterminate')", (key,)).fetchone()
            self.append(
                attempt_id=row["attempt_id"], kind=row["artifact_kind"],
                refs=json.loads(row["artifact_refs_json"]), key=key,
                outcome="failed" if already else "indeterminate", at=now,
                recipient_ref=row["recipient_ref"],
                reason=("上一次发送在交给宿主之后中断，无法确认是否送达；"
                        "为避免重复提醒，不再重发"),
                contract_hash=row["contract_hash"])
            settled.append(row["attempt_id"])
        return settled


# ---------------------------------------------------------------------------
# producers: pure reads over read-only connections
# ---------------------------------------------------------------------------


@dataclass
class Candidate:
    kind: str
    refs: list[str]
    body: str
    at: str = ""
    # Digest only: the refs listed but already announced before.
    context: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return idempotency_key(self.kind, self.refs)


def _ro(path: Path | None) -> sqlite3.Connection | None:
    if path is None or not Path(path).is_file():
        return None
    try:
        connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro",
                                     uri=True, timeout=10)
    except sqlite3.Error:
        return None
    connection.row_factory = sqlite3.Row
    return connection


def _table(connection: sqlite3.Connection | None, name: str) -> bool:
    if connection is None:
        return False
    try:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None
    except sqlite3.Error:
        return False


def _clip(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: max(1, limit - 1)].rstrip() + "…"


#: Batch ad (2026-10-08): the cockpit at "/" is v2 (ADR-0016).  Deep links
#: name the v2 page that finishes the job; the previous page is "/legacy".
COCKPIT_TASKS_PATH = "#/tasks"
COCKPIT_ADMIN_RUN_PATH = "admin#run"
COCKPIT_ADMIN_AI_PATH = "admin#ai"


def _link(base: str | None, path: str = "") -> str:
    if not base:
        return "请打开 cockpit 查看"
    return base.rstrip("/") + "/" + path.lstrip("/")


def _since(moment: Any, since: datetime) -> bool:
    parsed = parse_time(moment)
    return parsed is not None and parsed >= since


def weekly_brief_candidates(core: sqlite3.Connection | None, *, since: datetime,
                            cockpit_base_url: str | None) -> list[Candidate]:
    """The current issue of each brief, when it was published after approval."""

    if not (_table(core, "weekly_brief_issue_pointer")
            and _table(core, "weekly_brief_issue_versions")):
        return []
    out: list[Candidate] = []
    rows = core.execute(
        "SELECT v.version_id, v.record_json, v.created_at, v.content_hash "
        "FROM weekly_brief_issue_pointer p JOIN weekly_brief_issue_versions v "
        "ON v.version_id=p.version_id ORDER BY v.created_at, v.version_id").fetchall()
    for row in rows:
        if not _since(row["created_at"], since):
            continue
        try:
            issue = json.loads(row["record_json"])
        except (TypeError, ValueError):
            continue
        out.append(Candidate(
            kind="weekly_brief", refs=[str(row["version_id"])], at=str(row["created_at"]),
            body=render_weekly_brief(issue, cockpit_base_url)))
    return out


def render_weekly_brief(issue: Mapping[str, Any], cockpit_base_url: str | None) -> str:
    period = issue.get("period") or {}
    change = issue.get("change_summary") or {}
    industry = str(issue.get("industry_ref") or "").rsplit(":", 1)[-1] or "本行业"
    lines = [
        f"【Dalton 每周研究 Brief】{_clip(industry, 40)}｜第 {issue.get('version')} 期",
        f"研究窗口：{_clip(period.get('start'), 30)} 至 {_clip(period.get('end'), 30)}",
    ]
    if change.get("is_baseline"):
        lines.append(f"本期是首次基线：{len(change.get('baseline_claim_version_refs') or [])} "
                     "条正式 Claim，不冒充为本周新增。")
    else:
        lines.append(
            "本期变化：新增正式 Claim {new} 条，延续 {carried} 条，移出 {removed} 条；"
            "变化的 driver {drivers} 个；新增 / 已解决证据缺口 {gaps_new} / {gaps_done}。".format(
                new=len(change.get("new_claim_version_refs") or []),
                carried=len(change.get("carried_claim_version_refs") or []),
                removed=len(change.get("removed_claim_version_refs") or []),
                drivers=len(change.get("changed_driver_refs") or []),
                gaps_new=len(change.get("new_gap_refs") or []),
                gaps_done=len(change.get("resolved_gap_refs") or [])))
    thesis = change.get("changed_thesis_company_refs") or []
    if thesis:
        lines.append("观点有变化的公司：" + "、".join(
            _clip(str(ref).rsplit(":", 1)[-1], 20) for ref in thesis[:8]))
    reconciliations = issue.get("forecast_reconciliations") or []
    followups = issue.get("judgement_followups") or []
    if reconciliations or followups:
        lines.append(f"预测对账 {len(reconciliations)} 条，研判跟进 {len(followups)} 条。")
    lines.append("全文：" + _link(cockpit_base_url, "weekly-brief"))
    return "\n".join(lines)


def owner_lever_candidates(core: sqlite3.Connection | None, *, since: datetime,
                           cockpit_base_url: str | None) -> list[Candidate]:
    """Each emergency stop / resume-all receipt the owner wrote after approval."""

    if not _table(core, "owner_lever_receipts"):
        return []
    out: list[Candidate] = []
    for row in core.execute(
            "SELECT receipt_id, record_json, created_at FROM owner_lever_receipts "
            "WHERE lever IN ('emergency_stop','resume_all') ORDER BY sequence"):
        if not _since(row["created_at"], since):
            continue
        try:
            wire = json.loads(row["record_json"])
        except (TypeError, ValueError):
            continue
        out.append(Candidate(kind="owner_lever", refs=[str(row["receipt_id"])],
                             at=str(row["created_at"]),
                             body=render_owner_lever(wire, cockpit_base_url)))
    return out


def render_owner_lever(wire: Mapping[str, Any], cockpit_base_url: str | None) -> str:
    reason = _clip(wire.get("reason") or "未写", 200)
    if wire.get("lever") == "emergency_stop":
        until = wire.get("until")
        when = f"到 {until} 自动恢复" if until else "直到你恢复全部运行"
        lines = [
            "【Dalton】紧急停机已生效",
            "你按下了紧急停机：新的任务和付费模型调用都已暂停，恢复后继续。",
            f"持续：{when}",
            f"理由：{reason}",
            "恢复：管理员设置 → 运行控制 → 恢复全部运行。 "
            + _link(cockpit_base_url, COCKPIT_ADMIN_RUN_PATH),
        ]
    else:
        lines = [
            "【Dalton】已恢复全部运行",
            "紧急停机已解除，新的任务和模型调用会在下一轮恢复。",
            f"理由：{reason}",
            _link(cockpit_base_url),
        ]
    return "\n".join(lines)


def fallback_notice_candidates(router: sqlite3.Connection | None, *, since: datetime,
                               cockpit_base_url: str | None,
                               settled: set[str] | frozenset[str] = frozenset(),
                               ) -> list[Candidate]:
    """Open (unacknowledged) fallback notices written after approval, as one batch."""

    if not _table(router, "model_fallback_notices"):
        return []
    has_acks = _table(router, "model_fallback_notice_acks")
    query = ("SELECT n.notice_id, n.notice_json, n.created_at FROM model_fallback_notices n "
             + ("LEFT JOIN model_fallback_notice_acks a ON a.notice_id=n.notice_id "
                "WHERE a.notice_id IS NULL " if has_acks else "")
             + "ORDER BY n.notice_sequence")
    notices: list[tuple[str, Mapping[str, Any], str]] = []
    for row in router.execute(query):
        if not _since(row["created_at"], since) or row["notice_id"] in settled:
            continue
        try:
            notice = json.loads(row["notice_json"])
        except (TypeError, ValueError):
            continue
        notices.append((str(row["notice_id"]), notice, str(row["created_at"])))
    if not notices:
        return []
    return [Candidate(kind="model_fallback_notice",
                      refs=[ref for ref, _notice, _at in notices],
                      at=notices[0][2],
                      body=render_fallback_notices([n for _r, n, _a in notices],
                                                   cockpit_base_url))]


def render_fallback_notices(notices: Sequence[Mapping[str, Any]],
                            cockpit_base_url: str | None) -> str:
    lines = [f"【Dalton】模型回退通知（{len(notices)} 条）"]
    for notice in notices[:MAX_DIGEST_LINES]:
        lines.append("· " + _clip(notice.get("message"), 240))
    if len(notices) > MAX_DIGEST_LINES:
        lines.append(f"……另有 {len(notices) - MAX_DIGEST_LINES} 条，见管理员设置「AI 服务」")
    lines.append("AI 服务设置：" + _link(cockpit_base_url, COCKPIT_ADMIN_AI_PATH))
    return "\n".join(lines)


CALL_DIRECTION_LABELS = {"long": "做多", "short": "做空", "avoid": "回避"}


def needs_human_items(core: sqlite3.Connection | None) -> list[dict[str, Any]]:
    """Conviction calls and Investment Memos waiting for the owner's decision."""

    if core is None:
        return []
    from .needs_human import _company_label, _company_labels, memo_decisions

    items: list[dict[str, Any]] = []
    if _table(core, "conviction_call_proposals") and _table(core, "conviction_call_decisions"):
        rows = core.execute(
            "SELECT p.proposal_id, p.company_ref, p.direction, p.created_at "
            "FROM conviction_call_proposals p LEFT JOIN conviction_call_decisions d "
            "ON d.proposal_ref=p.proposal_id WHERE d.decision_id IS NULL "
            "ORDER BY p.created_at, p.proposal_id").fetchall()
        labels = _company_labels(core) if rows else {}
        for row in rows:
            items.append({
                "kind": "conviction_call", "ref": str(row["proposal_id"]),
                "at": str(row["created_at"]),
                "title": "是否采纳这条投资 call："
                         + CALL_DIRECTION_LABELS.get(row["direction"], row["direction"])
                         + "｜" + _company_label(labels, row["company_ref"]),
            })
    try:
        memos = memo_decisions(core)
    except sqlite3.Error:
        memos = []
    for memo in memos:
        items.append({"kind": "memo_decision", "ref": memo["ref"], "at": memo["at"],
                      "title": memo["title"]})
    items.sort(key=lambda item: (item["at"], item["ref"]))
    return items


def needs_human_digest(items: Sequence[Mapping[str, Any]], settled: set[str],
                       cockpit_base_url: str | None) -> Candidate | None:
    """One digest announcing what is new; listing what is still waiting."""

    fresh = [item for item in items if item["ref"] not in settled]
    if not fresh:
        return None
    lines = [f"【Dalton】需要你处理：{len(items)} 件等你裁决（新增 {len(fresh)} 件）"]
    for item in items[:MAX_DIGEST_LINES]:
        mark = "［新］" if item["ref"] not in settled else ""
        lines.append(f"· {mark}{_clip(item['title'], 120)}")
    if len(items) > MAX_DIGEST_LINES:
        lines.append(f"……另有 {len(items) - MAX_DIGEST_LINES} 件")
    lines.append(f"在「{_term('nav', 'tasks')}」页逐件裁决：" + _link(cockpit_base_url, COCKPIT_TASKS_PATH))
    return Candidate(kind="needs_human_digest", refs=[item["ref"] for item in fresh],
                     at=fresh[0]["at"], body="\n".join(lines),
                     context={"listed": len(items)})


# ---------------------------------------------------------------------------
# the host tool: openclaw message send --channel feishu
# ---------------------------------------------------------------------------

CommandRunner = Callable[[Sequence[str], Mapping[str, str], float], Mapping[str, Any]]


def child_environment() -> dict[str, str]:
    env = {name: os.environ[name] for name in _PASSTHROUGH_ENV if name in os.environ}
    env.update({name: value for name, value in os.environ.items()
                if name.startswith("OPENCLAW_")})
    return env


def _default_runner(argv: Sequence[str], env: Mapping[str, str],
                    timeout: float) -> Mapping[str, Any]:
    try:
        completed = subprocess.run(
            list(argv), check=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=timeout, env=dict(env))
    except subprocess.TimeoutExpired:
        raise DeliverySendError("openclaw_timeout") from None
    except OSError as exc:
        raise DeliverySendError("openclaw_unavailable", str(exc)[:200]) from None
    if len(completed.stdout) > MAX_COMMAND_OUTPUT_BYTES:
        raise DeliverySendError("openclaw_output_limit")
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace")[-300:]
        raise DeliverySendError(f"openclaw_exit_{completed.returncode}", detail)
    try:
        value = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise DeliverySendError("openclaw_invalid_json") from None
    if not isinstance(value, Mapping):
        raise DeliverySendError("openclaw_invalid_response")
    return value


def _find_message_id(value: Any, depth: int = 0) -> str | None:
    if depth > 6:
        return None
    if isinstance(value, Mapping):
        for key in ("messageId", "message_id"):
            found = value.get(key)
            if isinstance(found, (str, int)) and str(found):
                return str(found)
        for nested in value.values():
            found = _find_message_id(nested, depth + 1)
            if found:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_message_id(nested, depth + 1)
            if found:
                return found
    return None


def resolve_executable(configured: str | None) -> Path | None:
    candidates = [configured] if configured else []
    which = shutil.which("openclaw")
    if not configured:
        candidates += [which, *_EXECUTABLE_CANDIDATES]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return Path(candidate)
    return None


class OpenClawFeishuSender:
    """The one host tool this connector may call, with one fixed argv shape."""

    def __init__(self, *, executable: str | None, account: str | None,
                 timeout_seconds: float, runner: CommandRunner | None = None) -> None:
        self.executable = executable
        self.account = account
        self.timeout_seconds = float(timeout_seconds)
        self.runner = runner or _default_runner

    def argv(self, executable: str, chat_id: str, body: str) -> list[str]:
        argv = [executable, *CONTRACT_TERMS["host_tool"]["argv"]]
        if self.account:
            argv += ["--account", self.account]
        return argv + ["--target", chat_id, "--message", body, "--json"]

    def send(self, chat_id: str, body: str) -> str:
        executable = resolve_executable(self.executable)
        if executable is None:
            raise DeliverySendError("openclaw_executable_unavailable")
        value = self.runner(self.argv(str(executable), chat_id, body),
                            child_environment(), self.timeout_seconds)
        payload = value.get("payload") if isinstance(value.get("payload"), Mapping) else value
        if payload.get("ok") is False or value.get("ok") is False:
            raise DeliverySendError("openclaw_send_refused",
                                    _clip(payload.get("error") or value.get("error"), 200))
        message_id = _find_message_id(value)
        if not message_id:
            raise DeliverySendError("openclaw_send_missing_receipt")
        return message_id


# ---------------------------------------------------------------------------
# one pass
# ---------------------------------------------------------------------------


def day_start(now: datetime, zone_name: str) -> datetime:
    zone = ZoneInfo(zone_name)
    local = now.astimezone(zone)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def run_delivery_pass(
    state_dir: str | Path,
    *,
    now: datetime | None = None,
    settings: Mapping[str, Any] | None = None,
    sender: Any = None,
    service_config_path: str | Path | None = None,
) -> dict[str, Any]:
    """Decide what is due, send it through the host tool, and receipt it all."""

    state = Path(state_dir)
    moment = (now or _utc_now()).astimezone(timezone.utc)
    contract = load_contract(state)
    summary: dict[str, Any] = {"channel": CHANNEL, "as_of": _iso(moment),
                               "contract": contract.summary()}
    if not contract.approved:
        return {**summary, "status": "not_granted", "reason": contract.reason}
    if settings is None:
        settings = load_settings(state, service_config_path=service_config_path)
    else:
        settings = {**DEFAULT_SETTINGS, **settings}
    if sender is None:
        sender = OpenClawFeishuSender(executable=settings.get("openclaw_executable"),
                                      account=contract.account,
                                      timeout_seconds=settings["send_timeout_seconds"])
    base_url = settings.get("cockpit_base_url")
    since = contract.approved_at
    core = _ro(state / "core.sqlite")
    router_path = settings.get("model_router_db")
    router = _ro(Path(router_path) if router_path else state / "model-router.sqlite")
    ledger = DeliveryLedger(state / LEDGER_FILE_NAME)
    sent: list[dict[str, Any]] = []
    waiting: list[dict[str, Any]] = []
    try:
        interrupted = ledger.settle_interrupted(moment)
        candidates: list[Candidate] = []
        candidates += owner_lever_candidates(core, since=since, cockpit_base_url=base_url)
        # A batch announces only the notices nobody has been told about.
        candidates += fallback_notice_candidates(
            router, since=since, cockpit_base_url=base_url,
            settled=ledger.settled_refs("model_fallback_notice"))
        candidates += weekly_brief_candidates(core, since=since, cockpit_base_url=base_url)
        digest = needs_human_digest(needs_human_items(core),
                                    ledger.settled_refs("needs_human_digest"), base_url)
        if digest is not None:
            candidates.append(digest)

        settled_by_kind = {kind: ledger.settled_refs(kind) for kind in ARTIFACT_KINDS}
        start = day_start(moment, settings["day_timezone"])
        cap = int(settings["daily_message_cap"])
        candidates.sort(key=lambda item: (PRIORITY[item.kind], item.at, item.key))
        for item in candidates:
            if set(item.refs) <= settled_by_kind[item.kind]:
                continue
            gate = _gate(item, ledger, settings, moment)
            if gate is not None:
                waiting.append({"kind": item.kind, "refs": item.refs, "reason": gate})
                continue
            if ledger.sent_since(start) >= cap:
                if not ledger.capped_since(item.key, start):
                    ledger.append(attempt_id="attempt:" + uuid.uuid4().hex, kind=item.kind,
                                  refs=item.refs, key=item.key, outcome="capped", at=moment,
                                  recipient_ref=contract.chat_id,
                                  reason=f"今天已发 {cap} 条，达到每日上限；明天再发",
                                  contract_hash=contract.content_hash)
                waiting.append({"kind": item.kind, "refs": item.refs, "reason": "daily_cap"})
                continue
            result = _send_one(ledger, sender, contract, item, moment)
            sent.append(result)
            if result["outcome"] == "delivered":
                settled_by_kind[item.kind] |= set(item.refs)
    finally:
        ledger.close()
        if core is not None:
            core.close()
        if router is not None:
            router.close()
    delivered = [row for row in sent if row["outcome"] == "delivered"]
    failed = [row for row in sent if row["outcome"] != "delivered"]
    status = ("failed" if failed and not delivered
              else "delivered" if delivered else "idle")
    return {**summary, "status": status, "delivered": delivered, "failed": failed,
            "waiting": waiting, "interrupted_settled": interrupted,
            "reason": None if status != "failed" else failed[0].get("reason")}


def _gate(item: Candidate, ledger: DeliveryLedger, settings: Mapping[str, Any],
          now: datetime) -> str | None:
    """Why this candidate must wait (rate limit / digest window / retry), or None."""

    if item.kind == "needs_human_digest":
        last = ledger.last_attempt_at("needs_human_digest")
        window = timedelta(hours=int(settings["digest_interval_hours"]))
        if last is not None and now - last < window:
            return "digest_interval"
        return None
    if item.kind == "model_fallback_notice":
        last = ledger.last_attempt_at("model_fallback_notice")
        window = timedelta(minutes=int(settings["fallback_min_interval_minutes"]))
        if last is not None and now - last < window:
            return "fallback_rate_limit"
        return None
    failed = ledger.last_failure_at(item.key)
    if failed is not None and now - failed < timedelta(
            minutes=int(settings["retry_after_minutes"])):
        return "retry_after_failure"
    return None


def _send_one(ledger: DeliveryLedger, sender: Any, contract: ContractState,
              item: Candidate, now: datetime) -> dict[str, Any]:
    attempt = "attempt:" + uuid.uuid4().hex
    # Batch am: every Feishu message passes through the terminology registry,
    # so the chat reads the same words as the cockpit (no 车道 / 再买一次 ...).
    text = scrub(item.body)
    body = text if len(text) <= MAX_MESSAGE_CHARS \
        else text[: MAX_MESSAGE_CHARS - 1] + "…"
    common = dict(attempt_id=attempt, kind=item.kind, refs=item.refs, key=item.key,
                  recipient_ref=contract.chat_id, contract_hash=contract.content_hash)
    try:
        ledger.append(**common, outcome="attempting", at=now, body=body)
    except sqlite3.IntegrityError:
        return {"kind": item.kind, "refs": item.refs, "outcome": "failed",
                "reason": "already_settled"}
    try:
        message_id = sender.send(contract.chat_id, body)
    except DeliverySendError as exc:
        record = ledger.append(**common, outcome="failed", at=now,
                               reason=str(exc))
        return {"kind": item.kind, "refs": item.refs, "outcome": "failed",
                "reason": record["reason"]}
    except Exception as exc:  # noqa: BLE001 - every failure becomes a receipt
        record = ledger.append(**common, outcome="failed", at=now,
                               reason=f"{type(exc).__name__}: {exc}")
        return {"kind": item.kind, "refs": item.refs, "outcome": "failed",
                "reason": record["reason"]}
    try:
        ledger.append(**common, outcome="delivered", at=now,
                      message_id=str(message_id), body=body)
    except sqlite3.IntegrityError:
        # Another pass settled the same identity first; this one is a duplicate
        # the host already sent.  Recorded, not hidden.
        ledger.append(**common, outcome="failed", at=now,
                      message_id=str(message_id),
                      reason="duplicate_send: another pass delivered this message first")
        return {"kind": item.kind, "refs": item.refs, "outcome": "failed",
                "reason": "duplicate_send"}
    return {"kind": item.kind, "refs": item.refs, "outcome": "delivered",
            "message_id": str(message_id)}


# ---------------------------------------------------------------------------
# the writer-side wake: the fallback seam and the levers ask for a pass now
# ---------------------------------------------------------------------------

_WAKE_LOCK = threading.Lock()
_WAKE_COUNTER = 0


def request_wake(reason: str = "") -> int:
    """Ask the delivery lane to start a pass on its next tick (cheap, in-process)."""

    del reason
    global _WAKE_COUNTER
    with _WAKE_LOCK:
        _WAKE_COUNTER += 1
        return _WAKE_COUNTER


def wake_counter() -> int:
    with _WAKE_LOCK:
        return _WAKE_COUNTER


def latest_lever_sequence(state_dir: str | Path) -> int:
    """Newest emergency-stop / resume-all receipt, read ``mode=ro``; 0 if none."""

    core = _ro(Path(state_dir) / "core.sqlite")
    if core is None:
        return 0
    try:
        if not _table(core, "owner_lever_receipts"):
            return 0
        row = core.execute(
            "SELECT MAX(sequence) FROM owner_lever_receipts "
            "WHERE lever IN ('emergency_stop','resume_all')").fetchone()
        return int(row[0] or 0)
    except sqlite3.Error:
        return 0
    finally:
        core.close()


def ledger_summary(state_dir: str | Path, *, limit: int = 20) -> dict[str, Any]:
    """Read-only view of the ledger for an operator or a page."""

    path = Path(state_dir) / LEDGER_FILE_NAME
    if not path.is_file():
        return {"present": False, "receipts": []}
    ledger = DeliveryLedger(path, read_only=True)
    try:
        rows = ledger.connection.execute(
            "SELECT record_json FROM delivery_receipts ORDER BY sequence DESC LIMIT ?",
            (int(limit),)).fetchall()
        counts = dict(ledger.connection.execute(
            "SELECT outcome, COUNT(*) FROM delivery_receipts GROUP BY outcome").fetchall())
    finally:
        ledger.close()
    return {"present": True, "counts": counts,
            "receipts": [json.loads(row["record_json"]) for row in rows]}


# ---------------------------------------------------------------------------
# child entry point
# ---------------------------------------------------------------------------


def write_contract(state_dir: str | Path, record: Mapping[str, Any], *,
                   now: datetime | None = None) -> dict[str, Any]:
    """Atomically replace the contract file (0600), keeping the old one beside it."""

    path = contract_path(state_dir)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup = None
    if path.is_file():
        stamp = (now or _utc_now()).strftime("%Y%m%dT%H%M%SZ")
        backup = path.with_name(f"{path.name}.pre-{stamp}")
        backup.write_bytes(path.read_bytes())
        os.chmod(backup, 0o600)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return {"path": str(path), "backup": None if backup is None else str(backup)}


def _owner_command(args: Any) -> int:
    state = Path(args.state_dir).expanduser()
    if not (state / "core.sqlite").is_file():
        print(f"!! {state} 下没有 core.sqlite，不像一个环境的 state 目录", file=sys.stderr)
        return 2
    before = load_contract(state)
    try:
        if args.command == "approve":
            record = approve_contract(None, approved_by=args.approved_by,
                                      chat_id=args.chat_id, account=args.account)
        else:
            record = revoke_contract(None)
    except OwnerDeliveryError as exc:
        print(f"!! {exc}", file=sys.stderr)
        return 2
    after = evaluate_contract(record)
    report = {"mode": "apply" if args.apply else "dry-run",
              "contract_file": str(contract_path(state)),
              "before": before.summary(), "after": after.summary(),
              "record": record}
    if args.apply:
        report["written"] = write_contract(state, record)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Owner delivery pass (lane child).")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run one delivery pass (sends through OpenClaw)")
    run.add_argument("--state-dir", type=Path, required=True)
    run.add_argument("--summary-path", type=Path)
    status = sub.add_parser("status", help="read-only: contract state and recent receipts")
    status.add_argument("--state-dir", type=Path, required=True)
    approve = sub.add_parser("approve", help="owner: approve with the Feishu chat id "
                             "(dry-run unless --apply)")
    approve.add_argument("--state-dir", type=Path, required=True)
    approve.add_argument("--chat-id", required=True)
    approve.add_argument("--approved-by", required=True)
    approve.add_argument("--account")
    approve.add_argument("--apply", action="store_true")
    revoke = sub.add_parser("revoke", help="owner: back to proposed (dry-run unless --apply)")
    revoke.add_argument("--state-dir", type=Path, required=True)
    revoke.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.command in ("approve", "revoke"):
        return _owner_command(args)
    if args.command == "status":
        print(json.dumps({"contract": load_contract(args.state_dir).summary(),
                          "settings": load_settings(args.state_dir),
                          "ledger": ledger_summary(args.state_dir)},
                         ensure_ascii=False, indent=2, default=str))
        return 0
    try:
        result = run_delivery_pass(args.state_dir)
    except Exception as exc:  # noqa: BLE001 - the lane reads the summary
        result = {"status": "failed", "reason": f"{type(exc).__name__}: {exc}"[:500]}
    text = json.dumps(result, ensure_ascii=False, sort_keys=True, default=str)
    if args.summary_path is not None:
        args.summary_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = args.summary_path.with_name(f".{args.summary_path.name}.tmp")
        tmp.write_text(text + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, args.summary_path)
    print(text)
    return 0 if result.get("status") != "failed" else 1


__all__ = [
    "ARTIFACT_KINDS", "CAPABILITY_ID", "CHANNEL", "CONTRACT_FILE_NAME", "CONTRACT_HASH",
    "CONTRACT_ID", "CONTRACT_TERMS", "Candidate", "ContractState", "DEFAULT_SETTINGS",
    "DeliveryLedger", "DeliverySendError", "LEDGER_FILE_NAME", "OpenClawFeishuSender",
    "OwnerDeliveryError", "approve_contract", "build_contract_record", "contract_path",
    "evaluate_contract", "fallback_notice_candidates", "idempotency_key",
    "latest_lever_sequence", "ledger_summary", "load_contract", "load_settings",
    "needs_human_digest", "needs_human_items", "owner_lever_candidates", "request_wake",
    "revoke_contract", "run_delivery_pass", "validate_settings", "wake_counter",
    "weekly_brief_candidates",
]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
