"""Keep an industry-level finding that was retired at company level.

The span-level subject-absent detector (``claim_retirement``, v2/v3) retires
a Claim whose cited span and statement never name the company it is filed
under.  Right at company level -- and a loss of knowledge when the Claim was
about the industry: the Wells Fargo mid-year IT services CIO survey
(alphaengine-doc:320000609769188) was filed 54 times under CTSH and EPAM, and
ws-7d holds about a hundred hyperscaler-capex statements filed under one
hyperscaler or another.

ClaimVersion is a frozen contract and ``subject_ref`` is part of its hash, so
a Claim is never re-filed.  Instead, the way a reinstatement withdraws a
retirement (``claim_retirement_reinstatements``), a further append-only record
says *this retired claim version is evidence about this industry*:

* ``claim_industry_reattributions`` -- one row per claim version, binding the
  claim version and the retirement decision (ids and hashes), the company it
  was filed under, the mission's ``industry_ref``, the rule ref that decided
  it and who applied it.  Update and delete are refused by trigger.
* The retirement stands.  Every company-level read path keeps skipping the
  Claim through ``retired_claim_version_refs``, which this module never
  touches; only the industry-level reads below pick it up, and only under the
  industry the record names.

Who may write one:

* **automation** -- the mission's automation principal, only when the mission
  grants ``claim_challenge`` (the grant that let it retire the Claim), only
  for a ``subject_absent_from_source`` retirement -- or, since 2026-09-28, a
  ``citation_support_rejected`` one whose governing support verdict is
  *supported* and *about another subject* that is this mission's industry
  (:func:`about_other_evidence`) -- and only when the deterministic rule
  (``claim_industry_rule.judge``) fires on the exact statement and cited
  span, re-run here rather than trusted;
* **a person** (``human:``) -- any retired Claim except a boilerplate
  disclaimer, through the writer's ``reattribute_claim_to_industry``
  operation (``claim_industry_reattribution_cli ... --apply --actor human:``).

The ``industry_ref`` is always the covering mission's own; a caller cannot
name another.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .claim_industry_rule import (
    ABOUT_OTHER_RULE_REF,
    PRIOR_RULE_REFS,
    RULE_REF,
    judge,
    other_subject_scope,
    statement_names_industry,
)
from .store import DaltonStore, authorization_flag, authorized_flag, content_hash

SCHEMA_VERSION = "0.1"
_SCHEMA_PATH = Path(__file__).with_name("claim_industry_reattribution_schema.sql")
TABLE = "claim_industry_reattributions"
REVIEW_TABLE = "claim_industry_reattribution_reviews"
#: 2026-09-25b: withdrawn reattributions (one row per reattribution).
WITHDRAWAL_TABLE = "claim_industry_reattribution_withdrawals"
WITHDRAWAL_REASONS: tuple[str, ...] = ("not_industry_level_under_current_rule", "human_judgment")
REASON_CODES: tuple[str, ...] = ("industry_level_under_rule", "human_judgment")
#: The mission grant automation writes under: the one that let it retire.
WRITE_SCOPE = "claim_challenge"
#: Retirements automation may reattribute.  2026-09-28: a support retirement
#: too, when the verifier found the Claim supported and about another subject
#: that is this mission's industry (:func:`about_other_evidence`).
SUBJECT_ABSENT_REASON = "subject_absent_from_source"
SUPPORT_REASON = "citation_support_rejected"
AUTOMATIC_RETIREMENT_REASONS: tuple[str, ...] = (SUBJECT_ABSENT_REASON, SUPPORT_REASON)
#: Originals read per tick by the backfill, and records written per tick.
DEFAULT_MAX_DOCUMENTS = 20
DEFAULT_MAX_WRITES = 50
#: 2026-09-28: support retirements decided on their recorded verdicts alone
#: (no original read) per tick -- each is a review-cache write.
DEFAULT_MAX_VERDICT_REVIEWS = 100
_HUMAN_RE = re.compile(r"^human:[A-Za-z0-9._:-]{1,128}$")
_AUTOMATION_RE = re.compile(r"^automation:[A-Za-z0-9._:-]{1,128}$")


class ClaimReattributionError(RuntimeError):
    """Base error for the industry reattribution authority."""


class ClaimReattributionValidationError(ClaimReattributionError):
    """An argument is invalid."""


class ClaimReattributionConflict(ClaimReattributionError):
    """A gate refused, or a binding does not hold."""


class ClaimReattributionNotFound(ClaimReattributionError):
    """The named record does not exist."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any, name: str, *, maximum: int = 4000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ClaimReattributionValidationError(f"{name} must be text of 1..{maximum} characters")
    return value.strip()


def _actor(value: Any) -> str:
    actor = _text(value, "actor_ref", maximum=256)
    if not (_HUMAN_RE.fullmatch(actor) or _AUTOMATION_RE.fullmatch(actor)):
        raise ClaimReattributionValidationError("actor_ref must be a human: or automation: principal")
    return actor


def _table_exists(connection: Any, name: str) -> bool:
    try:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None
    except sqlite3.Error:
        return False


# -- the shared reads ------------------------------------------------------


def industry_reattributions(
    connection: Any, industry_ref: str | None = None,
) -> dict[str, dict[str, str]]:
    """claim_version_ref -> {industry_ref, reattribution_ref} for live ones.

    Live means the Claim is still retired: a retirement a reinstatement
    withdrew puts the Claim back under its company, and it is then that
    company's again, not the industry's.  Empty on a Core without the table.
    Every industry-level consumer reads through this (or the two helpers
    below); no company-level path does.
    """

    if not _table_exists(connection, TABLE):
        return {}
    from .claim_retirement import retired_claim_version_refs

    query = f"SELECT claim_version_ref, industry_ref, reattribution_id FROM {TABLE}"
    params: tuple[Any, ...] = ()
    if industry_ref is not None:
        query += " WHERE industry_ref=?"
        params = (industry_ref,)
    rows = connection.execute(query, params).fetchall()
    if not rows:
        return {}
    retired = retired_claim_version_refs(connection)
    withdrawn = withdrawn_reattribution_refs(connection)
    return {
        str(row[0]): {"industry_ref": str(row[1]), "reattribution_ref": str(row[2])}
        for row in rows if str(row[0]) in retired and str(row[2]) not in withdrawn
    }


def withdrawn_reattribution_refs(connection: Any) -> set[str]:
    """The reattributions a later record withdrew (2026-09-25b)."""

    if not _table_exists(connection, WITHDRAWAL_TABLE):
        return set()
    return {str(row[0]) for row in connection.execute(
        f"SELECT reattribution_ref FROM {WITHDRAWAL_TABLE}").fetchall()}


def reattributed_claim_version_refs(connection: Any, industry_ref: str) -> set[str]:
    """The retired claim versions that are evidence about this industry."""

    return set(industry_reattributions(connection, industry_ref))


def reattribution_state_probe(connection: Any) -> str:
    """One short string that moves whenever a reattribution is appended."""

    if not _table_exists(connection, TABLE):
        return f"{TABLE}:absent"
    row = connection.execute(f"SELECT COUNT(*), MAX(rowid) FROM {TABLE}").fetchone()
    probe = f"{TABLE}:{row[0]}:{row[1]}"
    if _table_exists(connection, WITHDRAWAL_TABLE):
        row = connection.execute(
            f"SELECT COUNT(*), MAX(rowid) FROM {WITHDRAWAL_TABLE}").fetchone()
        if row[0]:
            # Only once one exists, so a Core with none keeps its old probe.
            probe += f"|{WITHDRAWAL_TABLE}:{row[0]}:{row[1]}"
    return probe


def covering_missions(connection: Any) -> dict[str, dict[str, Any]]:
    """company_ref -> the active mission covering it, with its industry.

    Read from the mission pointer and the exact version it names; a version
    whose stored hash does not match the pointer is skipped (it covers
    nothing here).  ``industry_ref`` is the mission's own field -- the only
    place an industry ever comes from.
    """

    covering: dict[str, dict[str, Any]] = {}
    try:
        rows = connection.execute(
            "SELECT p.mission_version_id AS id, p.content_hash AS pointer_hash, "
            "v.record_json AS record_json, v.content_hash AS content_hash, "
            "v.industry_ref AS industry_ref FROM coverage_mission_pointer p "
            "JOIN coverage_mission_versions v ON v.mission_version_id=p.mission_version_id "
            "ORDER BY p.mission_ref"
        ).fetchall()
    except sqlite3.Error:
        return {}
    for row in rows:
        try:
            mission = json.loads(row["record_json"])
        except (TypeError, ValueError):
            continue
        if (row["pointer_hash"] != row["content_hash"]
                or mission.get("content_hash") != row["content_hash"]
                or mission.get("industry_ref") != row["industry_ref"]):
            continue
        industry = mission.get("industry_ref")
        autonomy = mission.get("autonomy") or {}
        for member in mission.get("universe") or ():
            if not isinstance(member, Mapping) or not member.get("company_ref"):
                continue
            covering.setdefault(str(member["company_ref"]), {
                "mission_version_ref": mission.get("id"),
                "mission_hash": row["content_hash"],
                "industry_ref": industry if isinstance(industry, str)
                and industry.startswith("industry:") else None,
                "principal": autonomy.get("automation_principal"),
                "granted": WRITE_SCOPE in (autonomy.get("may_write") or ()),
                "universe": [dict(item) for item in mission.get("universe") or ()
                             if isinstance(item, Mapping)],
            })
    return covering


def mission_roster(universe: Sequence[Mapping[str, Any]],
                   extra: Mapping[str, Sequence[str]] | None = None) -> dict[str, list[str]]:
    """company_ref -> every name of every covered company (a union)."""

    from .claim_subject import mission_subject_needles

    roster = {ref: set(values) for ref, values in mission_subject_needles(universe).items()}
    for member in universe:
        roster.setdefault(str(member.get("company_ref")), set())
    for ref, values in (extra or {}).items():
        if ref in roster:
            roster[ref].update(str(value).lower() for value in values if value)
    return {ref: sorted(values) for ref, values in roster.items()}


def inputs_hash(industry_ref: Any, roster: Mapping[str, Sequence[str]],
                verdict_keys: Sequence[str] | None = None) -> str:
    """What a review-cache mark was judged with.

    A support retirement's also names its governing verdicts and the
    about-other rule, so a newer verdict (the next contract's re-review) or a
    new rule judges it once more.
    """

    body: dict[str, Any] = {"rule": RULE_REF, "industry": industry_ref,
                            "roster": {ref: sorted(values) for ref, values in roster.items()}}
    if verdict_keys is not None:
        body.update({"about_other_rule": ABOUT_OTHER_RULE_REF,
                     "verdicts": sorted(verdict_keys)})
    return content_hash(body)


# -- a support retirement: supported, about another subject (2026-09-28) ----


def _rereview_pending(connection: Any, claim_version_ref: str, newest: int) -> bool:
    """An earlier contract's verdict the current contract has not re-asked yet.

    ``claim_support_backfill.rereview_retirements`` asks every support
    retirement once under each new contract; until it has (a mark under its
    pass ref, whatever it says), the verdict in hand may be about to change,
    so nothing is decided on it.
    """

    from .claim_support_verification import CONTRACT_REF, contract_version

    if newest >= contract_version(CONTRACT_REF):
        return False
    from .claim_support_backfill import REREVIEW_PASS_REF

    try:
        row = connection.execute(
            "SELECT 1 FROM claim_support_backfill_marks WHERE claim_version_ref=? AND pass_ref=?",
            (claim_version_ref, REREVIEW_PASS_REF)).fetchone()
    except sqlite3.Error:
        return True
    return row is None


def about_other_evidence(
    connection: Any, *, claim_version_ref: str, claim: Mapping[str, Any],
    industry_ref: Any, roster: Mapping[str, Sequence[str]],
    defer_pending_rereview: bool = True,
) -> dict[str, Any]:
    """May a ``citation_support_rejected`` retirement be read as industry evidence?

    Everything before the original is read, from the recorded verdicts alone
    (``claim_support_verification.governing_verdicts`` -- the newest contract's
    verdicts bound to this exact claim version, re-hashed):

    * none -> ``no_governing_verdict``; an earlier contract's verdict the
      current contract has not re-asked -> ``deferred`` (not a refusal);
    * every governing verdict must be ``supported`` and ``about_other``: a
      verdict that the cited sentences do *not* support the Claim is never
      reattributed, whoever it is about;
    * every one's ``other_subject`` must be this mission's industry, never a
      company (``claim_industry_rule.other_subject_scope``); an umbrella
      ("AI trade / tech sector") only when the statement's main clause names
      the industry's own collective (``statement_names_industry``).

    The industry-level rule on the exact span is the caller's next step.
    """

    from .claim_support_verification import contract_version, governing_verdicts

    result: dict[str, Any] = {
        "rule_ref": ABOUT_OTHER_RULE_REF, "ok": False, "deferred": False, "refusal": None,
        "verdict_keys": [], "contract_ref": None, "other_subjects": [], "scope": None,
    }

    def refuse(reason: str) -> dict[str, Any]:
        result["refusal"] = reason
        return result

    verdicts = governing_verdicts(connection, claim_version_ref=claim_version_ref, claim=claim)
    result["verdict_keys"] = [verdict["item_key"] for verdict in verdicts]
    if not verdicts:
        return refuse("no_governing_verdict")
    result["contract_ref"] = verdicts[0].get("contract_ref")
    if defer_pending_rereview and _rereview_pending(
            connection, claim_version_ref, contract_version(result["contract_ref"])):
        result["deferred"] = True
        return refuse("awaiting_current_contract_rereview")
    for verdict in verdicts:
        if (verdict.get("support"), verdict.get("subject_relation")) != ("supported", "about_other"):
            return refuse(f"verdict_{verdict.get('support')}_{verdict.get('subject_relation')}")
    others = sorted({str(verdict.get("other_subject") or "") for verdict in verdicts})
    result["other_subjects"] = others
    scopes = []
    for other in others:
        scope = other_subject_scope(other or None, industry_ref=industry_ref, roster=roster)
        if not scope["ok"]:
            return refuse(scope["refusal"])
        scopes.append(scope["scope"])
    result["scope"] = "industry" if "industry" in scopes else "umbrella"
    if result["scope"] == "umbrella":
        named = statement_names_industry(claim.get("normalized_statement"), industry_ref)
        result["statement_industry_terms"] = named
        if not named:
            return refuse("umbrella_subject_without_industry_in_statement")
    result["ok"] = True
    return result


# -- the authority ---------------------------------------------------------


class ClaimIndustryReattributionAuthority:
    """Append-only industry reattributions over untouched retirements."""

    _authorized = authorized_flag()

    def __init__(self, store: DaltonStore, *, clock: Callable[[], str] | None = None) -> None:
        self.store = store
        self.connection = store.connection
        self.clock = clock or _now
        self._authorization_flag = authorization_flag(
            self.connection, "dalton_claim_reattribution_authorized")
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        if self._authorized:
            raise RuntimeError("ClaimIndustryReattributionAuthority operation cannot be nested")
        self._authorized = True
        try:
            with self.store._transaction() as cur:
                yield cur
        finally:
            self._authorized = False

    # -- reads ---------------------------------------------------------------

    def reattributions(self, *, industry_ref: str | None = None,
                       limit: int = 500) -> list[dict[str, Any]]:
        query = f"SELECT record_json, content_hash FROM {TABLE}"
        params: list[Any] = []
        if industry_ref is not None:
            query += " WHERE industry_ref=?"
            params.append(industry_ref)
        query += " ORDER BY created_at, reattribution_id LIMIT ?"
        params.append(int(limit))
        return [
            {**json.loads(row["record_json"]), "content_hash": row["content_hash"]}
            for row in self.connection.execute(query, params).fetchall()
        ]

    def reviews(self) -> dict[str, dict[str, Any]]:
        return {
            row["claim_version_ref"]: dict(row)
            for row in self.connection.execute(f"SELECT * FROM {REVIEW_TABLE}").fetchall()
        }

    # -- writes --------------------------------------------------------------

    def record_review(self, *, claim_version_ref: str, inputs_hash: str,
                      outcome: str, refusal: str | None = None) -> None:
        """Remember that the backfill judged this Claim (a cache, not authority)."""

        at = self.clock()
        with self._transaction() as cur:
            cur.execute(
                f"INSERT INTO {REVIEW_TABLE}(claim_version_ref,rule_ref,inputs_hash,outcome,"
                "refusal,attempts,reviewed_at) VALUES(?,?,?,?,?,1,?) "
                "ON CONFLICT(claim_version_ref) DO UPDATE SET rule_ref=excluded.rule_ref, "
                "inputs_hash=excluded.inputs_hash, outcome=excluded.outcome, "
                "refusal=excluded.refusal, "
                f"attempts={REVIEW_TABLE}.attempts+1, reviewed_at=excluded.reviewed_at",
                (claim_version_ref, RULE_REF, inputs_hash, outcome,
                 None if refusal is None else str(refusal)[:300], at),
            )

    def reattribute(
        self,
        *,
        claim_version_ref: str,
        actor_ref: str,
        rationale: str,
        industry_ref: str | None = None,
        decision_hash: str | None = None,
        cited_span: str | None = None,
        source_text: str | None = None,
        document_title: str | None = None,
        roster_aliases: Mapping[str, Sequence[str]] | None = None,
    ) -> dict[str, Any]:
        """Record that this retired Claim is evidence about its mission's industry.

        Nothing is edited: the Claim, its challenge and its retirement stay
        byte for byte.  Automation must pass the exact cited span (and may
        pass the whole original and the document title, which the rule's
        own-document test reads); the rule is re-run here on the Claim's own
        statement.  One record per claim version, so a repeat is
        ``duplicate``.
        """

        from .claim_retirement import reinstated_claim_version_refs

        claim_version_ref = _text(claim_version_ref, "claim_version_ref", maximum=512)
        rationale = _text(rationale, "rationale")
        actor = _actor(actor_ref)
        automated = _AUTOMATION_RE.fullmatch(actor) is not None
        row = self.connection.execute(
            "SELECT d.record_json AS decision_json, d.content_hash AS decision_hash, "
            "d.decision AS decision, c.record_json AS challenge_json, "
            "c.content_hash AS challenge_hash "
            "FROM claim_retirement_decisions d "
            "JOIN claim_retirement_challenges c ON c.challenge_id=d.challenge_ref "
            "WHERE d.claim_version_ref=?", (claim_version_ref,),
        ).fetchone() if _table_exists(self.connection, "claim_retirement_decisions") else None
        if row is None:
            raise ClaimReattributionNotFound("no retirement decision names this claim version")
        decision = json.loads(row["decision_json"])
        challenge = json.loads(row["challenge_json"])
        if (decision.get("content_hash") != row["decision_hash"]
                or challenge.get("content_hash") != row["challenge_hash"]):
            raise ClaimReattributionConflict("claim retirement authority drifted")
        if row["decision"] != "retired":
            raise ClaimReattributionConflict("only a retired claim version can be reattributed")
        if claim_version_ref in reinstated_claim_version_refs(self.connection):
            raise ClaimReattributionConflict(
                "this retirement was withdrawn; the Claim is its company's again")
        if decision_hash is not None and decision_hash != row["decision_hash"]:
            raise ClaimReattributionConflict("decision hash binding failed")
        if challenge.get("reason_code") == "boilerplate_disclaimer":
            raise ClaimReattributionConflict("a disclaimer is not a finding about anything")
        claim_row = self.connection.execute(
            "SELECT claim_json, content_hash FROM claim_versions WHERE claim_version_id=?",
            (claim_version_ref,),
        ).fetchone()
        if claim_row is None:
            raise ClaimReattributionNotFound("claim version was not found")
        claim = json.loads(claim_row["claim_json"])
        if claim.get("content_hash") != claim_row["content_hash"]:
            raise ClaimReattributionConflict("claim version authority drifted")
        subject_ref = str(claim.get("subject_ref") or "")
        mission = covering_missions(self.connection).get(subject_ref)
        if mission is None:
            raise ClaimReattributionConflict("no active mission covers this Claim's company")
        if mission["industry_ref"] is None:
            raise ClaimReattributionConflict("the covering mission names no industry")
        if industry_ref is not None and industry_ref != mission["industry_ref"]:
            raise ClaimReattributionConflict(
                "industry_ref must be the covering mission's own industry")
        verdict: dict[str, Any] | None = None
        about_other: dict[str, Any] | None = None
        roster = mission_roster(mission["universe"], roster_aliases)
        if automated:
            if not mission["granted"] or mission["principal"] != actor:
                raise ClaimReattributionConflict(
                    f"the mission does not grant {WRITE_SCOPE} to this automation principal")
            if challenge.get("reason_code") not in AUTOMATIC_RETIREMENT_REASONS:
                raise ClaimReattributionConflict(
                    "automation may only reattribute a subject-absent retirement or a support "
                    "retirement about another subject")
            if challenge.get("reason_code") == SUPPORT_REASON:
                # The recorded verdicts are re-read here, never taken from the caller.
                about_other = about_other_evidence(
                    self.connection, claim_version_ref=claim_version_ref, claim=claim,
                    industry_ref=mission["industry_ref"], roster=roster)
                if not about_other["ok"]:
                    raise ClaimReattributionConflict(
                        f"the support verdict does not make this industry evidence: "
                        f"{about_other['refusal']}")
            if cited_span is None:
                raise ClaimReattributionConflict(
                    "the cited span cannot be read; nothing is reattributed unverified")
            verdict = judge(
                statement=claim.get("normalized_statement"), cited_span=cited_span,
                industry_ref=mission["industry_ref"], roster=roster,
                document_title=document_title, source_text=source_text,
            )
            if not verdict["industry_level"]:
                raise ClaimReattributionConflict(
                    f"the industry-level rule does not fire: {verdict['refusal']}")
            reason_code, rule_ref = "industry_level_under_rule", RULE_REF
        else:
            reason_code, rule_ref = "human_judgment", None
        wire: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "claim_version_ref": claim_version_ref,
            "claim_version_hash": claim_row["content_hash"],
            "claim_ref": claim.get("claim_ref"),
            "decision_ref": decision["id"],
            "decision_hash": row["decision_hash"],
            "challenge_ref": challenge["id"],
            "retired_reason_code": challenge.get("reason_code"),
            "retired_detector_ref": challenge.get("detector_ref"),
            "from_subject_ref": subject_ref,
            "industry_ref": mission["industry_ref"],
            "mission_version_ref": mission["mission_version_ref"],
            "mission_hash": mission["mission_hash"],
            "reason_code": reason_code,
            "rule_ref": rule_ref,
            "rule_evidence": None if verdict is None else {
                **{key: verdict[key] for key in (
                    "form", "statement_companies", "collective_terms",
                    "industry_terms", "survey_source") if key in verdict},
                **({} if about_other is None else {"about_other": {
                    key: about_other[key] for key in (
                        "rule_ref", "verdict_keys", "contract_ref", "other_subjects",
                        "scope", "statement_industry_terms") if key in about_other}}),
            },
            "cited_span_sha256": None if cited_span is None
            else hashlib.sha256(cited_span.encode("utf-8")).hexdigest(),
            "actor_ref": actor,
            "rationale": rationale,
            "created_at": self.clock(),
        }
        wire["id"] = "claim-industry-reattribution:" + content_hash(
            {"claim": claim_version_ref})[:32]
        wire["content_hash"] = content_hash({k: v for k, v in wire.items() if k != "content_hash"})
        with self._transaction() as cur:
            existing = cur.execute(
                f"SELECT record_json FROM {TABLE} WHERE claim_version_ref=?",
                (claim_version_ref,),
            ).fetchone()
            if existing is not None:
                return {**json.loads(existing["record_json"]), "status": "duplicate"}
            cur.execute(
                f"INSERT INTO {TABLE}(reattribution_id,claim_version_ref,claim_version_hash,"
                "decision_ref,decision_hash,from_subject_ref,industry_ref,mission_version_ref,"
                "reason_code,rule_ref,actor_ref,rationale,record_json,content_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (wire["id"], claim_version_ref, wire["claim_version_hash"], wire["decision_ref"],
                 wire["decision_hash"], subject_ref, wire["industry_ref"],
                 wire["mission_version_ref"], reason_code, rule_ref, actor, rationale,
                 json.dumps(wire, ensure_ascii=False, sort_keys=True),
                 wire["content_hash"], wire["created_at"]),
            )
        return {**wire, "status": "fresh"}


    def withdraw(
        self,
        *,
        claim_version_ref: str,
        actor_ref: str,
        rationale: str,
        reattribution_hash: str | None = None,
        cited_span: str | None = None,
        source_text: str | None = None,
        document_title: str | None = None,
        issuer_document: bool = False,
        roster_aliases: Mapping[str, Sequence[str]] | None = None,
    ) -> dict[str, Any]:
        """Withdraw one reattribution by appending a record that names it.

        Nothing is edited: the reattribution row stays, byte for byte, and the
        withdrawal binds its id and hash; the Claim stays retired and is no
        longer the industry's.  A person may withdraw any reattribution
        (``human_judgment``).  Automation may withdraw only one it made under
        an earlier rule (``PRIOR_RULE_REFS``), and only when today's rule,
        re-run here on the exact cited span, refuses the Claim
        (``not_industry_level_under_current_rule``).  One per reattribution,
        so a repeat is ``duplicate``.
        """

        claim_version_ref = _text(claim_version_ref, "claim_version_ref", maximum=512)
        rationale = _text(rationale, "rationale")
        actor = _actor(actor_ref)
        automated = _AUTOMATION_RE.fullmatch(actor) is not None
        row = self.connection.execute(
            f"SELECT record_json, content_hash FROM {TABLE} WHERE claim_version_ref=?",
            (claim_version_ref,),
        ).fetchone()
        if row is None:
            raise ClaimReattributionNotFound("no reattribution names this claim version")
        record = json.loads(row["record_json"])
        if record.get("content_hash") != row["content_hash"]:
            raise ClaimReattributionConflict("claim reattribution authority drifted")
        if reattribution_hash is not None and reattribution_hash != row["content_hash"]:
            raise ClaimReattributionConflict("reattribution hash binding failed")
        verdict: dict[str, Any] | None = None
        if automated:
            mission = covering_missions(self.connection).get(str(record.get("from_subject_ref")))
            if mission is None or not mission["granted"] or mission["principal"] != actor:
                raise ClaimReattributionConflict(
                    f"the mission does not grant {WRITE_SCOPE} to this automation principal")
            automatic = record.get("reason_code") == "industry_level_under_rule"
            # 2026-09-28: one made on a support verdict is re-judged on today's
            # verdicts too -- the next contract's re-review may say otherwise.
            on_support = automatic and record.get("retired_reason_code") == SUPPORT_REASON
            if not automatic or not (record.get("rule_ref") in PRIOR_RULE_REFS or on_support):
                raise ClaimReattributionConflict(
                    "automation may only withdraw an automatic reattribution made under an "
                    "earlier rule or on a support verdict")
            claim_row = self.connection.execute(
                "SELECT claim_json, content_hash FROM claim_versions WHERE claim_version_id=?",
                (claim_version_ref,),
            ).fetchone()
            claim = {} if claim_row is None else {
                **json.loads(claim_row["claim_json"]), "content_hash": claim_row["content_hash"]}
            roster = mission_roster(mission["universe"], roster_aliases)
            about_other = None
            if on_support:
                about_other = about_other_evidence(
                    self.connection, claim_version_ref=claim_version_ref, claim=claim,
                    industry_ref=record.get("industry_ref"), roster=roster)
                if about_other["deferred"]:
                    raise ClaimReattributionConflict(
                        "the support verdict is awaiting the current contract's re-review")
            if about_other is not None and not about_other["ok"]:
                verdict = {"industry_level": False, "refusal": about_other["refusal"]}
            else:
                if cited_span is None:
                    raise ClaimReattributionConflict(
                        "the cited span cannot be read; nothing is withdrawn unverified")
                verdict = judge(
                    statement=claim.get("normalized_statement"), cited_span=cited_span,
                    industry_ref=record.get("industry_ref"), roster=roster,
                    document_title=document_title, source_text=source_text,
                    issuer_document=issuer_document,
                )
            if verdict["industry_level"]:
                raise ClaimReattributionConflict(
                    "today's industry-level rule still keeps this Claim")
            reason_code, rule_ref = "not_industry_level_under_current_rule", RULE_REF
        else:
            reason_code, rule_ref = "human_judgment", None
        wire: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "claim_version_ref": claim_version_ref,
            "reattribution_ref": record["id"],
            "reattribution_hash": row["content_hash"],
            "industry_ref": record.get("industry_ref"),
            "reattributed_rule_ref": record.get("rule_ref"),
            "reason_code": reason_code,
            "rule_ref": rule_ref,
            "refusal": None if verdict is None else verdict.get("refusal"),
            "cited_span_sha256": None if cited_span is None
            else hashlib.sha256(cited_span.encode("utf-8")).hexdigest(),
            "actor_ref": actor,
            "rationale": rationale,
            "created_at": self.clock(),
        }
        wire["id"] = "claim-industry-reattribution-withdrawal:" + content_hash(
            {"reattribution": record["id"]})[:32]
        wire["content_hash"] = content_hash({k: v for k, v in wire.items() if k != "content_hash"})
        with self._transaction() as cur:
            existing = cur.execute(
                f"SELECT record_json FROM {WITHDRAWAL_TABLE} WHERE reattribution_ref=?",
                (record["id"],),
            ).fetchone()
            if existing is not None:
                return {**json.loads(existing["record_json"]), "status": "duplicate"}
            cur.execute(
                f"INSERT INTO {WITHDRAWAL_TABLE}(withdrawal_id,claim_version_ref,"
                "reattribution_ref,reattribution_hash,reason_code,rule_ref,actor_ref,rationale,"
                "record_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (wire["id"], claim_version_ref, record["id"], row["content_hash"], reason_code,
                 rule_ref, actor, rationale, json.dumps(wire, ensure_ascii=False, sort_keys=True),
                 wire["content_hash"], wire["created_at"]),
            )
        return {**wire, "status": "fresh"}


# -- the backfill ------------------------------------------------------------


def retired_candidates(connection: Any) -> list[dict[str, Any]]:
    """Retired-now Claims automation may reattribute, none reattributed yet, oldest first.

    Subject-absent retirements, and (2026-09-28) support retirements; which
    support retirement qualifies is decided per Claim from its recorded
    verdicts (:func:`about_other_evidence`).
    """

    from .claim_retirement import retired_claim_version_refs

    retired = retired_claim_version_refs(connection)
    if not retired:
        return []
    done: set[str] = set()
    if _table_exists(connection, TABLE):
        done = {str(row[0]) for row in connection.execute(
            f"SELECT claim_version_ref FROM {TABLE}").fetchall()}
    marks = ",".join("?" for _ in AUTOMATIC_RETIREMENT_REASONS)
    rows = connection.execute(
        "SELECT c.claim_version_ref AS ref, c.subject_ref AS subject_ref, "
        "c.reason_code AS reason_code, d.content_hash AS decision_hash, "
        "v.claim_json AS claim_json, v.content_hash AS claim_hash "
        "FROM claim_retirement_challenges c "
        "JOIN claim_retirement_decisions d ON d.challenge_ref=c.challenge_id "
        "JOIN claim_versions v ON v.claim_version_id=c.claim_version_ref "
        f"WHERE d.decision='retired' AND c.reason_code IN ({marks}) "
        "ORDER BY d.created_at, c.claim_version_ref",
        AUTOMATIC_RETIREMENT_REASONS,
    ).fetchall()
    return [dict(row) for row in rows if row["ref"] in retired and row["ref"] not in done]


def _claim_of(row: Mapping[str, Any]) -> dict[str, Any]:
    claim = json.loads(row["claim_json"])
    if row.get("claim_hash") is not None:
        claim.setdefault("content_hash", row["claim_hash"])
    return claim


def run_backfill(
    driver: Any,
    *,
    authority: ClaimIndustryReattributionAuthority | None,
    principal: str | None,
    citations: Mapping[str, Mapping[str, Any]] | None = None,
    texts: dict[str, str | None] | None = None,
    max_documents: int = DEFAULT_MAX_DOCUMENTS,
    max_writes: int = DEFAULT_MAX_WRITES,
    dry_run: bool = False,
    show: int | None = None,
    defer_pending_rereview: bool = True,
    max_verdict_reviews: int = DEFAULT_MAX_VERDICT_REVIEWS,
) -> dict[str, Any]:
    """Judge retired Claims under the industry rule; append what it keeps.

    ``driver`` is a ``claim_review.ClaimReviewDriver`` (read-only is fine for
    a dry run): it resolves each Claim's citation chain and reads the exact
    original, re-hashed.  Bounded per tick (``max_documents`` originals read,
    ``max_writes`` records appended, ``max_verdict_reviews`` support
    retirements refused on their verdicts alone) and idempotent: a reattributed Claim
    leaves the candidate query, and a refused one is marked with the rule ref
    and the hash of the alias table it was judged with (and, for a support
    retirement, of its governing verdicts), and is looked at again only when
    one of them changes.  Without a principal (no grant) it marks refusals and
    reports what it would append; on ``dry_run`` it writes nothing at all;
    without an authority it does nothing unless dry-running.

    A support retirement (2026-09-28) is first judged on its recorded
    verdicts alone -- no original is read for one the verdict already
    refuses -- and one whose verdict the current contract has not re-asked
    yet is ``awaiting_rereview``: neither marked nor judged, looked at again
    next tick.  ``defer_pending_rereview=False`` (read-only simulations only)
    judges it on the verdict in hand, to show what the re-review would leave.
    """

    connection = driver.connection
    summary: dict[str, Any] = {
        "rule_ref": RULE_REF, "about_other_rule_ref": ABOUT_OTHER_RULE_REF,
        "candidates": 0, "examined": 0,
        "reattributed": [], "would_reattribute": [], "not_industry_level": 0,
        "refusals": {}, "no_industry": 0, "unreadable": 0, "deferred": 0,
        "awaiting_rereview": 0, "already_reviewed": 0, "skipped": [], "refused_examples": [],
        "by_reason": {},
    }
    try:
        candidates = retired_candidates(connection)
    except sqlite3.Error as exc:
        summary["skipped"].append({"reason": f"{type(exc).__name__}: {exc}"})
        return summary
    summary["candidates"] = len(candidates)
    for row in candidates:
        summary["by_reason"][row["reason_code"]] = summary["by_reason"].get(row["reason_code"], 0) + 1
    if not candidates:
        return summary
    if authority is None and not dry_run:
        # A driver built without the authority (older wiring, a test) has no
        # memory of what it judged, so it would re-read the same originals
        # every tick for a report nobody asked for.
        summary["skipped"].append({"reason": "no reattribution authority on this driver"})
        return summary
    missions = covering_missions(connection)
    # Remembering a refusal is a read's cache, written with or without the
    # grant (the patrol's examination markers are too); appending a
    # reattribution needs the grant.
    can_mark = not dry_run and authority is not None
    writable = can_mark and principal is not None
    markers = authority.reviews() if authority is not None else {}
    extra = dict(getattr(driver, "needles", {}) or {})
    texts = {} if texts is None else texts
    read_here = written = verdict_reviews = 0
    chain_read = False  # the citation chain is read once, and only if needed

    def refused(row: Mapping[str, Any], item: dict[str, Any], digest_inputs: str,
                refusal: str) -> None:
        summary["not_industry_level"] += 1
        key = str(refusal).split(":", 1)[0]
        summary["refusals"][key] = summary["refusals"].get(key, 0) + 1
        if show is None or len(summary["refused_examples"]) < show:
            summary["refused_examples"].append({**item, "refusal": refusal})
        if can_mark:
            authority.record_review(claim_version_ref=row["ref"], inputs_hash=digest_inputs,
                                    outcome="not_industry_level", refusal=refusal)

    for row in candidates:
        mission = missions.get(row["subject_ref"])
        industry = None if mission is None else mission["industry_ref"]
        roster = {} if mission is None else mission_roster(mission["universe"], extra)
        on_support = row["reason_code"] == SUPPORT_REASON
        claim = _claim_of(row)
        about_other: dict[str, Any] | None = None
        if on_support and industry is not None:
            about_other = about_other_evidence(
                connection, claim_version_ref=row["ref"], claim=claim, industry_ref=industry,
                roster=roster, defer_pending_rereview=defer_pending_rereview)
        digest_inputs = inputs_hash(
            industry, roster,
            None if not on_support else (about_other or {}).get("verdict_keys", []))
        marker = markers.get(row["ref"])
        if (marker is not None and marker["rule_ref"] == RULE_REF
                and marker["inputs_hash"] == digest_inputs
                and marker["outcome"] in ("not_industry_level", "no_industry")):
            summary["already_reviewed"] += 1
            continue
        if industry is None:
            summary["no_industry"] += 1
            if can_mark:
                authority.record_review(claim_version_ref=row["ref"],
                                        inputs_hash=digest_inputs, outcome="no_industry")
            continue
        item = {"claim_version_ref": row["ref"], "subject_ref": row["subject_ref"],
                "industry_ref": industry, "retired_reason_code": row["reason_code"],
                "statement": str(claim.get("normalized_statement") or "")[:240]}
        if about_other is not None:
            item.update({"other_subjects": about_other["other_subjects"],
                         "verdict_contract_ref": about_other["contract_ref"],
                         "about_other_scope": about_other["scope"]})
            if about_other["deferred"]:
                summary["awaiting_rereview"] += 1
                continue
            if not about_other["ok"]:
                # Decided on the recorded verdicts; no original is read for it.
                if can_mark and verdict_reviews >= max(1, int(max_verdict_reviews)):
                    summary["deferred"] += 1
                    continue
                verdict_reviews += 1
                summary["examined"] += 1
                refused(row, item, digest_inputs, about_other["refusal"])
                continue
        if writable and written >= max(0, int(max_writes)):
            summary["deferred"] += 1
            continue
        if not chain_read and (citations is None or row["ref"] not in citations):
            citations, chain_read = {**driver._citations(), **(citations or {})}, True
        citation = (citations or {}).get(row["ref"])
        text = None
        if citation is not None:
            digest = citation["digest"]
            if digest not in texts:
                if read_here >= max(1, int(max_documents)):
                    summary["deferred"] += 1
                    continue
                read_here += 1
                texts[digest] = driver.source_text(digest)
            text = texts[digest]
        summary["examined"] += 1
        span = None
        if text is not None and citation is not None:
            start, end = citation.get("start"), citation.get("end")
            if isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(text):
                span = text[start:end]
        if span is None:
            summary["unreadable"] += 1
            if can_mark:
                authority.record_review(claim_version_ref=row["ref"],
                                        inputs_hash=digest_inputs, outcome="unreadable")
            continue
        facts = driver._document_facts(citation.get("document_ref"))
        verdict = judge(
            statement=claim.get("normalized_statement"), cited_span=span,
            industry_ref=industry, roster=roster, document_title=facts.get("title"),
            source_text=text, issuer_document=bool(facts.get("issuer_document")),
        )
        item.update({"document_ref": citation.get("document_ref"),
                     "form": verdict.get("form"), "survey_source": verdict.get("survey_source")})
        if not verdict["industry_level"]:
            refused(row, item, digest_inputs, verdict["refusal"])
            continue
        if not writable:
            summary["would_reattribute"].append(item)
            continue
        rationale = (
            f"按行业层面规则（{RULE_REF}）判定："
            "陈述不指向任何单一公司，谈的是整个行业；在公司口径下仍然退役，"
            "作为行业证据保留。")
        if on_support:
            rationale = (
                f"独立核验认定所引原文支持这条结论，但它讲的是"
                f"{'、'.join(about_other['other_subjects'])}而不是所挂公司"
                f"（{about_other['contract_ref']}）；该主体属于本任务的行业"
                f"（{ABOUT_OTHER_RULE_REF}），且按行业层面规则（{RULE_REF}）判定陈述"
                "不指向任何单一公司。在公司口径下仍然退役，作为行业证据保留。")
        try:
            record = authority.reattribute(
                claim_version_ref=row["ref"], actor_ref=principal,
                decision_hash=row["decision_hash"], industry_ref=industry,
                rationale=rationale,
                cited_span=span, source_text=text, document_title=facts.get("title"),
                roster_aliases=extra,
            )
        except ClaimReattributionError as exc:
            summary["skipped"].append({"claim_version_ref": row["ref"], "reason": str(exc)})
            continue
        written += 1
        authority.record_review(claim_version_ref=row["ref"], inputs_hash=digest_inputs,
                                outcome="reattributed")
        summary["reattributed"].append({**item, "reattribution_ref": record["id"],
                                        "status": record["status"]})
    return summary


def reattributions_to_recheck(connection: Any) -> list[dict[str, Any]]:
    """Standing automatic reattributions today's rules may disagree with.

    Those made under an earlier industry rule, and (2026-09-28) those made on
    a support verdict -- a later contract's re-review may answer otherwise.
    """

    if not _table_exists(connection, TABLE):
        return []
    withdrawn = withdrawn_reattribution_refs(connection)
    rows = connection.execute(
        f"SELECT r.reattribution_id AS id, r.claim_version_ref AS ref, "
        "r.content_hash AS reattribution_hash, r.rule_ref AS rule_ref, "
        "r.from_subject_ref AS subject_ref, r.industry_ref AS industry_ref, "
        "json_extract(r.record_json, '$.retired_reason_code') AS reason_code, "
        f"v.claim_json AS claim_json, v.content_hash AS claim_hash FROM {TABLE} r "
        "JOIN claim_versions v ON v.claim_version_id=r.claim_version_ref "
        "WHERE r.reason_code='industry_level_under_rule' "
        "ORDER BY r.created_at, r.claim_version_ref"
    ).fetchall()
    return [dict(row) for row in rows
            if (row["rule_ref"] in PRIOR_RULE_REFS or row["reason_code"] == SUPPORT_REASON)
            and row["id"] not in withdrawn]


def run_recheck(
    driver: Any,
    *,
    authority: ClaimIndustryReattributionAuthority | None,
    principal: str | None,
    citations: Mapping[str, Mapping[str, Any]] | None = None,
    texts: dict[str, str | None] | None = None,
    max_documents: int = DEFAULT_MAX_DOCUMENTS,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Re-judge standing automatic reattributions today's rules may refuse (2026-09-25b).

    Each one made under an earlier rule, or on a support verdict, is read
    again against its exact original under today's rule (and today's
    governing verdicts); when it is refused, the automation principal appends
    a withdrawal (the authority re-runs the check) and the Claim is simply
    retired again.  One whose verdict awaits the current contract's re-review
    is left for a later tick.  Without a principal, without an authority or
    on ``dry_run`` it reports only.  Bounded (``max_documents`` originals per
    tick) and idempotent: a withdrawn one leaves the query; one today's rules
    keep is marked in the review cache under today's rule and inputs and is
    not read again until either changes.
    """

    connection = driver.connection
    summary: dict[str, Any] = {
        "rule_ref": RULE_REF, "candidates": 0, "examined": 0, "withdrawn": [],
        "would_withdraw": [], "confirmed": 0, "unreadable": 0, "deferred": 0,
        "awaiting_rereview": 0, "already_reviewed": 0, "skipped": [],
    }
    try:
        candidates = reattributions_to_recheck(connection)
    except sqlite3.Error as exc:
        summary["skipped"].append({"reason": f"{type(exc).__name__}: {exc}"})
        return summary
    summary["candidates"] = len(candidates)
    if not candidates:
        return summary
    missions = covering_missions(connection)
    markers = authority.reviews() if authority is not None else {}
    texts = {} if texts is None else texts
    extra = dict(getattr(driver, "needles", {}) or {})
    writable = not dry_run and authority is not None and principal is not None
    read_here = 0
    chain_read = False
    for row in candidates:
        mission = missions.get(row["subject_ref"])
        roster = {} if mission is None else mission_roster(mission["universe"], extra)
        claim = _claim_of(row)
        on_support = row["reason_code"] == SUPPORT_REASON
        about_other = None
        if on_support:
            about_other = about_other_evidence(
                connection, claim_version_ref=row["ref"], claim=claim,
                industry_ref=row["industry_ref"], roster=roster)
            if about_other["deferred"]:
                summary["awaiting_rereview"] += 1
                continue
        digest_inputs = inputs_hash(row["industry_ref"], roster,
                                    None if about_other is None else about_other["verdict_keys"])
        marker = markers.get(row["ref"])
        if (marker is not None and marker["rule_ref"] == RULE_REF
                and marker["inputs_hash"] == digest_inputs
                and marker["outcome"] == "reattributed"):
            summary["already_reviewed"] += 1
            continue
        span = text = citation = None
        if about_other is not None and not about_other["ok"]:
            summary["examined"] += 1
            verdict = {"industry_level": False, "refusal": about_other["refusal"]}
        else:
            if not chain_read and (citations is None or row["ref"] not in citations):
                citations, chain_read = {**driver._citations(), **(citations or {})}, True
            citation = (citations or {}).get(row["ref"])
            if citation is not None:
                digest = citation["digest"]
                if digest not in texts:
                    if read_here >= max(1, int(max_documents)):
                        summary["deferred"] += 1
                        continue
                    read_here += 1
                    texts[digest] = driver.source_text(digest)
                text = texts[digest]
            summary["examined"] += 1
            if text is not None and citation is not None:
                start, end = citation.get("start"), citation.get("end")
                if isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(text):
                    span = text[start:end]
            if span is None:
                summary["unreadable"] += 1
                continue
            facts = driver._document_facts(citation.get("document_ref"))
            verdict = judge(
                statement=claim.get("normalized_statement"), cited_span=span,
                industry_ref=row["industry_ref"], roster=roster,
                document_title=facts.get("title"), source_text=text,
                issuer_document=bool(facts.get("issuer_document")),
            )
        if verdict["industry_level"]:
            summary["confirmed"] += 1
            if not dry_run and authority is not None:
                authority.record_review(claim_version_ref=row["ref"],
                                        inputs_hash=digest_inputs, outcome="reattributed")
            continue
        item = {"claim_version_ref": row["ref"], "subject_ref": row["subject_ref"],
                "industry_ref": row["industry_ref"], "refusal": verdict["refusal"],
                "retired_reason_code": row["reason_code"],
                "statement": str(claim.get("normalized_statement") or "")[:240]}
        if not writable:
            summary["would_withdraw"].append(item)
            continue
        facts = {} if citation is None else driver._document_facts(citation.get("document_ref"))
        if on_support:
            rationale = (f"按今天的核验结论与行业规则重判：{verdict['refusal']}。"
                         "这条结论不再作为行业证据；在公司口径下仍然退役。")
        else:
            rationale = (f"按行业层面规则 {RULE_REF} 重判：{verdict['refusal']}。"
                         "单独出现的 capex 等支出词不再算作本行业证据；撤回行业改挂，"
                         "该结论仍按公司口径退役。")
        try:
            record = authority.withdraw(
                claim_version_ref=row["ref"], actor_ref=principal,
                reattribution_hash=row["reattribution_hash"], rationale=rationale,
                cited_span=span, source_text=text, document_title=facts.get("title"),
                issuer_document=bool(facts.get("issuer_document")), roster_aliases=extra,
            )
        except ClaimReattributionError as exc:
            summary["skipped"].append({"claim_version_ref": row["ref"], "reason": str(exc)})
            continue
        summary["withdrawn"].append({**item, "withdrawal_ref": record["id"],
                                     "status": record["status"]})
    return summary


__all__ = [
    "ABOUT_OTHER_RULE_REF",
    "AUTOMATIC_RETIREMENT_REASONS",
    "ClaimIndustryReattributionAuthority",
    "SUPPORT_REASON",
    "about_other_evidence",
    "ClaimReattributionConflict",
    "ClaimReattributionError",
    "ClaimReattributionNotFound",
    "ClaimReattributionValidationError",
    "DEFAULT_MAX_DOCUMENTS",
    "DEFAULT_MAX_VERDICT_REVIEWS",
    "DEFAULT_MAX_WRITES",
    "REASON_CODES",
    "RULE_REF",
    "WITHDRAWAL_TABLE",
    "reattributions_to_recheck",
    "run_recheck",
    "withdrawn_reattribution_refs",
    "WRITE_SCOPE",
    "covering_missions",
    "industry_reattributions",
    "inputs_hash",
    "mission_roster",
    "reattributed_claim_version_refs",
    "reattribution_state_probe",
    "retired_candidates",
    "run_backfill",
]
