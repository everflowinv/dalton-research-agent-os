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
  a fact about the bytes that were read.  Since v2 it also fires at span
  level: the exact span the Claim cites *and* the Claim's own statement both
  never name the subject, in a document that is not the subject's own (see
  ``claim_subject``).  A morning digest names every covered company somewhere,
  so the whole-document test alone let an industry fact filed under one of
  them through.
- ``boilerplate_disclaimer``: the shared boilerplate filter the drafting path
  already applies, applied retroactively to Claims admitted before it existed.

A human may challenge and retire anything.  Automation may only act on a
deterministic detector, and only when the mission grants ``claim_challenge``.

2026-09-24: or on a *recorded verdict* -- ``citation_support_rejected``, an
independent model's append-only finding (``claim_support_verification``) that
the cited sentences do not support the statement or that it is about another
company.  Nothing is re-run for it, because a model is not a function of the
bytes; the authority instead re-reads the verdict row bound to the exact
claim version, its hash, its subject and its statement, and refuses without
one.  The review patrol never acts on it; the support backfill does.
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
from .store import DaltonStore, authorization_flag, authorized_flag, content_hash

SCHEMA_VERSION = "0.1"
_SCHEMA_PATH = Path(__file__).with_name("claim_retirement_schema.sql")

REASON_CODES: tuple[str, ...] = (
    "subject_absent_from_source",
    "boilerplate_disclaimer",
    "human_judgment",
    "citation_support_rejected",
)
DETERMINISTIC_REASONS = frozenset({"subject_absent_from_source", "boilerplate_disclaimer"})
# 2026-09-24: an independent model's recorded verdict (claim_support_verification)
# that the cited sentences do not support the statement, or that it is about
# another company.  Not deterministic -- a model said it -- so the authority
# does not re-run anything; it re-reads the append-only verdict row bound to
# the exact claim version, statement and subject, and refuses without one.
RECORDED_VERDICT_REASONS = frozenset({"citation_support_rejected"})
SUPPORT_VERIFIER_REF = "claim-verifier:citation-support:v1"
# v2: span level as well as document level (claim_subject.subject_absent_from_citation).
# v3 (2026-09-24 audit): the span rule also keeps a Claim whose statement leans
# on an antecedent just before the span, or names an executive, and a document
# is the subject's own on its filing cover or by density
# (claim_subject.own_document_evidence).  Stricter to retire, admission untouched.
SUBJECT_DETECTOR_REF = "claim-detector:subject-absent-from-source:v3"
#: The detector the pre-audit span retirements were made under -- the ones
#: the re-review may withdraw (claim_review.ClaimReviewDriver.rereview_retirements).
SPAN_V2_DETECTOR_REF = "claim-detector:subject-absent-from-source:v2"
#: How a span retirement's rationale begins (``detect``), which is how it is
#: told apart from a whole-document one under the same detector ref.
SPAN_RATIONALE_PREFIX = "这条结论所引的原文片段"
REINSTATEMENT_REASONS: tuple[str, ...] = (
    "human_judgment",
    "subject_named_under_current_rule",
)
#: The rule an automatic reinstatement re-runs.  v3: the current span
#: detector no longer fires.  v4 (2026-09-25 audit): and the subject is
#: positively named -- by the statement, its executive, the antecedent it
#: leans on, or a document that is the subject's own by more than its head
#: (``claim_subject.subject_named_for_reinstatement``).  A name somewhere in a
#: 1,200-character span no longer puts a Claim back.
PRIOR_REREVIEW_RULE_REFS = frozenset({"claim-rereview:subject-absent-span:v3"})
REREVIEW_RULE_REF = "claim-rereview:subject-named-strict:v4"
WITHDRAWAL_REASONS: tuple[str, ...] = (
    "human_judgment",
    "subject_not_named_under_strict_rule",
)
BOILERPLATE_DETECTOR_REF = "claim-detector:boilerplate-disclaimer:v1"
DETECTOR_REFS = {
    "subject_absent_from_source": SUBJECT_DETECTOR_REF,
    "boilerplate_disclaimer": BOILERPLATE_DETECTOR_REF,
    "citation_support_rejected": SUPPORT_VERIFIER_REF,
}
REASON_LABELS = {
    "subject_absent_from_source": "引用的原文（或所引片段及结论本身）没有提到这家公司",
    "boilerplate_disclaimer": "这是免责声明或套话，不是研究结论",
    "human_judgment": "你的判断",
    "citation_support_rejected": "独立模型核验：所引原文不支持这条结论，或它说的是另一家公司",
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


def subject_absent(
    *,
    statement: str,
    source_text: str | None,
    needles: Sequence[str],
    cited_span: str | None = None,
    document_is_own: bool = False,
    context_before: str | None = None,
    context_after: str | None = None,
    peer_needles: Sequence[str] = (),
) -> str | None:
    """Which form of the subject-absent rule fires: "document", "span" or None."""

    from .claim_subject import subject_absent_from_citation

    if source_text is None:
        return None
    if subject_absent_from_source(source_text, needles):
        return "document"
    if cited_span is not None and subject_absent_from_citation(
        span=cited_span, statement=statement, needles=needles,
        document_is_own=document_is_own, context_before=context_before,
        context_after=context_after, peer_needles=peer_needles,
    ):
        return "span"
    return None


def detect(
    *,
    statement: str,
    source_text: str | None,
    needles: Sequence[str],
    cited_span: str | None = None,
    document_is_own: bool = False,
    context_before: str | None = None,
    context_after: str | None = None,
    peer_needles: Sequence[str] = (),
) -> tuple[str, str] | None:
    """The first deterministic reason this Claim should be retired, or None.

    ``cited_span`` is the exact text the Claim's citation binds.  Without it
    only the whole-document rule can fire, which is how every caller behaved
    before the span rule existed.
    """

    if statement_is_boilerplate(statement):
        return ("boilerplate_disclaimer", "这条陈述命中了免责声明/套话过滤器，没有断言任何研究观点。")
    which = subject_absent(
        statement=statement, source_text=source_text, needles=needles,
        cited_span=cited_span, document_is_own=document_is_own,
        context_before=context_before, context_after=context_after,
        peer_needles=peer_needles,
    )
    names = "、".join(needles)
    if which == "document":
        return (
            "subject_absent_from_source",
            f"引用的原文全文（{len(source_text or ''):,} 字）里没有出现 {names} 中的任何一个，"
            "这份原文不是关于这家公司的。",
        )
    if which == "span":
        return (
            "subject_absent_from_source",
            f"{SPAN_RATIONALE_PREFIX}（{len(cited_span or ''):,} 字）和结论本身都没有出现 "
            f"{names} 中的任何一个，且这份原文不是该公司自己的文件（标题/开头未提到它）；"
            "它说的是别的公司或行业，被挂在了这家公司名下。",
        )
    return None


def _table_exists(connection: Any, name: str) -> bool:
    try:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None
    except sqlite3.Error:
        return False


def reinstated_claim_version_refs(connection: Any) -> set[str]:
    """Claim versions whose retirement a later record withdrew.

    Empty on a Core without the table (older, or opened read-only before the
    authority ever ran there).  The one query every read path shares; see
    :func:`retired_claim_version_refs`.
    """

    if not _table_exists(connection, "claim_retirement_reinstatements"):
        return set()
    # 2026-09-25: a reinstatement a later record withdrew no longer stands.
    withdrawn = ""
    if _table_exists(connection, "claim_retirement_reinstatement_withdrawals"):
        withdrawn = (" WHERE NOT EXISTS (SELECT 1 FROM claim_retirement_reinstatement_withdrawals w"
                     " WHERE w.reinstatement_ref=r.reinstatement_id)")
    return {
        str(row[0]) for row in connection.execute(
            "SELECT r.claim_version_ref FROM claim_retirement_reinstatements r" + withdrawn
        ).fetchall()
    }


def withdrawn_reinstatement_claim_version_refs(connection: Any) -> set[str]:
    """Claim versions whose reinstatement a later record withdrew (retired again)."""

    if not _table_exists(connection, "claim_retirement_reinstatement_withdrawals"):
        return set()
    return {
        str(row[0]) for row in connection.execute(
            "SELECT claim_version_ref FROM claim_retirement_reinstatement_withdrawals"
        ).fetchall()
    }


def retired_claim_version_refs(connection: Any) -> set[str]:
    """Claim versions that are retired *now*: retired, less reinstated.

    Every read path that skips retired Claims goes through this, so that a
    reinstatement puts a Claim back everywhere at once.  Empty on a Core
    without the decisions table.
    """

    if not _table_exists(connection, "claim_retirement_decisions"):
        return set()
    retired = {
        str(row[0]) for row in connection.execute(
            "SELECT claim_version_ref FROM claim_retirement_decisions WHERE decision='retired'"
        ).fetchall()
    }
    return retired - reinstated_claim_version_refs(connection)


def retirement_state_probe(connection: Any) -> str:
    """One short string that moves whenever the retired set can have moved.

    For change keys and cheap signatures (``lane_change_key``, the lanes'
    "is it worth re-running" counts): a retirement *and* a reinstatement each
    append a row, so both tables' append state is in it.
    """

    parts = []
    # 2026-09-25: an industry reattribution moves what an industry subject
    # reads (``company_research_view`` answers an industry with them), so the
    # lanes' change keys see it too.
    for table in ("claim_retirement_decisions", "claim_retirement_reinstatements",
                  "claim_retirement_reinstatement_withdrawals",
                  "claim_industry_reattributions"):
        if not _table_exists(connection, table):
            parts.append(f"{table}:absent")
            continue
        row = connection.execute(f"SELECT COUNT(*), MAX(rowid) FROM {table}").fetchone()
        parts.append(f"{table}:{row[0]}:{row[1]}")
    # 2026-09-25b: a withdrawn industry reattribution moves the industry reads
    # too.  Named only once one exists, so a Core without any keeps the probe
    # (and every lane change key built on it) it had.
    table = "claim_industry_reattribution_withdrawals"
    if _table_exists(connection, table):
        row = connection.execute(f"SELECT COUNT(*), MAX(rowid) FROM {table}").fetchone()
        if row[0]:
            parts.append(f"{table}:{row[0]}:{row[1]}")
    return "|".join(parts)


class ClaimRetirementAuthority:
    """Append-only challenges and retirements over an untouched Ledger."""

    _authorized = authorized_flag()

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
        self._authorization_flag = authorization_flag(
            self.connection, "dalton_claim_retirement_authorized")
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        self._admit_new_reason_codes()

    def _admit_new_reason_codes(self) -> None:
        """Widen an existing challenges table's reason CHECK, keeping every stored byte.

        ``CREATE TABLE IF NOT EXISTS`` never changes a table that is already
        there, so a Core created before ``citation_support_rejected`` existed
        would refuse the row.  Rebuilt the way ``debate_map`` widened its
        change-reason CHECK: same columns, same rows, same indexes and
        triggers, one transaction, foreign keys checked afterwards.
        """

        row = self.connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='claim_retirement_challenges'"
        ).fetchone()
        if row is None or all(code in (row[0] or "") for code in REASON_CODES):
            return
        if self.connection.in_transaction:
            raise ClaimRetirementConflict(
                "the claim challenge reason migration requires no open transaction")
        reasons = ",\n        ".join(f"'{code}'" for code in REASON_CODES)
        self.connection.execute("PRAGMA foreign_keys = OFF")
        try:
            self.connection.executescript(f"""
                BEGIN IMMEDIATE;
                DROP TRIGGER IF EXISTS claim_retirement_challenges_authorized_insert;
                DROP TRIGGER IF EXISTS claim_retirement_challenges_no_update;
                DROP TRIGGER IF EXISTS claim_retirement_challenges_no_delete;
                CREATE TABLE claim_retirement_challenges_v2 (
                    challenge_id TEXT PRIMARY KEY,
                    claim_version_ref TEXT NOT NULL REFERENCES claim_versions(claim_version_id),
                    claim_version_hash TEXT NOT NULL,
                    claim_ref TEXT NOT NULL,
                    subject_ref TEXT NOT NULL,
                    reason_code TEXT NOT NULL CHECK(reason_code IN (
                        {reasons}
                    )),
                    detector_ref TEXT,
                    detector_hash TEXT,
                    rationale TEXT NOT NULL,
                    actor_ref TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(claim_version_ref, reason_code)
                );
                INSERT INTO claim_retirement_challenges_v2 SELECT * FROM claim_retirement_challenges;
                DROP TABLE claim_retirement_challenges;
                ALTER TABLE claim_retirement_challenges_v2 RENAME TO claim_retirement_challenges;
                COMMIT;
            """)
        finally:
            self.connection.execute("PRAGMA foreign_keys = ON")
        # Indexes and triggers, exactly as the schema file declares them.
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        if self.connection.execute("PRAGMA foreign_key_check").fetchall():
            raise ClaimRetirementConflict("the claim challenge reason migration broke foreign keys")

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
        """Claim versions retired now -- a decision retired them and no
        reinstatement withdrew it; read paths skip exactly these."""

        return retired_claim_version_refs(self.connection)

    def reinstated_claim_version_refs(self) -> set[str]:
        return reinstated_claim_version_refs(self.connection)

    def reinstatements(self, *, limit: int = 200) -> list[dict[str, Any]]:
        import json

        return [
            {**json.loads(row["record_json"]), "content_hash": row["content_hash"]}
            for row in self.connection.execute(
                "SELECT record_json, content_hash FROM claim_retirement_reinstatements "
                "ORDER BY created_at, reinstatement_id LIMIT ?", (int(limit),),
            ).fetchall()
        ]

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
        if reason_code in DETERMINISTIC_REASONS | RECORDED_VERDICT_REASONS:
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
        cited_span: str | None = None,
        document_is_own: bool = False,
        context_before: str | None = None,
        context_after: str | None = None,
        peer_needles: Sequence[str] = (),
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
            if record["reason_code"] not in DETERMINISTIC_REASONS | RECORDED_VERDICT_REASONS:
                raise ClaimRetirementConflict(
                    "automation may only retire on a deterministic detector or a recorded verdict"
                )
            # Re-run the detector against the exact original, here.  A caller
            # that says the check fired is not evidence that it fired.
            if record["reason_code"] in RECORDED_VERDICT_REASONS:
                from .claim_support_verification import recorded_rejection

                if recorded_rejection(self.connection, claim_version_ref=record["claim_version_ref"],
                                      claim=claim) is None:
                    raise ClaimRetirementConflict(
                        "no recorded support verdict rejects this exact Claim version")
            elif record["reason_code"] == "boilerplate_disclaimer":  # noqa: SIM108
                if not statement_is_boilerplate(claim["normalized_statement"]):
                    raise ClaimRetirementConflict("detector no longer fires for this Claim")
            else:
                if source_text is None and self.source_text_resolver is not None:
                    source_text = self.source_text_resolver(record["claim_version_ref"])
                if source_text is None:
                    raise ClaimRetirementConflict(
                        "the original cannot be read; a Claim is never retired unverified"
                    )
                if subject_absent(
                    statement=claim["normalized_statement"], source_text=source_text,
                    needles=list(subject_needles), cited_span=cited_span,
                    document_is_own=document_is_own, context_before=context_before,
                    context_after=context_after, peer_needles=list(peer_needles),
                ) is None:
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

    def retire_by_hand(
        self,
        *,
        claim_version_ref: str,
        claim_version_hash: str,
        actor_ref: str,
        rationale: str,
    ) -> dict[str, Any]:
        """A person retires one admitted Claim: challenge and decision together.

        The owner's door for a Claim no detector will ever flag -- the
        2026-09-25b SEO statistics compilations, a statement impossible at its
        document's date.  It is exactly the two records a patrol writes (a
        ``human_judgment`` challenge, then a ``retired`` decision), bound to the
        exact claim version and its hash, so every read path treats the Claim
        as retired and a reinstatement can still withdraw it.  A Claim that
        already has a decision is left as it is (``already_decided``):
        decisions are one per Claim.
        """

        import json

        actor = _actor(actor_ref)
        if not _HUMAN_RE.fullmatch(actor):
            raise ClaimRetirementConflict("only a person retires a Claim by hand")
        claim_version_ref = _text(claim_version_ref, "claim_version_ref", maximum=512)
        claim_version_hash = _sha256(claim_version_hash, "claim_version_hash")
        rationale = _text(rationale, "rationale")
        claim = self._claim(claim_version_ref)
        if claim["content_hash"] != claim_version_hash:
            raise ClaimRetirementConflict("claim version hash binding failed")
        existing = self.connection.execute(
            "SELECT record_json FROM claim_retirement_decisions WHERE claim_version_ref=?",
            (claim_version_ref,),
        ).fetchone()
        if existing is not None:
            return {"status": "already_decided", "claim_version_ref": claim_version_ref,
                    "decision": json.loads(existing["record_json"])}
        challenge = self.challenge(
            claim_version_ref=claim_version_ref, claim_version_hash=claim_version_hash,
            reason_code="human_judgment", rationale=rationale, actor_ref=actor,
        )
        decision = self.decide(
            challenge_ref=challenge["id"], challenge_hash=challenge["content_hash"],
            decision="retired", actor_ref=actor, rationale=rationale,
        )
        return {"status": decision["status"], "claim_version_ref": claim_version_ref,
                "challenge_ref": challenge["id"], "decision_ref": decision["id"],
                "decision_hash": decision["content_hash"]}

    def reinstate(
        self,
        *,
        claim_version_ref: str,
        actor_ref: str,
        rationale: str,
        decision_hash: str | None = None,
        subject_needles: Sequence[str] = (),
        source_text: str | None = None,
        cited_span: str | None = None,
        document_is_own: bool = False,
        context_before: str | None = None,
        context_after: str | None = None,
        peer_needles: Sequence[str] = (),
        strict_own_document: str | None = None,
    ) -> dict[str, Any]:
        """Withdraw one retirement by appending a record that names it.

        Nothing is edited: the decision row stays, byte for byte, and the
        reinstatement binds its id and hash.  A person may withdraw any
        retirement (``human_judgment``).  Automation may withdraw only a
        subject-absent retirement, and only when the *current* span rule --
        today's alias table, the v3 context and own-document tests -- no
        longer fires on the exact original *and* the subject is positively
        named under the v4 reinstatement rule
        (``claim_subject.subject_named_for_reinstatement``; ``strict_own_document``
        is the own-document reason computed without the head test); both are
        re-run here, the same way ``decide`` re-runs a detector before it
        retires.  One reinstatement per decision, so a repeat is
        ``duplicate``.
        """

        import json

        claim_version_ref = _text(claim_version_ref, "claim_version_ref", maximum=512)
        rationale = _text(rationale, "rationale")
        actor = _actor(actor_ref)
        row = self.connection.execute(
            "SELECT record_json, content_hash, decision FROM claim_retirement_decisions "
            "WHERE claim_version_ref=?", (claim_version_ref,),
        ).fetchone()
        if row is None:
            raise ClaimRetirementNotFound("no retirement decision names this claim version")
        decision = json.loads(row["record_json"])
        if decision.get("content_hash") != row["content_hash"]:
            raise ClaimRetirementConflict("claim retirement decision authority drifted")
        if row["decision"] != "retired":
            raise ClaimRetirementConflict("only a retired claim version can be reinstated")
        if decision_hash is not None and _sha256(decision_hash, "decision_hash") != row["content_hash"]:
            raise ClaimRetirementConflict("decision hash binding failed")
        challenge = self.challenge_record(decision["challenge_ref"])
        claim = self._claim(claim_version_ref)
        rule_ref = None
        if _AUTOMATION_RE.fullmatch(actor):
            if challenge["reason_code"] != "subject_absent_from_source":
                raise ClaimRetirementConflict(
                    "automation may only reinstate a subject-absent retirement")
            if source_text is None and self.source_text_resolver is not None:
                source_text = self.source_text_resolver(claim_version_ref)
            if source_text is None or cited_span is None:
                raise ClaimRetirementConflict(
                    "the original cannot be read; a retirement is never withdrawn unverified")
            if subject_absent(
                statement=claim["normalized_statement"], source_text=source_text,
                needles=list(subject_needles), cited_span=cited_span,
                document_is_own=document_is_own, context_before=context_before,
                context_after=context_after, peer_needles=list(peer_needles),
            ) is not None:
                raise ClaimRetirementConflict("the current rule still retires this Claim")
            from .claim_subject import subject_named_for_reinstatement

            named_by = subject_named_for_reinstatement(
                statement=claim["normalized_statement"], span=cited_span,
                needles=list(subject_needles), peer_needles=list(peer_needles),
                context_before=context_before, context_after=context_after,
                own_document=strict_own_document,
            )
            if named_by is None:
                raise ClaimRetirementConflict(
                    "the subject is not positively named; a span that merely contains "
                    "the name does not put a Claim back")
            reason_code, rule_ref = "subject_named_under_current_rule", REREVIEW_RULE_REF
        else:
            reason_code, named_by = "human_judgment", None
        wire = {
            "schema_version": SCHEMA_VERSION,
            "claim_version_ref": claim_version_ref,
            "claim_ref": decision["claim_ref"],
            "subject_ref": challenge["subject_ref"],
            "decision_ref": decision["id"],
            "decision_hash": row["content_hash"],
            "challenge_ref": challenge["id"],
            "retired_reason_code": challenge["reason_code"],
            "retired_detector_ref": challenge.get("detector_ref"),
            "reason_code": reason_code,
            "rule_ref": rule_ref,
            "actor_ref": actor,
            "rationale": rationale,
            "created_at": self.clock(),
        }
        if named_by is not None:
            wire["named_by"] = named_by
        wire["id"] = "claim-retirement-reinstatement:" + content_hash(
            {"decision": decision["id"]})[:32]
        wire["content_hash"] = content_hash({k: v for k, v in wire.items() if k != "content_hash"})
        with self._transaction() as cur:
            existing = cur.execute(
                "SELECT record_json FROM claim_retirement_reinstatements WHERE decision_ref=?",
                (decision["id"],),
            ).fetchone()
            if existing is not None:
                return {**json.loads(existing["record_json"]), "status": "duplicate"}
            cur.execute(
                "INSERT INTO claim_retirement_reinstatements(reinstatement_id,claim_version_ref,"
                "decision_ref,decision_hash,reason_code,rule_ref,actor_ref,rationale,record_json,"
                "content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (wire["id"], claim_version_ref, decision["id"], row["content_hash"],
                 reason_code, rule_ref, actor, rationale,
                 json.dumps(wire, ensure_ascii=False, sort_keys=True),
                 wire["content_hash"], wire["created_at"]),
            )
        return {**wire, "status": "fresh"}

    def withdraw_reinstatement(
        self,
        *,
        claim_version_ref: str,
        actor_ref: str,
        rationale: str,
        reinstatement_hash: str | None = None,
        subject_needles: Sequence[str] = (),
        cited_span: str | None = None,
        context_before: str | None = None,
        context_after: str | None = None,
        peer_needles: Sequence[str] = (),
        strict_own_document: str | None = None,
    ) -> dict[str, Any]:
        """Withdraw a reinstatement, so the retirement stands again (2026-09-25).

        The decision table is one row per Claim for ever and a reinstatement
        is one per decision, so a wrong reinstatement cannot be undone by
        retiring the Claim again.  This appends the undo instead, binding the
        reinstatement's id and hash; nothing is edited.

        A person may withdraw any reinstatement (``human_judgment``).
        Automation may withdraw only an automatic one made under an earlier
        re-review rule (``PRIOR_REREVIEW_RULE_REFS``), and only when the
        current reinstatement rule, re-run here on the exact span, finds the
        subject not positively named (``subject_not_named_under_strict_rule``).
        One withdrawal per reinstatement; a repeat is ``duplicate``.
        """

        import json

        claim_version_ref = _text(claim_version_ref, "claim_version_ref", maximum=512)
        rationale = _text(rationale, "rationale")
        actor = _actor(actor_ref)
        row = self.connection.execute(
            "SELECT record_json, content_hash FROM claim_retirement_reinstatements "
            "WHERE claim_version_ref=? ORDER BY created_at DESC LIMIT 1", (claim_version_ref,),
        ).fetchone()
        if row is None:
            raise ClaimRetirementNotFound("no reinstatement names this claim version")
        reinstatement = json.loads(row["record_json"])
        if reinstatement.get("content_hash") != row["content_hash"]:
            raise ClaimRetirementConflict("claim reinstatement authority drifted")
        if (reinstatement_hash is not None
                and _sha256(reinstatement_hash, "reinstatement_hash") != row["content_hash"]):
            raise ClaimRetirementConflict("reinstatement hash binding failed")
        rule_ref = None
        if _AUTOMATION_RE.fullmatch(actor):
            if reinstatement.get("reason_code") != "subject_named_under_current_rule":
                raise ClaimRetirementConflict(
                    "automation may only withdraw an automatic reinstatement")
            if reinstatement.get("rule_ref") not in PRIOR_REREVIEW_RULE_REFS:
                raise ClaimRetirementConflict(
                    "a reinstatement made under the current rule is not re-judged by it")
            if cited_span is None:
                raise ClaimRetirementConflict(
                    "the cited span cannot be read; a reinstatement is never withdrawn unverified")
            from .claim_subject import subject_named_for_reinstatement

            claim = self._claim(claim_version_ref)
            named_by = subject_named_for_reinstatement(
                statement=claim["normalized_statement"], span=cited_span,
                needles=list(subject_needles), peer_needles=list(peer_needles),
                context_before=context_before, context_after=context_after,
                own_document=strict_own_document,
            )
            if named_by is not None:
                raise ClaimRetirementConflict(
                    f"the current rule still names the subject ({named_by})")
            reason_code, rule_ref = "subject_not_named_under_strict_rule", REREVIEW_RULE_REF
        else:
            reason_code = "human_judgment"
        wire = {
            "schema_version": SCHEMA_VERSION,
            "claim_version_ref": claim_version_ref,
            "claim_ref": reinstatement.get("claim_ref"),
            "reinstatement_ref": reinstatement["id"],
            "reinstatement_hash": row["content_hash"],
            "decision_ref": reinstatement.get("decision_ref"),
            "reinstated_rule_ref": reinstatement.get("rule_ref"),
            "reason_code": reason_code,
            "rule_ref": rule_ref,
            "actor_ref": actor,
            "rationale": rationale,
            "created_at": self.clock(),
        }
        wire["id"] = "claim-retirement-reinstatement-withdrawal:" + content_hash(
            {"reinstatement": reinstatement["id"]})[:32]
        wire["content_hash"] = content_hash({k: v for k, v in wire.items() if k != "content_hash"})
        with self._transaction() as cur:
            existing = cur.execute(
                "SELECT record_json FROM claim_retirement_reinstatement_withdrawals "
                "WHERE reinstatement_ref=?", (reinstatement["id"],),
            ).fetchone()
            if existing is not None:
                return {**json.loads(existing["record_json"]), "status": "duplicate"}
            cur.execute(
                "INSERT INTO claim_retirement_reinstatement_withdrawals(withdrawal_id,"
                "claim_version_ref,reinstatement_ref,reinstatement_hash,reason_code,rule_ref,"
                "actor_ref,rationale,record_json,content_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (wire["id"], claim_version_ref, reinstatement["id"], row["content_hash"],
                 reason_code, rule_ref, actor, rationale,
                 json.dumps(wire, ensure_ascii=False, sort_keys=True),
                 wire["content_hash"], wire["created_at"]),
            )
        return {**wire, "status": "fresh"}


__all__ = [
    "BOILERPLATE_DETECTOR_REF",
    "PRIOR_REREVIEW_RULE_REFS",
    "REINSTATEMENT_REASONS",
    "REREVIEW_RULE_REF",
    "WITHDRAWAL_REASONS",
    "withdrawn_reinstatement_claim_version_refs",
    "SPAN_RATIONALE_PREFIX",
    "SPAN_V2_DETECTOR_REF",
    "reinstated_claim_version_refs",
    "retired_claim_version_refs",
    "retirement_state_probe",
    "RECORDED_VERDICT_REASONS",
    "SUPPORT_VERIFIER_REF",
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
    "subject_absent",
    "subject_absent_from_source",
    "subject_needles",
]
