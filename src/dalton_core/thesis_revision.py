"""P14b: the human end of a ThesisRevisionCandidate.

ADR-0007 opened one sentence the system could not previously form -- *and
therefore the thesis that Claim supported may be weaker than we said* -- and
stopped there deliberately: automation may say it, and only a person may act
on it.  P14a built the saying half.  This module is the acting half, and it is
almost entirely refusals.

Three things happen here and nothing else.

**A reader.**  ``ThesisRevisionAuthority.undecided()`` returns the candidates
nobody has closed yet, each with the ``ThesisReflection`` P14a attached to it,
because the reflection is the half that says what we may have *missed* and a
person deciding without it is deciding on the proposal alone.

**A decision.**  ``decide()`` records one of three words -- ``accept``,
``reject``, ``defer`` -- against the candidate's content hash, append-only,
from a ``human:`` actor and no other.  ``defer`` is not terminal: it is the
honest answer to "one more quarter first", and the candidate comes back on the
next read.  ``accept`` and ``reject`` are.

**A version, on accept only.**  The new thesis goes through the same tables
ADR-0001 built for the admission -- an immutable candidate row, an immutable
decision row, an immutable ``ThesisVersion`` chained by ``prior_version_id``,
and the current pointer moved to it.  The old version is not touched, because
the chain is the only place a change of mind is legible.  The version's
``change_reason`` is built from the candidate's decision word and the exact
evidence refs that occasioned it, so replaying the chain answers *why* and not
only *what*.

What this module will not do, in every case by refusing rather than by not
having got round to it:

* an ``automation:`` actor is refused, twice -- once by the writer, which only
  exposes the operation as human governance, and once here;
* a ``NEW_THESIS`` candidate is refused on accept and told where to go: ADR-0007
  says a new thesis is a coverage admission, not a revision, and the admission
  path has bindings (mandate, driver pack, template) that a revision does not
  re-derive;
* a candidate whose thesis has no current pointer is refused for the same
  reason -- there is no chain to extend;
* a second terminal decision on a closed candidate is refused; a repeat of the
  identical decision is a ``duplicate`` and writes nothing.

Nothing here proposes anything.  The candidate came from the judgement lane and
this module never creates one.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import ThesisVersion
from .coverage_admission import (
    CONFIDENCE_LEVELS,
    THESIS_CONTENT_FIELDS,
    THESIS_SCHEMA_VERSION,
    validate_thesis_content,
)
from .research_playbook import DECISION_VOCABULARY
from .store import canonical_json, content_hash

SCHEMA_VERSION = "0.1"
_SCHEMA_PATH = Path(__file__).with_name("thesis_revision_schema.sql")

CHECKPOINT_KIND = "thesis_revision_candidate"
VERDICTS: tuple[str, ...] = ("accept", "reject", "defer")
TERMINAL_VERDICTS: frozenset[str] = frozenset({"accept", "reject"})

# ADR-0007, third paragraph: "A ``NEW_THESIS`` candidate is a coverage
# admission, not a revision."  Named here so the refusal can quote it.
ADMISSION_ONLY_DECISION = "NEW_THESIS"

MAX_REASON_CHARS = 2000
MAX_CHANGE_REASON_CHARS = 900
MAX_CHANGE_REASON_REFS = 8

VERDICT_LABELS: Mapping[str, str] = {
    "accept": "接受，出新版本",
    "reject": "不接受",
    "defer": "先放着，再看看",
}


class ThesisRevisionError(RuntimeError):
    """Base error for the candidate decision loop."""


class ThesisRevisionValidationError(ThesisRevisionError, ValueError):
    """A request does not satisfy the closed contract."""


class ThesisRevisionConflict(ThesisRevisionError):
    """A request conflicts with a decision that was already recorded."""


class ThesisRevisionNotFound(ThesisRevisionError):
    """The candidate named by the request is not in the ledger."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any, name: str, *, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ThesisRevisionValidationError(f"{name} must be a non-empty string")
    text = value.strip()
    if len(text) > maximum:
        raise ThesisRevisionValidationError(f"{name} is longer than {maximum} characters")
    return text


def _human(value: Any, name: str) -> str:
    actor = _text(value, name)
    if not actor.startswith("human:"):
        raise ThesisRevisionValidationError(
            "only a person decides a thesis revision candidate (ADR-0007)"
        )
    return actor


def _sha256(value: Any, name: str) -> str:
    digest = _text(value, name)
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ThesisRevisionValidationError(f"{name} must be a sha256 hex digest")
    return digest


def _decode(row: sqlite3.Row, name: str) -> dict[str, Any]:
    record = json.loads(row["record_json"])
    if record.get("content_hash") != row["content_hash"]:
        raise ThesisRevisionConflict(f"{name} did not read back as written")
    return record


def revision_change_reason(candidate: Mapping[str, Any]) -> str:
    """Why the new version exists, in one line, from the candidate itself.

    The decision word first, because that is the judgement; then the candidate
    that carried it, so the proposal and its reflection can be found; then the
    evidence refs, because ADR-0008 refuses a version that cannot name what it
    learned.  Deterministic: the same candidate always produces the same
    sentence, so a replay of the chain is a replay and not a re-narration.
    """

    refs = list(dict.fromkeys(candidate.get("evidence_refs") or ()))
    if not refs:
        raise ThesisRevisionValidationError(
            "a candidate with no evidence ref cannot occasion a version (ADR-0008)"
        )
    shown = refs[:MAX_CHANGE_REASON_REFS]
    tail = "" if len(refs) == len(shown) else f"，另 {len(refs) - len(shown)} 条"
    reason = (
        f"{candidate['decision']}｜人裁决的 thesis 修订候选 {candidate['id']}"
        f"｜证据：{'、'.join(shown)}{tail}"
    )
    return reason[:MAX_CHANGE_REASON_CHARS]


def proposed_content(
    *, current_content: Mapping[str, Any], candidate: Mapping[str, Any]
) -> dict[str, Any]:
    """The next version's content, defaulted from the current one.

    Only what the candidate actually proposed moves: the statement when it
    proposed one, the confidence when it proposed one.  ``claim_refs``,
    ``catalyst_refs`` and ``falsifier_refs`` are carried forward unchanged --
    a candidate's ``evidence_refs`` are what *occasioned* the revision, not a
    claim the revised thesis now rests on, and silently promoting one to the
    other would put a ref in the thesis that nobody chose to put there.  A
    person who wants the refs to move passes ``content`` explicitly.
    """

    content = {field: current_content[field] for field in THESIS_CONTENT_FIELDS}
    statement = candidate.get("proposed_statement")
    if statement is not None:
        content["statement"] = _text(statement, "proposed_statement", maximum=4000)
    confidence = candidate.get("proposed_confidence")
    if confidence is not None:
        if confidence not in CONFIDENCE_LEVELS:
            raise ThesisRevisionValidationError(
                "confidence is ADR-0001's ordinal vocabulary, never a float"
            )
        content["confidence"] = confidence
    content["change_reason"] = revision_change_reason(candidate)
    return validate_thesis_content(content)


class ThesisRevisionAuthority:
    """Read undecided candidates; record one human decision on one of them."""

    def __init__(self, store: Any) -> None:
        if not hasattr(store, "connection") or not hasattr(store, "_transaction"):
            raise TypeError("ThesisRevisionAuthority requires a DaltonStore")
        self.store = store
        self.connection: sqlite3.Connection = store.connection
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    # -- reading -----------------------------------------------------------

    def _candidate_row(self, candidate_ref: str) -> sqlite3.Row:
        try:
            row = self.connection.execute(
                "SELECT * FROM thesis_revision_candidates WHERE candidate_id=?",
                (candidate_ref,),
            ).fetchone()
        except sqlite3.OperationalError as exc:  # pragma: no cover - old Core
            raise ThesisRevisionNotFound(
                "this Core has no thesis revision candidates yet"
            ) from exc
        if row is None:
            raise ThesisRevisionNotFound("thesis revision candidate was not found")
        return row

    def candidate(self, candidate_ref: str) -> dict[str, Any]:
        """One candidate, with its reflection and its decision history."""

        row = self._candidate_row(_text(candidate_ref, "candidate_ref"))
        record = _decode(row, "ThesisRevisionCandidate")
        return {
            **record,
            "reflection": self._reflection(record.get("reflection_ref")),
            "decisions": self.decisions(record["id"]),
        }

    def _reflection(self, reflection_ref: Any) -> dict[str, Any] | None:
        if not reflection_ref:
            return None
        try:
            row = self.connection.execute(
                "SELECT * FROM thesis_reflections WHERE reflection_id=?",
                (reflection_ref,),
            ).fetchone()
        except sqlite3.OperationalError:  # pragma: no cover - old Core
            return None
        return None if row is None else _decode(row, "ThesisReflection")

    def decisions(self, candidate_ref: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM thesis_revision_decisions WHERE candidate_ref=? "
            "ORDER BY created_at, decision_id",
            (_text(candidate_ref, "candidate_ref"),),
        ).fetchall()
        return [_decode(row, "ThesisRevisionDecision") for row in rows]

    def closed_candidate_refs(self) -> set[str]:
        return {
            row["candidate_ref"]
            for row in self.connection.execute(
                "SELECT candidate_ref FROM thesis_revision_decisions WHERE terminal=1"
            ).fetchall()
        }

    def undecided(self, company_ref: str | None = None) -> list[dict[str, Any]]:
        """Every candidate nobody has accepted or rejected, oldest first.

        A deferred candidate is here: deferring is a decision about *when*, not
        about *whether*, and a queue that forgets what it was told to look at
        again is a queue that quietly decides by omission.  The last verdict
        rides along so the caller can say "you deferred this on Tuesday".
        """

        query = "SELECT * FROM thesis_revision_candidates"
        params: list[Any] = []
        if company_ref is not None:
            query += " WHERE company_ref=?"
            params.append(_text(company_ref, "company_ref"))
        query += " ORDER BY created_at, candidate_id"
        try:
            rows = self.connection.execute(query, params).fetchall()
        except sqlite3.OperationalError:  # pragma: no cover - old Core
            return []
        closed = self.closed_candidate_refs()
        open_candidates: list[dict[str, Any]] = []
        for row in rows:
            if row["candidate_id"] in closed:
                continue
            record = _decode(row, "ThesisRevisionCandidate")
            history = self.decisions(record["id"])
            open_candidates.append({
                **record,
                "reflection": self._reflection(record.get("reflection_ref")),
                "deferred": bool(history),
                "last_verdict": history[-1]["verdict"] if history else None,
                "last_reason": history[-1]["reason"] if history else None,
            })
        return open_candidates

    # -- the thesis chain --------------------------------------------------

    def current_version(self, thesis_ref: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT v.* FROM current_pointers p JOIN thesis_versions v "
            "ON v.version_id=p.version_id WHERE p.thesis_id=?",
            (_text(thesis_ref, "thesis_ref"),),
        ).fetchone()
        if row is None:
            return None
        return {
            "version_id": row["version_id"],
            "thesis_id": row["thesis_id"],
            "version_number": int(row["version_number"]),
            "content_hash": row["content_hash"],
            "content": json.loads(row["content_json"]),
        }

    def version_chain(self, thesis_ref: str) -> list[dict[str, Any]]:
        """Every version of one thesis, oldest first -- the replay ADR-0008 asks for."""

        rows = self.connection.execute(
            "SELECT * FROM thesis_versions WHERE thesis_id=? ORDER BY version_number",
            (_text(thesis_ref, "thesis_ref"),),
        ).fetchall()
        return [
            {
                "version_id": row["version_id"],
                "version_number": int(row["version_number"]),
                "prior_version_ref": row["prior_version_id"],
                "authority_kind": row["authority_kind"],
                "authority_ref": row["authority_ref"],
                "content_hash": row["content_hash"],
                "content": json.loads(row["content_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    # -- writing -----------------------------------------------------------

    def decide(
        self,
        *,
        candidate_ref: str,
        candidate_hash: str,
        verdict: str,
        reason: str,
        actor_ref: str,
        content: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """One person's word on one candidate, bound to what they read.

        ``candidate_hash`` is the binding: a decision recorded against a
        candidate whose content has moved is a decision about a different
        proposal, and there is no such thing as a hash that drifted honestly.
        """

        candidate_ref = _text(candidate_ref, "candidate_ref")
        candidate_hash = _sha256(candidate_hash, "candidate_hash")
        if verdict not in VERDICTS:
            raise ThesisRevisionValidationError(
                f"verdict must be one of {list(VERDICTS)}"
            )
        reason = _text(reason, "reason", maximum=MAX_REASON_CHARS)
        actor_ref = _human(actor_ref, "actor_ref")

        row = self._candidate_row(candidate_ref)
        candidate = _decode(row, "ThesisRevisionCandidate")
        if candidate["content_hash"] != candidate_hash:
            raise ThesisRevisionConflict("candidate hash binding failed")
        if candidate["decision"] not in DECISION_VOCABULARY:
            raise ThesisRevisionConflict("candidate carries a word outside the vocabulary")

        history = self.decisions(candidate_ref)
        terminal = [item for item in history if item["verdict"] in TERMINAL_VERDICTS]
        # The identity of a decision is what it says, not when it was sent, so
        # the *same* answer resent is a duplicate even after it closed the
        # candidate -- a retried RPC must not look like a person changing
        # their mind.  A *different* answer after a terminal one is the thing
        # that is refused.
        sequence = len(history) - len(terminal) if terminal else len(history)
        request = {
            "candidate_ref": candidate_ref,
            "candidate_hash": candidate_hash,
            "verdict": verdict,
            "reason": reason,
            "actor_ref": actor_ref,
            "sequence": sequence,
        }
        decision_id = "thesis-revision-decision:" + content_hash(request)[:32]
        existing = self.connection.execute(
            "SELECT * FROM thesis_revision_decisions WHERE decision_id=?",
            (decision_id,),
        ).fetchone()
        if existing is not None:
            return {**_decode(existing, "ThesisRevisionDecision"), "status": "duplicate"}
        if terminal:
            raise ThesisRevisionConflict(
                f"this candidate was already {terminal[0]['verdict']}ed"
            )

        resulting: dict[str, Any] | None = None
        if verdict == "accept":
            resulting = self._prepare_acceptance(candidate=candidate, content=content)

        created_at = _now()
        record = {
            "schema_version": SCHEMA_VERSION,
            "id": decision_id,
            "created_at": created_at,
            "candidate_ref": candidate_ref,
            "candidate_hash": candidate_hash,
            "candidate_decision": candidate["decision"],
            "thesis_ref": candidate.get("thesis_ref") or candidate["thesis_version_ref"],
            "thesis_version_ref": candidate["thesis_version_ref"],
            "thesis_version_hash": candidate["thesis_version_hash"],
            "company_ref": candidate["company_ref"],
            "reflection_ref": candidate.get("reflection_ref"),
            "evidence_refs": list(candidate.get("evidence_refs") or ()),
            "verdict": verdict,
            "reason": reason,
            "terminal": verdict in TERMINAL_VERDICTS,
            "sequence": sequence,
            "resulting_thesis_version_ref": (
                None if resulting is None else resulting["thesis_version_id"]
            ),
            "admission_candidate_ref": (
                None if resulting is None else resulting["admission_candidate_id"]
            ),
            "admission_decision_ref": (
                None if resulting is None else resulting["admission_decision_id"]
            ),
            "change_reason": None if resulting is None else resulting["content"]["change_reason"],
            "reviewer_ref": actor_ref,
            "actor_ref": actor_ref,
        }
        record["content_hash"] = content_hash(record)

        with self.store._transaction() as cur:
            if resulting is not None:
                self._write_version(cur, candidate=candidate, resulting=resulting,
                                    decision=record, created_at=created_at)
            cur.execute(
                "INSERT INTO thesis_revision_decisions(decision_id,candidate_ref,"
                "candidate_hash,thesis_ref,thesis_version_ref,company_ref,"
                "candidate_decision,verdict,reason,terminal,resulting_thesis_version_ref,"
                "admission_candidate_ref,admission_decision_ref,record_json,content_hash,"
                "actor_ref,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    decision_id, candidate_ref, candidate_hash, record["thesis_ref"],
                    candidate["thesis_version_ref"], candidate["company_ref"],
                    candidate["decision"], verdict, reason,
                    1 if record["terminal"] else 0,
                    record["resulting_thesis_version_ref"],
                    record["admission_candidate_ref"], record["admission_decision_ref"],
                    canonical_json(record), record["content_hash"], actor_ref, created_at,
                ),
            )
            written = _decode(
                cur.execute(
                    "SELECT * FROM thesis_revision_decisions WHERE decision_id=?",
                    (decision_id,),
                ).fetchone(),
                "ThesisRevisionDecision",
            )
        result: dict[str, Any] = {**written, "status": "fresh"}
        if resulting is not None:
            result["thesis_version"] = resulting["wire"]
        return result

    # -- accept ------------------------------------------------------------

    def _prepare_acceptance(
        self, *, candidate: Mapping[str, Any], content: Mapping[str, Any] | None
    ) -> dict[str, Any]:
        """Everything an accept needs, computed before anything is written."""

        if candidate["decision"] == ADMISSION_ONLY_DECISION:
            raise ThesisRevisionConflict(
                "a NEW_THESIS candidate is a coverage admission, not a revision "
                "(ADR-0007); propose it through propose_thesis_admission"
            )
        thesis_ref = candidate.get("thesis_ref")
        if not thesis_ref:
            raise ThesisRevisionConflict("the candidate does not name a thesis to revise")
        current = self.current_version(thesis_ref)
        if current is None:
            raise ThesisRevisionConflict(
                "this thesis has no admitted version; a first version is an "
                "admission (ADR-0001), not a revision"
            )
        if current["version_id"] != candidate["thesis_version_ref"]:
            raise ThesisRevisionConflict(
                f"this thesis is now at {current['version_id']}, and the candidate "
                f"was written against {candidate['thesis_version_ref']}"
            )
        if current["content_hash"] != candidate["thesis_version_hash"]:
            raise ThesisRevisionConflict("thesis version hash binding failed")

        current_content = {
            field: current["content"][field] for field in THESIS_CONTENT_FIELDS
        }
        if content is None:
            next_content = proposed_content(
                current_content=current_content, candidate=candidate
            )
        else:
            supplied = dict(content)
            supplied["change_reason"] = revision_change_reason(candidate)
            next_content = validate_thesis_content(supplied)
        # Compared without ``change_reason``, because the reason is derived
        # from the candidate and so is *always* different -- comparing with it
        # would make this refusal unreachable, which is the shape of a guard
        # that is there and does nothing.
        if {k: v for k, v in next_content.items() if k != "change_reason"} == {
            k: v for k, v in current_content.items() if k != "change_reason"
        }:
            raise ThesisRevisionConflict(
                "this revision says exactly what the current version says; a "
                "rewrite of an unchanged view is not a version (ADR-0008)"
            )
        self._check_claim_refs(next_content["claim_refs"])
        return {
            "thesis_ref": thesis_ref,
            "current": current,
            "content": next_content,
            "content_hash": content_hash(next_content),
            "thesis_version_id": f"thesis-version:{uuid.uuid4().hex}",
            "admission_candidate_id": "thesis-revision-admission-candidate:" + content_hash({
                "candidate_ref": candidate["id"], "content": next_content,
            })[:32],
            "admission_decision_id": "thesis-revision-admission-decision:" + content_hash({
                "candidate_ref": candidate["id"], "content": next_content,
            })[:32],
            "wire": None,
        }

    def _check_claim_refs(self, claim_refs: Sequence[str]) -> None:
        for claim_ref in claim_refs:
            if not self.connection.execute(
                "SELECT 1 FROM claim_versions WHERE claim_version_id=?", (claim_ref,)
            ).fetchone():
                raise ThesisRevisionNotFound(f"thesis ClaimVersion {claim_ref} was not found")

    def _write_version(
        self,
        cur: Any,
        *,
        candidate: Mapping[str, Any],
        resulting: dict[str, Any],
        decision: Mapping[str, Any],
        created_at: str,
    ) -> None:
        """The append-only half: a candidate row, a decision row, a version, a pointer.

        These are ADR-0001's own tables.  Using them rather than a private one
        is the point: a person reading ``thesis_versions`` sees one chain with
        one authority kind, and the revision is not a second class of thesis
        that some queries know about and others do not.
        """

        thesis_ref = resulting["thesis_ref"]
        current = resulting["current"]
        content = resulting["content"]
        digest = resulting["content_hash"]
        admission_candidate_id = resulting["admission_candidate_id"]
        admission_decision_id = resulting["admission_decision_id"]

        source = cur.execute(
            "SELECT * FROM thesis_admission_candidates WHERE thesis_ref=? "
            "ORDER BY created_at LIMIT 1",
            (thesis_ref,),
        ).fetchone()
        if source is None:
            raise ThesisRevisionConflict(
                "this thesis has no admission candidate to inherit its bindings from"
            )

        candidate_record = {
            "schema_version": SCHEMA_VERSION,
            "id": admission_candidate_id,
            "created_at": created_at,
            "thesis_ref": thesis_ref,
            "company_ref": candidate["company_ref"],
            "industry_ref": source["industry_ref"],
            "template_ref": json.loads(source["record_json"]).get("template_ref"),
            "driver_refs": json.loads(source["record_json"]).get("driver_refs") or [],
            "mandate_version_ref": source["mandate_version_ref"],
            "mandate_version_hash": source["mandate_version_hash"],
            "driver_pack_version_ref": source["driver_pack_version_ref"],
            "driver_pack_version_hash": source["driver_pack_version_hash"],
            "prior_version_ref": current["version_id"],
            "revision_candidate_ref": candidate["id"],
            "revision_candidate_hash": candidate["content_hash"],
            "content": content,
            "thesis_content_hash": digest,
            "proposed_by": decision["actor_ref"],
        }
        candidate_record["content_hash"] = content_hash(candidate_record)
        cur.execute(
            "INSERT INTO thesis_admission_candidates"
            "(candidate_id,thesis_ref,company_ref,industry_ref,mandate_version_ref,"
            "mandate_version_hash,driver_pack_version_ref,driver_pack_version_hash,"
            "content_json,content_hash,record_json,record_hash,proposed_by,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                admission_candidate_id, thesis_ref, candidate["company_ref"],
                source["industry_ref"], source["mandate_version_ref"],
                source["mandate_version_hash"], source["driver_pack_version_ref"],
                source["driver_pack_version_hash"], canonical_json(content), digest,
                canonical_json(candidate_record), candidate_record["content_hash"],
                decision["actor_ref"], created_at,
            ),
        )

        admission_decision = {
            "schema_version": SCHEMA_VERSION,
            "id": admission_decision_id,
            "created_at": created_at,
            "candidate_ref": admission_candidate_id,
            "candidate_hash": candidate_record["content_hash"],
            "verdict": "admit",
            "rationale": decision["reason"],
            "reviewer_ref": decision["actor_ref"],
            "revision_decision_ref": decision["id"],
            "resulting_thesis_version_ref": resulting["thesis_version_id"],
        }
        admission_decision["content_hash"] = content_hash(admission_decision)
        cur.execute(
            "INSERT INTO thesis_admission_decisions"
            "(decision_id,candidate_id,candidate_hash,verdict,rationale,reviewer_ref,"
            "record_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                admission_decision_id, admission_candidate_id,
                candidate_record["content_hash"], "admit", decision["reason"],
                decision["actor_ref"], canonical_json(admission_decision),
                admission_decision["content_hash"], created_at,
            ),
        )

        version_number = current["version_number"] + 1
        wire = ThesisVersion.from_dict({
            "schema_version": THESIS_SCHEMA_VERSION,
            "id": resulting["thesis_version_id"],
            "created_at": created_at,
            "thesis_ref": thesis_ref,
            "version": version_number,
            **content,
            "prior_version_ref": current["version_id"],
            "authority_kind": "human_admission",
            "authority_ref": admission_decision_id,
            "committed_by_ref": decision["actor_ref"],
            "content_hash": digest,
        }).to_dict()
        cur.execute(
            "INSERT INTO thesis_versions"
            "(version_id,thesis_id,version_number,content_json,content_hash,"
            "prior_version_id,change_id,verification_id,admission_decision_id,"
            "authority_kind,authority_ref,committed_by,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                resulting["thesis_version_id"], thesis_ref, version_number,
                canonical_json(wire), digest, current["version_id"], None, None,
                admission_decision_id, "human_admission", admission_decision_id,
                decision["actor_ref"], created_at,
            ),
        )
        cur.execute(
            "UPDATE current_pointers SET version_id=?,version_number=?,content_hash=?,"
            "updated_at=? WHERE thesis_id=?",
            (
                resulting["thesis_version_id"], version_number, digest, created_at,
                thesis_ref,
            ),
        )
        resulting["wire"] = wire


__all__ = [
    "ADMISSION_ONLY_DECISION",
    "CHECKPOINT_KIND",
    "SCHEMA_VERSION",
    "TERMINAL_VERDICTS",
    "ThesisRevisionAuthority",
    "ThesisRevisionConflict",
    "ThesisRevisionError",
    "ThesisRevisionNotFound",
    "ThesisRevisionValidationError",
    "VERDICTS",
    "VERDICT_LABELS",
    "proposed_content",
    "revision_change_reason",
]
