"""P10c: the mission's own documents, with every number traceable (vision v1.1).

Phase 9 froze what an Initial Screen contains (the Playbook's
``deliverable_templates.initial_screen``) and what it must satisfy to pass its
gate.  P9d filled the Ledger with Claims.  This module is where the two meet:
a versioned, append-only document per company, drafted from Claims the Ledger
already holds, and refused if it says a number it cannot trace.

Three rules the authority enforces rather than trusts:

- **Every number traces to a quantitative Claim.**  A section body may only
  carry a figure that appears in that section's ``numbers`` list bound to a
  quantitative claim version.  A period label (a year, a quarter, a fiscal
  year) is not a figure, the same distinction the drafting path already makes.
  An unsourced figure fails the publish; the drafter is expected to write
  "缺来源" instead of guessing.
- **Every cited Claim exists and is not retired.**  A section's ``claim_refs``
  must name live claim versions (P10b retirements are excluded), so a document
  can never rest on a Claim the system has already disowned.
- **The document binds the authorities it was written under.**  The mission
  version and the playbook version, both by hash: if either moves, the next
  version records the move instead of the text silently drifting.

Automation may publish only what the mission grants (``deliverable`` in
``autonomy.may_write``); a person may always publish.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .store import DaltonStore, content_hash

SCHEMA_VERSION = "0.1"
_SCHEMA_PATH = Path(__file__).with_name("mission_deliverable_schema.sql")
WRITE_SCOPE = "deliverable"
DELIVERABLE_KINDS: tuple[str, ...] = (
    "industry_framework", "initial_screen", "industry_model", "company_model",
    "forecast_lines", "investment_memo", "weekly_brief",
)
MAX_SECTIONS = 24
MAX_BODY_CHARS = 6000
GAP_MARKER = "缺来源"

# A year, quarter, half or fiscal-year label is a period, not a figure.  Same
# rule the extraction contract uses, so the two paths agree on what a number is.
_PERIOD_TOKEN_RE = re.compile(
    # ISO dates and the ".." ranges the Ledger prints for a reporting period,
    # which live were being read apart into "01" and "31".
    r"(?:19|20)\d{2}-\d{2}-\d{2}(?:\s*\.\.\s*(?:19|20)?\d{2}-\d{2}-\d{2})?"
    r"|(?:FY\s?)?(?:19|20)\d{2}(?:\s?[-–/]\s?(?:19|20)?\d{2})?(?:\s?(?:年|财年))?"
    r"|FY\s?\d{2}(?![0-9])"
    r"|Q[1-4]\s?(?:FY\s?)?(?:19|20)?\d{0,4}"
    r"|(?:19|20)\d{2}\s?Q[1-4]"
    r"|[1-4]Q(?:19|20)?\d{2}"
    r"|H[12]\s?(?:19|20)?\d{0,4}"
    r"|第?[一二三四1-4]季度",
    re.IGNORECASE,
)
# The Playbook's rule is about *timely numbers*: a measurement that has to come
# from a filing or a tool result.  A bare small integer with no unit, percent,
# currency or separator is a threshold, a count or an ordinal ("book-to-bill
# 跌破 1", "两条线"), not a measurement, and requiring a Claim for it would
# empty the document without making it truer.
_BARE_SMALL_INTEGER = 12
_VALUE_TOKEN_RE = re.compile(r"[$€£¥]\s?\d[\d,.]*|\d[\d,.]*\s?%|\d[\d,.]*")


class MissionDeliverableError(RuntimeError):
    """Base error for the deliverable authority."""


class MissionDeliverableValidationError(MissionDeliverableError):
    """A closed field or argument is invalid."""


class MissionDeliverableConflict(MissionDeliverableError):
    """An append-only record was reused with different semantics, or a gate refused."""


class MissionDeliverableNotFound(MissionDeliverableError):
    """The named record does not exist."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any, name: str, *, maximum: int = 2000, minimum: int = 1) -> str:
    if not isinstance(value, str) or not (minimum <= len(value.strip()) <= maximum):
        raise MissionDeliverableValidationError(f"{name} must be text of {minimum}..{maximum} characters")
    return value.strip()


def _sha256(value: Any, name: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise MissionDeliverableValidationError(f"{name} must be a SHA-256 hex digest")
    return value


# A drafted body may still carry a "C7" / "N1" citation tag; its digits are a
# reference, not a figure.  Live, they were the first thing the check flagged.
_CITATION_TAG_RE = re.compile(r"(?<![A-Za-z0-9])[CN]\d{1,3}(?![A-Za-z0-9])")


def value_tokens(text: str) -> list[str]:
    """Every figure the text asserts, with period labels and citation tags removed."""

    without_tags = _CITATION_TAG_RE.sub(" ", text or "")
    without_periods = _PERIOD_TOKEN_RE.sub(" ", without_tags)
    tokens = []
    for match in _VALUE_TOKEN_RE.finditer(without_periods):
        token = match.group(0).strip()
        plain = token.rstrip(".")
        if plain.isdigit() and int(plain) <= _BARE_SMALL_INTEGER:
            continue
        tokens.append(token)
    return tokens


def _normalise_number(token: str) -> str:
    return re.sub(r"[\s,]", "", token).rstrip(".")


def unsourced_numbers(body: str, numbers: Sequence[Mapping[str, Any]]) -> list[str]:
    """Figures in the body that no supplied, Claim-bound number accounts for."""

    sourced = set()
    for item in numbers:
        for token in value_tokens(str(item.get("text", ""))):
            sourced.add(_normalise_number(token))
    return [
        token for token in value_tokens(body)
        if _normalise_number(token) not in sourced
    ]


def validate_section(
    section: Mapping[str, Any], *, live_claim_refs: set[str] | None = None
) -> dict[str, Any]:
    """One closed section: a title, a body or a gap, and traceable numbers."""

    if not isinstance(section, Mapping):
        raise MissionDeliverableValidationError("section must be an object")
    allowed = {"title", "body", "claim_refs", "numbers", "gaps"}
    if set(section) - allowed or "title" not in section:
        raise MissionDeliverableValidationError("section has an invalid closed shape")
    title = _text(section["title"], "section.title", maximum=200)
    body = section.get("body") or ""
    if not isinstance(body, str) or len(body) > MAX_BODY_CHARS:
        raise MissionDeliverableValidationError("section.body must be text under 6000 characters")
    numbers = section.get("numbers") or []
    if not isinstance(numbers, list) or len(numbers) > 60:
        raise MissionDeliverableValidationError("section.numbers must be a list of at most 60 entries")
    checked: list[dict[str, Any]] = []
    for item in numbers:
        if not isinstance(item, Mapping) or set(item) - {"text", "claim_version_ref", "period"} or not {
            "text", "claim_version_ref"
        } <= set(item):
            raise MissionDeliverableValidationError("number entries need text and claim_version_ref")
        ref = _text(item["claim_version_ref"], "number.claim_version_ref", maximum=512)
        if live_claim_refs is not None and ref not in live_claim_refs:
            raise MissionDeliverableConflict(
                "a figure cites a Claim that is retired or does not exist: " + ref
            )
        checked.append({
            "text": _text(item["text"], "number.text", maximum=200),
            "claim_version_ref": ref,
            "period": None if item.get("period") is None else _text(item["period"], "number.period", maximum=120),
        })
    claim_refs = section.get("claim_refs") or []
    if not isinstance(claim_refs, list) or len(claim_refs) > 200 or any(
        not isinstance(ref, str) or not ref for ref in claim_refs
    ):
        raise MissionDeliverableValidationError("section.claim_refs must be a list of refs")
    if live_claim_refs is not None:
        missing = [ref for ref in claim_refs if ref not in live_claim_refs]
        if missing:
            raise MissionDeliverableConflict(
                "a section cites Claims that are retired or do not exist: " + ", ".join(missing[:3])
            )
    gaps = section.get("gaps") or []
    if not isinstance(gaps, list) or len(gaps) > 20 or any(
        not isinstance(gap, str) or not gap.strip() for gap in gaps
    ):
        raise MissionDeliverableValidationError("section.gaps must be a list of short strings")
    stray = unsourced_numbers(body, checked)
    if stray:
        raise MissionDeliverableConflict(
            f"section「{title}」carries figures with no Claim behind them: {stray[:5]}；"
            f"写 {GAP_MARKER} 而不是猜一个数字"
        )
    return {
        "title": title, "body": body.strip(), "claim_refs": list(dict.fromkeys(claim_refs)),
        "numbers": checked, "gaps": [gap.strip()[:300] for gap in gaps],
    }


class MissionDeliverableAuthority:
    """Append-only, hash-bound documents, refused when a number has no source."""

    def __init__(self, store: DaltonStore, *, clock: Callable[[], str] | None = None) -> None:
        self.store = store
        self.connection = store.connection
        self.clock = clock or _now
        self._authorized = False
        self.connection.create_function(
            "dalton_mission_deliverable_authorized", 0, lambda: int(self._authorized)
        )
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        if self._authorized:
            raise RuntimeError("MissionDeliverableAuthority operation cannot be nested")
        self._authorized = True
        try:
            with self.store._transaction() as cur:
                yield cur
        finally:
            self._authorized = False

    # -- reads ---------------------------------------------------------------

    def live_claim_version_refs(self) -> set[str]:
        """Claim versions a document may cite: everything the Ledger holds, less retirements."""

        refs = {
            row["claim_version_id"]
            for row in self.connection.execute("SELECT claim_version_id FROM claim_versions").fetchall()
        }
        try:
            retired = {
                row["claim_version_ref"]
                for row in self.connection.execute(
                    "SELECT claim_version_ref FROM claim_retirement_decisions WHERE decision='retired'"
                ).fetchall()
            }
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                raise
            retired = set()
        return refs - retired

    def latest(self, deliverable_ref: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT v.record_json AS record_json, v.content_hash AS content_hash "
            "FROM mission_deliverable_pointer p JOIN mission_deliverable_versions v "
            "ON v.version_id=p.version_id WHERE p.deliverable_ref=?",
            (_text(deliverable_ref, "deliverable_ref", maximum=512),),
        ).fetchone()
        if row is None:
            return None
        record = json.loads(row["record_json"])
        if record["content_hash"] != row["content_hash"]:
            raise MissionDeliverableConflict("mission deliverable authority drifted")
        return record

    def deliverables(
        self, mission_version_ref: str, *, kind: str | None = None, subject_ref: str | None = None
    ) -> list[dict[str, Any]]:
        query = (
            "SELECT v.record_json AS record_json FROM mission_deliverable_pointer p "
            "JOIN mission_deliverable_versions v ON v.version_id=p.version_id "
            "WHERE v.mission_version_ref=?"
        )
        params: list[Any] = [_text(mission_version_ref, "mission_version_ref", maximum=512)]
        if kind is not None:
            query += " AND v.kind=?"
            params.append(kind)
        if subject_ref is not None:
            query += " AND v.subject_ref=?"
            params.append(subject_ref)
        query += " ORDER BY v.created_at, v.version_id"
        return [json.loads(row["record_json"]) for row in self.connection.execute(query, params).fetchall()]

    # -- write ---------------------------------------------------------------

    def publish(
        self,
        *,
        kind: str,
        subject_ref: str,
        mission: Mapping[str, Any],
        playbook: Mapping[str, Any],
        template_ref: str,
        sections: Sequence[Mapping[str, Any]],
        summary: str,
        gaps: Sequence[str] = (),
        model_invocation_refs: Sequence[str] = (),
        actor_ref: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if kind not in DELIVERABLE_KINDS:
            raise MissionDeliverableValidationError(f"kind must be one of {list(DELIVERABLE_KINDS)}")
        subject_ref = _text(subject_ref, "subject_ref", maximum=256)
        actor_ref = _text(actor_ref, "actor_ref", maximum=256)
        if not (actor_ref.startswith("human:") or actor_ref.startswith("automation:")):
            raise MissionDeliverableValidationError("actor_ref must be a human: or automation: principal")
        if actor_ref.startswith("automation:"):
            if actor_ref != mission["autonomy"]["automation_principal"]:
                raise MissionDeliverableConflict("automation actor is not the mission principal")
            if WRITE_SCOPE not in mission["autonomy"]["may_write"]:
                raise MissionDeliverableConflict(
                    f"mission does not grant {WRITE_SCOPE} writes to automation"
                )
        if kind != "industry_framework" and not any(
            member["company_ref"] == subject_ref for member in mission["universe"]
        ) and subject_ref != mission["industry_ref"]:
            raise MissionDeliverableConflict("subject is not in the mission universe")
        if not isinstance(sections, Sequence) or not 1 <= len(sections) <= MAX_SECTIONS:
            raise MissionDeliverableValidationError(f"sections must be 1..{MAX_SECTIONS} entries")
        live = self.live_claim_version_refs()
        checked = [validate_section(section, live_claim_refs=live) for section in sections]
        if not any(section["body"] for section in checked):
            raise MissionDeliverableConflict("a deliverable with no written section is an empty shell")
        summary = _text(summary, "summary", maximum=2000)
        slug = subject_ref.rsplit(":", 1)[-1]
        deliverable_ref = f"mission-deliverable:{kind}:{slug}"
        record = {
            "schema_version": SCHEMA_VERSION,
            "deliverable_ref": deliverable_ref,
            "kind": kind,
            "subject_ref": subject_ref,
            "mission_version_ref": mission["id"],
            "mission_version_hash": mission["content_hash"],
            "playbook_version_ref": playbook["id"],
            "playbook_version_hash": playbook["content_hash"],
            "template_ref": _text(template_ref, "template_ref", maximum=256),
            "summary": summary,
            "sections": checked,
            "gaps": [str(gap).strip()[:300] for gap in gaps][:40],
            "model_invocation_refs": [str(ref)[:512] for ref in model_invocation_refs][:40],
            "actor_ref": actor_ref,
            "created_at": self.clock(),
        }
        # What the document says, without its version number or timestamp: an
        # unchanged body is a duplicate, not a new version.
        record["body_hash"] = content_hash({
            key: record[key] for key in (
                "kind", "subject_ref", "mission_version_ref", "mission_version_hash",
                "playbook_version_ref", "playbook_version_hash", "template_ref",
                "summary", "sections", "gaps",
            )
        })
        with self._transaction() as cur:
            pointer = cur.execute(
                "SELECT version_id, version_number, content_hash FROM mission_deliverable_pointer "
                "WHERE deliverable_ref=?", (deliverable_ref,),
            ).fetchone()
            version = 1 if pointer is None else int(pointer["version_number"]) + 1
            prior = None if pointer is None else pointer["version_id"]
            record["version"] = version
            record["prior_version_ref"] = prior
            record["id"] = f"mission-deliverable-version:{content_hash({'ref': deliverable_ref, 'version': version})[:32]}"
            record["content_hash"] = content_hash({k: v for k, v in record.items() if k != "content_hash"})
            if pointer is not None:
                existing = cur.execute(
                    "SELECT record_json FROM mission_deliverable_versions WHERE version_id=?",
                    (pointer["version_id"],),
                ).fetchone()
                current = json.loads(existing["record_json"])
                if current.get("body_hash") == record["body_hash"]:
                    return {**current, "status": "duplicate"}
            if idempotency_key is not None:
                key = _text(idempotency_key, "idempotency_key", maximum=512)
                seen = cur.execute(
                    "SELECT version_id FROM mission_deliverable_versions WHERE deliverable_ref=? "
                    "AND json_extract(record_json,'$.idempotency_key')=?", (deliverable_ref, key),
                ).fetchone()
                if seen is not None:
                    existing = cur.execute(
                        "SELECT record_json FROM mission_deliverable_versions WHERE version_id=?",
                        (seen["version_id"],),
                    ).fetchone()
                    return {**json.loads(existing["record_json"]), "status": "duplicate"}
                record["idempotency_key"] = key
                record["content_hash"] = content_hash({k: v for k, v in record.items() if k != "content_hash"})
            cur.execute(
                "INSERT INTO mission_deliverable_versions(version_id,deliverable_ref,version_number,"
                "prior_version_ref,mission_version_ref,mission_version_hash,playbook_version_ref,"
                "playbook_version_hash,kind,subject_ref,record_json,content_hash,actor_ref,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (record["id"], deliverable_ref, version, prior, mission["id"], mission["content_hash"],
                 playbook["id"], playbook["content_hash"], kind, subject_ref,
                 json.dumps(record, ensure_ascii=False, sort_keys=True), record["content_hash"],
                 actor_ref, record["created_at"]),
            )
            if pointer is None:
                cur.execute(
                    "INSERT INTO mission_deliverable_pointer(deliverable_ref,version_id,version_number,"
                    "content_hash,updated_at) VALUES(?,?,?,?,?)",
                    (deliverable_ref, record["id"], version, record["content_hash"], record["created_at"]),
                )
            else:
                cur.execute(
                    "UPDATE mission_deliverable_pointer SET version_id=?, version_number=?, "
                    "content_hash=?, updated_at=? WHERE deliverable_ref=?",
                    (record["id"], version, record["content_hash"], record["created_at"], deliverable_ref),
                )
        return {**record, "status": "fresh"}


__all__ = [
    "DELIVERABLE_KINDS",
    "GAP_MARKER",
    "MissionDeliverableAuthority",
    "MissionDeliverableConflict",
    "MissionDeliverableError",
    "MissionDeliverableNotFound",
    "MissionDeliverableValidationError",
    "WRITE_SCOPE",
    "unsourced_numbers",
    "validate_section",
    "value_tokens",
]
