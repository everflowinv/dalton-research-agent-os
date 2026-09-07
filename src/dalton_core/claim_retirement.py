"""P10b: a wrong Claim is challenged and retired, never edited (vision v1.1).

ADR-0005 let the mission admit qualitative Claims by policy, and live it did:
229 Claims, 224 of them qualitative.  Reading them showed two kinds of wrong
that no provenance check can catch, because both are perfectly provenanced:

- **The wrong company.** Fifty statements about LED lighting, EV charging and
  roadway products were admitted under EPAM's subject ref.  The AlphaEngine
  search returned another company's documents; every quote is exact, every
  hash verifies, and the subject is still wrong.
- **Disclaimers.** "Past performance is not indicative of future results",
  attributed, quoted, asserting nothing.

The Ledger is append-only and the ClaimVersion contract is frozen, so nothing
here edits or deletes a Claim.  Two append-only records carry the correction:

- a **challenge** names the exact claim version, the deterministic detector
  that fired and what it checked;
- a **retirement** records that the challenge stands.

Read paths (the cockpit's answers, the source-base checklist, and the
deliverables of P10c) skip retired Claims.  Every historical hash still
verifies, and the record of what the system once believed stays readable.

Both detectors are deterministic and re-runnable, and the authority re-runs
the detector at retirement time rather than trusting the caller:

- ``subject_absent_from_source``: the subject's ticker and name never appear
  in the exact original the Claim cites.  Not a judgment about the statement;
  a fact about the bytes that were read.
- ``boilerplate_disclaimer``: the shared boilerplate filter the drafting path
  already applies, applied retroactively to Claims admitted before it existed.

A human may challenge and retire anything.  Automation may only act on a
deterministic detector, and only when the mission grants ``claim_challenge``.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .document_extraction import statement_is_boilerplate
from .store import DaltonStore, content_hash

SCHEMA_VERSION = "0.1"
_SCHEMA_PATH = Path(__file__).with_name("claim_retirement_schema.sql")

REASON_CODES: tuple[str, ...] = (
    "subject_absent_from_source",
    "boilerplate_disclaimer",
    "human_judgment",
)
DETERMINISTIC_REASONS = frozenset({"subject_absent_from_source", "boilerplate_disclaimer"})
SUBJECT_DETECTOR_REF = "claim-detector:subject-absent-from-source:v1"
BOILERPLATE_DETECTOR_REF = "claim-detector:boilerplate-disclaimer:v1"
DETECTOR_REFS = {
    "subject_absent_from_source": SUBJECT_DETECTOR_REF,
    "boilerplate_disclaimer": BOILERPLATE_DETECTOR_REF,
}
REASON_LABELS = {
    "subject_absent_from_source": "引用的原文里从头到尾没有出现这家公司",
    "boilerplate_disclaimer": "这是免责声明或套话，不是研究结论",
    "human_judgment": "你的判断",
}
_HUMAN_RE = re.compile(r"^human:[A-Za-z0-9._:-]{1,128}$")
_AUTOMATION_RE = re.compile(r"^automation:[A-Za-z0-9._:-]{1,128}$")


class ClaimRetirementError(RuntimeError):
    """Base error for the challenge authority."""


class ClaimRetirementValidationError(ClaimRetirementError):
    """A closed field or argument is invalid."""


class ClaimRetirementConflict(ClaimRetirementError):
    """An append-only record was reused with different semantics, or a gate refused."""


class ClaimRetirementNotFound(ClaimRetirementError):
    """The named record does not exist."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any, name: str, *, maximum: int = 4000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ClaimRetirementValidationError(f"{name} must be text of 1..{maximum} characters")
    return value.strip()


def _sha256(value: Any, name: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ClaimRetirementValidationError(f"{name} must be a SHA-256 hex digest")
    return value


def _actor(value: Any) -> str:
    actor = _text(value, "actor_ref", maximum=256)
    if not (_HUMAN_RE.fullmatch(actor) or _AUTOMATION_RE.fullmatch(actor)):
        raise ClaimRetirementValidationError("actor_ref must be a human: or automation: principal")
    return actor


# -- detectors ---------------------------------------------------------------


def subject_needles(subject: Mapping[str, Any]) -> list[str]:
    """The strings whose absence proves the original is not about this company."""

    needles = []
    for key in ("ticker", "name"):
        value = subject.get(key)
        if isinstance(value, str) and len(value.strip()) >= 2:
            needles.append(value.strip().lower())
    return needles


def subject_absent_from_source(text: str, needles: Sequence[str]) -> bool:
    """True when none of the subject's names appears anywhere in the original.

    Deliberately blunt: one mention anywhere clears the Claim.  A document that
    never names the company it was filed under is not about that company, and
    that is a fact about the bytes, not a judgment about the statement.
    """

    if not needles:
        return False
    lowered = text.lower()
    return not any(needle in lowered for needle in needles)


def detect(
    *,
    statement: str,
    source_text: str | None,
    needles: Sequence[str],
) -> tuple[str, str] | None:
    """The first deterministic reason this Claim should be retired, or None."""

    if statement_is_boilerplate(statement):
        return ("boilerplate_disclaimer", "这条陈述命中了免责声明/套话过滤器，没有断言任何研究观点。")
    if source_text is not None and subject_absent_from_source(source_text, needles):
        names = "、".join(needles)
        return (
            "subject_absent_from_source",
            f"引用的原文全文（{len(source_text):,} 字）里没有出现 {names} 中的任何一个，"
            "这份原文不是关于这家公司的。",
        )
    return None


class ClaimRetirementAuthority:
    """Append-only challenges and retirements over an untouched Ledger."""

    def __init__(
        self,
        store: DaltonStore,
        *,
        source_text_resolver: Callable[[str], str | None] | None = None,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.store = store
        self.connection = store.connection
        self.source_text_resolver = source_text_resolver
        self.clock = clock or _now
        self._authorized = False
        self.connection.create_function(
            "dalton_claim_retirement_authorized", 0, lambda: int(self._authorized)
        )
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        if self._authorized:
            raise RuntimeError("ClaimRetirementAuthority operation cannot be nested")
        self._authorized = True
        try:
            with self.store._transaction() as cur:
                yield cur
        finally:
            self._authorized = False

    # -- reads ---------------------------------------------------------------

    def retired_claim_version_refs(self) -> set[str]:
        """Claim versions a decision has retired; read paths skip exactly these."""

        return {
            row["claim_version_ref"]
            for row in self.connection.execute(
                "SELECT claim_version_ref FROM claim_retirement_decisions WHERE decision='retired'"
            ).fetchall()
        }

    def challenges(self, *, open_only: bool = False, limit: int = 200) -> list[dict[str, Any]]:
        query = (
            "SELECT c.record_json AS record_json, c.content_hash AS content_hash, "
            "d.decision AS decision FROM claim_retirement_challenges c "
            "LEFT JOIN claim_retirement_decisions d ON d.challenge_ref=c.challenge_id"
        )
        if open_only:
            query += " WHERE d.decision_id IS NULL"
        query += " ORDER BY c.created_at, c.challenge_id LIMIT ?"
        import json

        return [
            {
                **json.loads(row["record_json"]),
                "content_hash": row["content_hash"],
                "decision": row["decision"],
            }
            for row in self.connection.execute(query, (int(limit),)).fetchall()
        ]

    def challenge_record(self, challenge_id: str) -> dict[str, Any]:
        import json

        row = self.connection.execute(
            "SELECT record_json, content_hash FROM claim_retirement_challenges WHERE challenge_id=?",
            (_text(challenge_id, "challenge_id", maximum=512),),
        ).fetchone()
        if row is None:
            raise ClaimRetirementNotFound("claim challenge was not found")
        record = json.loads(row["record_json"])
        if record["content_hash"] != row["content_hash"]:
            raise ClaimRetirementConflict("claim challenge authority drifted")
        return record

    def _claim(self, claim_version_ref: str) -> dict[str, Any]:
        import json

        row = self.connection.execute(
            "SELECT claim_json, content_hash FROM claim_versions WHERE claim_version_id=?",
            (claim_version_ref,),
        ).fetchone()
        if row is None:
            raise ClaimRetirementNotFound("claim version was not found")
        claim = json.loads(row["claim_json"])
        if claim.get("content_hash") != row["content_hash"]:
            raise ClaimRetirementConflict("claim version authority drifted")
        return claim

    # -- writes --------------------------------------------------------------

    def challenge(
        self,
        *,
        claim_version_ref: str,
        claim_version_hash: str,
        reason_code: str,
        rationale: str,
        actor_ref: str,
        detector_ref: str | None = None,
    ) -> dict[str, Any]:
        """Record that a Claim looks wrong.  Nothing is retired by this alone."""

        import json

        claim_version_ref = _text(claim_version_ref, "claim_version_ref", maximum=512)
        claim_version_hash = _sha256(claim_version_hash, "claim_version_hash")
        rationale = _text(rationale, "rationale")
        actor = _actor(actor_ref)
        if reason_code not in REASON_CODES:
            raise ClaimRetirementValidationError(f"reason_code must be one of {list(REASON_CODES)}")
        if reason_code in DETERMINISTIC_REASONS:
            expected = DETECTOR_REFS[reason_code]
            if detector_ref is not None and detector_ref != expected:
                raise ClaimRetirementValidationError("detector_ref does not match the reason code")
            detector_ref = expected
        elif _AUTOMATION_RE.fullmatch(actor):
            raise ClaimRetirementConflict("automation cannot raise a human judgment challenge")
        claim = self._claim(claim_version_ref)
        if claim["content_hash"] != claim_version_hash:
            raise ClaimRetirementConflict("claim version hash binding failed")
        record = {
            "schema_version": SCHEMA_VERSION,
            "claim_version_ref": claim_version_ref,
            "claim_version_hash": claim_version_hash,
            "claim_ref": claim["claim_ref"],
            "subject_ref": claim["subject_ref"],
            "reason_code": reason_code,
            "detector_ref": detector_ref,
            "rationale": rationale,
            "actor_ref": actor,
            "created_at": self.clock(),
        }
        record["id"] = "claim-retirement-challenge:" + content_hash(
            {"claim": claim_version_ref, "reason": reason_code}
        )[:32]
        record["content_hash"] = content_hash({k: v for k, v in record.items() if k != "content_hash"})
        with self._transaction() as cur:
            existing = cur.execute(
                "SELECT record_json, content_hash FROM claim_retirement_challenges "
                "WHERE claim_version_ref=? AND reason_code=?",
                (claim_version_ref, reason_code),
            ).fetchone()
            if existing is not None:
                return {**json.loads(existing["record_json"]), "status": "duplicate"}
            cur.execute(
                "INSERT INTO claim_retirement_challenges(challenge_id,claim_version_ref,claim_version_hash,claim_ref,"
                "subject_ref,reason_code,detector_ref,detector_hash,rationale,actor_ref,record_json,"
                "content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (record["id"], claim_version_ref, claim_version_hash, record["claim_ref"],
                 record["subject_ref"], reason_code, detector_ref,
                 None if detector_ref is None else content_hash({"detector": detector_ref}),
                 rationale, actor, json.dumps(record, ensure_ascii=False, sort_keys=True),
                 record["content_hash"], record["created_at"]),
            )
        return {**record, "status": "fresh"}

    def decide(
        self,
        *,
        challenge_ref: str,
        challenge_hash: str,
        decision: str,
        actor_ref: str,
        rationale: str,
        subject_needles: Sequence[str] = (),
        source_text: str | None = None,
    ) -> dict[str, Any]:
        """Retire or keep a challenged Claim.

        A deterministic detector is re-run here rather than trusted: a caller
        saying the check fired is not evidence that it fired.
        """

        import json

        challenge_ref = _text(challenge_ref, "challenge_ref", maximum=512)
        challenge_hash = _sha256(challenge_hash, "challenge_hash")
        rationale = _text(rationale, "rationale")
        actor = _actor(actor_ref)
        record = self.challenge_record(challenge_ref)
        if record["content_hash"] != challenge_hash:
            raise ClaimRetirementConflict("challenge hash binding failed")
        claim = self._claim(record["claim_version_ref"])
        if claim["content_hash"] != record["claim_version_hash"]:
            raise ClaimRetirementConflict("claim version changed since the challenge")
        if decision not in ("retired", "kept"):
            raise ClaimRetirementValidationError("decision must be retired or kept")
        if _AUTOMATION_RE.fullmatch(actor) and decision == "kept":
            raise ClaimRetirementConflict("only a person decides that a challenged Claim stands")
        if _AUTOMATION_RE.fullmatch(actor):
            if record["reason_code"] not in DETERMINISTIC_REASONS:
                raise ClaimRetirementConflict(
                    "automation may only retire on a deterministic detector"
                )
            # Re-run the detector against the exact original, here.  A caller
            # that says the check fired is not evidence that it fired.
            if record["reason_code"] == "boilerplate_disclaimer":  # noqa: SIM108
                if not statement_is_boilerplate(claim["normalized_statement"]):
                    raise ClaimRetirementConflict("detector no longer fires for this Claim")
            else:
                if source_text is None and self.source_text_resolver is not None:
                    source_text = self.source_text_resolver(record["claim_version_ref"])
                if source_text is None:
                    raise ClaimRetirementConflict(
                        "the original cannot be read; a Claim is never retired unverified"
                    )
                if not subject_absent_from_source(source_text, list(subject_needles)):
                    raise ClaimRetirementConflict("detector no longer fires for this Claim")
        wire = {
            "schema_version": SCHEMA_VERSION,
            "challenge_ref": challenge_ref,
            "challenge_hash": challenge_hash,
            "claim_version_ref": record["claim_version_ref"],
            "claim_ref": record["claim_ref"],
            "reason_code": record["reason_code"],
            "decision": decision,
            "actor_ref": actor,
            "rationale": rationale,
            "created_at": self.clock(),
        }
        wire["id"] = "claim-retirement-decision:" + content_hash(
            {"claim": record["claim_version_ref"]}
        )[:32]
        wire["content_hash"] = content_hash({k: v for k, v in wire.items() if k != "content_hash"})
        with self._transaction() as cur:
            existing = cur.execute(
                "SELECT record_json FROM claim_retirement_decisions WHERE claim_version_ref=?",
                (record["claim_version_ref"],),
            ).fetchone()
            if existing is not None:
                return {**json.loads(existing["record_json"]), "status": "duplicate"}
            cur.execute(
                "INSERT INTO claim_retirement_decisions(decision_id,claim_version_ref,challenge_ref,"
                "challenge_hash,decision,actor_ref,rationale,record_json,content_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (wire["id"], record["claim_version_ref"], challenge_ref, challenge_hash, decision,
                 actor, rationale, json.dumps(wire, ensure_ascii=False, sort_keys=True),
                 wire["content_hash"], wire["created_at"]),
            )
        return {**wire, "status": "fresh"}


__all__ = [
    "BOILERPLATE_DETECTOR_REF",
    "ClaimRetirementAuthority",
    "ClaimRetirementConflict",
    "ClaimRetirementError",
    "ClaimRetirementNotFound",
    "ClaimRetirementValidationError",
    "DETERMINISTIC_REASONS",
    "REASON_CODES",
    "REASON_LABELS",
    "SUBJECT_DETECTOR_REF",
    "detect",
    "subject_absent_from_source",
    "subject_needles",
]
