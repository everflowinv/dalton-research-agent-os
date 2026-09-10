"""P14d: re-open a passed gate, versioned, and never by itself.

Four companies have ``gate_passed`` on their Initial Screen and the selection
rule skips a company whose gate has passed, so ACN, CTSH, IBM and DXC would
never have their screen rewritten however much evidence arrived afterwards --
and a great deal has.  ADR-0008 named that an accident and said the fix is a
version, not a mutation: the old version and its ``gate_passed`` stage record
stay exactly where they are, and a re-issue is version N+1 with a
``prior_version_ref``, a ``change_reason`` and the refs that occasioned it.

The reopen *criterion* is the one the plan wrote down (row D5): re-run the exit
gate's structural self-assessment with current evidence and propose when an
item flips 缺 → 有.  Running that literally turns out to say nothing, and the
reason is worth stating because it shaped this module.

``initial_screen.assess_exit_gate`` asks four questions.  Two are about the
document that was written (is the thesis section long enough, are thesis / risk
/ relevance all present) and cannot change while the document does not.  One is
hardcoded ``True`` -- the publish path already refused any figure without a
Claim.  The fourth, ``source_base``, is about the mission's four required
readings, and it had to be 有 for the gate to pass at all.  So on a *passed*
version all four answers are 有 by construction, and nothing can flip.  The
four questions are a publish gate, not a reopen trigger.

What can flip is the evidence base underneath them, which is what the owner
actually named: how many filed statement lines we now hold, how many verified
figures, whether market data exists, whether consensus exists, how many live
Claims there are per section of the document.  Those are facts about the
Ledger, they are all countable, and each one is countable *as of a date* --
which is what makes the diff honest.  The baseline is not a stored assessment
(none was ever stored; the gate result survives only as one line of prose in
the stage record).  The baseline is the same computation run with the clock set
back to the moment the passed version was published.  So the assessment
answers a question with one meaning: **what do we know now that we did not know
when we wrote this?**

The gate's four questions are still recomputed and still reported, because a
recomputation that has *regressed* -- the source base lost a reading, a
retirement emptied the thesis section -- is something a person should see even
though it is not a reason to re-issue.  It is reported and it never triggers.

Everything here proposes.  ``gate_reopen`` is a human checkpoint by
construction (ADR-0008), the decision is append-only and bound to the
proposal's content hash, and an approved proposal is consumed exactly once --
by the version that cites it.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .coverage_mission import STAGE_REOPENED, fold_stage_status
from .store import canonical_json, content_hash

SCHEMA_VERSION = "0.1"
_SCHEMA_PATH = Path(__file__).with_name("deliverable_reopen_schema.sql")

CHECKPOINT_KIND = "gate_reopen"
STAGE_REF = "initial_screen"
DELIVERABLE_KIND = "initial_screen"

# ADR-0008's closed vocabulary, as it applies here.  A reopen driven by the
# evidence base is ``evidence_thicker``; a reopen the owner asked for without
# the assessment flipping anything is ``human_revision``.
CHANGE_REASON_EVIDENCE = "evidence_thicker"
CHANGE_REASON_HUMAN = "human_revision"
REOPEN_CHANGE_REASONS: tuple[str, ...] = (CHANGE_REASON_EVIDENCE, CHANGE_REASON_HUMAN)

VERDICTS: tuple[str, ...] = ("approve", "decline")
VERDICT_LABELS: Mapping[str, str] = {
    "approve": "重出一版",
    "decline": "不重出",
}

# Every item is a count with a threshold, so "缺 → 有" is a comparison and not
# a judgement, and so an owner who thinks a threshold is wrong can move it in
# one place instead of arguing with a model.
ITEM_LABELS: Mapping[str, str] = {
    "statements": "已入库的财报报表行",
    "verified_figures": "逐条核对过的文档数字",
    "market_data": "价格序列",
    "consensus": "市场一致预期",
    "claims_per_section": "每节可引用的结论条数",
}
REOPEN_ITEMS: tuple[str, ...] = tuple(ITEM_LABELS)

DEFAULT_POLICY: Mapping[str, Any] = {
    "schema_version": SCHEMA_VERSION,
    "policy_ref": "deliverable-reopen-policy:initial-screen:1",
    # One filed statement is a different document from none; the screens were
    # written before the statement lane existed at all.
    "min_statement_lines": 100,
    # A figure that was read out of a filing and checked against its own quote.
    "min_verified_figures": 1,
    # P11a. Its absence is why every passed screen's valuation section is a gap.
    "min_market_price_versions": 1,
    # P11b, Wave 2. Absent today, and the item says so rather than pretending.
    "min_consensus_versions": 1,
    # The gate wants three cited Claims in the thesis section; a document whose
    # every section could carry that many is a document worth redrafting.
    "min_claims_per_section": 3,
    "max_evidence_refs": 12,
}

MAX_REASON_CHARS = 2000


class DeliverableReopenError(RuntimeError):
    """Base error for the gate reopen loop."""


class DeliverableReopenValidationError(DeliverableReopenError, ValueError):
    """A request does not satisfy the closed contract."""


class DeliverableReopenConflict(DeliverableReopenError):
    """A request conflicts with what the append-only ledger already says."""


class DeliverableReopenNotFound(DeliverableReopenError):
    """The proposal or the passed version named by the request is not there."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any, name: str, *, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DeliverableReopenValidationError(f"{name} must be a non-empty string")
    text = value.strip()
    if len(text) > maximum:
        raise DeliverableReopenValidationError(f"{name} is longer than {maximum} characters")
    return text


def _human(value: Any, name: str) -> str:
    actor = _text(value, name)
    if not actor.startswith("human:"):
        raise DeliverableReopenValidationError(
            "reopening a passed gate is a human checkpoint (ADR-0008)"
        )
    return actor


def _sha256(value: Any, name: str) -> str:
    digest = _text(value, name)
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise DeliverableReopenValidationError(f"{name} must be a sha256 hex digest")
    return digest


def _decode(row: sqlite3.Row, name: str) -> dict[str, Any]:
    record = json.loads(row["record_json"])
    if record.get("content_hash") != row["content_hash"]:
        raise DeliverableReopenConflict(f"{name} did not read back as written")
    return record


def load_policy(path: str | Path | None) -> dict[str, Any]:
    """Thresholds from a JSON file, or the frozen defaults.

    A closed shape: a policy file with a key this module does not know is a
    typo that would otherwise be silently ignored, and the whole value of a
    threshold in a file is that it is the one the run actually used.
    """

    if path is None:
        return dict(DEFAULT_POLICY)
    raw = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise DeliverableReopenValidationError("a reopen policy is a JSON object")
    unknown = sorted(set(raw) - set(DEFAULT_POLICY))
    if unknown:
        raise DeliverableReopenValidationError(
            f"reopen policy has unknown keys: {', '.join(unknown)}"
        )
    policy = dict(DEFAULT_POLICY)
    policy.update(raw)
    for key, value in policy.items():
        if key.startswith(("min_", "max_")) and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            raise DeliverableReopenValidationError(f"{key} must be a non-negative integer")
    return policy


def _has_table(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _count(
    connection: sqlite3.Connection, sql: str, params: Sequence[Any]
) -> tuple[int, list[str]]:
    rows = connection.execute(sql, list(params)).fetchall()
    refs = [row[1] for row in rows if row[1]]
    return (int(rows[0][0]) if rows else 0), refs


# ---------------------------------------------------------------------------
# the assessment
# ---------------------------------------------------------------------------


def evidence_items(
    connection: sqlite3.Connection,
    *,
    company_ref: str,
    section_count: int,
    as_of: str | None = None,
    policy: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """The five countable items, as of a moment, with the refs behind them.

    ``as_of`` is the whole mechanism: pass the passed version's ``created_at``
    and you get the evidence base the screen was written on; pass ``None`` and
    you get today's.  Each item carries the refs it counted so a proposal can
    name what changed rather than asserting that something did.
    """

    settings = dict(policy or DEFAULT_POLICY)
    company_ref = _text(company_ref, "company_ref")
    cutoff = as_of
    limit = int(settings["max_evidence_refs"])
    items: list[dict[str, Any]] = []

    def item(
        item_ref: str, value: int, threshold: int, refs: Sequence[str], *, note: str = ""
    ) -> None:
        present = value >= threshold and threshold > 0
        items.append({
            "item_ref": item_ref,
            "label": ITEM_LABELS[item_ref],
            "value": value,
            "threshold": threshold,
            "present": present,
            "mark": "有" if present else "缺",
            "refs": list(dict.fromkeys(refs))[:limit],
            "note": note,
        })

    # statements -----------------------------------------------------------
    if _has_table(connection, "coverage_mission_statement_filings"):
        sql = (
            "SELECT COUNT(*), f.ingest_id FROM coverage_mission_statement_lines l "
            "JOIN coverage_mission_statement_filings f ON f.ingest_id=l.ingest_id "
            "WHERE f.company_ref=?"
        )
        params: list[Any] = [company_ref]
        if cutoff is not None:
            sql += " AND f.recorded_at<=?"
            params.append(cutoff)
        sql += " GROUP BY f.ingest_id"
        rows = connection.execute(sql, params).fetchall()
        total = sum(int(row[0]) for row in rows)
        item("statements", total, int(settings["min_statement_lines"]),
             [row[1] for row in rows])
    else:  # pragma: no cover - a Core without the statement lane
        item("statements", 0, int(settings["min_statement_lines"]), [],
             note="这个 Core 还没有报表行权威")

    # verified figures -----------------------------------------------------
    if _has_table(connection, "coverage_mission_document_figures"):
        sql = (
            "SELECT COUNT(*) OVER (), g.figure_id FROM coverage_mission_document_figures g "
            "LEFT JOIN coverage_mission_document_figure_retractions r "
            "ON r.figure_id=g.figure_id WHERE g.company_ref=? AND r.figure_id IS NULL"
        )
        params = [company_ref]
        if cutoff is not None:
            sql += " AND g.created_at<=?"
            params.append(cutoff)
        sql += " ORDER BY g.created_at, g.figure_id"
        value, refs = _count(connection, sql, params)
        item("verified_figures", value, int(settings["min_verified_figures"]), refs)
    else:  # pragma: no cover
        item("verified_figures", 0, int(settings["min_verified_figures"]), [],
             note="这个 Core 还没有文档数字权威")

    # market data ----------------------------------------------------------
    if _has_table(connection, "market_price_series_versions"):
        sql = (
            "SELECT COUNT(*) OVER (), version_id FROM market_price_series_versions "
            "WHERE company_ref=?"
        )
        params = [company_ref]
        if cutoff is not None:
            sql += " AND created_at<=?"
            params.append(cutoff)
        sql += " ORDER BY version_number DESC"
        value, refs = _count(connection, sql, params)
        item("market_data", value, int(settings["min_market_price_versions"]), refs)
    else:
        item("market_data", 0, int(settings["min_market_price_versions"]), [],
             note="P11a 的价格权威不在这个 Core 里")

    # consensus ------------------------------------------------------------
    if _has_table(connection, "consensus_estimate_versions"):
        sql = (
            "SELECT COUNT(*) OVER (), version_id FROM consensus_estimate_versions "
            "WHERE company_ref=?"
        )
        params = [company_ref]
        if cutoff is not None:
            sql += " AND created_at<=?"
            params.append(cutoff)
        sql += " ORDER BY created_at DESC"
        value, refs = _count(connection, sql, params)
        item("consensus", value, int(settings["min_consensus_versions"]), refs)
    else:
        # Not a zero we measured: an authority that does not exist yet. It
        # cannot flip, and saying so is the honest form of the gap the passed
        # screens already record in their valuation section.
        item("consensus", 0, int(settings["min_consensus_versions"]), [],
             note="P11b（Wave 2）还没落地，这一项现在不可能翻")

    # claims per section ---------------------------------------------------
    retired = _retired_claim_refs(connection)
    rows = connection.execute(
        "SELECT claim_version_id, claim_json, created_at FROM claim_versions "
        "ORDER BY created_at, claim_version_id"
    ).fetchall()
    live: list[str] = []
    for row in rows:
        if row["claim_version_id"] in retired:
            continue
        if cutoff is not None and row["created_at"] > cutoff:
            continue
        claim = json.loads(row["claim_json"])
        if (claim.get("subject_ref") or "") == company_ref:
            live.append(row["claim_version_id"])
    sections = max(1, int(section_count))
    per_section = len(live) // sections
    item("claims_per_section", per_section, int(settings["min_claims_per_section"]),
         list(reversed(live)),
         note=f"{len(live)} 条活着的结论 ÷ {sections} 节")
    return items


def _retired_claim_refs(connection: sqlite3.Connection) -> set[str]:
    if not _has_table(connection, "claim_retirement_decisions"):
        return set()
    try:
        rows = connection.execute(
            "SELECT c.claim_version_ref FROM claim_retirement_decisions d "
            "JOIN claim_retirement_challenges c ON c.challenge_id=d.challenge_ref "
            "WHERE d.verdict='retire'"
        ).fetchall()
    except sqlite3.OperationalError:  # pragma: no cover - older shape
        return set()
    return {row[0] for row in rows}


def folded_stage_history(
    connection: sqlite3.Connection, *, company_ref: str, stage_ref: str = STAGE_REF
) -> list[str]:
    """One company's ordered stage statuses, reopen markers included.

    ``coverage_mission._folded_rows`` is the authority's own version of this
    and is what the ladder validates against; this is the read-only twin for
    the callers that hold a bare connection -- the weekly lane, its CLI, and
    ``passed_version`` -- so all four answer "is this gate open right now" the
    same way. A Core without the marker table (an older state directory) reads
    exactly as it used to.
    """

    if not _has_table(connection, "coverage_mission_stage_records"):
        return []
    rows = [
        (row["created_at"], row["record_id"], row["status"])
        for row in connection.execute(
            "SELECT record_id, created_at, status FROM coverage_mission_stage_records "
            "WHERE company_ref=? AND stage_ref=?",
            (company_ref, stage_ref),
        ).fetchall()
    ]
    if _has_table(connection, "coverage_mission_stage_reopens"):
        rows += [
            (row["created_at"], row["record_id"], STAGE_REOPENED)
            for row in connection.execute(
                "SELECT record_id, created_at FROM coverage_mission_stage_reopens "
                "WHERE company_ref=? AND stage_ref=?",
                (company_ref, stage_ref),
            ).fetchall()
        ]
    return [status for _at, _id, status in sorted(rows)]


def passed_version(
    connection: sqlite3.Connection, *, company_ref: str, stage_ref: str = STAGE_REF
) -> dict[str, Any] | None:
    """The deliverable version whose publication the gate passed against.

    Read from the stage record's own evidence refs rather than from "the newest
    version", because that is the binding the gate was recorded with, and a
    later version (there is one for ACN) would silently move what the diff is
    measured from.

    P14-S: the whole ladder is read, not only the ``gate_passed`` rows, so the
    fold decides.  A company whose gate was passed and then reopened has no
    passed version to diff against, and answering with the superseded one
    would let the weekly lane propose reopening a gate that is already open.
    Across every mission version, because that is where the pass lives after a
    publish.  Since the P14d sequel that reopen is usually the marker rather
    than a bare ``gate_failed``, so the history is read through
    ``folded_stage_history``.
    """

    company_ref = _text(company_ref, "company_ref")
    if not _has_table(connection, "coverage_mission_stage_records"):
        return None
    if fold_stage_status(
        folded_stage_history(connection, company_ref=company_ref, stage_ref=stage_ref)
    ) != "gate_passed":
        return None
    ladder = connection.execute(
        "SELECT * FROM coverage_mission_stage_records WHERE company_ref=? AND stage_ref=? "
        "ORDER BY created_at,record_id",
        (company_ref, stage_ref),
    ).fetchall()
    rows = [row for row in ladder if row["status"] == "gate_passed"]
    row = rows[-1]
    record = json.loads(row["record_json"])
    for ref in record.get("evidence_refs") or ():
        version = connection.execute(
            "SELECT * FROM mission_deliverable_versions WHERE version_id=?", (ref,)
        ).fetchone()
        if version is None or version["kind"] != DELIVERABLE_KIND:
            continue
        return {
            "stage_record_ref": row["record_id"],
            "stage_record_hash": row["content_hash"],
            "passed_at": row["created_at"],
            "version_id": version["version_id"],
            "deliverable_ref": version["deliverable_ref"],
            "version_number": int(version["version_number"]),
            "content_hash": version["content_hash"],
            "created_at": version["created_at"],
            "mission_version_ref": version["mission_version_ref"],
            "mission_version_hash": version["mission_version_hash"],
            "record": json.loads(version["record_json"]),
        }
    return None


def reopen_assessment(
    connection: sqlite3.Connection,
    *,
    company_ref: str,
    policy: Mapping[str, Any] | None = None,
    gate: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Diff the evidence base now against the evidence base then.

    Returns a status rather than raising when there is nothing to diff: a
    company that never passed is not an error, it is the ordinary case, and a
    weekly lane that raised on it would stop on the first company it met.
    """

    company_ref = _text(company_ref, "company_ref")
    settings = dict(policy or DEFAULT_POLICY)
    passed = passed_version(connection, company_ref=company_ref)
    if passed is None:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "not_passed",
            "company_ref": company_ref,
            "reason": "这家公司还没有任何一版 Initial Screen 过闸",
        }
    section_count = max(1, len(passed["record"].get("sections") or ()))
    baseline = evidence_items(
        connection, company_ref=company_ref, section_count=section_count,
        as_of=passed["created_at"], policy=settings,
    )
    current = evidence_items(
        connection, company_ref=company_ref, section_count=section_count,
        as_of=None, policy=settings,
    )
    by_ref = {item["item_ref"]: item for item in baseline}
    diff: list[dict[str, Any]] = []
    for item in current:
        was = by_ref[item["item_ref"]]
        diff.append({
            "item_ref": item["item_ref"],
            "label": item["label"],
            "threshold": item["threshold"],
            "was": {"value": was["value"], "present": was["present"], "mark": was["mark"]},
            "now": {"value": item["value"], "present": item["present"], "mark": item["mark"]},
            "flipped": (not was["present"]) and item["present"],
            "regressed": was["present"] and not item["present"],
            "refs": item["refs"],
            "note": item["note"],
        })
    flipped = [entry["item_ref"] for entry in diff if entry["flipped"]]
    evidence_refs: list[str] = []
    for entry in diff:
        if entry["flipped"]:
            evidence_refs.extend(entry["refs"])
    evidence_refs = list(dict.fromkeys(evidence_refs))[: int(settings["max_evidence_refs"])]
    assessment = {
        "schema_version": SCHEMA_VERSION,
        "status": "reopen_proposed" if flipped else "no_flip",
        "company_ref": company_ref,
        "deliverable_ref": passed["deliverable_ref"],
        "stage_ref": STAGE_REF,
        "stage_record_ref": passed["stage_record_ref"],
        "passed_version_ref": passed["version_id"],
        "passed_version_hash": passed["content_hash"],
        "passed_version_number": passed["version_number"],
        "passed_at": passed["passed_at"],
        "published_at": passed["created_at"],
        "section_count": section_count,
        "mission_version_ref": passed["mission_version_ref"],
        "mission_version_hash": passed["mission_version_hash"],
        "policy_ref": settings["policy_ref"],
        "thresholds": {
            key: settings[key] for key in sorted(settings) if key.startswith("min_")
        },
        "diff": diff,
        "flipped": flipped,
        "regressed": [entry["item_ref"] for entry in diff if entry["regressed"]],
        "evidence_refs": evidence_refs,
        # Reported, never a trigger: on a passed version the exit gate's four
        # questions are all 有 by construction, so the only thing a
        # recomputation can show is a regression.
        "gate_recomputed": None if gate is None else dict(gate),
        "change_reason": CHANGE_REASON_EVIDENCE,
    }
    assessment["assessment_hash"] = content_hash({
        "company_ref": company_ref,
        "passed_version_ref": passed["version_id"],
        "policy_ref": settings["policy_ref"],
        "diff": [
            {"item_ref": entry["item_ref"], "was": entry["was"]["present"],
             "now": entry["now"]["present"], "value": entry["now"]["value"]}
            for entry in diff
        ],
    })
    return assessment


# ---------------------------------------------------------------------------
# the authority
# ---------------------------------------------------------------------------


class GateReopenAuthority:
    """Propose a reopen; record the one person's answer; hand out the permission."""

    def __init__(self, store: Any) -> None:
        if not hasattr(store, "connection") or not hasattr(store, "_transaction"):
            raise TypeError("GateReopenAuthority requires a DaltonStore")
        self.store = store
        self.connection: sqlite3.Connection = store.connection
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        self._missions: Any = None

    def _mission_authority(self) -> Any:
        """The stage ladder, opened once and only when an approval needs it.

        Lazily, because a decline touches the ladder not at all and most of
        this class is reading.
        """

        if self._missions is None:
            from .coverage_mission import CoverageMissionAuthority

            self._missions = CoverageMissionAuthority(self.store)
        return self._missions

    def _active_mission(self, proposal: Mapping[str, Any]) -> dict[str, Any]:
        """The mission version the marker will bind as provenance.

        Not the version the *proposal* was written under: that one may have
        rolled while the proposal waited for an answer, and a stage record
        binds the active version by contract. What the proposal's version is
        for is saying when the assessment was made, and it stays in the
        proposal.
        """

        missions = self._mission_authority()
        proposed_under = missions.mission(proposal["mission_version_ref"])
        row = self.connection.execute(
            "SELECT mission_version_id FROM coverage_mission_pointer WHERE mission_ref=?",
            (proposed_under["mission_ref"],),
        ).fetchone()
        if row is None:
            raise DeliverableReopenConflict("this mission has no active version")
        return missions.mission(row["mission_version_id"])

    # -- reading -----------------------------------------------------------

    def proposal(self, proposal_ref: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM gate_reopen_proposals WHERE proposal_id=?",
            (_text(proposal_ref, "proposal_ref"),),
        ).fetchone()
        if row is None:
            raise DeliverableReopenNotFound("gate reopen proposal was not found")
        return _decode(row, "GateReopenProposal")

    def proposals(self, company_ref: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM gate_reopen_proposals"
        params: list[Any] = []
        if company_ref is not None:
            query += " WHERE company_ref=?"
            params.append(_text(company_ref, "company_ref"))
        query += " ORDER BY created_at, proposal_id"
        return [
            _decode(row, "GateReopenProposal")
            for row in self.connection.execute(query, params).fetchall()
        ]

    def decision_for(self, proposal_ref: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM gate_reopen_decisions WHERE proposal_ref=?",
            (_text(proposal_ref, "proposal_ref"),),
        ).fetchone()
        return None if row is None else _decode(row, "GateReopenDecision")

    def undecided(self, company_ref: str | None = None) -> list[dict[str, Any]]:
        return [
            proposal for proposal in self.proposals(company_ref)
            if self.decision_for(proposal["id"]) is None
        ]

    def holds_assessment(self, *, company_ref: str, assessment_hash: str) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM gate_reopen_proposals WHERE company_ref=? AND assessment_hash=?",
            (company_ref, assessment_hash),
        ).fetchone() is not None

    # -- writing -----------------------------------------------------------

    def propose(
        self,
        *,
        assessment: Mapping[str, Any],
        mission: Mapping[str, Any],
        actor_ref: str,
    ) -> dict[str, Any]:
        """One proposal per (company, assessment hash).  Automation may write it."""

        if assessment.get("status") != "reopen_proposed":
            raise DeliverableReopenConflict(
                "a reopen proposal needs at least one item that flipped 缺 → 有"
            )
        actor_ref = _text(actor_ref, "actor_ref")
        if actor_ref.startswith("automation:") and (
            actor_ref != mission["autonomy"]["automation_principal"]
        ):
            raise DeliverableReopenConflict("automation actor is not the mission principal")
        if not (actor_ref.startswith("automation:") or actor_ref.startswith("human:")):
            raise DeliverableReopenValidationError(
                "actor_ref must be a human: or automation: principal"
            )
        if not assessment.get("evidence_refs"):
            raise DeliverableReopenConflict(
                "a reopen must name the evidence that thickened (ADR-0008)"
            )
        record = {
            "schema_version": SCHEMA_VERSION,
            "id": "gate-reopen-proposal:" + content_hash({
                "company_ref": assessment["company_ref"],
                "assessment_hash": assessment["assessment_hash"],
            })[:32],
            "created_at": _now(),
            "company_ref": assessment["company_ref"],
            "deliverable_ref": assessment["deliverable_ref"],
            "stage_ref": assessment["stage_ref"],
            "stage_record_ref": assessment["stage_record_ref"],
            "passed_version_ref": assessment["passed_version_ref"],
            "passed_version_hash": assessment["passed_version_hash"],
            "passed_version_number": assessment["passed_version_number"],
            "passed_at": assessment["passed_at"],
            "assessment_hash": assessment["assessment_hash"],
            "policy_ref": assessment["policy_ref"],
            "thresholds": dict(assessment["thresholds"]),
            "flipped": list(assessment["flipped"]),
            "regressed": list(assessment["regressed"]),
            "diff": [dict(entry) for entry in assessment["diff"]],
            "evidence_refs": list(assessment["evidence_refs"]),
            "gate_recomputed": assessment.get("gate_recomputed"),
            "checkpoint_kind": CHECKPOINT_KIND,
            "change_reason": assessment["change_reason"],
            "mission_version_ref": mission["id"],
            "mission_version_hash": mission["content_hash"],
            "actor_ref": actor_ref,
        }
        record["content_hash"] = content_hash(record)
        with self.store._transaction() as cur:
            existing = cur.execute(
                "SELECT * FROM gate_reopen_proposals WHERE company_ref=? AND assessment_hash=?",
                (record["company_ref"], record["assessment_hash"]),
            ).fetchone()
            if existing is not None:
                return {**_decode(existing, "GateReopenProposal"), "status": "duplicate"}
            cur.execute(
                "INSERT INTO gate_reopen_proposals(proposal_id,company_ref,deliverable_ref,"
                "stage_ref,passed_version_ref,passed_version_hash,assessment_hash,"
                "flipped_count,checkpoint_kind,change_reason,mission_version_ref,"
                "record_json,content_hash,actor_ref,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record["id"], record["company_ref"], record["deliverable_ref"],
                    record["stage_ref"], record["passed_version_ref"],
                    record["passed_version_hash"], record["assessment_hash"],
                    len(record["flipped"]), CHECKPOINT_KIND, record["change_reason"],
                    mission["id"], canonical_json(record), record["content_hash"],
                    actor_ref, record["created_at"],
                ),
            )
        return {**record, "status": "fresh"}

    def decide(
        self,
        *,
        proposal_ref: str,
        proposal_hash: str,
        verdict: str,
        reason: str,
        actor_ref: str,
    ) -> dict[str, Any]:
        """The human checkpoint.  One answer per proposal, bound to its hash."""

        proposal_ref = _text(proposal_ref, "proposal_ref")
        proposal_hash = _sha256(proposal_hash, "proposal_hash")
        if verdict not in VERDICTS:
            raise DeliverableReopenValidationError(
                f"verdict must be one of {list(VERDICTS)}"
            )
        reason = _text(reason, "reason", maximum=MAX_REASON_CHARS)
        actor_ref = _human(actor_ref, "actor_ref")
        proposal = self.proposal(proposal_ref)
        if proposal["content_hash"] != proposal_hash:
            raise DeliverableReopenConflict("proposal hash binding failed")
        existing = self.decision_for(proposal_ref)
        if existing is not None:
            if (
                existing["verdict"] == verdict
                and existing["reason"] == reason
                and existing["actor_ref"] == actor_ref
            ):
                repeated: dict[str, Any] = {**existing, "status": "duplicate"}
                if verdict == "approve":
                    # Heal: the marker is written after the decision, so a
                    # retry of the exact same approval is how a decision that
                    # landed without one gets its ladder entry.
                    repeated["stage_reopen"] = self._record_stage_reopen(proposal, existing)
                return repeated
            raise DeliverableReopenConflict(
                f"this proposal was already {existing['verdict']}d"
            )
        record = {
            "schema_version": SCHEMA_VERSION,
            "id": "gate-reopen-decision:" + content_hash({
                "proposal_ref": proposal_ref, "proposal_hash": proposal_hash,
            })[:32],
            "created_at": _now(),
            "proposal_ref": proposal_ref,
            "proposal_hash": proposal_hash,
            "company_ref": proposal["company_ref"],
            "deliverable_ref": proposal["deliverable_ref"],
            "passed_version_ref": proposal["passed_version_ref"],
            "verdict": verdict,
            "reason": reason,
            "change_reason": proposal["change_reason"],
            "evidence_refs": list(proposal["evidence_refs"]),
            "reviewer_ref": actor_ref,
            "actor_ref": actor_ref,
        }
        record["content_hash"] = content_hash(record)
        # Refuse before writing anything, not after. An approval whose ladder
        # entry cannot be written is an approval that means nothing -- the
        # re-issued screen's gate would be refused as a second decision on a
        # settled stage, which is exactly the hole this closes -- so the
        # preconditions are read first and the person is told why.
        if verdict == "approve":
            self._assert_reopenable(proposal)
        with self.store._transaction() as cur:
            cur.execute(
                "INSERT INTO gate_reopen_decisions(decision_id,proposal_ref,proposal_hash,"
                "company_ref,verdict,reason,record_json,content_hash,actor_ref,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    record["id"], proposal_ref, proposal_hash, proposal["company_ref"],
                    verdict, reason, canonical_json(record), record["content_hash"],
                    actor_ref, record["created_at"],
                ),
            )
        result: dict[str, Any] = {**record, "status": "fresh"}
        if verdict == "approve":
            result["stage_reopen"] = self._record_stage_reopen(proposal, record)
        return result

    # -- the ladder entry --------------------------------------------------

    def _assert_reopenable(self, proposal: Mapping[str, Any]) -> None:
        """Is the stage this proposal names actually a passed gate right now?"""

        missions = self._mission_authority()
        mission = self._active_mission(proposal)
        state = missions.current_stage_state(mission["mission_ref"], proposal["company_ref"])
        stage = (state["stages"] or {}).get(proposal["stage_ref"]) or {}
        if stage.get("status") != "gate_passed":
            raise DeliverableReopenConflict(
                f"{proposal['stage_ref']} is {stage.get('status') or 'not reached'} for "
                f"{proposal['company_ref']}; only a passed gate can be re-opened"
            )

    def _record_stage_reopen(
        self, proposal: Mapping[str, Any], decision: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Write the ladder's copy of this approval, idempotently.

        Called after the decision so that the failure a half-written approval
        leaves behind is the safe one: no marker means the re-issue is refused
        and a person notices, where an orphan marker would mean a gate
        un-decided by nobody. Re-running the same approval re-derives the same
        marker id and heals it.
        """

        missions = self._mission_authority()
        mission = self._active_mission(proposal)
        return missions.record_stage_reopen(
            mission_version_ref=mission["id"],
            mission_version_hash=mission["content_hash"],
            company_ref=proposal["company_ref"],
            stage_ref=proposal["stage_ref"],
            reopen_decision_ref=decision["id"],
            reopen_proposal_ref=proposal["id"],
            reopened_version_ref=proposal["passed_version_ref"],
            rationale=f"gate_reopen 已批准：{decision['reason']}"[:2000],
            actor_ref=decision["actor_ref"],
            idempotency_key=f"gate-reopen:{decision['id']}",
        )


# ---------------------------------------------------------------------------
# the permission the selection rule reads
# ---------------------------------------------------------------------------


def consumed_reopen_refs(connection: sqlite3.Connection) -> set[str]:
    """Every approved proposal a version has already cited.

    A permission is spent by the version that used it.  Without this, one
    approval would re-issue the screen on every tick, which is the noise
    ADR-0008 warns about wearing a version number.
    """

    refs: set[str] = set()
    if not _has_table(connection, "mission_deliverable_versions"):  # pragma: no cover
        return refs
    for row in connection.execute(
        "SELECT record_json FROM mission_deliverable_versions WHERE kind=?",
        (DELIVERABLE_KIND,),
    ).fetchall():
        revision = json.loads(row["record_json"]).get("revision")
        if isinstance(revision, Mapping) and revision.get("reopen_ref"):
            refs.add(str(revision["reopen_ref"]))
    return refs


def approved_reopen(
    connection: sqlite3.Connection, company_ref: str
) -> dict[str, Any] | None:
    """The approved, unspent reopen for one company, or ``None``.

    This is the whole of what the Initial Screen selection rule needs to know,
    and it is read-only: the lane cannot approve its own reopen, because the
    only way a row gets here is through the human checkpoint.
    """

    company_ref = _text(company_ref, "company_ref")
    if not _has_table(connection, "gate_reopen_decisions"):
        return None
    spent = consumed_reopen_refs(connection)
    rows = connection.execute(
        "SELECT d.*, p.record_json AS proposal_json FROM gate_reopen_decisions d "
        "JOIN gate_reopen_proposals p ON p.proposal_id=d.proposal_ref "
        "WHERE d.company_ref=? AND d.verdict='approve' ORDER BY d.created_at DESC",
        (company_ref,),
    ).fetchall()
    for row in rows:
        if row["proposal_ref"] in spent:
            continue
        decision = _decode(row, "GateReopenDecision")
        return {
            **decision,
            "proposal": json.loads(row["proposal_json"]),
        }
    return None


__all__ = [
    "CHANGE_REASON_EVIDENCE",
    "CHANGE_REASON_HUMAN",
    "CHECKPOINT_KIND",
    "DEFAULT_POLICY",
    "DELIVERABLE_KIND",
    "DeliverableReopenConflict",
    "DeliverableReopenError",
    "DeliverableReopenNotFound",
    "DeliverableReopenValidationError",
    "GateReopenAuthority",
    "ITEM_LABELS",
    "REOPEN_CHANGE_REASONS",
    "REOPEN_ITEMS",
    "SCHEMA_VERSION",
    "STAGE_REF",
    "VERDICTS",
    "VERDICT_LABELS",
    "approved_reopen",
    "consumed_reopen_refs",
    "evidence_items",
    "folded_stage_history",
    "load_policy",
    "passed_version",
    "reopen_assessment",
]
