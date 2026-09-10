"""Coverage mission authority: the task-layer object above the planner.

A CoverageMission is what a human hands the research OS ("establish first
coverage of US IT services").  It freezes the industry, the company universe
with tiers, the research questions, the expected deliverables, an honest
source plan (which connectors are wired, which are not), exact bindings to
one ResearchPlaybook, one ResearchConstitution and one active Mandate, the
autonomy grant for the automation principal that will execute it, and a
budget.  Publishing a mission is human-only and append-only.

Stage records are the append-only ledger of how each company in the
universe moves through the playbook's frozen stage order.  The rules that the
manual states in prose become checks here: a company enters stage k only
after passing the gate of stage k-1; passing a human-checkpoint gate (Deep
Insight Gate, Investment Memo) requires a ``human:`` actor; an automation
actor must be the mission's declared principal and hold the ``stage_record``
write scope; ``gate_passed`` always needs evidence refs.  Nothing here writes
Evidence, Claims, Theses or models.

P14-S makes those rules version-proof.  A stage state is a fact about
``(mission_ref, company_ref)`` that carries forward across mission versions
until something supersedes it; the mission version a record binds is
provenance, not scope.  ``current_stage_state`` and ``companies_at_or_past``
fold every version's records by time, ``record_stage`` validates the ladder
against that fold, and the record it writes still binds the *active* version
so the provenance stays exact.  Read ``fold_stage_status`` for the ordering
rules (see the ADR-0008 addendum).

Phase 9b adds a second append-only ledger beside stage transitions.  A
``coverage_mission_stage_claim`` binds every automation-created formal Claim
and its supporting Evidence to the exact mission version and the company's
current playbook stage.  It does not pass a gate or create a Claim; the SEC
lane records the pair only after the policy-authorized formal write exists.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .research_playbook import (
    STAGE_ORDER,
    ResearchPlaybookError,
    ResearchPlaybookNotFound,
    read_exact_playbook_version,
)
from .store import (
    DaltonStore, authorization_flag, authorized_flag, canonical_json, content_hash,
)


SCHEMA_VERSION = "0.1"
_SCHEMA_PATH = Path(__file__).with_name("coverage_mission_schema.sql")
# The lane ticket status that means the run did what it was dispatched to do.
# Every other terminal status -- failed, orphaned, anything new -- is a run
# that produced nothing, and is counted as such rather than assumed benign.
SEC_RUN_SUCCEEDED = "succeeded"
# A failure reason is a diagnostic, not evidence; it is bounded so a runaway
# traceback cannot become the largest thing in the ledger.
MAX_FAILURE_REASON_CHARS = 500

# P13ak: bounds on one statements ingest. A 10-Q parses to a few hundred lines
# across three statements; EPAM's came to 495. These are ceilings that refuse a
# runaway parse, not expectations.
MAX_STATEMENT_FILINGS = 8
MAX_STATEMENT_LINES = 4000
# How many times one company's statements run may be re-identified as a
# fresh attempt. The dispatcher decides when to stop; this bounds the field.
MAX_STATEMENT_ATTEMPTS = 3


def sec_run_failure_reason(summary: Any) -> str | None:
    """Why a SEC lane run failed, from the summary it left behind.

    The ticket status says ``failed``; the reason lives in the run summary's
    per-issuer error.  Seventy-three runs said ``failed`` and every one of them
    had died on the same connector-profile conflict -- one string that would
    have named the outage on the first occurrence.
    """

    if not isinstance(summary, Mapping):
        return None
    for issuer in summary.get("issuers") or ():
        if not isinstance(issuer, Mapping):
            continue
        error = issuer.get("error")
        if isinstance(error, str) and error.strip():
            return error.strip()[:MAX_FAILURE_REASON_CHARS]
    error = summary.get("error")
    if isinstance(error, str) and error.strip():
        return error.strip()[:MAX_FAILURE_REASON_CHARS]
    return None
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_HUMAN_RE = re.compile(r"^human:[A-Za-z0-9][A-Za-z0-9._/@:-]*$")
_AUTOMATION_RE = re.compile(r"^automation:[A-Za-z0-9][A-Za-z0-9._/@:-]*$")
_ACCESSION_RE = re.compile(r"^[0-9]{10}-[0-9]{2}-[0-9]{6}$")

COVERAGE_TIERS: tuple[str, ...] = ("A", "B", "C")
BOOTSTRAP_PRIORITIES: tuple[str, ...] = ("P0", "P1", "P2")
DELIVERABLE_KINDS: tuple[str, ...] = (
    "industry_framework",
    "initial_screen",
    "industry_model",
    "company_model",
    "forecast_lines",
    "investment_memo",
    "weekly_brief",
)
SOURCE_STATUSES: tuple[str, ...] = ("connected", "probe_only", "not_connected")
# Objects an automation principal may be granted to write inside a mission.
# Theses, constitutions, playbooks, missions, mandates and governance policy
# are never in this list: they stay human-only by construction.
AUTOMATION_WRITE_SCOPES: tuple[str, ...] = (
    "evidence",
    "claim",
    # P10b: challenge and retire a Claim the detectors prove wrong.  The Ledger
    # is never edited; the scope only lets automation append the correction.
    "claim_challenge",
    # P10c: write the mission's own documents (Initial Screen and the rest of
    # the Playbook's deliverables), with every figure bound to a Claim.
    "deliverable",
    "forecast_line",
    "model_run",
    "research_question",
    "observation",
    "stage_record",
    # P9c: derived forecast-vs-actual outcome records.  Appended to the
    # vocabulary; existing missions that do not list it stay read-only here.
    "forecast_reconciliation",
    # P9d-1: search-driven source discovery records (which documents a
    # connected library holds for a covered company) and the budgeted
    # acquisition of the documents they name.  Never Evidence or Claims.
    "source_discovery",
    # P14-0 (blueprint line G): the words the analyst layers about to be built
    # will need, added in one go so that a mission version can be published
    # once rather than once per slice. No consumer exists for any of them yet
    # and the live mission grants none; a scope in this tuple is a scope a
    # mission *may* grant, not one it has.
    #
    # Market layer: a day's OHLCV / share count / market capitalisation from a
    # priced source, the consensus a broker or a free analyst field reports,
    # the derived multiples and their history, and the dated corporate events
    # a price move has to be read against.
    "market_price",
    "consensus_estimate",
    "valuation",
    "market_event",
    # Reading layer: the ten-section company dossier and the map of what the
    # market disagrees about, both assembled from Claims rather than authored.
    "dossier",
    "debate_map",
    # Evolution layer: automation may *propose* that a forecast line or a
    # thesis should change, and may open a research task for itself. It may
    # never accept any of them -- that is the human checkpoint below, and it
    # is the whole point of separating the proposal from the decision.
    "forecast_revision_proposal",
    "thesis_revision_candidate",
    "research_task",
    # The five-word decision at the end of an Active Coverage event, recorded
    # as its own object so a call can be looked up rather than inferred.
    "conviction_call",
    # P12b: the claim index is a projection over Claims (aspect, as_of,
    # importance, dedupe group); tagging writes index entries, never Claims.
    "claim_index",
)
DISCOVERY_DISPATCH_STATUSES: tuple[str, ...] = ("launched", "succeeded", "failed", "rejected")
# Sources a mission may run search-driven discovery against, and the Core
# connector authority each one leaves behind.  ``source_ref`` is the mission
# source-plan key; ``connector_source_ref`` / ``operation`` are what the
# search's SourceEnvelope must carry; ``document_ref_prefix`` is the only
# shape a discovered document ref may take.  A source outside this table is
# never a discovery source, whatever its source-plan status says.
DISCOVERY_SOURCES: Mapping[str, Mapping[str, str]] = MappingProxyType({
    "source:alphaengine": MappingProxyType({
        "connector_source_ref": "source:alphaengine",
        "operation": "search_library",
        "document_ref_prefix": "alphaengine-doc:",
    }),
    # P9d-4a: Gemini web search.  Results are opaque URL refs derived from
    # citations; the page itself only enters authority through fetch_get.
    "source:web-search": MappingProxyType({
        "connector_source_ref": "source:public-web",
        "operation": "search_web",
        "document_ref_prefix": "public-web-url:sha256:",
    }),
    # P10s: the SEC filings index. A discovered document is the filing the
    # envelope actually returned, so it carries the filing's own ref. The URL
    # is a derived locator, rebuilt from the same raw bytes when the filing is
    # fetched -- the same way a web search document recovers its URL from the
    # envelope that cited it. Naming the queue rows by URL instead would have
    # broken the binding that every queued document is one record the source
    # returned, in order.
    "source:sec-edgar": MappingProxyType({
        "connector_source_ref": "source:sec-edgar",
        "operation": "list_filings",
        "document_ref_prefix": "sec:filing:",
    }),
    # S2: Guidepoint 专家访谈库。发现的单位是问答摘录，不是访谈稿——
    # 上游没有读全文的 op（见 guidepoint-get-transcript-narrowing-v1）。
    "source:guidepoint": MappingProxyType({
        "connector_source_ref": "source:guidepoint",
        "operation": "search_library",
        "document_ref_prefix": "guidepoint-excerpt:",
    }),
    # S1: local human / vendor feeds.  For a local feed the acquisition is the
    # discovery, so the discovery operation is the read itself and one record
    # names exactly the one document its envelope carries.
    "source:sales-notes": MappingProxyType({
        "connector_source_ref": "source:sales-notes",
        "operation": "get_note",
        "document_ref_prefix": "sales-note:",
    }),
    "source:company-wiki": MappingProxyType({
        "connector_source_ref": "source:company-wiki",
        "operation": "get_document",
        "document_ref_prefix": "company-wiki-doc:sha256:",
    }),
    # P16: the fund's own prior research (old screens, memos, models) filed
    # under a declared directory; a governed internal_prior source, not truth.
    "source:prior-research": MappingProxyType({
        "connector_source_ref": "source:prior-research",
        "operation": "get_document",
        "document_ref_prefix": "prior-research-doc:sha256:",
    }),
})
DISCOVERED_DOCUMENT_STATUSES: tuple[str, ...] = (
    "discovered", "already_in_authority", "acquisition_launched", "acquired",
    "acquisition_failed",
)
CHECKPOINT_KINDS: tuple[str, ...] = (
    "deep_insight_gate",
    "investment_memo",
    "thesis_admission",
    "thesis_revision",
    "forecast_overturn",
    "scope_expansion",
    "budget_expansion",
    # P14-0 (blueprint line G): the decisions the evolution layer will hand
    # back. A thesis revision candidate and a conviction call are automation's
    # proposals and a person's decisions (ADR-0007, proposed); reopening a
    # gate a company has already passed is the one way a terminal
    # ``gate_passed`` can move, and it is deliberately a human checkpoint
    # rather than a rule. No consumer yet; words only.
    "thesis_revision_candidate",
    "conviction_call",
    "gate_reopen",
)
# Checkpoints a mission can never drop, in addition to the playbook's
# human-checkpoint stages.
REQUIRED_CHECKPOINTS: frozenset[str] = frozenset({
    "thesis_admission", "thesis_revision", "scope_expansion", "budget_expansion",
})
STAGE_STATUSES: tuple[str, ...] = ("entered", "gate_passed", "gate_failed")
# The two statuses that decide a stage.  ``entered`` opens it; these close it.
STAGE_DECISIONS: frozenset[str] = frozenset({"gate_passed", "gate_failed"})
# P14d sequel: the fourth thing a folded stage can be, and the only one that is
# not a ``coverage_mission_stage_records`` status.  A reopen is its own record
# in its own ledger (``coverage_mission_stage_reopens``) because the three
# statuses were right and the live rows should keep their hashes; what was
# missing was a way to say that a person un-decided a decided gate.  It is a
# *folded* status only -- ``record_stage`` still takes one of three.
STAGE_REOPENED: str = "reopened"
# Every value ``fold_stage_status`` can return, for the readers that label it.
FOLDED_STAGE_STATUSES: tuple[str, ...] = (*STAGE_STATUSES, STAGE_REOPENED)


def fold_stage_status(statuses: Sequence[str]) -> str | None:
    """The one status a stage is in, given every record ever written for it.

    P14-S (ADR-0008 addendum).  A stage state is a fact about
    ``(mission_ref, company_ref, stage_ref)``; the mission version a record
    binds is *provenance*, not scope.  The ordering rules, in full:

    - Records fold in time order across **every** version of the mission_ref.
    - The last **decision** wins.  A later ``gate_failed`` supersedes an
      earlier ``gate_passed`` -- that is how a reopened gate is expressed --
      and a later ``gate_passed`` supersedes an earlier ``gate_failed``,
      which is how the ordinary retry has always worked inside one version.
    - ``entered`` never supersedes a decision.  Publishing a new mission
      version and re-seeding ``entered`` under it therefore cannot walk a
      passed gate backwards; a stage that has been decided stays decided.
    - A ``reopened`` marker supersedes the decision before it, and is itself
      superseded by the decision after it.  A company between the two is at
      the stage, ``reopened``: its screen was passed, a person approved
      re-opening it, and the re-issued one has not been decided yet.  This is
      the one status that does not come from a stage record; see
      ``record_stage_reopen``.
    - No record at all means the stage was never reached: ``None``.
    """

    state: str | None = None
    for status in statuses:
        if status in STAGE_DECISIONS or status == STAGE_REOPENED:
            state = status
        elif state is None:
            # ``entered`` opens a stage and never re-opens one: after a
            # decision or a reopen it is bookkeeping, not a state change.
            state = "entered"
    return state


_BINDING_FIELDS = frozenset({"playbook_version", "constitution_version", "mandate_version"})
_BODY_FIELDS = frozenset({
    "title", "objective", "industry_ref", "universe", "research_questions",
    "deliverables", "source_plan", "bindings", "autonomy", "budget",
})
_VERSION_FIELDS = _BODY_FIELDS | frozenset({
    "schema_version", "id", "created_at", "mission_ref", "version",
    "prior_version_ref", "actor_ref", "content_hash",
})
_STAGE_RECORD_FIELDS = frozenset({
    "schema_version", "id", "created_at", "mission_version_ref", "mission_version_hash",
    "company_ref", "stage_ref", "status", "evidence_refs", "rationale", "actor_ref",
    "content_hash",
})
_STAGE_REOPEN_FIELDS = frozenset({
    "schema_version", "id", "created_at", "status", "mission_version_ref",
    "mission_version_hash", "company_ref", "stage_ref", "reopen_decision_ref",
    "reopen_proposal_ref", "reopened_version_ref", "rationale", "actor_ref",
    "content_hash",
})
_STAGE_CLAIM_FIELDS = frozenset({
    "schema_version", "id", "created_at", "mission_version_ref",
    "mission_version_hash", "company_ref", "stage_ref", "claim_version_ref",
    "claim_version_hash", "evidence_version_ref", "evidence_version_hash",
    "source_location", "actor_ref", "content_hash",
})
_SOURCE_DISCOVERY_FIELDS = frozenset({
    "schema_version", "id", "created_at", "mission_version_ref",
    "mission_version_hash", "company_ref", "source_ref", "discovery_plan_ref",
    "discovery_plan_hash", "spec_ref", "query_hash", "parameters",
    "connector_invocation_ref", "connector_invocation_hash",
    "source_envelope_ref", "source_envelope_hash", "document_refs",
    "new_document_refs", "in_authority_document_refs", "actor_ref",
    "requested_by", "content_hash",
})
_DISCOVERY_AUTHORIZATION_FIELDS = frozenset({
    "mission_version_ref", "mission_version_hash", "mission_ref", "company_ref",
    "ticker", "source_ref", "actor_ref", "requested_by", "scope",
    "max_alphaengine_calls_24h",
})


# P11w: the check every stored figure has passed -- its digits and its
# as-reported label were both found in the exact quote it cites.
DOCUMENT_FIGURE_VERIFIER_REF = "verifier:document-figure-citation-digits:0.1"


def _period_key(value: Any) -> str:
    """The period as an identity, not as the document happened to spell it.

    "Fiscal 2025" and "fiscal 2025" are the same period, and treating them as
    two let one 10-K record the same $69.7B of revenue twice. Case and spacing
    are presentation; the period is what it says.
    """

    return re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")


class CoverageMissionError(RuntimeError):
    """Base error for the coverage mission authority."""


class CoverageMissionValidationError(CoverageMissionError):
    """A request does not satisfy the closed contract."""


class CoverageMissionConflict(CoverageMissionError):
    """A request conflicts with immutable authority or stage order."""


class CoverageMissionNotFound(CoverageMissionError):
    """A bound authority, mission version or pointer is absent."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CoverageMissionValidationError(f"{name} must be non-empty text")
    return value.strip()


def _human(value: Any, name: str = "actor_ref") -> str:
    value = _text(value, name)
    if _HUMAN_RE.fullmatch(value) is None:
        raise CoverageMissionValidationError(f"{name} must use the human: namespace")
    return value


def _actor(value: Any, name: str = "actor_ref") -> str:
    value = _text(value, name)
    if _HUMAN_RE.fullmatch(value) is None and _AUTOMATION_RE.fullmatch(value) is None:
        raise CoverageMissionValidationError(f"{name} must use the human: or automation: namespace")
    return value


def _sha256(value: Any, name: str) -> str:
    value = _text(value, name)
    if _SHA256_RE.fullmatch(value) is None:
        raise CoverageMissionValidationError(f"{name} must be a lowercase SHA-256")
    return value


def _texts(value: Any, name: str, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list):
        raise CoverageMissionValidationError(f"{name} must be an array")
    result = [_text(item, f"{name}[]") for item in value]
    if nonempty and not result:
        raise CoverageMissionValidationError(f"{name} must not be empty")
    if len(set(result)) != len(result):
        raise CoverageMissionValidationError(f"{name} must contain unique values")
    return result


def _closed(value: Any, fields: frozenset[str], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CoverageMissionValidationError(f"{name} must be an object")
    result = dict(value)
    if set(result) != fields:
        raise CoverageMissionValidationError(
            f"{name} has an invalid closed shape; missing={sorted(fields - set(result))}, "
            f"unknown={sorted(set(result) - fields)}"
        )
    return result


def _binding(value: Any, name: str) -> dict[str, str]:
    obj = _closed(value, frozenset({"ref", "hash"}), name)
    return {"ref": _text(obj["ref"], f"{name}.ref"), "hash": _sha256(obj["hash"], f"{name}.hash")}


def _non_negative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CoverageMissionValidationError(f"{name} must be a non-negative integer")
    return value


def _non_negative_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise CoverageMissionValidationError(f"{name} must be a non-negative number")
    return float(value)


def _vocabulary(value: Any, allowed: tuple[str, ...], name: str) -> str:
    value = _text(value, name)
    if value not in allowed:
        raise CoverageMissionValidationError(f"{name} must be one of {list(allowed)}")
    return value


def validate_mission_body(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the human-authored mission body without touching the store."""

    body = _closed(value, _BODY_FIELDS, "mission")
    body["title"] = _text(body["title"], "title")
    body["objective"] = _text(body["objective"], "objective")
    body["industry_ref"] = _text(body["industry_ref"], "industry_ref")

    if not isinstance(body["universe"], list) or not body["universe"]:
        raise CoverageMissionValidationError("universe must be a non-empty array")
    universe: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in body["universe"]:
        member = _closed(
            raw, frozenset({"company_ref", "ticker", "coverage_tier", "bootstrap_priority"}), "universe[]"
        )
        company_ref = _text(member["company_ref"], "universe[].company_ref")
        if company_ref in seen:
            raise CoverageMissionValidationError("universe company_ref must be unique")
        seen.add(company_ref)
        universe.append({
            "company_ref": company_ref,
            "ticker": _text(member["ticker"], "universe[].ticker"),
            "coverage_tier": _vocabulary(member["coverage_tier"], COVERAGE_TIERS, "universe[].coverage_tier"),
            "bootstrap_priority": _vocabulary(
                member["bootstrap_priority"], BOOTSTRAP_PRIORITIES, "universe[].bootstrap_priority"
            ),
        })
    body["universe"] = universe

    body["research_questions"] = _texts(body["research_questions"], "research_questions", nonempty=True)
    deliverables = _texts(body["deliverables"], "deliverables", nonempty=True)
    for item in deliverables:
        _vocabulary(item, DELIVERABLE_KINDS, "deliverables[]")
    body["deliverables"] = deliverables

    if not isinstance(body["source_plan"], list) or not body["source_plan"]:
        raise CoverageMissionValidationError("source_plan must be a non-empty array")
    sources: list[dict[str, str]] = []
    seen = set()
    for raw in body["source_plan"]:
        source = _closed(raw, frozenset({"source_ref", "role", "status"}), "source_plan[]")
        ref = _text(source["source_ref"], "source_plan[].source_ref")
        if ref in seen:
            raise CoverageMissionValidationError("source_plan source_ref must be unique")
        seen.add(ref)
        sources.append({
            "source_ref": ref,
            "role": _text(source["role"], "source_plan[].role"),
            "status": _vocabulary(source["status"], SOURCE_STATUSES, "source_plan[].status"),
        })
    body["source_plan"] = sources

    bindings = _closed(body["bindings"], _BINDING_FIELDS, "bindings")
    body["bindings"] = {field: _binding(bindings[field], f"bindings.{field}") for field in sorted(_BINDING_FIELDS)}

    autonomy = _closed(
        body["autonomy"], frozenset({"automation_principal", "may_write", "human_checkpoints"}), "autonomy"
    )
    principal = _text(autonomy["automation_principal"], "autonomy.automation_principal")
    if _AUTOMATION_RE.fullmatch(principal) is None:
        raise CoverageMissionValidationError("autonomy.automation_principal must use the automation: namespace")
    may_write = _texts(autonomy["may_write"], "autonomy.may_write")
    for item in may_write:
        _vocabulary(item, AUTOMATION_WRITE_SCOPES, "autonomy.may_write[]")
    checkpoints = _texts(autonomy["human_checkpoints"], "autonomy.human_checkpoints", nonempty=True)
    for item in checkpoints:
        _vocabulary(item, CHECKPOINT_KINDS, "autonomy.human_checkpoints[]")
    missing = REQUIRED_CHECKPOINTS - set(checkpoints)
    if missing:
        raise CoverageMissionValidationError(
            f"autonomy.human_checkpoints cannot drop {sorted(missing)}"
        )
    body["autonomy"] = {
        "automation_principal": principal,
        "may_write": may_write,
        "human_checkpoints": checkpoints,
    }

    budget = _closed(
        body["budget"],
        frozenset({"max_daily_paid_calls", "max_daily_cost_usd", "max_alphaengine_calls_24h"}),
        "budget",
    )
    body["budget"] = {
        "max_daily_paid_calls": _non_negative_int(budget["max_daily_paid_calls"], "budget.max_daily_paid_calls"),
        "max_daily_cost_usd": _non_negative_number(budget["max_daily_cost_usd"], "budget.max_daily_cost_usd"),
        "max_alphaengine_calls_24h": _non_negative_int(
            budget["max_alphaengine_calls_24h"], "budget.max_alphaengine_calls_24h"
        ),
    }
    return body


def validate_coverage_mission_version(value: Mapping[str, Any]) -> dict[str, Any]:
    wire = dict(value)
    if set(wire) != _VERSION_FIELDS or wire.get("schema_version") != SCHEMA_VERSION:
        raise CoverageMissionValidationError("coverage mission has an invalid closed shape")
    for field in ("id", "created_at", "mission_ref"):
        wire[field] = _text(wire[field], field)
    wire["actor_ref"] = _human(wire["actor_ref"])
    wire["content_hash"] = _sha256(wire["content_hash"], "content_hash")
    if type(wire["version"]) is not int or wire["version"] < 1:
        raise CoverageMissionValidationError("coverage mission version must be positive")
    if wire["prior_version_ref"] is not None:
        wire["prior_version_ref"] = _text(wire["prior_version_ref"], "prior_version_ref")
    wire.update(validate_mission_body({field: wire[field] for field in _BODY_FIELDS}))
    base = dict(wire)
    expected_hash = base.pop("content_hash")
    if content_hash(base) != expected_hash:
        raise CoverageMissionValidationError("coverage mission content_hash is invalid")
    return wire


def validate_mission_stage_record(value: Mapping[str, Any]) -> dict[str, Any]:
    wire = dict(value)
    if set(wire) != _STAGE_RECORD_FIELDS or wire.get("schema_version") != SCHEMA_VERSION:
        raise CoverageMissionValidationError("mission stage record has an invalid closed shape")
    for field in ("id", "created_at", "mission_version_ref", "company_ref", "rationale"):
        wire[field] = _text(wire[field], field)
    wire["mission_version_hash"] = _sha256(wire["mission_version_hash"], "mission_version_hash")
    wire["stage_ref"] = _vocabulary(wire["stage_ref"], STAGE_ORDER, "stage_ref")
    wire["status"] = _vocabulary(wire["status"], STAGE_STATUSES, "status")
    wire["evidence_refs"] = _texts(wire["evidence_refs"], "evidence_refs")
    wire["actor_ref"] = _actor(wire["actor_ref"])
    wire["content_hash"] = _sha256(wire["content_hash"], "content_hash")
    base = dict(wire)
    expected_hash = base.pop("content_hash")
    if content_hash(base) != expected_hash:
        raise CoverageMissionValidationError("mission stage record content_hash is invalid")
    return wire


def validate_mission_stage_reopen(value: Mapping[str, Any]) -> dict[str, Any]:
    """The closed shape of a reopen marker.

    Its ``status`` is fixed rather than a vocabulary: a row in this ledger says
    exactly one thing, and a marker that could say something else would be a
    second way to write a stage status.
    """

    wire = dict(value)
    if set(wire) != _STAGE_REOPEN_FIELDS or wire.get("schema_version") != SCHEMA_VERSION:
        raise CoverageMissionValidationError("mission stage reopen has an invalid closed shape")
    for field in (
        "id", "created_at", "mission_version_ref", "company_ref", "rationale",
        "reopen_decision_ref", "reopen_proposal_ref", "reopened_version_ref",
    ):
        wire[field] = _text(wire[field], field)
    wire["mission_version_hash"] = _sha256(wire["mission_version_hash"], "mission_version_hash")
    wire["stage_ref"] = _vocabulary(wire["stage_ref"], STAGE_ORDER, "stage_ref")
    if wire["status"] != STAGE_REOPENED:
        raise CoverageMissionValidationError("a stage reopen's status is always 'reopened'")
    wire["actor_ref"] = _actor(wire["actor_ref"])
    if not wire["actor_ref"].startswith("human:"):
        raise CoverageMissionValidationError(
            "re-opening a decided gate is a human checkpoint (ADR-0008)"
        )
    wire["content_hash"] = _sha256(wire["content_hash"], "content_hash")
    base = dict(wire)
    expected_hash = base.pop("content_hash")
    if content_hash(base) != expected_hash:
        raise CoverageMissionValidationError("mission stage reopen content_hash is invalid")
    return wire


def validate_mission_stage_claim(value: Mapping[str, Any]) -> dict[str, Any]:
    wire = dict(value)
    if set(wire) != _STAGE_CLAIM_FIELDS or wire.get("schema_version") != SCHEMA_VERSION:
        raise CoverageMissionValidationError("mission stage claim has an invalid closed shape")
    for field in (
        "id", "created_at", "mission_version_ref", "company_ref", "claim_version_ref",
        "evidence_version_ref", "source_location",
    ):
        wire[field] = _text(wire[field], field)
    wire["mission_version_hash"] = _sha256(wire["mission_version_hash"], "mission_version_hash")
    wire["claim_version_hash"] = _sha256(wire["claim_version_hash"], "claim_version_hash")
    wire["evidence_version_hash"] = _sha256(
        wire["evidence_version_hash"], "evidence_version_hash"
    )
    wire["stage_ref"] = _vocabulary(wire["stage_ref"], STAGE_ORDER, "stage_ref")
    wire["actor_ref"] = _actor(wire["actor_ref"])
    if _AUTOMATION_RE.fullmatch(wire["actor_ref"]) is None:
        raise CoverageMissionValidationError(
            "mission stage claim actor_ref must use the automation: namespace"
        )
    if not wire["source_location"].startswith("sec:accession:"):
        raise CoverageMissionValidationError(
            "mission stage claim source_location must bind a SEC accession"
        )
    wire["content_hash"] = _sha256(wire["content_hash"], "content_hash")
    base = dict(wire)
    expected_hash = base.pop("content_hash")
    if content_hash(base) != expected_hash:
        raise CoverageMissionValidationError("mission stage claim content_hash is invalid")
    return wire


def validate_mission_source_discovery(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one append-only source discovery record (P9d-1)."""

    wire = dict(value)
    if set(wire) != _SOURCE_DISCOVERY_FIELDS or wire.get("schema_version") != SCHEMA_VERSION:
        raise CoverageMissionValidationError("mission source discovery has an invalid closed shape")
    for field in (
        "id", "created_at", "mission_version_ref", "company_ref", "source_ref",
        "discovery_plan_ref", "spec_ref", "connector_invocation_ref",
        "source_envelope_ref",
    ):
        wire[field] = _text(wire[field], field)
    for field in (
        "mission_version_hash", "discovery_plan_hash", "query_hash",
        "connector_invocation_hash", "source_envelope_hash", "content_hash",
    ):
        wire[field] = _sha256(wire[field], field)
    if not isinstance(wire["parameters"], Mapping):
        raise CoverageMissionValidationError("mission source discovery parameters must be an object")
    wire["parameters"] = json.loads(canonical_json(wire["parameters"]))
    refs = _texts(wire["document_refs"], "document_refs")
    if len(set(refs)) != len(refs):
        raise CoverageMissionValidationError("mission source discovery document_refs must be unique")
    new_refs = _texts(wire["new_document_refs"], "new_document_refs")
    present = _texts(wire["in_authority_document_refs"], "in_authority_document_refs")
    if sorted(new_refs + present) != sorted(refs) or set(new_refs) & set(present):
        raise CoverageMissionValidationError(
            "mission source discovery must partition document_refs into new and in-authority"
        )
    source = DISCOVERY_SOURCES.get(wire["source_ref"])
    if source is None:
        raise CoverageMissionValidationError("mission source discovery source_ref is not a discovery source")
    if any(not ref.startswith(source["document_ref_prefix"]) for ref in refs):
        raise CoverageMissionValidationError(
            f"mission source discovery document_refs must start with {source['document_ref_prefix']}"
        )
    if source["operation"] == "search_library":
        if set(wire["parameters"]) != {"query", "filters", "cursor"}:
            raise CoverageMissionValidationError("search_library discovery parameters have an invalid shape")
    elif source["operation"] == "list_filings":
        # P10s: the filings index is asked for an issuer and a form, so it has
        # no query to record. Pinned here as well as in the plan so a malformed
        # parameter set cannot reach the ledger through a hand-made call.
        if set(wire["parameters"]) != {"issuer", "form", "date_from", "date_to", "limit"}:
            raise CoverageMissionValidationError("list_filings discovery parameters have an invalid shape")
    elif set(wire["parameters"]) != {"query", "date_after", "date_before"}:
        raise CoverageMissionValidationError("search_web discovery parameters have an invalid shape")
    wire["document_refs"] = refs
    wire["new_document_refs"] = new_refs
    wire["in_authority_document_refs"] = present
    wire["actor_ref"] = _actor(wire["actor_ref"])
    if _AUTOMATION_RE.fullmatch(wire["actor_ref"]) is None:
        raise CoverageMissionValidationError(
            "mission source discovery actor_ref must use the automation: namespace"
        )
    wire["requested_by"] = _actor(wire["requested_by"])
    base = dict(wire)
    expected_hash = base.pop("content_hash")
    if content_hash(base) != expected_hash:
        raise CoverageMissionValidationError("mission source discovery content_hash is invalid")
    return wire


def _statement_line_row(row: Any) -> dict[str, Any]:
    """One stored line, with its breakdown flag derived rather than trusted.

    P13an: the parser reported some dimensioned lines with the flag clear, so
    rows already in the ledger carry it wrong. The ledger is append-only and
    those rows cannot be corrected in place -- but a line filed along a
    dimension is a breakdown by construction, so it is derived on the way out
    and every reader, old rows included, sees the truth.
    """

    wire = dict(row)
    wire["is_breakdown"] = bool(row["is_breakdown"]) or row["dimension_axis"] is not None
    return wire


def _canonical_record(raw: Any, name: str) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise CoverageMissionConflict(f"{name} record is missing")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CoverageMissionConflict(f"{name} record is invalid") from exc
    if not isinstance(value, dict) or canonical_json(value) != raw:
        raise CoverageMissionConflict(f"{name} record is not canonical")
    return value


def _ref(prefix: str, identity: Mapping[str, Any]) -> str:
    return f"{prefix}:{content_hash(identity)[:32]}"


_HOST_RE = re.compile(r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")


def _needs(value: Any, name: str) -> list[dict[str, str]]:
    """Closed ordered list of ``{company_ref, spec_ref}`` stage needs."""

    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise CoverageMissionValidationError(f"{name} must be a sequence")
    result: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"company_ref", "spec_ref"}:
            raise CoverageMissionValidationError(f"{name} items need exactly company_ref and spec_ref")
        result.append({
            "company_ref": _text(item["company_ref"], f"{name}.company_ref"),
            "spec_ref": _text(item["spec_ref"], f"{name}.spec_ref"),
        })
    if len(result) > 50:
        raise CoverageMissionValidationError(f"{name} is limited to 50 entries")
    return result


def _host_list(value: Any, name: str) -> list[str]:
    """Lowercase registrable hostnames, unique, in the order given."""

    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise CoverageMissionValidationError(f"{name} must be a list of hostnames")
    hosts: list[str] = []
    for item in value:
        if not isinstance(item, str) or _HOST_RE.fullmatch(item) is None:
            raise CoverageMissionValidationError(f"{name} entries must be lowercase hostnames")
        if item not in hosts:
            hosts.append(item)
    if len(hosts) > 50:
        raise CoverageMissionValidationError(f"{name} may list at most 50 hosts")
    return hosts


def _document_hosts(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise CoverageMissionValidationError("document_hosts must map document refs to hosts")
    return {
        _text(ref, "document_ref"): _host_list([host], "document_hosts")[0]
        for ref, host in value.items()
    }


class CoverageMissionAuthority:
    """Publish missions, record stage progress and project mission state."""

    _authorized = authorized_flag()

    def __init__(self, store: DaltonStore):
        self.store = store
        self.connection = store.connection
        self._authorization_flag = authorization_flag(
            self.connection, "dalton_coverage_mission_authorized")
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        self._migrate_discovered_document_host()
        self._migrate_settlement_failure_reason()
        self._migrate_plan_sufficiency()

    def _migrate_plan_sufficiency(self) -> None:
        """P13ai: add the nullable ``sufficiency_json`` column to older ledgers."""

        columns = {
            row[1] for row in self.connection.execute(
                "PRAGMA table_info(coverage_mission_research_plans)"
            ).fetchall()
        }
        if "sufficiency_json" in columns:
            return
        if self.connection.in_transaction:
            raise RuntimeError("plan sufficiency migration requires no open transaction")
        self.connection.execute(
            "ALTER TABLE coverage_mission_research_plans ADD COLUMN sufficiency_json TEXT"
        )

    def _migrate_settlement_failure_reason(self) -> None:
        """P13z: add the nullable ``failure_reason`` column to older ledgers.

        Additive only; settlements already written keep every value and gain
        ``failure_reason=NULL``, which is honest -- nobody recorded why those
        runs failed, and inventing a reason now would be worse than the gap.
        """

        columns = {
            row[1] for row in self.connection.execute(
                "PRAGMA table_info(coverage_mission_sec_dispatch_settlements)"
            ).fetchall()
        }
        if "failure_reason" in columns:
            return
        if self.connection.in_transaction:
            raise RuntimeError("settlement failure reason migration requires no open transaction")
        self.connection.execute(
            "ALTER TABLE coverage_mission_sec_dispatch_settlements "
            "ADD COLUMN failure_reason TEXT"
        )

    def _migrate_discovered_document_host(self) -> None:
        """P9d-13: add the nullable ``host`` column to ledgers created earlier.

        Additive only; existing rows keep every value and gain ``host=NULL``,
        which the discovery coordinator backfills from each row's exact
        discovery envelope.  Fresh databases already carry the column.
        """

        columns = {
            row[1] for row in self.connection.execute(
                "PRAGMA table_info(coverage_mission_discovered_documents)"
            ).fetchall()
        }
        if "host" in columns:
            return
        if self.connection.in_transaction:
            raise RuntimeError("discovered document host migration requires no open transaction")
        self.connection.execute(
            "ALTER TABLE coverage_mission_discovered_documents ADD COLUMN host TEXT"
        )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        if self._authorized:
            raise RuntimeError("CoverageMissionAuthority operation cannot be nested")
        self._authorized = True
        try:
            with self.store._transaction() as cur:
                yield cur
        finally:
            self._authorized = False

    @staticmethod
    def _request_hash(operation: str, request: Mapping[str, Any]) -> str:
        return content_hash({"operation": operation, "request": dict(request)})

    def _idem(
        self, cur: sqlite3.Cursor, key: str, operation: str, request_hash: str,
        *, marker: str = "status",
    ) -> dict[str, Any] | None:
        row = cur.execute(
            "SELECT * FROM coverage_mission_idempotency WHERE idempotency_key=?", (key,)
        ).fetchone()
        if row is None:
            return None
        if row["operation"] != operation or row["request_hash"] != request_hash:
            raise CoverageMissionConflict("idempotency key conflicts with prior request")
        return {**json.loads(row["result_json"]), marker: "duplicate"}

    def _save_idem(
        self, cur: sqlite3.Cursor, key: str, operation: str, request_hash: str,
        result: Mapping[str, Any], created_at: str,
    ) -> None:
        cur.execute(
            "INSERT INTO coverage_mission_idempotency"
            "(idempotency_key,operation,request_hash,result_json,created_at) VALUES(?,?,?,?,?)",
            (key, operation, request_hash, canonical_json(result), created_at),
        )

    # -- binding validation -------------------------------------------------

    def _validate_playbook_binding(self, cur: sqlite3.Cursor, binding: Mapping[str, str]) -> dict[str, Any]:
        try:
            playbook = read_exact_playbook_version(self.connection, binding["ref"])
        except ResearchPlaybookNotFound as exc:
            raise CoverageMissionNotFound(str(exc)) from exc
        except ResearchPlaybookError as exc:
            raise CoverageMissionConflict(str(exc)) from exc
        if playbook["content_hash"] != binding["hash"]:
            raise CoverageMissionConflict("playbook binding failed")
        pointer = cur.execute(
            "SELECT playbook_version_id FROM research_playbook_pointer WHERE playbook_ref=?",
            (playbook["playbook_ref"],),
        ).fetchone()
        if pointer is None or pointer["playbook_version_id"] != binding["ref"]:
            raise CoverageMissionConflict("playbook is not the active version")
        return playbook

    def _validate_constitution_binding(
        self, cur: sqlite3.Cursor, binding: Mapping[str, str], industry_ref: str
    ) -> dict[str, Any]:
        table = cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_constitution_versions'"
        ).fetchone()
        if table is None:
            raise CoverageMissionNotFound("research constitution authority is not open on this Core")
        row = cur.execute(
            "SELECT * FROM research_constitution_versions WHERE constitution_version_id=?",
            (binding["ref"],),
        ).fetchone()
        if row is None:
            raise CoverageMissionNotFound("research constitution version was not found")
        wire = _canonical_record(row["record_json"], "research constitution")
        base = dict(wire)
        asserted = base.pop("content_hash", None)
        if (
            wire.get("id") != binding["ref"]
            or asserted != row["content_hash"]
            or asserted != binding["hash"]
            or content_hash(base) != asserted
        ):
            raise CoverageMissionConflict("constitution binding failed")
        if row["industry_ref"] != industry_ref or wire.get("industry_ref") != industry_ref:
            raise CoverageMissionConflict("constitution does not govern the mission industry")
        pointer = cur.execute(
            "SELECT constitution_version_id FROM research_constitution_pointer WHERE constitution_ref=?",
            (row["constitution_ref"],),
        ).fetchone()
        if pointer is None or pointer["constitution_version_id"] != binding["ref"]:
            raise CoverageMissionConflict("constitution is not the active version")
        return wire

    def _validate_mandate_binding(
        self, cur: sqlite3.Cursor, binding: Mapping[str, str], industry_ref: str
    ) -> dict[str, Any]:
        row = cur.execute(
            "SELECT * FROM mandate_versions WHERE version_id=?", (binding["ref"],)
        ).fetchone()
        if row is None:
            raise CoverageMissionNotFound("mandate version was not found")
        wire = _canonical_record(row["record_json"], "mandate")
        base = dict(wire)
        asserted = base.pop("content_hash", None)
        if (
            wire.get("id") != binding["ref"]
            or asserted != row["content_hash"]
            or asserted != binding["hash"]
            or content_hash(base) != asserted
        ):
            raise CoverageMissionConflict("mandate binding failed")
        if industry_ref not in set(wire.get("scope_refs", [])):
            raise CoverageMissionConflict("mandate does not cover the mission industry")
        now = _now()
        if (
            wire.get("effective_from") > now
            or (wire.get("effective_until") is not None and wire["effective_until"] <= now)
        ):
            raise CoverageMissionConflict("mandate is outside its effective window")
        pointer = cur.execute(
            "SELECT version_id,active FROM mandate_pointer WHERE mandate_ref=?",
            (wire["mandate_ref"],),
        ).fetchone()
        if pointer is None or pointer["version_id"] != binding["ref"] or int(pointer["active"]) != 1:
            raise CoverageMissionConflict("mandate is not the active version")
        return wire

    # -- mission versions ----------------------------------------------------

    def create_mission(
        self,
        mission_ref: str,
        *,
        title: str,
        objective: str,
        industry_ref: str,
        universe: list[Mapping[str, Any]],
        research_questions: list[str],
        deliverables: list[str],
        source_plan: list[Mapping[str, Any]],
        bindings: Mapping[str, Any],
        autonomy: Mapping[str, Any],
        budget: Mapping[str, Any],
        actor_ref: str,
        version_id: str,
        prior_version_ref: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        mission_ref = _text(mission_ref, "mission_ref")
        actor_ref = _human(actor_ref)
        version_id = _text(version_id, "version_id")
        idempotency_key = _text(idempotency_key, "idempotency_key")
        if prior_version_ref is not None:
            prior_version_ref = _text(prior_version_ref, "prior_version_ref")
        body = validate_mission_body({
            "title": title,
            "objective": objective,
            "industry_ref": industry_ref,
            "universe": universe,
            "research_questions": research_questions,
            "deliverables": deliverables,
            "source_plan": source_plan,
            "bindings": bindings,
            "autonomy": autonomy,
            "budget": budget,
        })
        request = {
            "mission_ref": mission_ref,
            **body,
            "actor_ref": actor_ref,
            "version_id": version_id,
            "prior_version_ref": prior_version_ref,
        }
        request_hash = self._request_hash("create_mission", request)
        with self._transaction() as cur:
            duplicate = self._idem(cur, idempotency_key, "create_mission", request_hash)
            if duplicate is not None:
                return duplicate
            latest = cur.execute(
                "SELECT mission_version_id,version_number FROM coverage_mission_versions "
                "WHERE mission_ref=? ORDER BY version_number DESC LIMIT 1",
                (mission_ref,),
            ).fetchone()
            if latest is None:
                if prior_version_ref is not None:
                    raise CoverageMissionConflict("first mission cannot have a prior version")
                version = 1
            else:
                if prior_version_ref != latest["mission_version_id"]:
                    raise CoverageMissionConflict("mission must continue the latest version")
                version = int(latest["version_number"]) + 1
            if cur.execute(
                "SELECT 1 FROM coverage_mission_versions WHERE mission_version_id=?", (version_id,)
            ).fetchone():
                raise CoverageMissionConflict("mission version id already exists")
            playbook = self._validate_playbook_binding(cur, body["bindings"]["playbook_version"])
            self._validate_constitution_binding(
                cur, body["bindings"]["constitution_version"], body["industry_ref"]
            )
            self._validate_mandate_binding(cur, body["bindings"]["mandate_version"], body["industry_ref"])
            required_stage_checkpoints = {
                stage["stage_ref"] for stage in playbook["stages"] if stage["human_checkpoint"]
            }
            missing = required_stage_checkpoints - set(body["autonomy"]["human_checkpoints"])
            if missing:
                raise CoverageMissionConflict(
                    f"mission cannot drop playbook human checkpoints {sorted(missing)}"
                )
            created_at = _now()
            record = {
                "schema_version": SCHEMA_VERSION,
                "id": version_id,
                "created_at": created_at,
                "mission_ref": mission_ref,
                "version": version,
                "prior_version_ref": prior_version_ref,
                **body,
                "actor_ref": actor_ref,
            }
            wire = dict(record)
            wire["content_hash"] = content_hash(record)
            validate_coverage_mission_version(wire)
            cur.execute(
                "INSERT INTO coverage_mission_versions"
                "(mission_version_id,mission_ref,version_number,prior_version_id,industry_ref,"
                "playbook_version_ref,constitution_version_ref,mandate_version_ref,"
                "record_json,content_hash,actor_ref,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    version_id, mission_ref, version, prior_version_ref, body["industry_ref"],
                    body["bindings"]["playbook_version"]["ref"],
                    body["bindings"]["constitution_version"]["ref"],
                    body["bindings"]["mandate_version"]["ref"],
                    canonical_json(wire), wire["content_hash"], actor_ref, created_at,
                ),
            )
            cur.execute(
                "INSERT INTO coverage_mission_pointer"
                "(mission_ref,mission_version_id,version_number,content_hash,updated_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(mission_ref) DO UPDATE SET "
                "mission_version_id=excluded.mission_version_id,"
                "version_number=excluded.version_number,"
                "content_hash=excluded.content_hash,updated_at=excluded.updated_at",
                (mission_ref, version_id, version, wire["content_hash"], created_at),
            )
            result = {"status": "fresh", **wire}
            self._save_idem(cur, idempotency_key, "create_mission", request_hash, result, created_at)
            return result

    def mission(self, version_id: str) -> dict[str, Any]:
        version_id = _text(version_id, "version_id")
        row = self.connection.execute(
            "SELECT * FROM coverage_mission_versions WHERE mission_version_id=?", (version_id,)
        ).fetchone()
        if row is None:
            raise CoverageMissionNotFound("coverage mission version was not found")
        wire = validate_coverage_mission_version(_canonical_record(row["record_json"], "coverage mission"))
        if (
            wire["id"] != row["mission_version_id"]
            or wire["mission_ref"] != row["mission_ref"]
            or wire["version"] != row["version_number"]
            or wire["prior_version_ref"] != row["prior_version_id"]
            or wire["industry_ref"] != row["industry_ref"]
            or wire["bindings"]["playbook_version"]["ref"] != row["playbook_version_ref"]
            or wire["bindings"]["constitution_version"]["ref"] != row["constitution_version_ref"]
            or wire["bindings"]["mandate_version"]["ref"] != row["mandate_version_ref"]
            or wire["actor_ref"] != row["actor_ref"]
            or wire["created_at"] != row["created_at"]
            or wire["content_hash"] != row["content_hash"]
        ):
            raise CoverageMissionConflict("coverage mission authority drifted")
        return wire

    def active_mission(self, mission_ref: str) -> dict[str, Any]:
        mission_ref = _text(mission_ref, "mission_ref")
        pointer = self.connection.execute(
            "SELECT * FROM coverage_mission_pointer WHERE mission_ref=?", (mission_ref,)
        ).fetchone()
        if pointer is None:
            raise CoverageMissionNotFound("coverage mission pointer was not found")
        wire = self.mission(pointer["mission_version_id"])
        if wire["version"] != pointer["version_number"] or wire["content_hash"] != pointer["content_hash"]:
            raise CoverageMissionConflict("coverage mission pointer drifted")
        return wire

    def authorize_sec_lane(
        self,
        *,
        company_ref: str,
        ticker: str,
        actor_ref: str,
        mission_version_ref: str | None = None,
        mission_version_hash: str | None = None,
    ) -> dict[str, Any]:
        """Resolve and validate the mission grant for one zero-cost SEC run.

        The connector's own rate policy still bounds public HTTP calls.  SEC
        carries no paid-call or dollar charge, so this authorization reserves
        zero against the mission's paid budgets while retaining their exact
        limits in the returned receipt.
        """

        company_ref = _text(company_ref, "company_ref")
        ticker = _text(ticker, "ticker")
        actor_ref = _actor(actor_ref)
        if mission_version_ref is None:
            candidates: list[dict[str, Any]] = []
            for row in self.connection.execute(
                "SELECT mission_version_id FROM coverage_mission_pointer ORDER BY mission_ref"
            ).fetchall():
                mission = self.mission(row["mission_version_id"])
                if company_ref in {member["company_ref"] for member in mission["universe"]}:
                    candidates.append(mission)
            if len(candidates) != 1:
                raise CoverageMissionConflict(
                    "SEC automation requires exactly one active mission for the company"
                )
            mission = candidates[0]
        else:
            mission = self.mission(_text(mission_version_ref, "mission_version_ref"))
            pointer = self.connection.execute(
                "SELECT mission_version_id FROM coverage_mission_pointer WHERE mission_ref=?",
                (mission["mission_ref"],),
            ).fetchone()
            if pointer is None or pointer["mission_version_id"] != mission["id"]:
                raise CoverageMissionConflict("SEC automation must bind the active mission version")
        if mission_version_hash is not None and mission["content_hash"] != _sha256(
            mission_version_hash, "mission_version_hash"
        ):
            raise CoverageMissionConflict("SEC automation mission hash binding failed")
        if actor_ref != mission["autonomy"]["automation_principal"]:
            raise CoverageMissionConflict("SEC automation actor is not the mission principal")
        member = next(
            (item for item in mission["universe"] if item["company_ref"] == company_ref), None
        )
        if member is None or member["ticker"] != ticker:
            raise CoverageMissionConflict("SEC automation company/ticker is outside the mission universe")
        source = next(
            (item for item in mission["source_plan"] if item["source_ref"] == "source:sec-edgar"),
            None,
        )
        if source is None or source["status"] != "connected":
            raise CoverageMissionConflict("mission does not mark SEC EDGAR as connected")
        required_writes = {"claim", "evidence", "research_question", "observation", "stage_record"}
        missing = required_writes - set(mission["autonomy"]["may_write"])
        if missing:
            raise CoverageMissionConflict(
                f"mission does not grant SEC automation writes {sorted(missing)}"
            )
        cur = self.connection.cursor()
        self._validate_playbook_binding(cur, mission["bindings"]["playbook_version"])
        self._validate_constitution_binding(
            cur, mission["bindings"]["constitution_version"], mission["industry_ref"]
        )
        self._validate_mandate_binding(
            cur, mission["bindings"]["mandate_version"], mission["industry_ref"]
        )
        return {
            "mission_version_ref": mission["id"],
            "mission_version_hash": mission["content_hash"],
            "mission_ref": mission["mission_ref"],
            "company_ref": company_ref,
            "ticker": ticker,
            "actor_ref": actor_ref,
            "paid_calls_reserved": 0,
            "cost_usd_reserved": 0.0,
            "budget": dict(mission["budget"]),
        }

    def authorize_forecast_reconciliation(
        self,
        *,
        company_ref: str,
        actor_ref: str | None = None,
        mission_version_ref: str | None = None,
        mission_version_hash: str | None = None,
    ) -> dict[str, Any]:
        """Resolve the mission grant for automation forecast reconciliation.

        Requires exactly one active mission covering the company, the
        ``forecast_reconciliation`` write scope and the ``forecast_overturn``
        human checkpoint (so an overturn candidate has somewhere to escalate).
        Reconciliation is a zero-cost derived write; no budget is reserved.
        """

        company_ref = _text(company_ref, "company_ref")
        if mission_version_ref is None:
            candidates: list[dict[str, Any]] = []
            for row in self.connection.execute(
                "SELECT mission_version_id FROM coverage_mission_pointer ORDER BY mission_ref"
            ).fetchall():
                mission = self.mission(row["mission_version_id"])
                if company_ref in {member["company_ref"] for member in mission["universe"]}:
                    candidates.append(mission)
            if len(candidates) != 1:
                raise CoverageMissionConflict(
                    "forecast reconciliation requires exactly one active mission for the company"
                )
            mission = candidates[0]
        else:
            mission = self.mission(_text(mission_version_ref, "mission_version_ref"))
            pointer = self.connection.execute(
                "SELECT mission_version_id FROM coverage_mission_pointer WHERE mission_ref=?",
                (mission["mission_ref"],),
            ).fetchone()
            if pointer is None or pointer["mission_version_id"] != mission["id"]:
                raise CoverageMissionConflict(
                    "forecast reconciliation must bind the active mission version"
                )
        if mission_version_hash is not None and mission["content_hash"] != _sha256(
            mission_version_hash, "mission_version_hash"
        ):
            raise CoverageMissionConflict("forecast reconciliation mission hash binding failed")
        principal = mission["autonomy"]["automation_principal"]
        if actor_ref is None:
            actor_ref = principal
        elif _actor(actor_ref) != principal:
            raise CoverageMissionConflict(
                "forecast reconciliation actor is not the mission principal"
            )
        if company_ref not in {member["company_ref"] for member in mission["universe"]}:
            raise CoverageMissionConflict("company is outside the mission universe")
        if "forecast_reconciliation" not in mission["autonomy"]["may_write"]:
            raise CoverageMissionConflict(
                "mission does not grant forecast_reconciliation writes to automation"
            )
        if "forecast_overturn" not in mission["autonomy"]["human_checkpoints"]:
            raise CoverageMissionConflict(
                "mission does not list the forecast_overturn human checkpoint"
            )
        cur = self.connection.cursor()
        self._validate_playbook_binding(cur, mission["bindings"]["playbook_version"])
        self._validate_constitution_binding(
            cur, mission["bindings"]["constitution_version"], mission["industry_ref"]
        )
        self._validate_mandate_binding(
            cur, mission["bindings"]["mandate_version"], mission["industry_ref"]
        )
        return {
            "mission_version_ref": mission["id"],
            "mission_version_hash": mission["content_hash"],
            "mission_ref": mission["mission_ref"],
            "company_ref": company_ref,
            "actor_ref": actor_ref,
            "scope": "forecast_reconciliation",
        }

    # -- P9d-1: source discovery ---------------------------------------------

    def _resolve_active_mission_for_company(
        self,
        company_ref: str,
        *,
        purpose: str,
        mission_version_ref: str | None,
        mission_version_hash: str | None,
    ) -> dict[str, Any]:
        if mission_version_ref is None:
            candidates: list[dict[str, Any]] = []
            for row in self.connection.execute(
                "SELECT mission_version_id FROM coverage_mission_pointer ORDER BY mission_ref"
            ).fetchall():
                mission = self.mission(row["mission_version_id"])
                if company_ref in {member["company_ref"] for member in mission["universe"]}:
                    candidates.append(mission)
            if len(candidates) != 1:
                raise CoverageMissionConflict(
                    f"{purpose} requires exactly one active mission for the company"
                )
            mission = candidates[0]
        else:
            mission = self.mission(_text(mission_version_ref, "mission_version_ref"))
            pointer = self.connection.execute(
                "SELECT mission_version_id FROM coverage_mission_pointer WHERE mission_ref=?",
                (mission["mission_ref"],),
            ).fetchone()
            if pointer is None or pointer["mission_version_id"] != mission["id"]:
                raise CoverageMissionConflict(f"{purpose} must bind the active mission version")
        if mission_version_hash is not None and mission["content_hash"] != _sha256(
            mission_version_hash, "mission_version_hash"
        ):
            raise CoverageMissionConflict(f"{purpose} mission hash binding failed")
        return mission

    def authorize_source_discovery(
        self,
        *,
        company_ref: str,
        source_ref: str,
        requested_by: str,
        mission_version_ref: str | None = None,
        mission_version_hash: str | None = None,
    ) -> dict[str, Any]:
        """Resolve the mission grant for one budgeted library search.

        Automation (``requested_by`` equal to the mission principal) needs the
        source marked ``connected`` in the mission's source plan and the
        ``source_discovery`` + ``observation`` write scopes.  A ``human:``
        requester may run a discovery under a ``probe_only`` source (that is
        how an owner rehearses a connector before promoting it), but the
        record still binds the mission, its principal and its budget.
        """

        company_ref = _text(company_ref, "company_ref")
        source_ref = _text(source_ref, "source_ref")
        requested_by = _actor(requested_by, "requested_by")
        mission = self._resolve_active_mission_for_company(
            company_ref,
            purpose="source discovery",
            mission_version_ref=mission_version_ref,
            mission_version_hash=mission_version_hash,
        )
        principal = mission["autonomy"]["automation_principal"]
        human_request = _HUMAN_RE.fullmatch(requested_by) is not None
        if not human_request and requested_by != principal:
            raise CoverageMissionConflict("source discovery requester is not the mission principal")
        member = next(
            (item for item in mission["universe"] if item["company_ref"] == company_ref), None
        )
        if member is None:
            raise CoverageMissionConflict("company is outside the mission universe")
        if source_ref not in DISCOVERY_SOURCES:
            raise CoverageMissionConflict(f"{source_ref} is not a search-driven discovery source")
        source = next(
            (item for item in mission["source_plan"] if item["source_ref"] == source_ref), None
        )
        if source is None:
            raise CoverageMissionConflict(f"mission source plan does not list {source_ref}")
        if source["status"] == "not_connected":
            raise CoverageMissionConflict(f"mission marks {source_ref} as not_connected")
        if not human_request:
            if source["status"] != "connected":
                raise CoverageMissionConflict(
                    f"mission marks {source_ref} as {source['status']}; automation discovery "
                    "requires connected"
                )
            missing = {"source_discovery", "observation"} - set(mission["autonomy"]["may_write"])
            if missing:
                raise CoverageMissionConflict(
                    f"mission does not grant source discovery writes {sorted(missing)}"
                )
        cur = self.connection.cursor()
        self._validate_playbook_binding(cur, mission["bindings"]["playbook_version"])
        self._validate_constitution_binding(
            cur, mission["bindings"]["constitution_version"], mission["industry_ref"]
        )
        self._validate_mandate_binding(
            cur, mission["bindings"]["mandate_version"], mission["industry_ref"]
        )
        return {
            "mission_version_ref": mission["id"],
            "mission_version_hash": mission["content_hash"],
            "mission_ref": mission["mission_ref"],
            "company_ref": company_ref,
            "ticker": member["ticker"],
            "source_ref": source_ref,
            "actor_ref": principal,
            "requested_by": requested_by,
            "scope": "source_discovery",
            "max_alphaengine_calls_24h": int(mission["budget"]["max_alphaengine_calls_24h"]),
        }

    @staticmethod
    def _validate_discovery_authorization(value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping) or set(value) != _DISCOVERY_AUTHORIZATION_FIELDS:
            raise CoverageMissionValidationError("discovery authorization has an invalid closed shape")
        return json.loads(canonical_json(value))

    def record_discovery_dispatch(
        self,
        *,
        authorization: Mapping[str, Any],
        discovery_plan_ref: str,
        discovery_plan_hash: str,
        spec_ref: str,
        query_hash: str,
        ticket_ref: str,
    ) -> dict[str, Any]:
        """Record one launched discovery child (status ``launched``)."""

        authorization = self._validate_discovery_authorization(authorization)
        exact = self.authorize_source_discovery(
            company_ref=authorization["company_ref"],
            source_ref=authorization["source_ref"],
            requested_by=authorization["requested_by"],
            mission_version_ref=authorization["mission_version_ref"],
            mission_version_hash=authorization["mission_version_hash"],
        )
        if exact != authorization:
            raise CoverageMissionConflict("discovery authorization drifted")
        discovery_plan_ref = _text(discovery_plan_ref, "discovery_plan_ref")
        discovery_plan_hash = _sha256(discovery_plan_hash, "discovery_plan_hash")
        spec_ref = _text(spec_ref, "spec_ref")
        query_hash = _sha256(query_hash, "query_hash")
        ticket_ref = _text(ticket_ref, "ticket_ref")
        created_at = _now()
        identity = {
            "mission_version_ref": exact["mission_version_ref"],
            "company_ref": exact["company_ref"],
            "source_ref": exact["source_ref"],
            "spec_ref": spec_ref,
            "query_hash": query_hash,
            "ticket_ref": ticket_ref,
        }
        dispatch_id = _ref("mission-discovery-dispatch", identity)
        record = {
            **identity,
            "dispatch_id": dispatch_id,
            "mission_version_hash": exact["mission_version_hash"],
            "discovery_plan_ref": discovery_plan_ref,
            "discovery_plan_hash": discovery_plan_hash,
            "actor_ref": exact["actor_ref"],
            "requested_by": exact["requested_by"],
            "authorization": exact,
            "status": "launched",
            "failure_reason": None,
            "created_at": created_at,
            "updated_at": created_at,
        }
        with self._transaction() as cur:
            existing = cur.execute(
                "SELECT dispatch_id FROM coverage_mission_discovery_dispatches WHERE dispatch_id=?",
                (dispatch_id,),
            ).fetchone()
            if existing is not None:
                raise CoverageMissionConflict("discovery dispatch already recorded for this ticket")
            cur.execute(
                "INSERT INTO coverage_mission_discovery_dispatches"
                "(dispatch_id,mission_version_ref,mission_version_hash,company_ref,source_ref,"
                "discovery_plan_ref,discovery_plan_hash,spec_ref,query_hash,actor_ref,requested_by,"
                "authorization_json,status,ticket_ref,failure_reason,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    dispatch_id, exact["mission_version_ref"], exact["mission_version_hash"],
                    exact["company_ref"], exact["source_ref"], discovery_plan_ref,
                    discovery_plan_hash, spec_ref, query_hash, exact["actor_ref"],
                    exact["requested_by"], canonical_json(exact), "launched", ticket_ref,
                    None, created_at, created_at,
                ),
            )
        return record

    def _dispatch_row(self, row: Any) -> dict[str, Any]:
        return {
            "dispatch_id": row["dispatch_id"],
            "mission_version_ref": row["mission_version_ref"],
            "mission_version_hash": row["mission_version_hash"],
            "company_ref": row["company_ref"],
            "source_ref": row["source_ref"],
            "discovery_plan_ref": row["discovery_plan_ref"],
            "discovery_plan_hash": row["discovery_plan_hash"],
            "spec_ref": row["spec_ref"],
            "query_hash": row["query_hash"],
            "actor_ref": row["actor_ref"],
            "requested_by": row["requested_by"],
            "authorization": json.loads(row["authorization_json"]),
            "status": row["status"],
            "ticket_ref": row["ticket_ref"],
            "failure_reason": row["failure_reason"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def open_discovery_dispatches(
        self, *, limit: int = 20, source_ref: str | None = None
    ) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise CoverageMissionValidationError("discovery dispatch limit must be 1..100")
        query = "SELECT * FROM coverage_mission_discovery_dispatches WHERE status='launched'"
        params: list[Any] = []
        if source_ref is not None:
            query += " AND source_ref=?"
            params.append(_text(source_ref, "source_ref"))
        query += " ORDER BY created_at,dispatch_id LIMIT ?"
        params.append(limit)
        rows = self.connection.execute(query, params).fetchall()
        return [self._dispatch_row(row) for row in rows]

    def discovery_dispatches(
        self, mission_version_ref: str, *, company_ref: str | None = None,
        spec_ref: str | None = None, limit: int = 100,
    ) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise CoverageMissionValidationError("discovery dispatch limit must be 1..1000")
        query = "SELECT * FROM coverage_mission_discovery_dispatches WHERE mission_version_ref=?"
        params: list[Any] = [_text(mission_version_ref, "mission_version_ref")]
        if company_ref is not None:
            query += " AND company_ref=?"
            params.append(_text(company_ref, "company_ref"))
        if spec_ref is not None:
            query += " AND spec_ref=?"
            params.append(_text(spec_ref, "spec_ref"))
        query += " ORDER BY created_at DESC,dispatch_id DESC LIMIT ?"
        params.append(limit)
        return [self._dispatch_row(row) for row in self.connection.execute(query, params).fetchall()]

    def settle_discovery_dispatch(
        self, dispatch_id: str, *, status: str, reason: str | None = None
    ) -> dict[str, Any]:
        dispatch_id = _text(dispatch_id, "dispatch_id")
        status = _vocabulary(status, ("succeeded", "failed", "rejected"), "status")
        if status != "succeeded":
            reason = _text(reason, "reason")
        elif reason is not None:
            raise CoverageMissionValidationError("a succeeded dispatch carries no failure reason")
        with self._transaction() as cur:
            row = cur.execute(
                "SELECT * FROM coverage_mission_discovery_dispatches WHERE dispatch_id=?",
                (dispatch_id,),
            ).fetchone()
            if row is None:
                raise CoverageMissionNotFound("discovery dispatch was not found")
            if row["status"] != "launched":
                if row["status"] == status and row["failure_reason"] == reason:
                    return self._dispatch_row(row)
                raise CoverageMissionConflict("discovery dispatch is already settled differently")
            now = _now()
            cur.execute(
                "UPDATE coverage_mission_discovery_dispatches SET status=?,failure_reason=?,"
                "updated_at=? WHERE dispatch_id=? AND status='launched'",
                (status, reason, now, dispatch_id),
            )
            if cur.rowcount != 1:
                raise CoverageMissionConflict("discovery dispatch state changed concurrently")
            row = cur.execute(
                "SELECT * FROM coverage_mission_discovery_dispatches WHERE dispatch_id=?",
                (dispatch_id,),
            ).fetchone()
        return self._dispatch_row(row)

    def record_source_discovery(
        self,
        *,
        authorization: Mapping[str, Any],
        discovery_plan_ref: str,
        discovery_plan_hash: str,
        spec_ref: str,
        query_hash: str,
        parameters: Mapping[str, Any],
        connector_invocation_ref: str,
        connector_invocation_hash: str,
        source_envelope_ref: str,
        source_envelope_hash: str,
        document_refs: list[str],
        in_authority_document_refs: list[str],
        document_hosts: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Append one discovery record and register its new documents.

        The search itself already left Core connector authority behind; this
        binds that exact invocation / envelope to the mission, company and
        plan spec, and opens one ``discovered`` document row per ref Core does
        not yet hold.  Documents already in authority are recorded as such
        and never re-queued.
        """

        authorization = self._validate_discovery_authorization(authorization)
        hosts = _document_hosts(document_hosts)
        exact = self.authorize_source_discovery(
            company_ref=authorization["company_ref"],
            source_ref=authorization["source_ref"],
            requested_by=authorization["requested_by"],
            mission_version_ref=authorization["mission_version_ref"],
            mission_version_hash=authorization["mission_version_hash"],
        )
        if exact != authorization:
            raise CoverageMissionConflict("discovery authorization drifted")
        discovery_plan_ref = _text(discovery_plan_ref, "discovery_plan_ref")
        discovery_plan_hash = _sha256(discovery_plan_hash, "discovery_plan_hash")
        spec_ref = _text(spec_ref, "spec_ref")
        query_hash = _sha256(query_hash, "query_hash")
        connector_invocation_ref = _text(connector_invocation_ref, "connector_invocation_ref")
        connector_invocation_hash = _sha256(connector_invocation_hash, "connector_invocation_hash")
        source_envelope_ref = _text(source_envelope_ref, "source_envelope_ref")
        source_envelope_hash = _sha256(source_envelope_hash, "source_envelope_hash")
        refs = _texts(document_refs, "document_refs")
        present = _texts(in_authority_document_refs, "in_authority_document_refs")
        if not set(present) <= set(refs):
            raise CoverageMissionValidationError(
                "in_authority_document_refs must be a subset of document_refs"
            )
        new_refs = [ref for ref in refs if ref not in set(present)]
        invocation = self.connection.execute(
            "SELECT content_hash FROM connector_invocations WHERE connector_invocation_id=?",
            (connector_invocation_ref,),
        ).fetchone()
        if invocation is None or invocation["content_hash"] != connector_invocation_hash:
            raise CoverageMissionConflict("discovery connector invocation binding failed")
        envelope = self.connection.execute(
            "SELECT connector_invocation_ref,content_hash,record_json FROM "
            "connector_source_envelopes WHERE source_envelope_id=?",
            (source_envelope_ref,),
        ).fetchone()
        if (
            envelope is None
            or envelope["content_hash"] != source_envelope_hash
            or envelope["connector_invocation_ref"] != connector_invocation_ref
        ):
            raise CoverageMissionConflict("discovery source envelope binding failed")
        envelope_record = json.loads(envelope["record_json"])
        discovery_source = DISCOVERY_SOURCES[exact["source_ref"]]
        if (
            envelope_record.get("source") != discovery_source["connector_source_ref"]
            or envelope_record.get("operation") != discovery_source["operation"]
            or list(envelope_record.get("source_record_refs") or []) != refs
        ):
            raise CoverageMissionConflict("discovery document_refs differ from the source envelope")
        identity = {
            "mission_version_ref": exact["mission_version_ref"],
            "mission_version_hash": exact["mission_version_hash"],
            "company_ref": exact["company_ref"],
            "source_ref": exact["source_ref"],
            "discovery_plan_ref": discovery_plan_ref,
            "discovery_plan_hash": discovery_plan_hash,
            "spec_ref": spec_ref,
            "query_hash": query_hash,
            "parameters": json.loads(canonical_json(parameters)),
            "connector_invocation_ref": connector_invocation_ref,
            "connector_invocation_hash": connector_invocation_hash,
            "source_envelope_ref": source_envelope_ref,
            "source_envelope_hash": source_envelope_hash,
            "document_refs": refs,
            "new_document_refs": new_refs,
            "in_authority_document_refs": present,
            "actor_ref": exact["actor_ref"],
            "requested_by": exact["requested_by"],
        }
        record_id = _ref("mission-source-discovery", identity)
        existing = self.connection.execute(
            "SELECT record_json FROM coverage_mission_source_discoveries "
            "WHERE mission_version_ref=? AND source_envelope_ref=?",
            (exact["mission_version_ref"], source_envelope_ref),
        ).fetchone()
        if existing is not None:
            wire = validate_mission_source_discovery(
                _canonical_record(existing["record_json"], "mission source discovery")
            )
            if wire["id"] != record_id:
                raise CoverageMissionConflict("source envelope is already bound to another discovery")
            return {**wire, "status": "duplicate"}
        created_at = _now()
        record = {"schema_version": SCHEMA_VERSION, "id": record_id, "created_at": created_at, **identity}
        wire = {**record, "content_hash": content_hash(record)}
        validate_mission_source_discovery(wire)
        with self._transaction() as cur:
            cur.execute(
                "INSERT INTO coverage_mission_source_discoveries"
                "(record_id,mission_version_ref,mission_version_hash,company_ref,source_ref,"
                "discovery_plan_ref,discovery_plan_hash,spec_ref,query_hash,connector_invocation_ref,"
                "source_envelope_ref,source_envelope_hash,actor_ref,requested_by,record_json,"
                "content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record_id, exact["mission_version_ref"], exact["mission_version_hash"],
                    exact["company_ref"], exact["source_ref"], discovery_plan_ref,
                    discovery_plan_hash, spec_ref, query_hash, connector_invocation_ref,
                    source_envelope_ref, source_envelope_hash, exact["actor_ref"],
                    exact["requested_by"], canonical_json(wire), wire["content_hash"], created_at,
                ),
            )
            for ref in refs:
                status = "already_in_authority" if ref in set(present) else "discovered"
                document_id = _ref(
                    "mission-discovered-document",
                    {"mission_version_ref": exact["mission_version_ref"], "document_ref": ref},
                )
                if cur.execute(
                    "SELECT 1 FROM coverage_mission_discovered_documents WHERE record_id=?",
                    (document_id,),
                ).fetchone() is not None:
                    continue
                cur.execute(
                    "INSERT INTO coverage_mission_discovered_documents"
                    "(record_id,mission_version_ref,company_ref,source_ref,document_ref,"
                    "discovery_ref,status,ticket_ref,failure_reason,created_at,updated_at,host) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        document_id, exact["mission_version_ref"], exact["company_ref"],
                        exact["source_ref"], ref, record_id, status, None, None,
                        created_at, created_at, hosts.get(ref),
                    ),
                )
        return {**wire, "status": "fresh"}

    def source_discoveries(
        self, mission_version_ref: str, *, company_ref: str | None = None,
        spec_ref: str | None = None, limit: int = 100,
    ) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise CoverageMissionValidationError("discovery limit must be 1..1000")
        query = "SELECT * FROM coverage_mission_source_discoveries WHERE mission_version_ref=?"
        params: list[Any] = [_text(mission_version_ref, "mission_version_ref")]
        if company_ref is not None:
            query += " AND company_ref=?"
            params.append(_text(company_ref, "company_ref"))
        if spec_ref is not None:
            query += " AND spec_ref=?"
            params.append(_text(spec_ref, "spec_ref"))
        query += " ORDER BY created_at DESC,record_id DESC LIMIT ?"
        params.append(limit)
        records = []
        for row in self.connection.execute(query, params).fetchall():
            wire = validate_mission_source_discovery(
                _canonical_record(row["record_json"], "mission source discovery")
            )
            if wire["id"] != row["record_id"] or wire["content_hash"] != row["content_hash"]:
                raise CoverageMissionConflict("mission source discovery authority drifted")
            records.append(wire)
        return records

    def _document_row(self, row: Any) -> dict[str, Any]:
        return {
            "record_id": row["record_id"],
            "mission_version_ref": row["mission_version_ref"],
            "company_ref": row["company_ref"],
            "source_ref": row["source_ref"],
            "document_ref": row["document_ref"],
            "discovery_ref": row["discovery_ref"],
            "status": row["status"],
            "ticket_ref": row["ticket_ref"],
            "failure_reason": row["failure_reason"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "host": row["host"],
        }

    def discovered_documents(
        self, mission_version_ref: str, *, company_ref: str | None = None,
        status: str | None = None, limit: int = 100,
    ) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise CoverageMissionValidationError("discovered document limit must be 1..1000")
        query = "SELECT * FROM coverage_mission_discovered_documents WHERE mission_version_ref=?"
        params: list[Any] = [_text(mission_version_ref, "mission_version_ref")]
        if company_ref is not None:
            query += " AND company_ref=?"
            params.append(_text(company_ref, "company_ref"))
        if status is not None:
            query += " AND status=?"
            params.append(_vocabulary(status, DISCOVERED_DOCUMENT_STATUSES, "status"))
        query += " ORDER BY created_at,record_id LIMIT ?"
        params.append(limit)
        return [self._document_row(row) for row in self.connection.execute(query, params).fetchall()]

    def next_discovered_document(
        self,
        *,
        source_ref: str | None = None,
        preferred_hosts: Sequence[str] = (),
        skip_hosts: Sequence[str] = (),
        preferred_needs: Sequence[Mapping[str, str]] = (),
    ) -> dict[str, Any] | None:
        """Next ``discovered`` document across active missions, or None.

        ``source_ref`` narrows to one discovery source: each acquisition lane
        only ever picks documents its own connector can fetch.

        P9d-13: the plan may declare an acquisition policy.  Documents on a
        ``preferred_hosts`` host (first-party investor relations, typically)
        come before the rest; within each group the oldest wins.  Documents on
        a ``skip_hosts`` host are never picked: those hosts refuse the lane and
        every attempt would only spend a governed call to learn it again.
        Rows without a host (non-URL sources, or not yet backfilled) are
        neither preferred nor skipped.

        P10a: ``preferred_needs`` is an ordered list of ``{company_ref,
        spec_ref}`` the mission still needs for its current stage.  A document
        matching the first need comes before one matching the second, and both
        before a document no stage needs.  Without it the oldest row wins,
        which on live spent a whole day of governed calls on one company.
        """

        preferred = _host_list(preferred_hosts, "preferred_hosts")
        skipped = _host_list(skip_hosts, "skip_hosts")
        needs = _needs(preferred_needs, "preferred_needs")
        query = (
            "SELECT d.* FROM coverage_mission_discovered_documents d "
            "JOIN coverage_mission_pointer p ON p.mission_version_id=d.mission_version_ref "
            "LEFT JOIN coverage_mission_source_discoveries s ON s.record_id=d.discovery_ref "
            "WHERE d.status='discovered'"
        )
        params: list[Any] = []
        if source_ref is not None:
            query += " AND d.source_ref=?"
            params.append(_text(source_ref, "source_ref"))
        if skipped:
            query += " AND (d.host IS NULL OR d.host NOT IN (%s))" % ",".join("?" * len(skipped))
            params.extend(skipped)
        query += " ORDER BY"
        if needs:
            clauses = " ".join(
                "WHEN d.company_ref=? AND s.spec_ref=? THEN %d" % index
                for index in range(len(needs))
            )
            query += " CASE %s ELSE %d END," % (clauses, len(needs))
            for need in needs:
                params.extend((need["company_ref"], need["spec_ref"]))
        if preferred:
            query += " CASE WHEN d.host IN (%s) THEN 0 ELSE 1 END," % ",".join("?" * len(preferred))
            params.extend(preferred)
        query += " d.created_at,d.record_id LIMIT 1"
        row = self.connection.execute(query, params).fetchone()
        return None if row is None else self._document_row(row)

    def record_research_plan(
        self, plan: Mapping[str, Any], *, decided_by: str,
        model_profile_ref: str | None = None, work_order_ref: str | None = None,
    ) -> dict[str, Any]:
        """Store one verified plan, bound to the state it was decided from.

        A plan is a proposal about the system's own work, not a claim about the
        world, so it lives here rather than in the Ledger. Keyed by (mission
        version, state hash): deciding twice against an unchanged state is the
        same decision, and re-deciding costs nothing.
        """

        for field in ("state_hash", "assessment", "directives", "inquiries", "content_hash"):
            if field not in plan:
                raise CoverageMissionValidationError(f"plan is missing {field}")
        mission_version_ref = _text(
            plan.get("mission_version_ref") or "", "mission_version_ref")
        state_hash = _text(plan["state_hash"], "state_hash")
        decided_by = _text(decided_by, "decided_by")
        plan_id = _ref("mission-research-plan", {
            "mission_version_ref": mission_version_ref, "state_hash": state_hash,
        })
        existing = self.connection.execute(
            "SELECT * FROM coverage_mission_research_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if existing is not None:
            return {**self._plan_row(existing), "status": "duplicate"}
        now = _now()
        with self._transaction() as cur:
            cur.execute(
                "INSERT INTO coverage_mission_research_plans("
                "plan_id,mission_version_ref,state_hash,assessment,directives_json,"
                "inquiries_json,sufficiency_json,model_profile_ref,work_order_ref,"
                "decided_by,created_at,content_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (plan_id, mission_version_ref, state_hash, plan["assessment"],
                 canonical_json(plan["directives"]), canonical_json(plan["inquiries"]),
                 canonical_json(plan.get("sufficiency") or []),
                 model_profile_ref, work_order_ref, decided_by, now, plan["content_hash"]),
            )
            row = cur.execute(
                "SELECT * FROM coverage_mission_research_plans WHERE plan_id=?", (plan_id,)
            ).fetchone()
        return {**self._plan_row(row), "status": "fresh"}

    @staticmethod
    def _plan_row(row: Any) -> dict[str, Any]:
        wire = dict(row)
        wire["directives"] = json.loads(wire.pop("directives_json"))
        wire["inquiries"] = json.loads(wire.pop("inquiries_json"))
        raw = wire.pop("sufficiency_json", None)
        # A plan written before judgements existed made none; an empty list is
        # "said nothing", which is what the lane treats as silence anyway.
        wire["sufficiency"] = json.loads(raw) if raw else []
        return wire

    def latest_research_plan(self, mission_version_ref: str) -> dict[str, Any] | None:
        """The most recent plan for this mission version, or None."""

        row = self.connection.execute(
            "SELECT * FROM coverage_mission_research_plans WHERE mission_version_ref=? "
            "ORDER BY created_at DESC, plan_id DESC LIMIT 1",
            (_text(mission_version_ref, "mission_version_ref"),),
        ).fetchone()
        return None if row is None else self._plan_row(row)

    def research_plan_for_state(
        self, mission_version_ref: str, state_hash: str
    ) -> dict[str, Any] | None:
        """The plan decided from exactly this state, if one was."""

        row = self.connection.execute(
            "SELECT * FROM coverage_mission_research_plans "
            "WHERE mission_version_ref=? AND state_hash=?",
            (_text(mission_version_ref, "mission_version_ref"),
             _text(state_hash, "state_hash")),
        ).fetchone()
        return None if row is None else self._plan_row(row)

    def record_metric_observations(
        self, *, company_ref: str, proposals: Sequence[Mapping[str, Any]], observed_by: str
    ) -> dict[str, Any]:
        """Journal verified metric proposals for one company.

        Every proposal must already have been verified against its own quote by
        ``verify_metric_proposal``; this stores what was seen, it does not judge
        it.  Storing is idempotent per (company, metric, document), so the same
        document read again adds nothing and cannot corroborate itself.
        """

        from .metric_discovery import validate_metric_proposal

        company_ref = _text(company_ref, "company_ref")
        observed_by = _text(observed_by, "observed_by")
        if not isinstance(proposals, Sequence) or isinstance(proposals, (str, bytes)):
            raise CoverageMissionValidationError("proposals must be a sequence")
        wires = []
        for item in proposals:
            if not isinstance(item, Mapping):
                raise CoverageMissionValidationError("each proposal must be an object")
            wire = validate_metric_proposal({key: item.get(key) for key in (
                "metric_ref", "label", "unit", "evidence_phrase", "quote_id", "document_ref",
            )})
            citation = item.get("citation_text")
            if not isinstance(citation, str) or not citation.strip():
                raise CoverageMissionValidationError(
                    "a metric observation must carry the quote that proposed it"
                )
            wires.append((wire, citation))
        now = _now()
        recorded, duplicates, retracted = [], [], []
        with self._transaction() as cur:
            for wire, citation in wires:
                observation_id = _ref("mission-metric-observation", {
                    "company_ref": company_ref,
                    "metric_ref": wire["metric_ref"],
                    "document_ref": wire["document_ref"],
                })
                existing = cur.execute(
                    "SELECT observation_id FROM coverage_mission_metric_observations "
                    "WHERE observation_id=?", (observation_id,),
                ).fetchone()
                if existing is not None:
                    # A retracted observation is held, not free: re-proposing it
                    # does not revive it. Reported apart from a plain duplicate
                    # because "we have this" and "we decided this was wrong" are
                    # different answers, and the second one wants looking at.
                    if cur.execute(
                        "SELECT 1 FROM coverage_mission_metric_observation_retractions "
                        "WHERE observation_id=?", (observation_id,),
                    ).fetchone() is not None:
                        retracted.append(wire["metric_ref"])
                    else:
                        duplicates.append(wire["metric_ref"])
                    continue
                cur.execute(
                    "INSERT INTO coverage_mission_metric_observations("
                    "observation_id,company_ref,document_ref,metric_ref,label,unit,"
                    "evidence_phrase,quote_id,citation_text,observed_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        observation_id, company_ref, wire["document_ref"], wire["metric_ref"],
                        wire["label"], wire["unit"], wire["evidence_phrase"], wire["quote_id"],
                        citation, observed_by, now,
                    ),
                )
                recorded.append(wire["metric_ref"])
        return {"recorded": recorded, "duplicates": duplicates, "retracted": retracted}

    def record_document_figures(
        self,
        *,
        company_ref: str,
        review_ref: str,
        document_ref: str,
        source_manifest_hash: str,
        source_grade: str,
        figures: Sequence[Mapping[str, Any]],
        observed_by: str,
    ) -> dict[str, Any]:
        """Journal figures read out of one document's window.

        The digit and label check is re-run *here*, against the citation text
        each figure carries, rather than trusted from the caller.  It is the
        one thing that separates a figure from a number a model produced, so it
        belongs at the boundary that writes rather than in the code that asks:
        a future caller that forgets to verify cannot write an unverified row.
        """

        from .document_figure_grade import GRADES
        from .document_numeric_claim import (
            NumericCandidateError,
            verify_numeric_candidate,
        )

        company_ref = _text(company_ref, "company_ref")
        review_ref = _text(review_ref, "review_ref")
        document_ref = _text(document_ref, "document_ref")
        source_manifest_hash = _text(source_manifest_hash, "source_manifest_hash")
        observed_by = _text(observed_by, "observed_by")
        if source_grade not in GRADES:
            raise CoverageMissionValidationError(
                f"source_grade must be one of {sorted(GRADES)}"
            )
        if not isinstance(figures, Sequence) or isinstance(figures, (str, bytes)):
            raise CoverageMissionValidationError("figures must be a sequence")
        wires: list[dict[str, Any]] = []
        for item in figures:
            if not isinstance(item, Mapping):
                raise CoverageMissionValidationError("each figure must be an object")
            citation = item.get("citation_text")
            if not isinstance(citation, str) or not citation.strip():
                raise CoverageMissionValidationError(
                    "a figure must carry the exact quote it was read from"
                )
            candidate = {key: item.get(key) for key in (
                "quote_id", "metric_ref", "subject_as_named", "as_reported_label", "value",
                "unit",
                "currency", "period", "basis", "scale",
            )}
            try:
                wires.append(verify_numeric_candidate(
                    candidate, {candidate["quote_id"]: citation}))
            except NumericCandidateError as exc:
                raise CoverageMissionValidationError(
                    f"figure is not supported by the quote it cites: {exc}"
                ) from exc
        now = _now()
        recorded, duplicates, retracted = [], [], []
        with self._transaction() as cur:
            for wire in wires:
                # P12i: what makes two figures the same fact is the company,
                # the measure, the period and the document -- not which window
                # happened to find it. ACN's first two figures were one fact:
                # $69.7B of fiscal 2025 revenue, read at offset 40,800 and
                # again at 186,000, kept apart only by "Fiscal 2025" against
                # "fiscal 2025". The quote is data, not identity.
                figure_id = _ref("mission-document-figure", {
                    "company_ref": company_ref,
                    "metric_ref": wire["metric_ref"],
                    "period": _period_key(wire["period"]),
                    "document_ref": document_ref,
                })
                if cur.execute(
                    "SELECT 1 FROM coverage_mission_document_figures WHERE figure_id=?",
                    (figure_id,),
                ).fetchone() is not None:
                    # A withdrawn figure keeps its identity, so re-reading the
                    # same fact from the same document cannot quietly reinstate
                    # it. Reported separately: "we already have this" and "we
                    # decided this one was wrong" are different answers.
                    if cur.execute(
                        "SELECT 1 FROM coverage_mission_document_figure_retractions "
                        "WHERE figure_id=?", (figure_id,),
                    ).fetchone() is not None:
                        retracted.append(wire["metric_ref"])
                    else:
                        duplicates.append(wire["metric_ref"])
                    continue
                record = {
                    "figure_id": figure_id, "company_ref": company_ref,
                    "review_ref": review_ref, "document_ref": document_ref,
                    "source_manifest_hash": source_manifest_hash,
                    "quote_id": wire["quote_id"],
                    "citation_text": wire["citation_text"],
                    "metric_ref": wire["metric_ref"],
                    "as_reported_label": wire["as_reported_label"],
                    "period": wire["period"], "value": wire["value"],
                    "unit": wire["unit"], "currency": wire["currency"],
                    "scale": wire["scale"], "basis": wire["basis"],
                    "source_grade": source_grade,
                    "verified_by": DOCUMENT_FIGURE_VERIFIER_REF,
                    "observed_by": observed_by, "created_at": now,
                }
                record["content_hash"] = content_hash(record)
                cur.execute(
                    "INSERT INTO coverage_mission_document_figures("
                    "figure_id,company_ref,review_ref,document_ref,source_manifest_hash,"
                    "quote_id,citation_text,metric_ref,as_reported_label,period,value,"
                    "unit,currency,scale,basis,source_grade,verified_by,observed_by,"
                    "created_at,content_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    tuple(record[key] for key in (
                        "figure_id", "company_ref", "review_ref", "document_ref",
                        "source_manifest_hash", "quote_id", "citation_text", "metric_ref",
                        "as_reported_label", "period", "value", "unit", "currency",
                        "scale", "basis", "source_grade", "verified_by", "observed_by",
                        "created_at", "content_hash",
                    )),
                )
                recorded.append(wire["metric_ref"])
        return {"recorded": recorded, "duplicates": duplicates,
                "retracted": retracted, "source_grade": source_grade}

    def retract_document_figure(
        self, figure_id: str, *, reason: str, retracted_by: str
    ) -> dict[str, Any]:
        """Withdraw a figure that should never have been recorded."""

        figure_id = _text(figure_id, "figure_id")
        reason = _text(reason, "reason")
        retracted_by = _text(retracted_by, "retracted_by")
        row = self.connection.execute(
            "SELECT 1 FROM coverage_mission_document_figures WHERE figure_id=?",
            (figure_id,),
        ).fetchone()
        if row is None:
            raise CoverageMissionNotFound("document figure was not found")
        existing = self.connection.execute(
            "SELECT * FROM coverage_mission_document_figure_retractions WHERE figure_id=?",
            (figure_id,),
        ).fetchone()
        if existing is not None:
            return {**dict(existing), "status_marker": "duplicate"}
        now = _now()
        with self._transaction() as cur:
            cur.execute(
                "INSERT INTO coverage_mission_document_figure_retractions("
                "figure_id,reason,retracted_by,retracted_at) VALUES(?,?,?,?)",
                (figure_id, reason, retracted_by, now),
            )
        return {"figure_id": figure_id, "reason": reason, "retracted_by": retracted_by,
                "retracted_at": now, "status_marker": "fresh"}

    def retracted_document_figures(self) -> list[dict[str, Any]]:
        """Every withdrawn figure and why, so the mistake stays legible."""

        return [dict(row) for row in self.connection.execute(
            "SELECT r.*, f.company_ref, f.metric_ref, f.value, f.unit, f.currency, "
            "f.document_ref FROM coverage_mission_document_figure_retractions r "
            "JOIN coverage_mission_document_figures f ON f.figure_id=r.figure_id "
            "ORDER BY r.retracted_at, r.figure_id"
        ).fetchall()]

    def document_figures(
        self,
        company_ref: str,
        *,
        source_grade: str | None = None,
        metric_ref: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Figures held for one company, newest last, filterable by grade.

        The grade filter is the point of storing it: a model that should only
        stand on published figures asks for ``company-filed-document`` and gets
        exactly those, without the spoken ones having been discarded.
        """

        # A retracted figure is not data any more; no read returns it.
        query = (
            "SELECT f.* FROM coverage_mission_document_figures f "
            "LEFT JOIN coverage_mission_document_figure_retractions r "
            "ON r.figure_id=f.figure_id "
            "WHERE r.figure_id IS NULL AND f.company_ref=?"
        )
        params: list[Any] = [_text(company_ref, "company_ref")]
        if source_grade is not None:
            from .document_figure_grade import GRADES

            if source_grade not in GRADES:
                raise CoverageMissionValidationError(
                    f"source_grade must be one of {sorted(GRADES)}"
                )
            query += " AND f.source_grade=?"
            params.append(source_grade)
        if metric_ref is not None:
            query += " AND f.metric_ref=?"
            params.append(_text(metric_ref, "metric_ref"))
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 2000:
            raise CoverageMissionValidationError("document figure limit must be 1..2000")
        query += " ORDER BY f.created_at, f.figure_id LIMIT ?"
        params.append(limit)
        return [dict(row) for row in self.connection.execute(query, params).fetchall()]

    def retract_metric_observation(
        self, observation_id: str, *, reason: str, retracted_by: str
    ) -> dict[str, Any]:
        """Unlearn a measure that was never this company's."""

        observation_id = _text(observation_id, "observation_id")
        reason = _text(reason, "reason")
        retracted_by = _text(retracted_by, "retracted_by")
        row = self.connection.execute(
            "SELECT 1 FROM coverage_mission_metric_observations WHERE observation_id=?",
            (observation_id,),
        ).fetchone()
        if row is None:
            raise CoverageMissionNotFound("metric observation was not found")
        existing = self.connection.execute(
            "SELECT * FROM coverage_mission_metric_observation_retractions "
            "WHERE observation_id=?", (observation_id,),
        ).fetchone()
        if existing is not None:
            return {**dict(existing), "status_marker": "duplicate"}
        now = _now()
        with self._transaction() as cur:
            cur.execute(
                "INSERT INTO coverage_mission_metric_observation_retractions("
                "observation_id,reason,retracted_by,retracted_at) VALUES(?,?,?,?)",
                (observation_id, reason, retracted_by, now),
            )
        return {"observation_id": observation_id, "reason": reason,
                "retracted_by": retracted_by, "retracted_at": now, "status_marker": "fresh"}

    def retracted_metric_observations(self) -> list[dict[str, Any]]:
        """Every unlearned measure and why, so the mistake stays legible."""

        return [dict(row) for row in self.connection.execute(
            "SELECT r.*, o.company_ref, o.metric_ref, o.label, o.unit, o.document_ref "
            "FROM coverage_mission_metric_observation_retractions r "
            "JOIN coverage_mission_metric_observations o "
            "ON o.observation_id=r.observation_id "
            "ORDER BY r.retracted_at, r.observation_id"
        ).fetchall()]

    def metric_observations(self, company_ref: str) -> list[dict[str, Any]]:
        """Every measure this company has been seen judged on, as proposals.

        A retracted observation is not evidence any more, so no read returns
        it -- the same rule ``document_figures`` follows.
        """

        rows = self.connection.execute(
            "SELECT o.metric_ref,o.label,o.unit,o.evidence_phrase,o.quote_id,o.document_ref "
            "FROM coverage_mission_metric_observations o "
            "LEFT JOIN coverage_mission_metric_observation_retractions r "
            "ON r.observation_id=o.observation_id "
            "WHERE r.observation_id IS NULL AND o.company_ref=? "
            "ORDER BY o.created_at, o.observation_id",
            (_text(company_ref, "company_ref"),),
        ).fetchall()
        return [dict(row) for row in rows]

    def metric_requirements(self, company_ref: str) -> list[dict[str, Any]]:
        """The corroborated requirements derived from those observations.

        Derived on read rather than stored: the corroboration threshold is one
        rule in one place, and a company that gains its second citation today
        becomes owed the figure today without anything being rewritten.
        """

        from .metric_discovery import establish_requirements

        return establish_requirements(self.metric_observations(company_ref))

    def document_spec_refs(self, mission_version_ref: str) -> dict[str, str]:
        """document_ref → the discovery spec that found it (P10a reading order)."""

        rows = self.connection.execute(
            "SELECT d.document_ref AS document_ref, s.spec_ref AS spec_ref "
            "FROM coverage_mission_discovered_documents d "
            "JOIN coverage_mission_source_discoveries s ON s.record_id=d.discovery_ref "
            "WHERE d.mission_version_ref=?",
            (_text(mission_version_ref, "mission_version_ref"),),
        ).fetchall()
        return {row["document_ref"]: row["spec_ref"] for row in rows}

    def discovered_documents_held_by_skip(
        self, *, source_ref: str | None = None, skip_hosts: Sequence[str] = ()
    ) -> int:
        """How many ``discovered`` documents the plan's ``skip_hosts`` are holding back."""

        skipped = _host_list(skip_hosts, "skip_hosts")
        if not skipped:
            return 0
        query = (
            "SELECT COUNT(*) FROM coverage_mission_discovered_documents d "
            "JOIN coverage_mission_pointer p ON p.mission_version_id=d.mission_version_ref "
            "WHERE d.status='discovered' AND d.host IN (%s)" % ",".join("?" * len(skipped))
        )
        params: list[Any] = list(skipped)
        if source_ref is not None:
            query += " AND d.source_ref=?"
            params.append(_text(source_ref, "source_ref"))
        return int(self.connection.execute(query, params).fetchone()[0])

    def already_held_documents(
        self, *, source_ref: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Documents recorded ``already_in_authority`` under active missions.

        P9d-12: search marks a citation this way when Core already holds its
        bytes (a human fetched it first).  Nothing needs fetching, but the
        document still owes the human queue a review; the coordinator settles
        these to ``acquired`` and registers that review.
        """

        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise CoverageMissionValidationError("discovered document limit must be 1..100")
        query = (
            "SELECT d.* FROM coverage_mission_discovered_documents d "
            "JOIN coverage_mission_pointer p ON p.mission_version_id=d.mission_version_ref "
            "WHERE d.status='already_in_authority'"
        )
        params: list[Any] = []
        if source_ref is not None:
            query += " AND d.source_ref=?"
            params.append(_text(source_ref, "source_ref"))
        query += " ORDER BY d.created_at,d.record_id LIMIT ?"
        params.append(limit)
        return [self._document_row(row) for row in self.connection.execute(query, params).fetchall()]

    def documents_without_host(
        self, *, source_ref: str, limit: int = 25
    ) -> list[dict[str, Any]]:
        """Rows recorded before P9d-13 whose host is still unknown, oldest first."""

        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise CoverageMissionValidationError("discovered document limit must be 1..100")
        rows = self.connection.execute(
            "SELECT d.* FROM coverage_mission_discovered_documents d "
            "JOIN coverage_mission_pointer p ON p.mission_version_id=d.mission_version_ref "
            "WHERE d.host IS NULL AND d.source_ref=? ORDER BY d.created_at,d.record_id LIMIT ?",
            (_text(source_ref, "source_ref"), limit),
        ).fetchall()
        return [self._document_row(row) for row in rows]

    def set_document_host(self, record_id: str, host: str) -> dict[str, Any]:
        """Backfill one row's host; only a NULL host may be written, once."""

        record_id = _text(record_id, "record_id")
        host = _host_list([host], "host")[0]
        with self._transaction() as cur:
            cur.execute(
                "UPDATE coverage_mission_discovered_documents SET host=? "
                "WHERE record_id=? AND host IS NULL",
                (host, record_id),
            )
            if cur.rowcount != 1:
                raise CoverageMissionConflict("document host is already recorded or the row is missing")
            row = cur.execute(
                "SELECT * FROM coverage_mission_discovered_documents WHERE record_id=?", (record_id,)
            ).fetchone()
        return self._document_row(row)

    def discovery_record(self, record_id: str) -> dict[str, Any]:
        """One source discovery by id, hash-verified."""

        record_id = _text(record_id, "record_id")
        row = self.connection.execute(
            "SELECT * FROM coverage_mission_source_discoveries WHERE record_id=?", (record_id,)
        ).fetchone()
        if row is None:
            raise CoverageMissionNotFound("mission source discovery was not found")
        wire = validate_mission_source_discovery(
            _canonical_record(row["record_json"], "mission source discovery")
        )
        if wire["id"] != row["record_id"] or wire["content_hash"] != row["content_hash"]:
            raise CoverageMissionConflict("mission source discovery authority drifted")
        return wire

    def retryable_failed_document(
        self, *, older_than: timedelta, as_of: datetime | None = None,
        source_ref: str | None = None, skip_hosts: Sequence[str] = (),
    ) -> dict[str, Any] | None:
        """Oldest ``acquisition_failed`` document whose last update is older
        than the retry interval, or None.  Failures (provider errors, orphaned
        children after a deploy restart) are retried once the interval passes;
        the wait keeps the shared budget honest instead of hot-looping."""

        if not isinstance(older_than, timedelta) or older_than <= timedelta(0):
            raise CoverageMissionValidationError("retry interval must be a positive duration")
        now = as_of if as_of is not None else datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise CoverageMissionValidationError("as_of must carry a timezone")
        cutoff = (now - older_than).isoformat(timespec="microseconds")
        query = (
            "SELECT d.* FROM coverage_mission_discovered_documents d "
            "JOIN coverage_mission_pointer p ON p.mission_version_id=d.mission_version_ref "
            "WHERE d.status='acquisition_failed' AND d.updated_at<?"
        )
        params: list[Any] = [cutoff]
        if source_ref is not None:
            query += " AND d.source_ref=?"
            params.append(_text(source_ref, "source_ref"))
        skipped = _host_list(skip_hosts, "skip_hosts")
        if skipped:
            # A host the plan skips is not retried either; it would only fail again.
            query += " AND (d.host IS NULL OR d.host NOT IN (%s))" % ",".join("?" * len(skipped))
            params.extend(skipped)
        query += " ORDER BY d.updated_at,d.record_id LIMIT 1"
        row = self.connection.execute(query, params).fetchone()
        return None if row is None else self._document_row(row)

    def launched_discovered_documents(
        self, *, limit: int = 20, source_ref: str | None = None
    ) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise CoverageMissionValidationError("discovered document limit must be 1..100")
        query = "SELECT * FROM coverage_mission_discovered_documents WHERE status='acquisition_launched'"
        params: list[Any] = []
        if source_ref is not None:
            query += " AND source_ref=?"
            params.append(_text(source_ref, "source_ref"))
        query += " ORDER BY updated_at,record_id LIMIT ?"
        params.append(limit)
        rows = self.connection.execute(query, params).fetchall()
        return [self._document_row(row) for row in rows]

    def mark_discovered_document_launched(self, record_id: str, ticket_ref: str) -> dict[str, Any]:
        record_id = _text(record_id, "record_id")
        ticket_ref = _text(ticket_ref, "ticket_ref")
        with self._transaction() as cur:
            row = cur.execute(
                "SELECT * FROM coverage_mission_discovered_documents WHERE record_id=?", (record_id,)
            ).fetchone()
            if row is None:
                raise CoverageMissionNotFound("discovered document was not found")
            if row["status"] != "discovered":
                if row["status"] == "acquisition_launched" and row["ticket_ref"] == ticket_ref:
                    return self._document_row(row)
                raise CoverageMissionConflict("discovered document is not awaiting acquisition")
            now = _now()
            cur.execute(
                "UPDATE coverage_mission_discovered_documents SET status='acquisition_launched',"
                "ticket_ref=?,updated_at=? WHERE record_id=? AND status='discovered'",
                (ticket_ref, now, record_id),
            )
            if cur.rowcount != 1:
                raise CoverageMissionConflict("discovered document state changed concurrently")
            row = cur.execute(
                "SELECT * FROM coverage_mission_discovered_documents WHERE record_id=?", (record_id,)
            ).fetchone()
        return self._document_row(row)

    def mark_failed_document_retry_launched(self, record_id: str, ticket_ref: str) -> dict[str, Any]:
        """Move an ``acquisition_failed`` row back to ``acquisition_launched``
        for a bounded retry.  The coordinator enforces the retry interval; the
        prior failure reason is cleared so the row reads like a fresh launch."""

        record_id = _text(record_id, "record_id")
        ticket_ref = _text(ticket_ref, "ticket_ref")
        with self._transaction() as cur:
            row = cur.execute(
                "SELECT * FROM coverage_mission_discovered_documents WHERE record_id=?", (record_id,)
            ).fetchone()
            if row is None:
                raise CoverageMissionNotFound("discovered document was not found")
            if row["status"] != "acquisition_failed":
                raise CoverageMissionConflict("discovered document has not failed acquisition")
            now = _now()
            cur.execute(
                "UPDATE coverage_mission_discovered_documents SET status='acquisition_launched',"
                "ticket_ref=?,failure_reason=NULL,updated_at=? "
                "WHERE record_id=? AND status='acquisition_failed'",
                (ticket_ref, now, record_id),
            )
            if cur.rowcount != 1:
                raise CoverageMissionConflict("discovered document state changed concurrently")
            row = cur.execute(
                "SELECT * FROM coverage_mission_discovered_documents WHERE record_id=?", (record_id,)
            ).fetchone()
        return self._document_row(row)

    def settle_document_already_held(self, record_id: str) -> dict[str, Any]:
        """Settle a document whose bytes Core already holds.

        A human acquisition puts original bytes into connector authority
        without touching this ledger, so the row can read ``discovered`` (the
        fetch came after discovery) or ``already_in_authority`` (search found
        the bytes already there) while the document is fully held.  The caller
        proves the bytes are in authority for this source; this moves the row
        to ``acquired`` so the human review queue picks it up and no second
        paid fetch is spent.
        """

        record_id = _text(record_id, "record_id")
        with self._transaction() as cur:
            row = cur.execute(
                "SELECT * FROM coverage_mission_discovered_documents WHERE record_id=?", (record_id,)
            ).fetchone()
            if row is None:
                raise CoverageMissionNotFound("discovered document was not found")
            if row["status"] == "acquired":
                return self._document_row(row)
            if row["status"] not in ("discovered", "already_in_authority"):
                raise CoverageMissionConflict("document is not awaiting acquisition")
            now = _now()
            cur.execute(
                "UPDATE coverage_mission_discovered_documents SET status='acquired',"
                "failure_reason=NULL,updated_at=? WHERE record_id=? "
                "AND status IN ('discovered','already_in_authority')",
                (now, record_id),
            )
            if cur.rowcount != 1:
                raise CoverageMissionConflict("discovered document state changed concurrently")
            row = cur.execute(
                "SELECT * FROM coverage_mission_discovered_documents WHERE record_id=?", (record_id,)
            ).fetchone()
        return self._document_row(row)

    def settle_discovered_document(
        self, record_id: str, *, status: str, reason: str | None = None
    ) -> dict[str, Any]:
        record_id = _text(record_id, "record_id")
        status = _vocabulary(status, ("acquired", "acquisition_failed"), "status")
        if status == "acquisition_failed":
            reason = _text(reason, "reason")
        elif reason is not None:
            raise CoverageMissionValidationError("an acquired document carries no failure reason")
        with self._transaction() as cur:
            row = cur.execute(
                "SELECT * FROM coverage_mission_discovered_documents WHERE record_id=?", (record_id,)
            ).fetchone()
            if row is None:
                raise CoverageMissionNotFound("discovered document was not found")
            if row["status"] != "acquisition_launched":
                if row["status"] == status and row["failure_reason"] == reason:
                    return self._document_row(row)
                raise CoverageMissionConflict("discovered document is not in a launched acquisition")
            now = _now()
            cur.execute(
                "UPDATE coverage_mission_discovered_documents SET status=?,failure_reason=?,"
                "updated_at=? WHERE record_id=? AND status='acquisition_launched'",
                (status, reason, now, record_id),
            )
            if cur.rowcount != 1:
                raise CoverageMissionConflict("discovered document state changed concurrently")
            row = cur.execute(
                "SELECT * FROM coverage_mission_discovered_documents WHERE record_id=?", (record_id,)
            ).fetchone()
        return self._document_row(row)

    # -- P9d-2: human extraction queue for acquired documents -----------------

    _CARRIED_STATUSES: tuple[str, ...] = (
        "discovered", "already_in_authority", "acquisition_failed", "acquisition_launched", "acquired",
    )

    def carry_forward_superseded_documents(
        self, mission_ref: str, *, source_ref: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Re-register documents left under a superseded version of a mission.

        P9d-12: every acquisition query joins the active version pointer, so
        publishing a new mission version silently strands whatever the prior
        version had discovered but not finished.  Live, v3 -> v4 stranded ten
        URLs.  This copies each unfinished row into the current version, under
        the current version's own grant: the company must still be in the
        universe and the source still ``connected`` for automation, exactly as
        for a fresh discovery.  Rows the grant refuses are reported, not moved.

        The copy keeps ``document_ref``, ``discovery_ref`` (the original
        envelope is still the only route from ref to URL), ``host``, and the
        original timestamps, so queue order and retry timing are preserved.
        ``acquisition_launched`` becomes ``discovered``: its bytes will land in
        authority through the old ticket and the already-held path settles the
        copy without a second fetch.  ``acquired`` rows whose review under the
        old version was already resolved are finished and are not copied.
        Idempotent: a document already present under the current version is
        never copied twice.
        """

        mission_ref = _text(mission_ref, "mission_ref")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise CoverageMissionValidationError("carry-forward limit must be 1..500")
        pointer = self.connection.execute(
            "SELECT mission_version_id FROM coverage_mission_pointer WHERE mission_ref=?",
            (mission_ref,),
        ).fetchone()
        if pointer is None:
            return []
        current_ref = pointer["mission_version_id"]
        mission = self.mission(current_ref)
        principal = mission["autonomy"]["automation_principal"]
        query = (
            "SELECT d.* FROM coverage_mission_discovered_documents d "
            "JOIN coverage_mission_versions v ON v.mission_version_id=d.mission_version_ref "
            "WHERE v.mission_ref=? AND d.mission_version_ref<>? AND d.status IN (%s) "
            "AND NOT EXISTS (SELECT 1 FROM coverage_mission_discovered_documents c "
            "WHERE c.mission_version_ref=? AND c.document_ref=d.document_ref)"
            % ",".join("?" * len(self._CARRIED_STATUSES))
        )
        params: list[Any] = [mission_ref, current_ref, *self._CARRIED_STATUSES, current_ref]
        if source_ref is not None:
            query += " AND d.source_ref=?"
            params.append(_text(source_ref, "source_ref"))
        # A document can sit under several superseded versions (v2 and v4,
        # live).  The most recent version's row carries the latest state, so
        # it wins; the rest are the same document and are not copied twice.
        # Live, the second copy hit the UNIQUE constraint and took the whole
        # discovery tick down with it.
        query += " ORDER BY v.version_number DESC,d.created_at,d.record_id LIMIT ?"
        params.append(limit)
        rows = self.connection.execute(query, params).fetchall()
        if not rows:
            return []
        grants: dict[tuple[str, str], dict[str, Any] | str] = {}
        result: list[dict[str, Any]] = []
        carried_refs: set[str] = set()
        for row in rows:
            if row["document_ref"] in carried_refs:
                continue
            entry = {
                "document_ref": row["document_ref"], "from_version_ref": row["mission_version_ref"],
                "company_ref": row["company_ref"], "source_ref": row["source_ref"],
            }
            if row["status"] == "acquired":
                review = self.connection.execute(
                    "SELECT state FROM coverage_mission_document_reviews "
                    "WHERE mission_version_ref=? AND document_ref=?",
                    (row["mission_version_ref"], row["document_ref"]),
                ).fetchone()
                if review is not None and review["state"] != "awaiting_human_extraction":
                    continue  # finished under the old version; nothing owed
            key = (row["company_ref"], row["source_ref"])
            if key not in grants:
                try:
                    grants[key] = self.authorize_source_discovery(
                        company_ref=key[0], source_ref=key[1], requested_by=principal,
                        mission_version_ref=current_ref,
                    )
                except CoverageMissionError as exc:
                    grants[key] = f"{type(exc).__name__}: {exc}"
            grant = grants[key]
            if isinstance(grant, str):
                result.append({**entry, "status": "skipped", "reason": grant})
                continue
            status = "discovered" if row["status"] == "acquisition_launched" else row["status"]
            reason = row["failure_reason"] if status == "acquisition_failed" else None
            # An acquired row's ticket is how the review plane finds its
            # manifest; a failed row's ticket is its evidence.  Only a row that
            # is back to discovered starts without one.
            ticket_ref = row["ticket_ref"] if status in ("acquired", "acquisition_failed") else None
            record_id = _ref(
                "mission-discovered-document",
                {"mission_version_ref": current_ref, "document_ref": row["document_ref"]},
            )
            with self._transaction() as cur:
                cur.execute(
                    "INSERT INTO coverage_mission_discovered_documents"
                    "(record_id,mission_version_ref,company_ref,source_ref,document_ref,"
                    "discovery_ref,status,ticket_ref,failure_reason,created_at,updated_at,host) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        record_id, current_ref, row["company_ref"], row["source_ref"],
                        row["document_ref"], row["discovery_ref"], status, ticket_ref, reason,
                        row["created_at"], row["updated_at"], row["host"],
                    ),
                )
            carried_refs.add(row["document_ref"])
            result.append({**entry, "status": status, "record_id": record_id})
        return result

    def backfill_document_reviews(self, mission_ref: str, *, limit: int = 100) -> list[dict[str, Any]]:
        """Recover acquisitions completed before queue deployment or a crash.

        Select only missing reviews in the active mission, then re-derive
        each grant through the normal registration path. No acquisition or
        model calls occur; resolved reviews can never be reopened.
        """
        mission_ref = _text(mission_ref, "mission_ref")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise CoverageMissionValidationError("backfill limit must be 1..100")
        rows = self.connection.execute(
            "SELECT d.record_id,d.mission_version_ref FROM coverage_mission_discovered_documents d "
            "JOIN coverage_mission_pointer p ON p.mission_version_id=d.mission_version_ref "
            "LEFT JOIN coverage_mission_document_reviews r "
            "ON r.mission_version_ref=d.mission_version_ref AND r.document_ref=d.document_ref "
            "WHERE p.mission_ref=? AND d.status='acquired' AND r.review_id IS NULL "
            "ORDER BY d.created_at,d.record_id LIMIT ?", (mission_ref, limit),
        ).fetchall()
        result = []
        for row in rows:
            mission = self.mission(row["mission_version_ref"])
            try:
                review = self.register_document_review(
                    row["record_id"], requested_by=mission["autonomy"]["automation_principal"],
                )
                result.append({"record_id": row["record_id"], "status": review["status"], "review_id": review["review_id"]})
            except CoverageMissionError as exc:
                result.append({"record_id": row["record_id"], "status": "not_registered", "reason": f"{type(exc).__name__}: {exc}"})
        return result

    def register_document_review(self, record_id: str, *, requested_by: str) -> dict[str, Any]:
        """Open one human extraction review for an acquired document.

        The discovery loop calls this right after a discovered document
        settles ``acquired``.  It re-derives the mission's source_discovery
        grant, so a mission that has since been replaced (or one that never
        granted discovery) refuses instead of queueing work.  One review per
        mission version + document; replay returns the existing row.
        """

        record_id = _text(record_id, "record_id")
        requested_by = _text(requested_by, "requested_by")
        row = self.connection.execute(
            "SELECT * FROM coverage_mission_discovered_documents WHERE record_id=?", (record_id,)
        ).fetchone()
        if row is None:
            raise CoverageMissionNotFound("discovered document was not found")
        if row["status"] != "acquired":
            raise CoverageMissionConflict(
                "document review requires an acquired document"
            )
        authorization = self.authorize_source_discovery(
            company_ref=row["company_ref"],
            source_ref=row["source_ref"],
            requested_by=requested_by,
            mission_version_ref=row["mission_version_ref"],
        )
        if authorization["mission_version_ref"] != row["mission_version_ref"]:
            raise CoverageMissionConflict("discovery authorization drifted")
        existing = self.connection.execute(
            "SELECT * FROM coverage_mission_document_reviews "
            "WHERE mission_version_ref=? AND document_ref=?",
            (row["mission_version_ref"], row["document_ref"]),
        ).fetchone()
        if existing is not None:
            return {"status": "duplicate", **self._review_row(existing)}
        review_id = _ref("mission-document-review", {
            "mission_version_ref": row["mission_version_ref"],
            "document_ref": row["document_ref"],
        })
        now = _now()
        with self._transaction() as cur:
            cur.execute(
                "INSERT INTO coverage_mission_document_reviews("
                "review_id,mission_version_ref,company_ref,source_ref,document_ref,"
                "discovered_document_ref,state,registered_by,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?, 'awaiting_human_extraction', ?, ?, ?)",
                (
                    review_id, row["mission_version_ref"], row["company_ref"],
                    row["source_ref"], row["document_ref"], row["record_id"],
                    authorization["actor_ref"], now, now,
                ),
            )
            review = cur.execute(
                "SELECT * FROM coverage_mission_document_reviews WHERE review_id=?", (review_id,)
            ).fetchone()
        return {"status": "fresh", **self._review_row(review)}

    def resolve_document_review(
        self,
        review_id: str,
        *,
        resolution: str,
        actor_ref: str,
        candidate_claim_version_ref: str | None = None,
        rationale: str | None = None,
        expected_review_hash: str | None = None,
    ) -> dict[str, Any]:
        """Close an extraction review.

        ``extraction_staged`` binds the candidate that was staged, by a person
        through the ADR-0003 B path or by the mission's automation through the
        ADR-0005 policy path; ``dismissed`` records why the document is not
        worth extracting.  An automation actor must be the review's mission
        principal; anything else is refused.
        """

        review_id = _text(review_id, "review_id")
        resolution = _vocabulary(
            resolution, ("extraction_staged", "dismissed"), "resolution"
        )
        actor_ref = _actor(actor_ref, "actor_ref")
        if _HUMAN_RE.fullmatch(actor_ref) is None:
            owner = self.connection.execute(
                "SELECT mission_version_ref FROM coverage_mission_document_reviews WHERE review_id=?",
                (review_id,),
            ).fetchone()
            if owner is None:
                raise CoverageMissionNotFound("document review was not found")
            principal = self.mission(owner["mission_version_ref"])["autonomy"]["automation_principal"]
            if actor_ref != principal:
                raise CoverageMissionValidationError(
                    "document review resolution requires a human or the review's mission principal"
                )
        if expected_review_hash is not None:
            expected_review_hash = _sha256(expected_review_hash, "expected_review_hash")
        if resolution == "extraction_staged":
            candidate_claim_version_ref = _text(
                candidate_claim_version_ref, "candidate_claim_version_ref"
            )
            if not candidate_claim_version_ref.startswith("candidate-claim-version:"):
                raise CoverageMissionValidationError(
                    "extraction_staged requires a staged candidate claim version"
                )
            # The staging authority is a separate database shared with the
            # Cockpit review plane; the writer op verifies existence there
            # before calling this method.
        else:
            if candidate_claim_version_ref is not None:
                raise CoverageMissionValidationError(
                    "dismissed cannot bind a candidate claim version"
                )
        rationale = _text(rationale, "rationale") if rationale is not None else None
        if resolution == "dismissed" and rationale is None:
            raise CoverageMissionValidationError("dismissed requires a rationale")
        with self._transaction() as cur:
            row = cur.execute(
                "SELECT * FROM coverage_mission_document_reviews WHERE review_id=?", (review_id,)
            ).fetchone()
            if row is None:
                raise CoverageMissionNotFound("document review was not found")
            if row["state"] == resolution:
                if (row["candidate_claim_version_ref"], row["rationale"]) != (
                    candidate_claim_version_ref, rationale,
                ):
                    raise CoverageMissionConflict("document review resolution payload changed")
                return {"status": "duplicate", **self._review_row(row)}
            if row["state"] != "awaiting_human_extraction":
                raise CoverageMissionConflict("document review is already resolved")
            if expected_review_hash is not None and expected_review_hash != content_hash(self._review_row(row)):
                raise CoverageMissionConflict("document review changed; reload before deciding")
            now = _now()
            cur.execute(
                "UPDATE coverage_mission_document_reviews SET state=?,"
                "candidate_claim_version_ref=?,rationale=?,updated_at=? "
                "WHERE review_id=? AND state='awaiting_human_extraction'",
                (resolution, candidate_claim_version_ref, rationale, now, review_id),
            )
            if cur.rowcount != 1:
                raise CoverageMissionConflict("document review state changed concurrently")
            row = cur.execute(
                "SELECT * FROM coverage_mission_document_reviews WHERE review_id=?", (review_id,)
            ).fetchone()
        return {"status": "fresh", **self._review_row(row)}

    def document_review(self, review_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM coverage_mission_document_reviews WHERE review_id=?",
            (_text(review_id, "review_id"),),
        ).fetchone()
        if row is None:
            raise CoverageMissionNotFound("document review was not found")
        return self._review_row(row)

    def document_reviews(
        self,
        mission_version_ref: str,
        *,
        state: str | None = None,
        company_ref: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        mission_version_ref = _text(mission_version_ref, "mission_version_ref")
        if state is not None:
            state = _vocabulary(
                state,
                ("awaiting_human_extraction", "extraction_staged", "dismissed"),
                "state",
            )
        if company_ref is not None:
            company_ref = _text(company_ref, "company_ref")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise CoverageMissionValidationError("document review limit must be 1..500")
        query = (
            "SELECT * FROM coverage_mission_document_reviews WHERE mission_version_ref=?"
        )
        params: list[Any] = [mission_version_ref]
        if state is not None:
            query += " AND state=?"
            params.append(state)
        if company_ref is not None:
            query += " AND company_ref=?"
            params.append(company_ref)
        query += " ORDER BY created_at,review_id LIMIT ?"
        params.append(limit)
        return [self._review_row(row) for row in self.connection.execute(query, params).fetchall()]

    @staticmethod
    def _review_row(row: Any) -> dict[str, Any]:
        return {
            "review_id": row["review_id"],
            "mission_version_ref": row["mission_version_ref"],
            "company_ref": row["company_ref"],
            "source_ref": row["source_ref"],
            "document_ref": row["document_ref"],
            "discovered_document_ref": row["discovered_document_ref"],
            "state": row["state"],
            "candidate_claim_version_ref": row["candidate_claim_version_ref"],
            "rationale": row["rationale"],
            "registered_by": row["registered_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def sec_lane_authorization_for_company(self, company_ref: str) -> dict[str, Any]:
        """Resolve the sole active mission and return its exact SEC grant."""

        company_ref = _text(company_ref, "company_ref")
        matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for row in self.connection.execute(
            "SELECT mission_version_id FROM coverage_mission_pointer ORDER BY mission_ref"
        ).fetchall():
            mission = self.mission(row["mission_version_id"])
            member = next(
                (item for item in mission["universe"] if item["company_ref"] == company_ref),
                None,
            )
            if member is not None:
                matches.append((mission, member))
        if len(matches) != 1:
            raise CoverageMissionConflict(
                "SEC observation requires exactly one active mission for the company"
            )
        mission, member = matches[0]
        return self.authorize_sec_lane(
            company_ref=company_ref,
            ticker=member["ticker"],
            actor_ref=mission["autonomy"]["automation_principal"],
            mission_version_ref=mission["id"],
            mission_version_hash=mission["content_hash"],
        )

    def queue_sec_dispatch(
        self,
        *,
        authorization: Mapping[str, Any],
        form: str,
        filed_from: str,
        filed_to: str,
        expected_accession: str,
        observation_ref: str,
    ) -> dict[str, Any]:
        """Persist an idempotent mission SEC dispatch before the launcher slot."""

        authorization = dict(authorization)
        exact = self.authorize_sec_lane(
            company_ref=authorization.get("company_ref"),
            ticker=authorization.get("ticker"),
            actor_ref=authorization.get("actor_ref"),
            mission_version_ref=authorization.get("mission_version_ref"),
            mission_version_hash=authorization.get("mission_version_hash"),
        )
        if canonical_json(exact) != canonical_json(authorization):
            raise CoverageMissionConflict("SEC dispatch authorization drifted")
        if form not in {"10-Q", "10-K"}:
            raise CoverageMissionValidationError("SEC dispatch form must be 10-Q or 10-K")
        filed_from = _text(filed_from, "filed_from")
        filed_to = _text(filed_to, "filed_to")
        if filed_from > filed_to:
            raise CoverageMissionValidationError("SEC dispatch filing window is reversed")
        expected_accession = _text(expected_accession, "expected_accession")
        if _ACCESSION_RE.fullmatch(expected_accession) is None:
            raise CoverageMissionValidationError("SEC dispatch accession is invalid")
        observation_ref = _text(observation_ref, "observation_ref")
        request = {
            "mission_version_ref": exact["mission_version_ref"],
            "mission_version_hash": exact["mission_version_hash"],
            "company_ref": exact["company_ref"],
            "ticker": exact["ticker"],
            "actor_ref": exact["actor_ref"],
            "form": form,
            "filed_from": filed_from,
            "filed_to": filed_to,
            "expected_accession": expected_accession,
            "observation_ref": observation_ref,
        }
        request_hash = content_hash(request)
        dispatch_id = f"mission-sec-dispatch:{request_hash[:32]}"
        existing = self.connection.execute(
            "SELECT * FROM coverage_mission_sec_dispatches WHERE dispatch_id=?",
            (dispatch_id,),
        ).fetchone()
        if existing is not None:
            if existing["request_hash"] != request_hash:
                raise CoverageMissionConflict("SEC dispatch identity drifted")
            return {**dict(existing), "authorization": json.loads(existing["authorization_json"]),
                    "status_marker": "duplicate"}
        now = _now()
        with self._transaction() as cur:
            cur.execute(
                "INSERT INTO coverage_mission_sec_dispatches"
                "(dispatch_id,mission_version_ref,mission_version_hash,company_ref,ticker,actor_ref,"
                "form,filed_from,filed_to,expected_accession,observation_ref,authorization_json,"
                "request_hash,status,ticket_ref,failure_reason,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',NULL,NULL,?,?)",
                (
                    dispatch_id, exact["mission_version_ref"], exact["mission_version_hash"],
                    exact["company_ref"], exact["ticker"], exact["actor_ref"], form,
                    filed_from, filed_to, expected_accession, observation_ref,
                    canonical_json(exact), request_hash, now, now,
                ),
            )
        return {
            **request, "dispatch_id": dispatch_id, "authorization": exact,
            "request_hash": request_hash, "status": "pending", "ticket_ref": None,
            "created_at": now, "updated_at": now, "status_marker": "fresh",
        }

    def settle_sec_dispatch(
        self, dispatch_id: str, *, outcome: str, ticket_ref: str | None = None,
        detail: str | None = None, failure_reason: str | None = None,
    ) -> dict[str, Any]:
        """Record that a launched dispatch's run is over.

        ``finished`` means its lane ticket reached a terminal state.
        ``orphaned`` means the ticket is gone -- a restart, a pruned ticket
        directory -- which is still over, and leaving those unsettled is what
        froze the quarterly lane.
        """

        dispatch_id = _text(dispatch_id, "dispatch_id")
        if outcome not in ("finished", "orphaned"):
            raise CoverageMissionValidationError(
                "SEC dispatch settlement outcome must be finished or orphaned"
            )
        row = self.connection.execute(
            "SELECT status FROM coverage_mission_sec_dispatches WHERE dispatch_id=?",
            (dispatch_id,),
        ).fetchone()
        if row is None:
            raise CoverageMissionNotFound("mission SEC dispatch was not found")
        if row["status"] != "launched":
            raise CoverageMissionConflict("only a launched SEC dispatch settles")
        existing = self.connection.execute(
            "SELECT * FROM coverage_mission_sec_dispatch_settlements WHERE dispatch_id=?",
            (dispatch_id,),
        ).fetchone()
        if existing is not None:
            return {**dict(existing), "status_marker": "duplicate"}
        now = _now()
        reason = (None if failure_reason is None
                  else str(failure_reason)[:MAX_FAILURE_REASON_CHARS])
        with self._transaction() as cur:
            cur.execute(
                "INSERT INTO coverage_mission_sec_dispatch_settlements("
                "dispatch_id,ticket_ref,outcome,detail,settled_at,failure_reason) "
                "VALUES(?,?,?,?,?,?)",
                (dispatch_id, ticket_ref, outcome,
                 None if detail is None else str(detail)[:500], now, reason),
            )
        return {"dispatch_id": dispatch_id, "ticket_ref": ticket_ref, "outcome": outcome,
                "detail": detail, "failure_reason": reason, "settled_at": now,
                "status_marker": "fresh"}

    def void_sec_dispatch_attempt(
        self, dispatch_id: str, *, reason: str, voided_by: str
    ) -> dict[str, Any]:
        """Record that an attempt proved nothing about the filing it was spent on.

        The dispatch stays; only its claim on the retry budget is withdrawn.
        """

        dispatch_id = _text(dispatch_id, "dispatch_id")
        reason = _text(reason, "reason")
        voided_by = _text(voided_by, "voided_by")
        row = self.connection.execute(
            "SELECT 1 FROM coverage_mission_sec_dispatches WHERE dispatch_id=?",
            (dispatch_id,),
        ).fetchone()
        if row is None:
            raise CoverageMissionNotFound("mission SEC dispatch was not found")
        existing = self.connection.execute(
            "SELECT * FROM coverage_mission_sec_dispatch_attempt_voids WHERE dispatch_id=?",
            (dispatch_id,),
        ).fetchone()
        if existing is not None:
            return {**dict(existing), "status_marker": "duplicate"}
        now = _now()
        with self._transaction() as cur:
            cur.execute(
                "INSERT INTO coverage_mission_sec_dispatch_attempt_voids("
                "dispatch_id,reason,voided_by,voided_at) VALUES(?,?,?,?)",
                (dispatch_id, reason, voided_by, now),
            )
        return {"dispatch_id": dispatch_id, "reason": reason, "voided_by": voided_by,
                "voided_at": now, "status_marker": "fresh"}

    def voided_sec_dispatch_attempts(self) -> list[dict[str, Any]]:
        """Every withdrawn attempt and why, so the forgiveness stays legible."""

        return [dict(row) for row in self.connection.execute(
            "SELECT v.*, d.company_ref, d.expected_accession "
            "FROM coverage_mission_sec_dispatch_attempt_voids v "
            "JOIN coverage_mission_sec_dispatches d ON d.dispatch_id=v.dispatch_id "
            "ORDER BY v.voided_at, v.dispatch_id"
        ).fetchall()]

    def sec_dispatch_outcomes(self, company_ref: str | None = None) -> dict[str, Any]:
        """How the settled SEC runs actually went, per outcome.

        P13z: ``outcome`` answers "is the run over", which is what unblocked the
        quarterly dispatcher.  It does not answer "did it work", and nothing
        else did either: 73 dispatches settled ``finished`` while every one of
        their runs had failed with the same connector conflict, and the only
        trace was a ``detail`` column no read returned.  Five companies went a
        day without a single filing arriving and the ledger looked healthy.

        The breakdown is kept rather than collapsed to a pass/fail count: a
        run whose ticket was pruned is a different problem from one that ran
        and failed, and treating them alike hides whichever is rarer.
        """

        query = (
            "SELECT s.detail AS detail, COUNT(*) AS n, MAX(s.settled_at) AS last_at "
            "FROM coverage_mission_sec_dispatch_settlements s "
            "JOIN coverage_mission_sec_dispatches d ON d.dispatch_id=s.dispatch_id"
        )
        params: list[Any] = []
        if company_ref is not None:
            query += " WHERE d.company_ref=?"
            params.append(_text(company_ref, "company_ref"))
        query += " GROUP BY s.detail"
        by_detail: dict[str, int] = {}
        last_failure_at: str | None = None
        last_failure_detail: str | None = None
        for row in self.connection.execute(query, params).fetchall():
            detail = row["detail"] or "unknown"
            by_detail[detail] = int(row["n"])
            if detail != SEC_RUN_SUCCEEDED and (
                last_failure_at is None or (row["last_at"] or "") > last_failure_at
            ):
                last_failure_at, last_failure_detail = row["last_at"], detail
        succeeded = by_detail.get(SEC_RUN_SUCCEEDED, 0)
        return {
            "settled": sum(by_detail.values()),
            "succeeded": succeeded,
            "unsuccessful": sum(by_detail.values()) - succeeded,
            "by_detail": by_detail,
            "last_failure_detail": last_failure_detail,
            "last_failure_at": last_failure_at,
        }

    def unsettled_sec_dispatches(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Launched dispatches whose run has not been recorded as over."""

        if type(limit) is not int or not 1 <= limit <= 500:
            raise CoverageMissionValidationError("SEC dispatch limit must be 1..500")
        rows = self.connection.execute(
            "SELECT d.* FROM coverage_mission_sec_dispatches d "
            "LEFT JOIN coverage_mission_sec_dispatch_settlements s "
            "ON s.dispatch_id=d.dispatch_id "
            "WHERE d.status='launched' AND s.dispatch_id IS NULL "
            "ORDER BY d.created_at,d.dispatch_id LIMIT ?", (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def pending_sec_dispatches(self, *, limit: int = 1) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 20:
            raise CoverageMissionValidationError("SEC dispatch limit must be 1..20")
        rows = self.connection.execute(
            "SELECT * FROM coverage_mission_sec_dispatches WHERE status='pending' "
            "ORDER BY created_at,dispatch_id LIMIT ?", (limit,),
        ).fetchall()
        return [
            {**dict(row), "authorization": json.loads(row["authorization_json"])}
            for row in rows
        ]

    def mark_sec_dispatch_launched(self, dispatch_id: str, ticket_ref: str) -> dict[str, Any]:
        dispatch_id = _text(dispatch_id, "dispatch_id")
        ticket_ref = _text(ticket_ref, "ticket_ref")
        row = self.connection.execute(
            "SELECT * FROM coverage_mission_sec_dispatches WHERE dispatch_id=?", (dispatch_id,)
        ).fetchone()
        if row is None:
            raise CoverageMissionNotFound("mission SEC dispatch was not found")
        if row["status"] == "launched":
            if row["ticket_ref"] != ticket_ref:
                raise CoverageMissionConflict("mission SEC dispatch bound another ticket")
            return {**dict(row), "authorization": json.loads(row["authorization_json"]),
                    "status_marker": "duplicate"}
        now = _now()
        with self._transaction() as cur:
            cur.execute(
                "UPDATE coverage_mission_sec_dispatches SET status='launched',ticket_ref=?,"
                "updated_at=? WHERE dispatch_id=? AND status='pending'",
                (ticket_ref, now, dispatch_id),
            )
            if cur.rowcount != 1:
                raise CoverageMissionConflict("mission SEC dispatch state changed concurrently")
        return {
            **dict(row), "status": "launched", "ticket_ref": ticket_ref,
            "updated_at": now, "authorization": json.loads(row["authorization_json"]),
            "status_marker": "fresh",
        }

    def mark_sec_dispatch_rejected(self, dispatch_id: str, reason: str) -> dict[str, Any]:
        dispatch_id = _text(dispatch_id, "dispatch_id")
        reason = _text(reason, "reason")
        row = self.connection.execute(
            "SELECT * FROM coverage_mission_sec_dispatches WHERE dispatch_id=?", (dispatch_id,)
        ).fetchone()
        if row is None:
            raise CoverageMissionNotFound("mission SEC dispatch was not found")
        if row["status"] == "rejected":
            if row["failure_reason"] != reason:
                raise CoverageMissionConflict("mission SEC dispatch has another rejection reason")
            return {**dict(row), "authorization": json.loads(row["authorization_json"]),
                    "status_marker": "duplicate"}
        if row["status"] != "pending":
            raise CoverageMissionConflict("launched SEC dispatch cannot be rejected")
        now = _now()
        with self._transaction() as cur:
            cur.execute(
                "UPDATE coverage_mission_sec_dispatches SET status='rejected',failure_reason=?,"
                "updated_at=? WHERE dispatch_id=? AND status='pending'",
                (reason, now, dispatch_id),
            )
            if cur.rowcount != 1:
                raise CoverageMissionConflict("mission SEC dispatch state changed concurrently")
        return {
            **dict(row), "status": "rejected", "failure_reason": reason,
            "updated_at": now, "authorization": json.loads(row["authorization_json"]),
            "status_marker": "fresh",
        }

    # -- company model specifications (P13al) --------------------------------

    def record_company_model_spec(
        self, spec: Mapping[str, Any], *, mission_version_ref: str,
        model_profile_ref: str | None = None, work_order_ref: str | None = None,
    ) -> dict[str, Any]:
        """Store one verified model specification for one company.

        Keyed by (company, state hash): deciding twice about an unchanged
        disclosure is the same decision. A specification is a judgement about
        how to model a company, not a claim about the world, so it lives here
        rather than in the Ledger -- the numbers it eventually produces are the
        things that will need citing.
        """

        required = (
            "company_ref", "state_hash", "assessment", "revenue_drivers",
            "expense_lines", "forecast_statements", "operating_metrics",
            "horizon", "decided_by", "task_hash", "content_hash",
        )
        for field in required:
            if spec.get(field) in (None, ""):
                raise CoverageMissionValidationError(
                    f"company model spec is missing {field}")
        company_ref = _text(spec["company_ref"], "company_ref")
        state_hash = _text(spec["state_hash"], "state_hash")
        mission_version_ref = _text(mission_version_ref, "mission_version_ref")
        spec_id = _ref("company-model-spec", {
            "company_ref": company_ref, "state_hash": state_hash,
        })
        existing = self.connection.execute(
            "SELECT * FROM coverage_mission_company_model_specs WHERE spec_id=?",
            (spec_id,),
        ).fetchone()
        if existing is not None:
            return {**self._model_spec_row(existing), "status": "duplicate"}
        now = _now()
        with self._transaction() as cur:
            cur.execute(
                "INSERT INTO coverage_mission_company_model_specs("
                "spec_id,company_ref,mission_version_ref,state_hash,assessment,"
                "revenue_drivers_json,expense_lines_json,forecast_statements_json,"
                "operating_metrics_json,horizon_json,task_hash,model_profile_ref,"
                "work_order_ref,decided_by,created_at,content_hash) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    spec_id, company_ref, mission_version_ref, state_hash,
                    spec["assessment"],
                    canonical_json(spec["revenue_drivers"]),
                    canonical_json(spec["expense_lines"]),
                    canonical_json(spec["forecast_statements"]),
                    canonical_json(spec["operating_metrics"]),
                    canonical_json(spec["horizon"]),
                    _sha256(spec["task_hash"], "task_hash"),
                    model_profile_ref, work_order_ref,
                    _text(spec["decided_by"], "decided_by"), now,
                    _sha256(spec["content_hash"], "content_hash"),
                ),
            )
            row = cur.execute(
                "SELECT * FROM coverage_mission_company_model_specs WHERE spec_id=?",
                (spec_id,),
            ).fetchone()
        return {**self._model_spec_row(row), "status": "fresh"}

    @staticmethod
    def _model_spec_row(row: Any) -> dict[str, Any]:
        wire = dict(row)
        for field in ("revenue_drivers", "expense_lines", "forecast_statements",
                      "operating_metrics", "horizon"):
            wire[field] = json.loads(wire.pop(f"{field}_json"))
        return wire

    def company_model_spec_for_state(
        self, company_ref: str, state_hash: str
    ) -> dict[str, Any] | None:
        """The specification decided from exactly this disclosure, if one was."""

        row = self.connection.execute(
            "SELECT * FROM coverage_mission_company_model_specs "
            "WHERE company_ref=? AND state_hash=?",
            (_text(company_ref, "company_ref"), _text(state_hash, "state_hash")),
        ).fetchone()
        return None if row is None else self._model_spec_row(row)

    def latest_company_model_spec(self, company_ref: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM coverage_mission_company_model_specs WHERE company_ref=? "
            "ORDER BY created_at DESC, spec_id DESC LIMIT 1",
            (_text(company_ref, "company_ref"),),
        ).fetchone()
        return None if row is None else self._model_spec_row(row)

    def company_model_specs(self, company_ref: str | None = None) -> list[dict[str, Any]]:
        if company_ref is None:
            rows = self.connection.execute(
                "SELECT * FROM coverage_mission_company_model_specs "
                "ORDER BY company_ref,created_at"
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM coverage_mission_company_model_specs "
                "WHERE company_ref=? ORDER BY created_at",
                (_text(company_ref, "company_ref"),),
            ).fetchall()
        return [self._model_spec_row(row) for row in rows]

    # -- statement ingest (P13ak) --------------------------------------------

    def queue_statement_dispatch(
        self, *, authorization: Mapping[str, Any], form: str = "10-Q",
        filing_limit: int = 1, attempt: int = 0, retry_salt: str | None = None,
    ) -> dict[str, Any]:
        """Queue one financial-statements run for a company, idempotently.

        The same company, form, depth and attempt is the same dispatch: asking
        twice does not produce two children parsing the same filing.

        ``attempt`` is what makes a retry possible at all. Without it the
        identity is the request, so a dispatch rejected once -- by a governance
        record that was not yet approved, say -- would be rejected forever, and
        approving the record afterwards would change nothing. The caller passes
        how many runs this company has already spent; the cap on those lives
        with the dispatcher, which is what knows when a company has had enough.

        ``retry_salt`` is for the other kind of failure: the ones that were
        never about this company. A malformed EDGAR identity fails every run
        the same way, and it does not spend a company's attempts, so without a
        second thing in the identity those retries would all collapse onto one
        dispatch that already failed. The dispatcher passes a value that moves
        with time rather than with the company, which is what makes the lane
        both stop hammering a broken configuration and pick itself up once the
        configuration is fixed.
        """

        authorization = dict(authorization)
        exact = self.authorize_sec_lane(
            company_ref=authorization.get("company_ref"),
            ticker=authorization.get("ticker"),
            actor_ref=authorization.get("actor_ref"),
            mission_version_ref=authorization.get("mission_version_ref"),
            mission_version_hash=authorization.get("mission_version_hash"),
        )
        if canonical_json(exact) != canonical_json(authorization):
            raise CoverageMissionConflict("statement dispatch authorization drifted")
        if form not in {"10-Q", "10-K"}:
            raise CoverageMissionValidationError("statement dispatch form must be 10-Q or 10-K")
        if type(filing_limit) is not int or not 1 <= filing_limit <= MAX_STATEMENT_FILINGS:
            raise CoverageMissionValidationError(
                f"statement dispatch limit must be 1..{MAX_STATEMENT_FILINGS}"
            )
        if type(attempt) is not int or not 0 <= attempt <= MAX_STATEMENT_ATTEMPTS:
            raise CoverageMissionValidationError(
                f"statement dispatch attempt must be 0..{MAX_STATEMENT_ATTEMPTS}"
            )
        request = {
            "mission_version_ref": exact["mission_version_ref"],
            "mission_version_hash": exact["mission_version_hash"],
            "company_ref": exact["company_ref"], "ticker": exact["ticker"],
            "actor_ref": exact["actor_ref"], "form": form,
            "filing_limit": filing_limit, "attempt": attempt,
        }
        if retry_salt is not None:
            request["retry_salt"] = _text(retry_salt, "retry_salt")[:64]
        request_hash = content_hash(request)
        dispatch_id = f"mission-statement-dispatch:{request_hash[:32]}"
        existing = self.connection.execute(
            "SELECT * FROM coverage_mission_statement_dispatches WHERE dispatch_id=?",
            (dispatch_id,),
        ).fetchone()
        if existing is not None:
            if existing["request_hash"] != request_hash:
                raise CoverageMissionConflict("statement dispatch identity drifted")
            return {**dict(existing),
                    "authorization": json.loads(existing["authorization_json"]),
                    "status_marker": "duplicate"}
        now = _now()
        with self._transaction() as cur:
            cur.execute(
                "INSERT INTO coverage_mission_statement_dispatches"
                "(dispatch_id,mission_version_ref,mission_version_hash,company_ref,ticker,"
                "actor_ref,form,filing_limit,attempt,authorization_json,request_hash,status,"
                "ticket_ref,failure_reason,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,'pending',NULL,NULL,?,?)",
                (
                    dispatch_id, exact["mission_version_ref"], exact["mission_version_hash"],
                    exact["company_ref"], exact["ticker"], exact["actor_ref"], form,
                    filing_limit, attempt, canonical_json(exact), request_hash, now, now,
                ),
            )
        return {
            **request, "dispatch_id": dispatch_id, "authorization": exact,
            "request_hash": request_hash, "status": "pending", "ticket_ref": None,
            "created_at": now, "updated_at": now, "status_marker": "fresh",
        }

    def _statement_dispatch(self, dispatch_id: str) -> Any:
        row = self.connection.execute(
            "SELECT * FROM coverage_mission_statement_dispatches WHERE dispatch_id=?",
            (_text(dispatch_id, "dispatch_id"),),
        ).fetchone()
        if row is None:
            raise CoverageMissionNotFound("mission statement dispatch was not found")
        return row

    def pending_statement_dispatches(self, *, limit: int = 1) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 20:
            raise CoverageMissionValidationError("statement dispatch limit must be 1..20")
        rows = self.connection.execute(
            "SELECT * FROM coverage_mission_statement_dispatches WHERE status='pending' "
            "ORDER BY created_at,dispatch_id LIMIT ?", (limit,),
        ).fetchall()
        return [{**dict(row), "authorization": json.loads(row["authorization_json"])}
                for row in rows]

    def launched_statement_dispatches(self, *, limit: int = 50) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 500:
            raise CoverageMissionValidationError("statement dispatch limit must be 1..500")
        rows = self.connection.execute(
            "SELECT * FROM coverage_mission_statement_dispatches WHERE status='launched' "
            "ORDER BY created_at,dispatch_id LIMIT ?", (limit,),
        ).fetchall()
        return [{**dict(row), "authorization": json.loads(row["authorization_json"])}
                for row in rows]

    def mark_statement_dispatch_launched(
        self, dispatch_id: str, ticket_ref: str
    ) -> dict[str, Any]:
        ticket_ref = _text(ticket_ref, "ticket_ref")
        row = self._statement_dispatch(dispatch_id)
        if row["status"] == "launched":
            if row["ticket_ref"] != ticket_ref:
                raise CoverageMissionConflict("statement dispatch bound another ticket")
            return {**dict(row), "status_marker": "duplicate"}
        now = _now()
        with self._transaction() as cur:
            cur.execute(
                "UPDATE coverage_mission_statement_dispatches SET status='launched',"
                "ticket_ref=?,updated_at=? WHERE dispatch_id=? AND status='pending'",
                (ticket_ref, now, row["dispatch_id"]),
            )
            if cur.rowcount != 1:
                raise CoverageMissionConflict("statement dispatch state changed concurrently")
        return {**dict(row), "status": "launched", "ticket_ref": ticket_ref,
                "updated_at": now, "status_marker": "fresh"}

    def settle_statement_dispatch(
        self, dispatch_id: str, *, outcome: str, failure_reason: str | None = None,
    ) -> dict[str, Any]:
        """Close a launched dispatch as succeeded or failed.

        Terminal success is a status here rather than a side journal, which is
        what the SEC filing dispatches needed retrofitting for.
        """

        if outcome not in ("succeeded", "failed"):
            raise CoverageMissionValidationError(
                "statement dispatch outcome must be succeeded or failed"
            )
        row = self._statement_dispatch(dispatch_id)
        if row["status"] in ("succeeded", "failed"):
            if row["status"] != outcome:
                raise CoverageMissionConflict("statement dispatch already settled otherwise")
            return {**dict(row), "status_marker": "duplicate"}
        if row["status"] != "launched":
            raise CoverageMissionConflict("only a launched statement dispatch settles")
        reason = (None if failure_reason is None
                  else str(failure_reason)[:MAX_FAILURE_REASON_CHARS])
        now = _now()
        with self._transaction() as cur:
            cur.execute(
                "UPDATE coverage_mission_statement_dispatches SET status=?,failure_reason=?,"
                "updated_at=? WHERE dispatch_id=? AND status='launched'",
                (outcome, reason, now, row["dispatch_id"]),
            )
            if cur.rowcount != 1:
                raise CoverageMissionConflict("statement dispatch state changed concurrently")
        return {**dict(row), "status": outcome, "failure_reason": reason,
                "updated_at": now, "status_marker": "fresh"}

    def reject_statement_dispatch(self, dispatch_id: str, reason: str) -> dict[str, Any]:
        reason = _text(reason, "reason")
        row = self._statement_dispatch(dispatch_id)
        if row["status"] == "rejected":
            if row["failure_reason"] != reason:
                raise CoverageMissionConflict("statement dispatch has another rejection reason")
            return {**dict(row), "status_marker": "duplicate"}
        if row["status"] != "pending":
            raise CoverageMissionConflict("only a pending statement dispatch is rejected")
        now = _now()
        with self._transaction() as cur:
            cur.execute(
                "UPDATE coverage_mission_statement_dispatches SET status='rejected',"
                "failure_reason=?,updated_at=? WHERE dispatch_id=? AND status='pending'",
                (reason[:MAX_FAILURE_REASON_CHARS], now, row["dispatch_id"]),
            )
            if cur.rowcount != 1:
                raise CoverageMissionConflict("statement dispatch state changed concurrently")
        return {**dict(row), "status": "rejected",
                "failure_reason": reason[:MAX_FAILURE_REASON_CHARS],
                "updated_at": now, "status_marker": "fresh"}

    def record_statement_observation(
        self, *, dispatch_id: str, observation: Mapping[str, Any],
        governance_ref: str, governance_hash: str,
    ) -> dict[str, Any]:
        """Store one validated statements observation: its filings and lines.

        The observation is what the child already validated against the frozen
        output contract, so this checks identity and bounds rather than shape.
        A filing already held for this company is left alone -- re-parsing the
        same 10-Q is the same filing, not a second copy of its lines.
        """

        row = self._statement_dispatch(dispatch_id)
        if row["status"] not in ("launched", "succeeded"):
            raise CoverageMissionConflict(
                "statements are recorded against a launched dispatch"
            )
        if not isinstance(observation, Mapping):
            raise CoverageMissionValidationError("statement observation must be an object")
        observation = dict(observation)
        cik = _text(observation.get("cik"), "cik")
        entity_name = _text(observation.get("entity_name"), "entity_name")
        source_refs = _texts(observation.get("source_record_refs"),
                             "source_record_refs", nonempty=True)
        filings = observation.get("filings")
        if not isinstance(filings, list) or not filings:
            raise CoverageMissionValidationError("statement observation carries no filing")
        if len(filings) > MAX_STATEMENT_FILINGS:
            raise CoverageMissionValidationError(
                f"statement observation exceeds {MAX_STATEMENT_FILINGS} filings"
            )
        company_ref = row["company_ref"]
        governance_ref = _text(governance_ref, "governance_ref")
        governance_hash = _sha256(governance_hash, "governance_hash")
        now = _now()
        recorded: list[dict[str, Any]] = []
        for filing in filings:
            if not isinstance(filing, Mapping):
                raise CoverageMissionValidationError("statement filing must be an object")
            accession = _text(filing.get("accession"), "accession")
            if _ACCESSION_RE.fullmatch(accession) is None:
                raise CoverageMissionValidationError("statement accession is invalid")
            form = _vocabulary(filing.get("form"), ("10-Q", "10-K"), "form")
            lines = filing.get("lines")
            if not isinstance(lines, list):
                raise CoverageMissionValidationError("statement filing carries no lines")
            if len(lines) > MAX_STATEMENT_LINES:
                raise CoverageMissionValidationError(
                    f"statement filing exceeds {MAX_STATEMENT_LINES} lines"
                )
            held = self.connection.execute(
                "SELECT * FROM coverage_mission_statement_filings "
                "WHERE company_ref=? AND accession=?", (company_ref, accession),
            ).fetchone()
            if held is not None:
                recorded.append({**dict(held), "status_marker": "duplicate"})
                continue
            identity = {
                "company_ref": company_ref, "cik": cik, "accession": accession,
                "form": form, "line_count": len(lines),
            }
            ingest_id = f"statement-ingest:{content_hash(identity)[:32]}"
            body = {
                **identity, "entity_name": entity_name,
                "filed": _text(filing.get("filed"), "filed"),
                "report_date": _text(filing.get("report_date"), "report_date"),
                "source_record_refs": source_refs,
                "governance_ref": governance_ref, "governance_hash": governance_hash,
            }
            line_hash = content_hash(body)
            with self._transaction() as cur:
                cur.execute(
                    "INSERT INTO coverage_mission_statement_filings"
                    "(ingest_id,dispatch_id,company_ref,cik,entity_name,accession,form,filed,"
                    "report_date,line_count,source_record_refs_json,governance_ref,"
                    "governance_hash,recorded_at,content_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        ingest_id, row["dispatch_id"], company_ref, cik, entity_name,
                        accession, form, body["filed"], body["report_date"], len(lines),
                        canonical_json(source_refs), governance_ref, governance_hash,
                        now, line_hash,
                    ),
                )
                for ordinal, line in enumerate(lines):
                    cur.execute(
                        "INSERT INTO coverage_mission_statement_lines"
                        "(line_id,ingest_id,statement,ordinal,concept,label,level,parent_concept,"
                        "is_breakdown,dimension_axis,dimension_member,period_start,period_end,"
                        "value,unit,balance) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            f"{ingest_id}#{ordinal}", ingest_id,
                            _vocabulary(line.get("statement"),
                                        ("income", "balance", "cash"), "statement"),
                            ordinal,
                            _text(line.get("concept"), "concept"),
                            _text(line.get("label"), "label"),
                            _non_negative_int(line.get("level"), "level"),
                            line.get("parent_concept"),
                            1 if line.get("is_breakdown") else 0,
                            line.get("dimension_axis"), line.get("dimension_member"),
                            line.get("period_start"),
                            _text(line.get("period_end"), "period_end"),
                            None if line.get("value") is None else str(line["value"]),
                            _text(line.get("unit"), "unit"),
                            line.get("balance"),
                        ),
                    )
            recorded.append({
                **body, "ingest_id": ingest_id, "dispatch_id": row["dispatch_id"],
                "recorded_at": now, "content_hash": line_hash, "status_marker": "fresh",
            })
        return {
            "dispatch_id": row["dispatch_id"], "company_ref": company_ref,
            "filings": recorded,
            "line_count": sum(int(item["line_count"]) for item in recorded),
        }

    def statement_filings(self, company_ref: str | None = None) -> list[dict[str, Any]]:
        if company_ref is None:
            rows = self.connection.execute(
                "SELECT * FROM coverage_mission_statement_filings "
                "ORDER BY company_ref,report_date,accession"
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM coverage_mission_statement_filings WHERE company_ref=? "
                "ORDER BY report_date,accession", (_text(company_ref, "company_ref"),),
            ).fetchall()
        return [{**dict(row),
                 "source_record_refs": json.loads(row["source_record_refs_json"])}
                for row in rows]

    def statement_lines(
        self, ingest_id: str, *, statement: str | None = None
    ) -> list[dict[str, Any]]:
        ingest_id = _text(ingest_id, "ingest_id")
        if statement is None:
            rows = self.connection.execute(
                "SELECT * FROM coverage_mission_statement_lines WHERE ingest_id=? "
                "ORDER BY ordinal", (ingest_id,),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM coverage_mission_statement_lines "
                "WHERE ingest_id=? AND statement=? ORDER BY ordinal",
                (ingest_id, _vocabulary(statement, ("income", "balance", "cash"),
                                        "statement")),
            ).fetchall()
        return [_statement_line_row(row) for row in rows]

    def statement_series_lines(
        self, company_ref: str, concept: str, *, statement: str | None = None,
    ) -> list[dict[str, Any]]:
        """Every filed line for one concept, across every filing held.

        Carries ``filed`` and ``accession`` from the filing they came from,
        because a series has to prefer the most recent statement of a restated
        quarter and has to be able to name where each figure came from.
        """

        company_ref = _text(company_ref, "company_ref")
        concept = _text(concept, "concept")
        query = (
            "SELECT l.*, f.accession, f.filed, f.report_date "
            "FROM coverage_mission_statement_lines l "
            "JOIN coverage_mission_statement_filings f USING(ingest_id) "
            "WHERE f.company_ref=? AND l.concept=?"
        )
        params: list[Any] = [company_ref, concept]
        if statement is not None:
            query += " AND l.statement=?"
            params.append(_vocabulary(statement, ("income", "balance", "cash"),
                                      "statement"))
        query += " ORDER BY f.filed, f.accession, l.ordinal"
        return [_statement_line_row(row)
                for row in self.connection.execute(query, params).fetchall()]

    def statement_coverage(self, company_ref: str) -> dict[str, Any]:
        """What statements this company already has, for the dispatcher to skip.

        Also counts the dispatches still in flight: a company with one running
        does not need a second, and this is what stops the queue from growing
        faster than one child at a time can drain it.
        """

        company_ref = _text(company_ref, "company_ref")
        held = self.statement_filings(company_ref)
        open_rows = self.connection.execute(
            "SELECT status,COUNT(*) AS n FROM coverage_mission_statement_dispatches "
            "WHERE company_ref=? GROUP BY status", (company_ref,),
        ).fetchall()
        by_status = {str(row["status"]): int(row["n"]) for row in open_rows}
        # P13ak: why the failures happened, not just how many. A run that died
        # before it reached SEC -- a misconfigured identity, an unapproved
        # record -- says nothing about this company, and spending its retry
        # budget on our own fault would leave the lane permanently idle for
        # everyone once the fault was fixed.
        failures = [
            {"reason": row["failure_reason"], "at": row["updated_at"],
             "status": row["status"]}
            for row in self.connection.execute(
                "SELECT status,failure_reason,updated_at "
                "FROM coverage_mission_statement_dispatches "
                "WHERE company_ref=? AND status IN ('failed','rejected') "
                "ORDER BY updated_at,dispatch_id", (company_ref,),
            ).fetchall()
        ]
        return {
            "company_ref": company_ref, "failures": failures,
            "accessions": [item["accession"] for item in held],
            "forms": sorted({item["form"] for item in held}),
            # P13am: per form, because how much history a model needs is asked
            # and answered per form. A company holding four 10-Qs and no 10-K
            # is short of neither if only quarters were wanted.
            "held_by_form": {
                form: sum(1 for item in held if item["form"] == form)
                for form in sorted({item["form"] for item in held})
            },
            "latest_report_date": max((item["report_date"] for item in held), default=None),
            "line_count": sum(int(item["line_count"]) for item in held),
            "dispatches": by_status,
            "open_dispatches": by_status.get("pending", 0) + by_status.get("launched", 0),
        }

    # -- stage records -------------------------------------------------------

    def _mission_ref_of(self, cur: sqlite3.Cursor, mission_version_ref: str) -> str:
        """The mission a version belongs to; the version itself if it is unknown.

        Falling back to the ref keeps a folded read on a stray version from
        silently folding *nothing*: it reads exactly that version instead.
        """

        row = cur.execute(
            "SELECT mission_ref FROM coverage_mission_versions WHERE mission_version_id=?",
            (mission_version_ref,),
        ).fetchone()
        return row["mission_ref"] if row is not None else mission_version_ref

    def _folded_rows(
        self, cur: sqlite3.Cursor, mission_ref: str, company_ref: str | None = None
    ) -> list[Any]:
        """Every stage record of every version of one mission, in time order.

        Ordered by ``created_at`` then ``record_id`` so two records written in
        the same microsecond still fold deterministically.  Version number is
        deliberately *not* in the order: what happened first happened first,
        and a version roll is not an event in a company's ladder.
        """

        records = (
            "SELECT r.record_id AS record_id, r.mission_version_ref AS mission_version_ref, "
            "r.company_ref AS company_ref, r.stage_ref AS stage_ref, r.status AS status, "
            "r.actor_ref AS actor_ref, r.created_at AS created_at, r.record_json AS record_json, "
            "v.version_number AS version_number, 'stage' AS record_kind "
            "FROM coverage_mission_stage_records r "
            "JOIN coverage_mission_versions v ON v.mission_version_id=r.mission_version_ref "
            "WHERE v.mission_ref=?"
        )
        # The reopens fold in as a fourth status without being one: they are a
        # separate append-only ledger, read here in the same time order,
        # because "a person un-decided this gate" is an event in the company's
        # ladder and there is no other place it could be read from.
        reopens = (
            "SELECT o.record_id AS record_id, o.mission_version_ref AS mission_version_ref, "
            "o.company_ref AS company_ref, o.stage_ref AS stage_ref, ? AS status, "
            "o.actor_ref AS actor_ref, o.created_at AS created_at, o.record_json AS record_json, "
            "v.version_number AS version_number, 'reopen' AS record_kind "
            "FROM coverage_mission_stage_reopens o "
            "JOIN coverage_mission_versions v ON v.mission_version_id=o.mission_version_ref "
            "WHERE v.mission_ref=?"
        )
        params: list[Any] = [mission_ref]
        if company_ref is not None:
            records += " AND r.company_ref=?"
            params.append(company_ref)
        # The reopen arm's ``status`` is a bound parameter rather than an
        # interpolated constant: nothing here is caller-supplied, but SQL built
        # by f-string is a habit worth not having in a file this size.
        params.append(STAGE_REOPENED)
        params.append(mission_ref)
        if company_ref is not None:
            reopens += " AND o.company_ref=?"
            params.append(company_ref)
        query = f"{records} UNION ALL {reopens} ORDER BY created_at,record_id"
        return cur.execute(query, params).fetchall()

    def _folded_statuses(
        self, cur: sqlite3.Cursor, mission_ref: str, company_ref: str
    ) -> dict[str, list[str]]:
        """Ordered status history per stage for one company, across all versions."""

        state: dict[str, list[str]] = {stage: [] for stage in STAGE_ORDER}
        for row in self._folded_rows(cur, mission_ref, company_ref):
            if row["stage_ref"] in state:
                state[row["stage_ref"]].append(row["status"])
        return state

    def stage_state_by_company(self, mission_ref: str) -> dict[str, dict[str, list[str]]]:
        """``{company_ref: {stage_ref: [status, ...]}}`` folded across versions.

        The shape ``evaluate_mission`` and the cockpit already speak, filled
        from every version of the mission rather than only the active one.
        Callers that used to build this map from ``stage_records(mission_id)``
        were reading a ladder that emptied itself every time the owner
        published a version.
        """

        mission_ref = _text(mission_ref, "mission_ref")
        state: dict[str, dict[str, list[str]]] = {}
        for row in self._folded_rows(self.connection.cursor(), mission_ref):
            state.setdefault(row["company_ref"], {}).setdefault(row["stage_ref"], []).append(
                row["status"]
            )
        return state

    def current_stage_state(self, mission_ref: str, company_ref: str) -> dict[str, Any]:
        """Where one company stands on the ladder, folded across every version.

        The reader ADR-0008's addendum names: a stage state is a fact about
        ``(mission_ref, company_ref)`` that carries forward until it is
        superseded, and the mission version each record binds is provenance --
        it says *when and under what mission* the state was reached, and it is
        reported per stage for exactly that reason.  See ``fold_stage_status``
        for the ordering rules.
        """

        mission_ref = _text(mission_ref, "mission_ref")
        company_ref = _text(company_ref, "company_ref")
        rows = self._folded_rows(self.connection.cursor(), mission_ref, company_ref)
        stages: dict[str, dict[str, Any]] = {}
        for row in rows:
            if row["stage_ref"] not in STAGE_ORDER:
                continue
            entry = stages.setdefault(row["stage_ref"], {"history": []})
            entry["history"].append({
                "status": row["status"],
                # Which ledger this came from: a stage record, or the reopen
                # marker that supersedes one. Two rows can carry the same
                # ``status`` for different reasons, and a reader deciding
                # whether to offer a decision needs to know which it is
                # looking at without re-deriving it from the status.
                "record_kind": row["record_kind"],
                "at": row["created_at"],
                "record_ref": row["record_id"],
                "actor_ref": row["actor_ref"],
                # Provenance: the mission version this fact was written under.
                "mission_version_ref": row["mission_version_ref"],
                "mission_version_number": int(row["version_number"]),
            })
        for stage_ref, entry in stages.items():
            entry["status"] = fold_stage_status([item["status"] for item in entry["history"]])
            decisive = [item for item in entry["history"] if item["status"] == entry["status"]]
            settled = decisive[-1] if decisive else entry["history"][-1]
            entry["at"] = settled["at"]
            entry["record_ref"] = settled["record_ref"]
            entry["mission_version_ref"] = settled["mission_version_ref"]
            entry["mission_version_number"] = settled["mission_version_number"]
        entered = [stage for stage in STAGE_ORDER if stage in stages]
        completed = [
            stage for stage in STAGE_ORDER
            if stages.get(stage, {}).get("status") == "gate_passed"
        ]
        current = entered[-1] if entered else None
        current_status = stages[current]["status"] if current is not None else None
        if current is None:
            next_stage: str | None = STAGE_ORDER[0]
        elif current_status == "gate_passed":
            index = STAGE_ORDER.index(current)
            next_stage = STAGE_ORDER[index + 1] if index + 1 < len(STAGE_ORDER) else None
        else:
            next_stage = current
        return {
            "projection_kind": "coverage_mission_stage_state",
            "mission_ref": mission_ref,
            "company_ref": company_ref,
            "stages": stages,
            "entered_stages": entered,
            "completed_stages": completed,
            "current_stage": current,
            "current_status": current_status,
            "next_stage": next_stage,
            "record_count": len(rows),
        }

    def companies_at_or_past(self, stage_ref: str, mission_ref: str) -> list[str]:
        """Every company of this mission that has ever reached ``stage_ref``.

        Reached means: it entered that stage under some version of the
        mission, or it passed the stage before it, which is the same event
        seen from one step back -- live, four companies passed their Initial
        Screen and none has entered ``deep_insight_gate``, and all four are
        past the screen.

        Deliberately **monotone**, unlike ``current_stage_state``: this answers
        "did this ever happen", so a later ``gate_failed`` -- a reopen -- does
        not un-reach a stage that was reached.  Residency (P14a) is built on
        that: a company leaves by leaving the mission universe, which is a
        human act, not by a gate being reopened.  Ask ``current_stage_state``
        when the question is "where does it stand *now*".
        """

        stage_ref = _vocabulary(stage_ref, STAGE_ORDER, "stage_ref")
        mission_ref = _text(mission_ref, "mission_ref")
        index = STAGE_ORDER.index(stage_ref)
        at_or_past = STAGE_ORDER[index:]
        previous = STAGE_ORDER[index - 1] if index > 0 else None
        histories: dict[str, dict[str, list[str]]] = {}
        for row in self._folded_rows(self.connection.cursor(), mission_ref):
            histories.setdefault(row["company_ref"], {}).setdefault(row["stage_ref"], []).append(
                row["status"]
            )
        reached: list[str] = []
        for company_ref, history in histories.items():
            if any(history.get(stage) for stage in at_or_past):
                reached.append(company_ref)
            elif previous is not None and "gate_passed" in (history.get(previous) or ()):
                reached.append(company_ref)
        return sorted(reached)

    def record_stage(
        self,
        *,
        mission_version_ref: str,
        mission_version_hash: str,
        company_ref: str,
        stage_ref: str,
        status: str,
        evidence_refs: list[str],
        rationale: str,
        actor_ref: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        mission_version_ref = _text(mission_version_ref, "mission_version_ref")
        mission_version_hash = _sha256(mission_version_hash, "mission_version_hash")
        company_ref = _text(company_ref, "company_ref")
        stage_ref = _vocabulary(stage_ref, STAGE_ORDER, "stage_ref")
        status = _vocabulary(status, STAGE_STATUSES, "status")
        evidence_refs = _texts(evidence_refs, "evidence_refs")
        rationale = _text(rationale, "rationale")
        actor_ref = _actor(actor_ref)
        idempotency_key = _text(idempotency_key, "idempotency_key")
        if status == "gate_passed" and not evidence_refs:
            raise CoverageMissionValidationError("gate_passed requires at least one evidence ref")
        request = {
            "mission_version_ref": mission_version_ref,
            "mission_version_hash": mission_version_hash,
            "company_ref": company_ref,
            "stage_ref": stage_ref,
            "status": status,
            "evidence_refs": evidence_refs,
            "rationale": rationale,
            "actor_ref": actor_ref,
        }
        request_hash = self._request_hash("record_stage", request)
        with self._transaction() as cur:
            duplicate = self._idem(
                cur, idempotency_key, "record_stage", request_hash, marker="status_marker"
            )
            if duplicate is not None:
                return duplicate
            mission = self.mission(mission_version_ref)
            if mission["content_hash"] != mission_version_hash:
                raise CoverageMissionConflict("mission version hash binding failed")
            pointer = cur.execute(
                "SELECT mission_version_id FROM coverage_mission_pointer WHERE mission_ref=?",
                (mission["mission_ref"],),
            ).fetchone()
            if pointer is None or pointer["mission_version_id"] != mission_version_ref:
                raise CoverageMissionConflict("stage records must bind the active mission version")
            if company_ref not in {member["company_ref"] for member in mission["universe"]}:
                raise CoverageMissionConflict("company is not in the mission universe")
            playbook = self._validate_playbook_binding(cur, mission["bindings"]["playbook_version"])
            stage = next(item for item in playbook["stages"] if item["stage_ref"] == stage_ref)
            if actor_ref.startswith("automation:"):
                if actor_ref != mission["autonomy"]["automation_principal"]:
                    raise CoverageMissionConflict("automation actor is not the mission principal")
                if "stage_record" not in mission["autonomy"]["may_write"]:
                    raise CoverageMissionConflict("mission does not grant stage_record writes to automation")
                if status == "gate_passed" and stage["human_checkpoint"]:
                    raise CoverageMissionConflict(
                        f"{stage_ref} is a human checkpoint; gate_passed requires a human: actor"
                    )
            # P14-S: the ladder is validated against the *folded* state --
            # every version of this mission_ref, in time order -- while the
            # record itself still binds the active version as provenance.
            # Before this, a company that passed its Initial Screen under v13
            # could not enter deep_insight_gate under v14: the new version's
            # ledger was empty, so "cannot be entered before initial_screen
            # gate_passed" refused a gate that had demonstrably passed. The
            # version rolls; what the company did does not un-happen.
            state = self._folded_statuses(cur, mission["mission_ref"], company_ref)
            index = STAGE_ORDER.index(stage_ref)
            folded = fold_stage_status(state[stage_ref])
            if status == "entered":
                # P14d sequel: an approved reopen is what makes a second
                # ``entered`` legal, and the only thing that does. Without one
                # this is still "you already entered it".
                if state[stage_ref] and folded != STAGE_REOPENED:
                    raise CoverageMissionConflict(f"{stage_ref} was already entered for this company")
                if index > 0 and fold_stage_status(state[STAGE_ORDER[index - 1]]) != "gate_passed":
                    raise CoverageMissionConflict(
                        f"{stage_ref} cannot be entered before {STAGE_ORDER[index - 1]} gate_passed"
                    )
            else:
                if "entered" not in state[stage_ref]:
                    raise CoverageMissionConflict(f"{stage_ref} must be entered before its gate is decided")
                # Still refused on a decided stage -- the ladder is right to
                # refuse a second decision on a settled gate. What changed is
                # that an approved reopen unsettles it, so ``folded`` is
                # ``reopened`` here and the re-issued screen's gate lands.
                if folded == "gate_passed":
                    raise CoverageMissionConflict(f"{stage_ref} gate was already passed for this company")
            identity = dict(request)
            record_id = _ref("mission-stage-record", identity)
            existing = cur.execute(
                "SELECT record_json FROM coverage_mission_stage_records WHERE record_id=?", (record_id,)
            ).fetchone()
            if existing is not None:
                return {**_canonical_record(existing["record_json"], "mission stage record"), "status_marker": "duplicate"}
            created_at = _now()
            record = {
                "schema_version": SCHEMA_VERSION,
                "id": record_id,
                "created_at": created_at,
                **identity,
            }
            wire = dict(record)
            wire["content_hash"] = content_hash(record)
            validate_mission_stage_record(wire)
            cur.execute(
                "INSERT INTO coverage_mission_stage_records"
                "(record_id,mission_version_ref,company_ref,stage_ref,status,actor_ref,"
                "record_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    record_id, mission_version_ref, company_ref, stage_ref, status, actor_ref,
                    canonical_json(wire), wire["content_hash"], created_at,
                ),
            )
            result = {**wire, "status_marker": "fresh"}
            self._save_idem(cur, idempotency_key, "record_stage", request_hash, result, created_at)
            return result

    def record_stage_reopen(
        self,
        *,
        mission_version_ref: str,
        mission_version_hash: str,
        company_ref: str,
        stage_ref: str,
        reopen_decision_ref: str,
        reopen_proposal_ref: str,
        reopened_version_ref: str,
        rationale: str,
        actor_ref: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Un-decide a decided gate, on one person's recorded say-so (ADR-0008).

        This is the write the stage ladder was missing.  A gate that has been
        passed is settled and ``record_stage`` is right to refuse a second
        decision on it; the thing that was impossible to say was that a person
        had decided it should stop being settled.  Saying it here, once, in its
        own append-only ledger, is what makes the re-issued screen's gate
        writable -- and it leaves the record of the first pass exactly where it
        was, which is the whole of ADR-0008.

        ``human:`` only, and not because the playbook says the stage is a human
        checkpoint: ``gate_reopen`` is a human checkpoint *by construction*
        (ADR-0008), whatever any stage is marked, so the rule is here and not
        conditional.  The two refs are what make the row checkable rather than
        a story: the ``gate_reopen`` decision that authorised it, and the
        deliverable version whose gate it re-opens.
        """

        mission_version_ref = _text(mission_version_ref, "mission_version_ref")
        mission_version_hash = _sha256(mission_version_hash, "mission_version_hash")
        company_ref = _text(company_ref, "company_ref")
        stage_ref = _vocabulary(stage_ref, STAGE_ORDER, "stage_ref")
        reopen_decision_ref = _text(reopen_decision_ref, "reopen_decision_ref")
        reopen_proposal_ref = _text(reopen_proposal_ref, "reopen_proposal_ref")
        reopened_version_ref = _text(reopened_version_ref, "reopened_version_ref")
        rationale = _text(rationale, "rationale")
        actor_ref = _actor(actor_ref)
        idempotency_key = _text(idempotency_key, "idempotency_key")
        if not actor_ref.startswith("human:"):
            raise CoverageMissionConflict(
                "re-opening a decided gate is a human checkpoint (ADR-0008)"
            )
        request = {
            "mission_version_ref": mission_version_ref,
            "mission_version_hash": mission_version_hash,
            "company_ref": company_ref,
            "stage_ref": stage_ref,
            "reopen_decision_ref": reopen_decision_ref,
            "reopen_proposal_ref": reopen_proposal_ref,
            "reopened_version_ref": reopened_version_ref,
            "rationale": rationale,
            "actor_ref": actor_ref,
        }
        request_hash = self._request_hash("record_stage_reopen", request)
        with self._transaction() as cur:
            duplicate = self._idem(
                cur, idempotency_key, "record_stage_reopen", request_hash,
                marker="status_marker",
            )
            if duplicate is not None:
                return duplicate
            mission = self.mission(mission_version_ref)
            if mission["content_hash"] != mission_version_hash:
                raise CoverageMissionConflict("mission version hash binding failed")
            pointer = cur.execute(
                "SELECT mission_version_id FROM coverage_mission_pointer WHERE mission_ref=?",
                (mission["mission_ref"],),
            ).fetchone()
            if pointer is None or pointer["mission_version_id"] != mission_version_ref:
                raise CoverageMissionConflict("stage records must bind the active mission version")
            if company_ref not in {member["company_ref"] for member in mission["universe"]}:
                raise CoverageMissionConflict("company is not in the mission universe")
            state = self._folded_statuses(cur, mission["mission_ref"], company_ref)
            folded = fold_stage_status(state[stage_ref])
            if folded != "gate_passed":
                raise CoverageMissionConflict(
                    f"{stage_ref} is {folded or 'not reached'} for this company; only a "
                    "passed gate can be re-opened"
                )
            identity = dict(request)
            record_id = _ref("mission-stage-reopen", identity)
            existing = cur.execute(
                "SELECT record_json FROM coverage_mission_stage_reopens WHERE record_id=?",
                (record_id,),
            ).fetchone()
            if existing is not None:
                return {
                    **_canonical_record(existing["record_json"], "mission stage reopen"),
                    "status_marker": "duplicate",
                }
            spent = cur.execute(
                "SELECT record_id FROM coverage_mission_stage_reopens "
                "WHERE company_ref=? AND stage_ref=? AND reopen_decision_ref=?",
                (company_ref, stage_ref, reopen_decision_ref),
            ).fetchone()
            if spent is not None:
                raise CoverageMissionConflict(
                    "this gate_reopen decision has already re-opened this stage"
                )
            created_at = _now()
            record = {
                "schema_version": SCHEMA_VERSION,
                "id": record_id,
                "created_at": created_at,
                "status": STAGE_REOPENED,
                **identity,
            }
            wire = dict(record)
            wire["content_hash"] = content_hash(record)
            validate_mission_stage_reopen(wire)
            cur.execute(
                "INSERT INTO coverage_mission_stage_reopens"
                "(record_id,mission_version_ref,company_ref,stage_ref,reopen_decision_ref,"
                "reopen_proposal_ref,reopened_version_ref,record_json,content_hash,actor_ref,"
                "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record_id, mission_version_ref, company_ref, stage_ref,
                    reopen_decision_ref, reopen_proposal_ref, reopened_version_ref,
                    canonical_json(wire), wire["content_hash"], actor_ref, created_at,
                ),
            )
            result = {**wire, "status_marker": "fresh"}
            self._save_idem(
                cur, idempotency_key, "record_stage_reopen", request_hash, result, created_at
            )
            return result

    def stage_reopens(
        self, mission_ref: str, company_ref: str | None = None, stage_ref: str | None = None
    ) -> list[dict[str, Any]]:
        """Every reopen ever approved for one mission, oldest first.

        Keyed on ``mission_ref`` rather than on a version, for the same reason
        the ladder is: a reopen is a fact about the company, and the version it
        binds is provenance.
        """

        mission_ref = _text(mission_ref, "mission_ref")
        query = (
            "SELECT o.record_json AS record_json FROM coverage_mission_stage_reopens o "
            "JOIN coverage_mission_versions v ON v.mission_version_id=o.mission_version_ref "
            "WHERE v.mission_ref=?"
        )
        params: list[Any] = [mission_ref]
        if company_ref is not None:
            query += " AND o.company_ref=?"
            params.append(_text(company_ref, "company_ref"))
        if stage_ref is not None:
            query += " AND o.stage_ref=?"
            params.append(_vocabulary(stage_ref, STAGE_ORDER, "stage_ref"))
        query += " ORDER BY o.created_at,o.record_id"
        return [
            _canonical_record(row["record_json"], "mission stage reopen")
            for row in self.connection.execute(query, params).fetchall()
        ]
    def stage_records(self, mission_version_ref: str, company_ref: str | None = None) -> list[dict[str, Any]]:
        mission_version_ref = _text(mission_version_ref, "mission_version_ref")
        query = "SELECT * FROM coverage_mission_stage_records WHERE mission_version_ref=?"
        params: list[Any] = [mission_version_ref]
        if company_ref is not None:
            query += " AND company_ref=?"
            params.append(_text(company_ref, "company_ref"))
        query += " ORDER BY created_at,record_id"
        records = []
        for row in self.connection.execute(query, params).fetchall():
            wire = validate_mission_stage_record(_canonical_record(row["record_json"], "mission stage record"))
            if (
                wire["id"] != row["record_id"]
                or wire["mission_version_ref"] != row["mission_version_ref"]
                or wire["company_ref"] != row["company_ref"]
                or wire["stage_ref"] != row["stage_ref"]
                or wire["status"] != row["status"]
                or wire["actor_ref"] != row["actor_ref"]
                or wire["created_at"] != row["created_at"]
                or wire["content_hash"] != row["content_hash"]
            ):
                raise CoverageMissionConflict("mission stage record authority drifted")
            records.append(wire)
        return records

    def record_stage_claim(
        self,
        *,
        mission_version_ref: str,
        mission_version_hash: str,
        company_ref: str,
        ticker: str,
        claim_version_ref: str,
        claim_version_hash: str,
        evidence_version_ref: str,
        evidence_version_hash: str,
        source_location: str,
        actor_ref: str,
    ) -> dict[str, Any]:
        """Bind one already-formal Claim/Evidence pair to the current stage."""

        authorization = self.authorize_sec_lane(
            company_ref=company_ref,
            ticker=ticker,
            actor_ref=actor_ref,
            mission_version_ref=mission_version_ref,
            mission_version_hash=mission_version_hash,
        )
        claim_version_ref = _text(claim_version_ref, "claim_version_ref")
        evidence_version_ref = _text(evidence_version_ref, "evidence_version_ref")
        claim_version_hash = _sha256(claim_version_hash, "claim_version_hash")
        evidence_version_hash = _sha256(evidence_version_hash, "evidence_version_hash")
        source_location = _text(source_location, "source_location")
        claim = self.store.get_claim(claim_version_ref)
        if (
            claim is None
            or claim.get("content_hash") != claim_version_hash
            or (claim.get("claim") or {}).get("subject_ref") != company_ref
        ):
            raise CoverageMissionConflict("formal Claim binding failed")
        evidence = self.connection.execute(
            "SELECT content_hash FROM evidence_versions WHERE evidence_version_id=?",
            (evidence_version_ref,),
        ).fetchone()
        if evidence is None or evidence["content_hash"] != evidence_version_hash:
            raise CoverageMissionConflict("formal Evidence binding failed")

        progress = self.mission_progress(authorization["mission_ref"])
        company = next(
            item for item in progress["companies"] if item["company_ref"] == company_ref
        )
        stage_ref = company["next_stage"] or company["current_stage"] or STAGE_ORDER[0]
        if company["current_stage"] != stage_ref:
            self.record_stage(
                mission_version_ref=mission_version_ref,
                mission_version_hash=mission_version_hash,
                company_ref=company_ref,
                stage_ref=stage_ref,
                status="entered",
                evidence_refs=[],
                rationale="SEC automation entered the current mission stage before recording evidence.",
                actor_ref=actor_ref,
                idempotency_key=f"mission-stage-enter:{mission_version_ref}:{company_ref}:{stage_ref}",
            )
        identity = {
            "mission_version_ref": mission_version_ref,
            "mission_version_hash": mission_version_hash,
            "company_ref": company_ref,
            "stage_ref": stage_ref,
            "claim_version_ref": claim_version_ref,
            "claim_version_hash": claim_version_hash,
            "evidence_version_ref": evidence_version_ref,
            "evidence_version_hash": evidence_version_hash,
            "source_location": source_location,
            "actor_ref": actor_ref,
        }
        record_id = _ref("mission-stage-claim", identity)
        existing = self.connection.execute(
            "SELECT record_json FROM coverage_mission_stage_claims "
            "WHERE mission_version_ref=? AND claim_version_ref=?",
            (mission_version_ref, claim_version_ref),
        ).fetchone()
        if existing is not None:
            wire = validate_mission_stage_claim(
                _canonical_record(existing["record_json"], "mission stage claim")
            )
            if wire["id"] != record_id:
                raise CoverageMissionConflict("formal Claim is already bound differently")
            return {**wire, "status": "duplicate"}
        created_at = _now()
        record = {
            "schema_version": SCHEMA_VERSION,
            "id": record_id,
            "created_at": created_at,
            **identity,
        }
        wire = {**record, "content_hash": content_hash(record)}
        validate_mission_stage_claim(wire)
        with self._transaction() as cur:
            cur.execute(
                "INSERT INTO coverage_mission_stage_claims"
                "(record_id,mission_version_ref,company_ref,stage_ref,claim_version_ref,"
                "claim_version_hash,evidence_version_ref,evidence_version_hash,source_location,"
                "actor_ref,record_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record_id, mission_version_ref, company_ref, stage_ref, claim_version_ref,
                    claim_version_hash, evidence_version_ref, evidence_version_hash,
                    source_location, actor_ref, canonical_json(wire), wire["content_hash"], created_at,
                ),
            )
        return {**wire, "status": "fresh"}

    def stage_claims(
        self, mission_version_ref: str, company_ref: str | None = None
    ) -> list[dict[str, Any]]:
        mission_version_ref = _text(mission_version_ref, "mission_version_ref")
        query = "SELECT * FROM coverage_mission_stage_claims WHERE mission_version_ref=?"
        params: list[Any] = [mission_version_ref]
        if company_ref is not None:
            query += " AND company_ref=?"
            params.append(_text(company_ref, "company_ref"))
        query += " ORDER BY created_at,record_id"
        records = []
        for row in self.connection.execute(query, params).fetchall():
            wire = validate_mission_stage_claim(
                _canonical_record(row["record_json"], "mission stage claim")
            )
            if wire["id"] != row["record_id"] or wire["content_hash"] != row["content_hash"]:
                raise CoverageMissionConflict("mission stage claim authority drifted")
            records.append(wire)
        return records

    def mission_progress(self, mission_ref: str) -> dict[str, Any]:
        mission = self.active_mission(mission_ref)
        companies = []
        for member in mission["universe"]:
            # P14-S: folded across every version of the mission_ref. Progress
            # that reset itself on a version roll was not progress.
            folded = self.current_stage_state(mission["mission_ref"], member["company_ref"])
            companies.append({
                **member,
                "current_stage": folded["current_stage"],
                "current_status": folded["current_status"],
                "completed_stages": folded["completed_stages"],
                "next_stage": folded["next_stage"],
                "record_count": folded["record_count"],
                "claim_count": self.connection.execute(
                    "SELECT COUNT(*) FROM coverage_mission_stage_claims "
                    "WHERE mission_version_ref=? AND company_ref=?",
                    (mission["id"], member["company_ref"]),
                ).fetchone()[0],
                "discovery_count": self.connection.execute(
                    "SELECT COUNT(*) FROM coverage_mission_source_discoveries "
                    "WHERE mission_version_ref=? AND company_ref=?",
                    (mission["id"], member["company_ref"]),
                ).fetchone()[0],
                "discovered_document_count": self.connection.execute(
                    "SELECT COUNT(*) FROM coverage_mission_discovered_documents "
                    "WHERE mission_version_ref=? AND company_ref=?",
                    (mission["id"], member["company_ref"]),
                ).fetchone()[0],
                "acquired_document_count": self.connection.execute(
                    "SELECT COUNT(*) FROM coverage_mission_discovered_documents "
                    "WHERE mission_version_ref=? AND company_ref=? "
                    "AND status IN ('acquired','already_in_authority')",
                    (mission["id"], member["company_ref"]),
                ).fetchone()[0],
                "awaiting_extraction_review_count": self.connection.execute(
                    "SELECT COUNT(*) FROM coverage_mission_document_reviews "
                    "WHERE mission_version_ref=? AND company_ref=? "
                    "AND state='awaiting_human_extraction'",
                    (mission["id"], member["company_ref"]),
                ).fetchone()[0],
            })
        return {
            "projection_kind": "coverage_mission_progress",
            "mission_ref": mission["mission_ref"],
            "mission_version_ref": mission["id"],
            "mission_version_hash": mission["content_hash"],
            "stage_order": list(STAGE_ORDER),
            "companies": companies,
        }


__all__ = [
    "AUTOMATION_WRITE_SCOPES",
    "DISCOVERY_SOURCES",
    "BOOTSTRAP_PRIORITIES",
    "CHECKPOINT_KINDS",
    "COVERAGE_TIERS",
    "DELIVERABLE_KINDS",
    "DISCOVERED_DOCUMENT_STATUSES",
    "DISCOVERY_DISPATCH_STATUSES",
    "FOLDED_STAGE_STATUSES",
    "MAX_FAILURE_REASON_CHARS",
    "REQUIRED_CHECKPOINTS",
    "SEC_RUN_SUCCEEDED",
    "SOURCE_STATUSES",
    "STAGE_DECISIONS",
    "STAGE_REOPENED",
    "STAGE_STATUSES",
    "fold_stage_status",
    "sec_run_failure_reason",
    "CoverageMissionAuthority",
    "CoverageMissionConflict",
    "CoverageMissionError",
    "CoverageMissionNotFound",
    "CoverageMissionValidationError",
    "validate_coverage_mission_version",
    "validate_mission_body",
    "validate_mission_stage_record",
    "validate_mission_stage_reopen",
    "validate_mission_stage_claim",
    "validate_mission_source_discovery",
]
