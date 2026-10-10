"""P14a: the child that judges a bounded batch of events, one call each.

Everything expensive happens here, so everything careful happens here too.

The batch is bounded three ways and any of them can end the run: the number of
events per company, the number per run, and the ``event_response`` share of
the mission's day.  The pool is checked *before* each pair of calls and the
reservation is the two calls' own ceilings, because a pool that only counts
settled spend admits a whole day's work before the first bill arrives (P14e
found this and ``ThesisImpactBudgetStore`` found it before that).

Every event is judged at most once, and that is enforced by a UNIQUE column
rather than by this loop remembering: the loop is one process and the ledger
outlives it.

The verifier runs on a second model configuration.  If it is absent, or its
family cannot be resolved, or it resolves to the producer's family, the
judgement is recorded as ``refused`` and no effect is applied -- an unverified
decision is not a decision this system acts on.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .budget_pools import POOL_EXHAUSTED_STATUS
from .call_budget import resolve_run_budget
from .cockpit_model import CockpitModel
from .coverage_mission import CoverageMissionAuthority
from .event_judgement import (
    DAILY_DIGEST_MARKER,
    EVENT_PROMPT_CONTRACT_VERSION,
    MAX_PROMPT_BYTES,
    PURPOSE,
    REFLECTION_PURPOSE,
    REFLECTION_VERIFIER_PURPOSE,
    VERIFIER_PURPOSE,
    EventJudgementAuthority,
    EventJudgementError,
    apply_effect,
    build_context,
    judge,
    pool_state,
    record_judgement_followups,
    reflect,
    reflection_is_owed,
    route_family_resolver,
    sweep_judgement_followups,
    verify,
    verify_reflection,
)
from .event_judgement import open_research_material
from . import event_judgement_result as result_lane
from .event_triage import (
    BYPASS_KINDS as TRIAGE_BYPASS_KINDS,
    TRIAGE_PURPOSE,
    gate as triage_gate,
    record_triage,
    screen as triage_screen,
    screened_out_sql,
    triage_for,
    triage_table_exists,
)
from .mission_deliverable import MissionDeliverableAuthority
from .model_configurations import register_model_config_name
from .research_event import (UNDATED_PAGE_TIERS, ResearchEventAuthority,
                             evergreen_page_reason)
from .source_capability_map import build_map, prompt_table
from .store import content_hash
from .store import DaltonStore
from .tracking_cadence import load_policy, screen_passed_companies

SUMMARY_SCHEMA_VERSION = "0.1"
EVENT_MODEL_CONTRACT_VERSION = "2"
JUDGE_MODEL_CONFIG = register_model_config_name("event-judgement-model-config.json")
VERIFIER_MODEL_CONFIG = register_model_config_name("event-verifier-model-config.json")

# One event's prompt is a table, not a document: the theses, a handful of
# drivers, five past decisions and a dozen Claims. It has never needed a large
# window and giving it one would let a bad context grow into a bad answer.
# CockpitModel enforces this field as UTF-8 bytes and also exposes it to the
# router as the worst-case input-token reservation. Keep the two bounds equal:
# advertising the old 60k reservation made every current brain route exceed
# this lane's unchanged $0.10 per-call cap before a call could start.
MAX_INPUT_TOKENS = MAX_PROMPT_BYTES
# Batch F: a result-type event answers with its 实际 vs 预期 table, the market
# reaction and the thesis impact as well as the decision -- about 2-3 K tokens
# of Chinese; 1,500 truncated it into a refusal.
MAX_OUTPUT_TOKENS = 4_000
MAX_COST_USD = 1.0
TIMEOUT_SECONDS = 180
MAX_EVENTS_PER_COMPANY = 3
MAX_EVENTS_PER_RUN = 8
HK_TIMEZONE = ZoneInfo("Asia/Hong_Kong")


def event_model_contract_ref(budget: dict[str, Any] | None = None) -> str:
    limits = budget or {
        "max_input_tokens": MAX_INPUT_TOKENS,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "max_cost_usd": MAX_COST_USD,
        "timeout_seconds": TIMEOUT_SECONDS,
    }
    fingerprint = content_hash({
        "version": EVENT_MODEL_CONTRACT_VERSION,
        "prompt_contract": EVENT_PROMPT_CONTRACT_VERSION,
        "max_input_bytes": limits["max_input_tokens"],
        "max_output_tokens": limits["max_output_tokens"],
        "max_cost_usd": str(limits["max_cost_usd"]),
        "timeout_seconds": limits["timeout_seconds"],
    })
    return f"event-model-contract:{fingerprint[:16]}"


class EventCockpitModel(CockpitModel):
    """Namespace scheduler identity to this lane's prompt and budget contract."""

    def call(self, *, purpose: str, request_id: str, prompt: str,
             mission: Any, producer_route_decision_refs=()):
        return super().call(
            purpose=purpose,
            request_id=f"{event_model_contract_ref(self.budget_for(purpose))}:{request_id}",
            prompt=prompt,
            mission=mission,
            producer_route_decision_refs=producer_route_decision_refs,
        )


def buyback_group_key(event: dict[str, Any]) -> tuple[str, ...] | None:
    payload = event.get("payload") or {}
    if event.get("kind") != "buyback_disclosure":
        return None
    if payload.get("market") == "HK":
        cluster = str(payload.get("cluster_key") or "")
        week = cluster.split(":", 1)[0]
        try:
            datetime.strptime(f"{week}-1", "%G-W%V-%u")
        except ValueError:
            return None
        else:
            return ("hk_week", str(event.get("company_ref")), week)
    accession = payload.get("accession")
    if (payload.get("disclosure_kind") == "issuer_purchases_table"
            and isinstance(accession, str) and accession):
        return ("us_filing", accession)
    return None


# -- what may share a call ---------------------------------------------------
#
# 2026-09-25: every judgement is an Opus call at about 0.28 USD, and across the
# fourteen days of the ledger 1,638 of 1,641 came back NO_CHANGE.  The three
# that did not were Claims drawn from public web pages on the company's own
# results and risk disclosures; none was a sales note, an expert excerpt, a
# headline, a crowd post or a Claim drawn from those.  So those low-tier inputs
# still reach the judge in full -- nothing is skipped, and no cheaper model
# decides anything -- but one company's low-tier inputs from one day share one
# call, a few at a time.  And a document found again under another discovery
# is the same document: it is judged once.

#: Event kinds whose tier is what the desk heard, an expert said, a headline
#: or a crowd post.  Filings, transcripts, prices, ownership and Claims from
#: anywhere else keep a call of their own.
LOW_TIER_KINDS: frozenset[str] = frozenset({
    "news", "sales_note", "expert_excerpt", "crowd_post",
})
#: Claim sources that are those same low-tier documents, by the source segment
#: of ``claim:<source>:<id>``.  ``public_web`` is deliberately absent: every
#: decision other than NO_CHANGE in the ledger so far was one of those.
LOW_TIER_CLAIM_SOURCES: frozenset[str] = frozenset({
    "sales_notes", "company_wiki", "guidepoint",
})
#: At most this many low-tier inputs in one call, so a busy day is several
#: small batches rather than one prompt nobody reads to the end.
MAX_LOW_TIER_BATCH = 6
#: And no slot other than a buyback's grows past this, repeated documents
#: included: every member is printed in full and the prompt has a byte bound.
MAX_GROUP_INPUTS = 12


def claim_source(event: dict[str, Any]) -> str | None:
    ref = (event.get("payload") or {}).get("claim_ref")
    if not isinstance(ref, str):
        return None
    parts = ref.split(":")
    return parts[1] if len(parts) >= 3 and parts[0] == "claim" and parts[1] else None


def is_low_tier(event: dict[str, Any]) -> bool:
    """A low-tier input that may share a call with its company's same-day peers."""

    kind = event.get("kind")
    if kind in LOW_TIER_KINDS:
        return True
    return kind == "claim" and claim_source(event) in LOW_TIER_CLAIM_SOURCES


def document_key(event: dict[str, Any]) -> tuple[str, ...] | None:
    """One company, one kind, one document -- whatever discovery found it."""

    document = (event.get("payload") or {}).get("document_ref")
    if not isinstance(document, str) or not document:
        return None
    return ("document", str(event.get("company_ref")), str(event.get("kind")), document)


# 2026-09-25: a Claim event carries no ``document_ref``, so "one document, one
# read" never applied to Claims: the 28 risk factors extracted from one DXC
# 10-K page were judged one Opus call each (09-25, ~7.8 USD, all NO_CHANGE),
# and the backlog holds 1,521 public_web and 792 alphaengine Claims from 225
# and 92 documents.  A Claim's document is found the way the Claim index finds
# it: evidence -> the transcript correction set its lineage names (directly or
# through its citation binding) -> that set's ``document_ref``; failing that,
# the source envelope the evidence was captured in.  The Claims of one document
# then share one slot of at most MAX_DOCUMENT_CLAIMS -- every one printed in
# full, the prompt saying any single one can decide -- and nothing is skipped:
# unlike a repeated document event, a Claim is never answered from another
# Claim's judgement.
#
# public_web stays out of LOW_TIER_CLAIM_SOURCES (every decision other than
# NO_CHANGE so far was a public_web Claim), and so does alphaengine: two thirds
# of its Claims are earnings-call transcripts -- management on its own results,
# the same kind of statement as the ACN bookings Claims that moved a view --
# and batching by document saves more than a day batch would (8.6 Claims a
# document against at most six a day batch).

#: A slot of Claims drawn from one document holds at most this many.
MAX_DOCUMENT_CLAIMS = MAX_GROUP_INPUTS
_CORRECTION_SET_PREFIX = "transcript-correction-set-version:"
_CITATION_BINDING_PREFIX = "transcript-claim-citation-binding:"


class ClaimDocuments:
    """A Claim event -> the document its Claim was drawn from, read once per run."""

    def __init__(self, connection: Any, state_dir: Path | None = None) -> None:
        self.connection = connection
        self.state_dir = state_dir
        self._claims: dict[str, str | None] = {}
        self._sets: dict[str, str | None] = {}
        self._results: dict[str, tuple[str, ...] | None] = {}

    def result_key(self, event: dict[str, Any]) -> tuple[str, ...] | None:
        """Batch F: a release and its call (±3 days) are one result event."""

        if not result_lane.is_result_event(event):
            return None
        ref = str(event.get("id"))
        if ref not in self._results:
            try:
                self._results[ref] = result_lane.result_key(self.connection, event, state_dir=self.state_dir)
            except Exception:  # noqa: BLE001 - an unanchorable event keeps its own slot
                self._results[ref] = None
        return self._results[ref]

    def __call__(self, event: dict[str, Any]) -> str | None:
        if event.get("kind") != "claim":
            return None
        ref = (event.get("payload") or {}).get("claim_version_ref")
        if not isinstance(ref, str) or not ref:
            return None
        if ref not in self._claims:
            self._claims[ref] = self._resolve(ref)
        return self._claims[ref]

    def _query(self, sql: str, params: tuple[Any, ...]) -> list[Any]:
        try:
            return self.connection.execute(sql, params).fetchall()
        except Exception:  # noqa: BLE001 - a Core without these tables has no documents
            return []

    def _correction_set_document(self, version_ref: str) -> str | None:
        if version_ref not in self._sets:
            rows = self._query(
                "SELECT record_json FROM transcript_correction_set_versions "
                "WHERE version_id=?", (version_ref,))
            document = None
            if rows:
                try:
                    document = json.loads(rows[0][0]).get("document_ref")
                except (TypeError, ValueError, AttributeError):
                    document = None
            self._sets[version_ref] = (
                document if isinstance(document, str) and document else None)
        return self._sets[version_ref]

    def _resolve(self, claim_version_ref: str) -> str | None:
        found: list[str] = []
        envelopes: list[str] = []
        for (evidence_json,) in self._query(
                "SELECT v.evidence_json FROM evidence_relations r "
                "JOIN evidence_versions v ON v.evidence_version_id=r.evidence_version_id "
                "WHERE r.claim_version_id=?", (claim_version_ref,)):
            try:
                evidence = json.loads(evidence_json)
            except (TypeError, ValueError):
                continue
            if not isinstance(evidence, dict):
                continue
            sets = [item for item in evidence.get("source_lineage") or ()
                    if isinstance(item, str) and item.startswith(_CORRECTION_SET_PREFIX)]
            for artifact in evidence.get("artifact_refs") or ():
                binding = artifact.get("ref") if isinstance(artifact, dict) else None
                if isinstance(binding, str) and binding.startswith(_CITATION_BINDING_PREFIX):
                    sets.extend(row[0] for row in self._query(
                        "SELECT correction_set_version_ref FROM "
                        "transcript_claim_citation_bindings WHERE binding_id=?", (binding,)))
            found.extend(document for document in map(self._correction_set_document, sets)
                         if document)
            envelope = evidence.get("source_envelope_ref")
            if isinstance(envelope, str) and envelope:
                envelopes.append(envelope)
        # A Claim several documents support is read with the first of them in a
        # fixed order, so the same Claim lands in the same slot every run.
        if found:
            return sorted(found)[0]
        return sorted(envelopes)[0] if envelopes else None


def _utc_day(value: Any) -> str:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)[:10]
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).date().isoformat()


def event_group_key(
    event: dict[str, Any], document_of: Any = None,
) -> tuple[str, ...]:
    """The slot an event is judged in: a filing, a closed HK week, a Claim's
    document, a low-tier day, or itself.

    ``document_of`` (a :class:`ClaimDocuments`) finds a Claim's document; the
    Claims of one document share a slot, unless they are low-tier Claims,
    which keep their day batch.
    """

    key = buyback_group_key(event)
    if key is not None:
        return key
    # 2026-10-07: one filing's Item 5 / Item 9B trading arrangements are one
    # table -- an Amazon 10-K lists seven -- and are read together, as one
    # 10-Q's monthly buyback rows are. Each row stays its own event.
    if event.get("kind") == "insider_trading_plan":
        accession = (event.get("payload") or {}).get("accession")
        if isinstance(accession, str) and accession:
            return ("plan_filing", str(event.get("company_ref")), accession)
    # Batch F: an earnings release and its results call are one occurrence,
    # judged together on the same-event pack.
    result_key = getattr(document_of, "result_key", None)
    if result_key is not None:
        key = result_key(event)
        if key is not None:
            return key
    # A low-tier Claim keeps its day batch: a sales note yields a Claim or two,
    # and six of a day's in one call is fewer calls than one call a note.
    document = (None if document_of is None or is_low_tier(event)
                else document_of(event))
    if document:
        return ("claim_document", str(event.get("company_ref")), document)
    if is_low_tier(event):
        return ("low_tier", str(event.get("company_ref")), _utc_day(event.get("occurred_at")))
    return ("event", event["id"])


def _slot_cap(key: tuple[str, ...]) -> int | None:
    if key[0] == "low_tier":
        return MAX_LOW_TIER_BATCH
    if key[0] == "claim_document":
        return MAX_DOCUMENT_CLAIMS
    if key[0] in ("plan_filing", "result_event"):
        return MAX_GROUP_INPUTS
    return None


def _coherent_group(group: list[dict[str, Any]], document_of: Any = None) -> bool:
    """Every member shares the primary's slot or repeats a document already in it."""

    key = event_group_key(group[0], document_of)
    cap = _slot_cap(key)
    if cap is not None and key[0] == "claim_document" and len(group) > cap:
        return False
    documents: set[tuple[str, ...]] = set()
    for index, item in enumerate(group):
        document = document_key(item)
        if index and event_group_key(item, document_of) != key and document not in documents:
            return False
        if document is not None:
            documents.add(document)
    return True


# 2026-09-30: a price move is re-recorded every time its price series is
# refreshed -- the payload carries the series version and the connector
# invocation, so the same close-to-close move is a new event by the ledger's
# idempotency rule (ACN's 2026-09-08 move is 24 events).  Between 09-24 and
# 09-30, 163 of the 170 price moves the judge paid for (39.9 of 41.9 USD) were
# a company/day/direction whose move had already been judged NO_CHANGE.  The
# event stays (which refresh saw it is a fact); the judgement is the earlier
# one, recorded again at no cost, under exactly the document rule below.
EQUIVALENT_PAYLOAD_FIELDS: dict[str, tuple[str, ...]] = {
    "price_move": ("as_of", "direction"),
}


def equivalence_key(event: dict[str, Any]) -> tuple[str, ...] | None:
    """One company, one kind, one market day and direction -- whichever refresh saw it."""

    fields = EQUIVALENT_PAYLOAD_FIELDS.get(str(event.get("kind")))
    if fields is None:
        return None
    payload = event.get("payload") or {}
    values = [payload.get(field) for field in fields]
    if any(not isinstance(value, str) or not value for value in values):
        return None
    return ("equivalent", str(event.get("company_ref")), str(event.get("kind")), *values)


def prior_no_change_judgements(
    connection: Any, event_group: list[dict[str, Any]], *, state_dir: Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Members whose document or equivalent event was already judged, only NO_CHANGE.

    Keyed by event id, valued by the earlier judgement record.  A document
    (or a price move of the same company, day and direction) that was ever
    judged anything else is judged again in full: the saving is only taken
    where the answer is already known to be "nothing changes".
    """

    found: dict[str, dict[str, Any]] = {}
    for item in event_group:
        if result_lane.is_result_event(item):
            # Batch F: a result event with new material is judged (or
            # re-judged) on its pack; its dedupe is the result-event path's.
            continue
        key = document_key(item)
        if key is not None:
            rows = connection.execute(
                "SELECT j.decision AS decision, j.record_json AS record_json "
                "FROM event_judgements j JOIN research_events e ON e.event_id = j.event_ref "
                "WHERE e.company_ref=? AND e.kind=? "
                "AND json_extract(e.record_json, '$.payload.document_ref')=? "
                "AND e.event_id<>? ORDER BY j.created_at, j.judgement_id",
                (key[1], key[2], key[3], item["id"]),
            ).fetchall()
        else:
            key = equivalence_key(item)
            if key is None:
                continue
            fields = EQUIVALENT_PAYLOAD_FIELDS[key[2]]
            rows = connection.execute(
                "SELECT j.decision AS decision, j.record_json AS record_json "
                "FROM event_judgements j JOIN research_events e ON e.event_id = j.event_ref "
                "WHERE e.company_ref=? AND e.kind=? "
                + "".join(f"AND json_extract(e.record_json, '$.payload.{field}')=? "
                          for field in fields)
                + "AND e.event_id<>? ORDER BY j.created_at, j.judgement_id",
                (key[1], key[2], *key[3:], item["id"]),
            ).fetchall()
        if rows and all(row["decision"] == "NO_CHANGE" for row in rows):
            record = json.loads(rows[0]["record_json"])
            if (state_dir is not None and result_lane.lacked_body(record)[0]
                    and (record.get("result") or {}).get("evidence_status") == "insufficient"
                    and result_lane.body_document(state_dir, item, None, None) is not None):
                # The same gap does not get its NO_CHANGE again once the
                # material it lacked has arrived: judge it on the body.
                continue
            record["_repeat_kind"] = (
                "repeated_document" if key[0] == "document" else "repeated_event")
            found[item["id"]] = record
    return found


def _closed_hk_week(group_key: tuple[str, ...] | None, now: datetime) -> bool:
    if group_key is None or group_key[0] != "hk_week":
        return True
    local = now.astimezone(HK_TIMEZONE)
    year, week, _ = local.isocalendar()
    return group_key[2] < f"{year:04d}-W{week:02d}"


def group_evidence_events(
    events: ResearchEventAuthority, event_group: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """All evidence in an HK week; only new rows are judgement aliases."""

    key = buyback_group_key(event_group[0])
    if key is None or key[0] != "hk_week":
        return event_group
    rows = events.connection.execute(
        "SELECT event_id FROM research_events WHERE company_ref=? "
        "AND kind='buyback_disclosure' ORDER BY occurred_at, event_id",
        (event_group[0]["company_ref"],),
    ).fetchall()
    matching = [event for row in rows if buyback_group_key(
        event := events.event(row["event_id"])) == key]
    primary = event_group[0]
    return [primary, *(event for event in matching if event["id"] != primary["id"])]


def incremental_group_hash(event_group: list[dict[str, Any]]) -> str:
    return content_hash({"events": [
        {"id": event["id"], "content_hash": event.get("content_hash")}
        for event in sorted(event_group, key=lambda item: item["id"])
    ]})


def _record_repeat(
    judgements: EventJudgementAuthority, event: dict[str, Any],
    prior: dict[str, Any], *, mission: Any, actor_ref: str,
) -> dict[str, Any]:
    """Bind a repeated document to the NO_CHANGE its first reading got, at no cost."""

    marker = {"cost_micros": 0, "repeat_of_event_ref": prior["event_ref"],
              "repeat_of_judgement_ref": prior["id"]}
    kind = prior.get("_repeat_kind") or "repeated_document"
    judgement = {
        key: prior.get(key) for key in (
            "decision", "action", "driver_refs", "thesis_refs", "because",
            "citations", "note", "research_question", "forecast_change")
    }
    judgement["model"] = {**(prior.get("model") or {}), **marker}
    verifier = prior.get("verifier") or {}
    verification = {**verifier,
                    "model": {**(prior.get("verifier_model") or {}), **marker}}
    return judgements.record(
        event=event, judgement=judgement, verification=verification,
        effect={"kind": kind, "status": "recorded",
                "primary_event_ref": prior["event_ref"],
                "judgement_ref": prior["id"]},
        mission=mission, actor_ref=actor_ref,
    )


def _write_owner_only(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=1))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def missing_write_scopes(mission: Any) -> list[str]:
    """What the judgement lane needs granted before it spends anything.

    ``deliverable`` is the event note, ``forecast_line`` the revision entry
    point, ``thesis_revision_candidate`` the ADR-0007 proposal.  Only the
    first is required: a mission that grants none of the other two still gets
    judgements and notes, and its revise-shaped decisions become proposals
    rather than being dropped.  The list is reported so an operator can see
    which half of the brain is switched off.
    """

    granted = set(mission["autonomy"]["may_write"])
    return [
        word for word in ("deliverable", "forecast_line", "thesis_revision_candidate")
        if word not in granted
    ]


def unjudged_events(
    events: ResearchEventAuthority,
    judgements: EventJudgementAuthority,
    *,
    company_ref: str,
    limit: int | None,
) -> list[dict[str, Any]]:
    """This company's oldest events with no judgement.

    An anti-join in SQL rather than "read the newest N and filter": the filter
    version reads a fixed window and, once that window is entirely judged,
    returns nothing while older events sit unjudged for ever. Oldest first so
    a decision reads the events before it in the order they happened, and so
    the backlog drains from the end that is not moving.
    """

    screened = (screened_out_sql("e") if triage_table_exists(events.connection) else "")
    rows = events.connection.execute(
        "SELECT e.event_id AS event_id FROM research_events e "
        "LEFT JOIN event_judgements j ON j.event_ref = e.event_id "
        "WHERE e.company_ref = ? AND j.event_ref IS NULL" + screened + " "
        "ORDER BY e.occurred_at ASC, e.event_id ASC LIMIT ?",
        (company_ref, max(1, int(limit))),
    ).fetchall()
    return [events.event(row["event_id"]) for row in rows]


def unjudged_event_groups(
    events: ResearchEventAuthority,
    judgements: EventJudgementAuthority,
    *,
    company_ref: str,
    limit: int | None,
    now: datetime | None = None,
    mission_version_refs: tuple[str, ...] | None = None,
    newest_first: bool = False,
) -> list[list[dict[str, Any]]]:
    """Oldest eligible events, grouped by one filing, closed HK ISO week or low-tier day.

    A US 10-Q normally contributes three monthly rows. They remain three
    immutable events because the month in which purchases stopped is evidence,
    but the analyst reads their common accession as one table and makes one
    judgement. HK daily rows remain raw while the current Hong Kong week is
    open, then share one company/week judgement slot. One company's low-tier
    inputs (:func:`is_low_tier`) from one UTC day share a slot of at most
    :data:`MAX_LOW_TIER_BATCH`, the Claims drawn from one document
    (:class:`ClaimDocuments`) share one of at most :data:`MAX_DOCUMENT_CLAIMS`,
    and an input repeating a document already in the selection joins that
    document's slot. Other kinds, including Form 4
    insider transactions, retain one slot each.
    """

    mission_sql = ""
    parameters: list[Any] = [company_ref]
    if mission_version_refs is not None:
        if not mission_version_refs:
            return []
        mission_sql = " AND e.mission_version_ref IN (" + ",".join(
            "?" for _ in mission_version_refs) + ")"
        parameters.extend(mission_version_refs)
    # An event the triage screen took out of the backlog is settled without a
    # judgement: it is not offered again (event_triage).
    if triage_table_exists(events.connection):
        mission_sql += screened_out_sql("e")
    rows = events.connection.execute(
        "SELECT e.event_id AS event_id FROM research_events e "
        "LEFT JOIN event_judgements j ON j.event_ref = e.event_id "
        "WHERE e.company_ref = ? AND j.event_ref IS NULL " + mission_sql +
        "ORDER BY e.occurred_at " + ("DESC" if newest_first else "ASC")
        + ", e.event_id " + ("DESC" if newest_first else "ASC"),
        parameters,
    ).fetchall()
    groups: list[list[dict[str, Any]]] = []
    positions: dict[tuple[str, ...], int] = {}
    documents: dict[tuple[str, ...], int] = {}
    moment = now or datetime.now(timezone.utc)
    from .claim_retirement import retired_claim_version_refs

    # An event that *is* a Claim is recorded while the Claim is live; if P10b
    # retires it before the judge gets to it, judging it would put a disowned
    # fact in front of the model (live 2026-09-24: judged 11:40 on a Claim
    # retired 10:49).  It is skipped, not judged: a later revoked retirement
    # (a reinstatement) makes it eligible again: the set is read every time.
    retired = retired_claim_version_refs(events.connection)
    document_of = ClaimDocuments(events.connection)
    for row in rows:
        event = events.event(row["event_id"])
        if retired and retired.intersection(
                ref for ref in event.get("source_refs") or ()
                if isinstance(ref, str)):
            continue
        # Recorded before the tracking scan learned to refuse reference pages:
        # an encyclopedia or profile page is not judged as news either.
        if (event.get("kind") == "news"
                and event.get("evidence_tier") in UNDATED_PAGE_TIERS
                and evergreen_page_reason(
                    host=(event.get("payload") or {}).get("host")) is not None):
            continue
        grouped_key = buyback_group_key(event)
        if not _closed_hk_week(grouped_key, moment):
            continue
        key = event_group_key(event, document_of)
        document = document_key(event)
        if (document is not None and document in documents
                and len(groups[documents[document]]) < MAX_GROUP_INPUTS):
            # The same document found again: one read, whatever slot it is in.
            groups[documents[document]].append(event)
            continue
        cap = _slot_cap(key)
        if key in positions and (cap is None or len(groups[positions[key]]) < cap):
            groups[positions[key]].append(event)
            if document is not None:
                documents[document] = positions[key]
            continue
        if limit is not None and len(groups) >= max(1, int(limit)):
            continue
        positions[key] = len(groups)
        if document is not None:
            documents[document] = len(groups)
        groups.append([event])
    # A result event is led by its release (then its call): the judgement is
    # the release's, the call and the repeats are its group.
    for key, index in positions.items():
        if key[0] == "result_event":
            groups[index] = result_lane.primary_first(groups[index])
    return groups


# -- batch F: result-type events beyond the anti-join ---------------------------


def _is_judged(connection: Any, event_ref: str) -> bool:
    try:
        return connection.execute("SELECT 1 FROM event_judgements WHERE event_ref=?",
                                  (event_ref,)).fetchone() is not None
    except Exception:  # noqa: BLE001 - no ledger yet: nothing is judged
        return False


def job_group_hash(job: dict[str, Any]) -> str:
    """The identity a launcher ticket names: the group, and for a re-judgement its number."""

    group = job["group"]
    if job.get("kind") != "rejudge":
        return incremental_group_hash(group)
    return content_hash({
        "rejudgement": int(job["rejudge"]["sequence"]), "reason": job["rejudge"]["reason"],
        "events": [{"id": event["id"], "content_hash": event.get("content_hash")}
                   for event in sorted(group, key=lambda item: item["id"])],
    })


def result_jobs(
    events: ResearchEventAuthority,
    *,
    company_ref: str,
    state_dir: Path | None,
    now: datetime | None = None,
    mission_version_refs: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """Result events owing work although their release is judged.

    ``rejudge``: the occurrence owes a bounded re-judgement
    (:func:`event_judgement_result.rejudgement_reason` -- the body its last
    judgement lacked has arrived, or a same-event document arrived within 72
    hours; at most two).  ``repeat``: an unjudged re-recording of an occurrence
    already judged, with nothing new -- it is recorded against the latest
    judgement at no cost (an old event processed again dedupes, it is not
    judged twice).
    """

    connection = events.connection
    moment = now or datetime.now(timezone.utc)
    since = (moment.date() - timedelta(days=result_lane.REJUDGE_LOOKBACK_DAYS + 10)).isoformat()
    keyed: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for event in result_lane.result_events(connection, company_ref, since=since):
        if mission_version_refs is not None and event.get("mission_version_ref") not in mission_version_refs:
            continue
        key = result_lane.result_key(connection, event, state_dir=state_dir)
        if key is not None:
            keyed.setdefault(key, []).append(event)
    identity = None
    jobs: list[dict[str, Any]] = []
    for key, members in sorted(keyed.items()):
        members = result_lane.primary_first(members)
        primary = members[0]
        if not _is_judged(connection, primary["id"]):
            continue  # the unjudged path judges the release itself
        if identity is None and state_dir is not None:
            try:
                identity = result_lane._identity(state_dir, company_ref)
            except Exception:  # noqa: BLE001
                identity = {}
        unjudged = [item["id"] for item in members if not _is_judged(connection, item["id"])]
        reason = result_lane.rejudgement_reason(connection, state_dir, primary, members, now=moment,
                                                identity=identity)
        if reason is not None:
            jobs.append({"kind": "rejudge", "key": key, "group": members, "rejudge": reason,
                         "new_refs": unjudged})
        elif unjudged:
            jobs.append({"kind": "repeat", "key": key, "group": members, "primary": primary,
                         "new_refs": unjudged})
    return jobs


def _record_result_repeat(
    judgements: EventJudgementAuthority, event: dict[str, Any], primary: dict[str, Any],
    *, mission: Any, actor_ref: str, reason: str,
) -> dict[str, Any] | None:
    """An unjudged re-recording of a judged result event, bound to its latest judgement at no cost."""

    latest = judgements.latest_judgement_for(primary["id"])
    if latest is None:
        return None
    marker = {"cost_micros": 0, "repeat_of_event_ref": latest["event_ref"],
              "repeat_of_judgement_ref": latest["id"]}
    judgement = {key: latest.get(key) for key in (
        "decision", "action", "driver_refs", "thesis_refs", "because", "citations", "note",
        "research_question", "forecast_change", "result")}
    judgement["model"] = {**(latest.get("model") or {}), **marker}
    verification = {**(latest.get("verifier") or {}),
                    "model": {**(latest.get("verifier_model") or {}), **marker}}
    effect = {"kind": "repeated_result_event", "status": "recorded",
              "primary_event_ref": primary["id"], "judgement_ref": latest["id"], "reason": reason,
              **({"daily_digest": DAILY_DIGEST_MARKER} if latest.get("decision") == "NO_CHANGE" else {})}
    return judgements.record(
        event=event, judgement=judgement, verification=verification, effect=effect,
        mission=mission, actor_ref=actor_ref,
        annotations={"main_event_ref": primary["id"],
                     "event_dates": result_lane.event_dates(event)})


def _material_record(material: dict[str, Any] | None, decided: dict[str, Any]) -> dict[str, Any] | None:
    """What a judgement stores about what it read (the full pack text stays in the run directory)."""

    if not material:
        return None
    cited: set[str] = set()
    result = decided.get("result") or {}
    for row in result.get("actual_vs_expected") or ():
        cited.update(row.get("sources") or ())
        for cell in (row.get("actual"), row.get("expected"), row.get("prior_guidance")):
            cited.update(result_lane._tags_in(cell))
    reaction = result.get("market_reaction") or {}
    cited.update(result_lane._tags_in(reaction.get("move"), reaction.get("benchmark")))
    cited.update(ref.strip("[] ") for ref in decided.get("citations") or ())
    texts = material.get("tag_texts") or {}
    return {
        "contract": material.get("contract"), "kind": material.get("kind"), "anchor": material.get("anchor"),
        "dates": material.get("dates"), "body_read": bool(material.get("body_read")),
        "body_document_ref": material.get("body_document_ref"),
        "documents_read": list(material.get("documents_read") or ()),
        "pack": material.get("pack"), "tags": dict(material.get("tags") or {}),
        "tag_texts": {tag: str(texts[tag])[:1500] for tag in sorted(cited) if tag in texts},
        "tag_sources": {tag: label for tag, label in (material.get("tag_sources") or {}).items() if tag in cited},
        "main_event_ref": material.get("main_event_ref"),
        "group_event_refs": list(material.get("group_event_refs") or ()),
        "catalyst_entry_ref": material.get("catalyst_entry_ref"),
        "catalyst_entry": material.get("catalyst_entry"),
        "release_source": material.get("release_source"),
        "prior_guidance_source": material.get("prior_guidance_source"),
        "baseline_as_of": material.get("baseline_as_of"),
        "consensus_version_ref": material.get("consensus_version_ref"),
        "applicable_rows": list(material.get("applicable_rows") or ()),
    }


def _prompt_sink(summary_dir: Path, event_ref: str, files: list[str]) -> Callable[[str, str], None]:
    """Write each prompt sent into the run directory (owner-only), for audit."""

    safe = "".join(char if char.isalnum() or char in "-_" else "_" for char in event_ref)[-80:]

    def sink(role: str, prompt: str) -> None:
        folder = summary_dir / "prompts"
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = folder / f"{safe}.{role}.txt"
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=folder)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(prompt)
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        files.append(str(path.relative_to(summary_dir)))

    return sink


def _record_aliases(
    judgements: EventJudgementAuthority, aliases: list[dict[str, Any]], event: dict[str, Any],
    decided: dict[str, Any], checked: dict[str, Any], *, mission: Any, actor_ref: str,
    rejudgement_ref: str | None,
) -> None:
    """The group's other inputs, each recorded as judged with its main event (linked by id)."""

    for grouped_event in aliases:
        alias_judgement = dict(decided)
        alias_judgement["model"] = {
            **(decided.get("model") or {}), "cost_micros": 0,
            "grouped_under_event_ref": event["id"],
        }
        alias_verification = dict(checked)
        alias_verification["model"] = {
            **(checked.get("model") or {}), "cost_micros": 0,
            "grouped_under_event_ref": event["id"],
        }
        judgements.record(
            event=grouped_event, judgement=alias_judgement,
            verification=alias_verification,
            effect={"kind": "grouped_judgement", "status": "recorded",
                    "primary_event_ref": event["id"],
                    **({"rejudgement_ref": rejudgement_ref} if rejudgement_ref else {}),
                    # A grouped input is an input examined that day;
                    # the digest lists it beside its primary.
                    **({"daily_digest": DAILY_DIGEST_MARKER}
                       if decided["decision"] == "NO_CHANGE" else {})},
            mission=mission, actor_ref=actor_ref,
            annotations={"main_event_ref": event["id"],
                         "event_dates": result_lane.event_dates(grouped_event)},
        )


def _build_material(state_dir: Path, connection: Any, event: dict[str, Any], group: list[dict[str, Any]], *,
                    mission: Any, day: str, pack_builder: Any, budget_chars: int) -> dict[str, Any]:
    material = result_lane.build_result_material(
        state_dir, connection, event, group=group, mission=mission, today=day,
        pack_builder=pack_builder, budget_chars=budget_chars)
    anchor = material.get("anchor") or {}
    if anchor.get("day"):
        try:
            from .catalyst_calendar import entry_for_event

            entry = entry_for_event(connection, str(event["company_ref"]), anchor["day"])
        except Exception:  # noqa: BLE001 - no calendar is no link
            entry = None
        if entry is not None:
            material["catalyst_entry_ref"] = entry["entry_ref"]
            material["catalyst_entry"] = entry
    return material


def _material_opener(state_dir: Path) -> Callable[[Any], dict[str, Any]]:
    def opener(spec: Any) -> dict[str, Any]:
        return result_lane.open_material(state_dir, spec)

    return opener


def config_fingerprint(*paths: Path | None) -> str:
    """A short hash of the model configurations this run is using.

    Folded into every ``request_id`` because a cockpit WorkOrder is content
    addressed on (purpose, request_id, mission version, prompt) and *not* on
    the configuration that answered it. Without this, a run made under a
    misconfigured verifier poisons every event it touched: the configuration
    is fixed, the request is identical, and the scheduler replays the old
    result -- including its old route -- for ever. With it, fixing the
    configuration is a different request and the events become retryable.
    """

    material = []
    for path in paths:
        if path is None:
            material.append(None)
            continue
        try:
            material.append(Path(path).expanduser().read_text(encoding="utf-8"))
        except OSError:
            material.append(str(path))
    return content_hash({"configs": material})[:8]


def same_routing_policy(judge: Path | None, verifier: Path | None) -> str | None:
    """The cheap pre-check: two configurations that route the same way.

    Catches the misconfiguration that actually happens -- somebody copies the
    judge's file to the verifier's name -- before a single call is paid for.
    It is not the guarantee; the guarantee is the family check on the route
    the broker took. It is the difference between finding out for nothing and
    finding out for the price of a pair of calls.
    """

    if judge is None or verifier is None:
        return None
    try:
        left = json.loads(Path(judge).expanduser().read_text(encoding="utf-8"))
        right = json.loads(Path(verifier).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if left == right:
        return "the judge and verifier configurations are the same file"
    if left.get("routing_policy_ref") == right.get("routing_policy_ref"):
        return (
            "the judge and verifier configurations name the same routing policy "
            f"({left.get('routing_policy_ref')}), so both calls select the same model"
        )
    return None


def _model(config_path: Path | None, state_dir: Path, scheduler_db: Path | None) -> Any:
    if config_path is None:
        return None
    return EventCockpitModel(
        json.loads(Path(config_path).expanduser().read_text(encoding="utf-8")),
        scheduler_db=str(scheduler_db or (state_dir / "scheduler.sqlite")),
        max_input_tokens=MAX_INPUT_TOKENS, max_output_tokens=MAX_OUTPUT_TOKENS,
        max_cost_usd=MAX_COST_USD, timeout_seconds=TIMEOUT_SECONDS,
    )


def _budget_db(config_path: Path | None, model: Any) -> str | None:
    """The day ledger the judge's calls are admitted into, if one is configured."""

    config = getattr(model, "config", None)
    if config is None and config_path is not None:
        try:
            config = json.loads(Path(config_path).expanduser().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            config = None
    value = (config or {}).get("budget_db") if isinstance(config, dict) else None
    return value if isinstance(value, str) and value else None


def _call_budget(model: Any, purpose: str) -> dict[str, Any]:
    resolver = getattr(model, "budget_for", None)
    if callable(resolver):
        return resolver(purpose)
    return {
        "max_input_tokens": MAX_INPUT_TOKENS,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "max_cost_usd": MAX_COST_USD,
        "timeout_seconds": TIMEOUT_SECONDS,
    }


def _event_run_budget(config_path: Path | None, model: Any) -> dict[str, Any]:
    config = getattr(model, "config", None)
    if config is None and config_path is not None:
        config = json.loads(Path(config_path).expanduser().read_text(encoding="utf-8"))
    return resolve_run_budget(
        config or {}, PURPOSE,
        defaults={
            "max_events": MAX_EVENTS_PER_RUN,
            "max_events_per_company": MAX_EVENTS_PER_COMPANY,
        },
    )


def research_admitter_for(store: DaltonStore, mission: Any):
    """P14e's admission entry point, by name, or nothing.

    Called by name because P14e is a branch and not yet on main: if the module
    is absent the ``research`` decision is queued with a reason rather than
    silently dropped, and when the branch merges this begins working with no
    change here.
    """

    try:
        from .bounded_planner_loop import BoundedPlannerAuthority
        from .research_task import admit_inquiry, plan_admissions
    except ImportError:
        return None

    def admit(event: Any, judgement: Any) -> dict[str, Any]:
        authority = BoundedPlannerAuthority(store)
        inquiry = {
            "rank": 1,
            "question": judgement["research_question"],
            "wants": judgement["because"],
            "company_ref": event["company_ref"],
        }
        plan = {"inquiries": [inquiry]}
        entries = plan_admissions(authority, mission=mission, plan=plan)
        entry = entries[0] if entries else None
        if entry is None or not entry.get("admissible"):
            return {"status": "queued",
                    "reason": (entry or {}).get("reason") or "no admissible inquiry"}
        admitted = admit_inquiry(
            authority, _backlog(store), mission=mission,
            plan_ref=event["id"], inquiry=inquiry, entry=entry,
        )
        return {"status": "admitted", "loop_ref": admitted.get("loop_ref"),
                "inquiry_ref": admitted.get("inquiry_ref")}

    return admit


def _backlog(store: DaltonStore) -> Any:
    from .research_question_backlog import ResearchQuestionBacklog

    return ResearchQuestionBacklog(store)


def _triage(
    store: DaltonStore, *, event_group: list[dict[str, Any]], context: dict[str, Any],
    model: Any, mission: Any, actor_ref: str, request_id: str, day: str,
    judgements: EventJudgementAuthority, summary: dict[str, Any],
) -> tuple[str, int]:
    """Screen one group; ``(what happens next, micros booked)``.

    ``judge``, ``skip`` (screened out) or ``stop`` (the pool refused the
    screen itself).  It writes its own triage rows and the call's spend and
    has no path to a judgement.  A group already escalated (a judgement
    refused and retried, a replayed run) goes straight back to the judge
    without a second screen.  A screen that failed is not a decision and
    writes no row: the group goes to the judge this time (fail open) and is
    screened again if it comes back.
    """

    tally = summary["triage"]
    prior = [row for row in (triage_for(store.connection, item["id"])
                             for item in event_group) if row is not None]
    escalated = [row for row in prior if row["outcome"] == "escalated"]
    if escalated:
        record_triage(
            store, event_group=event_group,
            decision={key: escalated[0][key] for key in ("outcome", "route", "verdict")},
            verdict=None, mission=mission, actor_ref=actor_ref,
            inherited_from=escalated[0]["id"])
        tally["routes"]["prior"] = tally["routes"].get("prior", 0) + 1
        tally["escalated"] += 1
        return "judge", 0
    verdict = (None if event_group[0].get("kind") in TRIAGE_BYPASS_KINDS
               else triage_screen(context, model=model, mission=mission,
                                  request_id=request_id))
    if verdict is not None and verdict.get("failure_trace") is not None:
        summary["failed_model_traces"].append(verdict["failure_trace"])
    cost = judgements.record_spend(
        call=(verdict or {}).get("model"), purpose=TRIAGE_PURPOSE,
        outcome=f"triage:{(verdict or {}).get('status') or 'bypass'}",
        event_ref=event_group[0]["id"], mission=mission, day=day,
    )
    tally["cost_micros"] += cost
    if verdict is not None and verdict.get("lane_status") == POOL_EXHAUSTED_STATUS:
        return "stop", cost
    decision = triage_gate(event_group=event_group, verdict=verdict)
    if decision["route"] == "triage_failed":
        tally.setdefault("failures", []).append(
            {"event_ref": event_group[0]["id"], "reason": str(verdict.get("reason"))[:300]})
    else:
        record_triage(store, event_group=event_group, decision=decision, verdict=verdict,
                      mission=mission, actor_ref=actor_ref)
    route = f"{decision['outcome']}:{decision['route']}"
    tally["routes"][route] = tally["routes"].get(route, 0) + 1
    if decision["outcome"] == "screened_out":
        tally["screened_out"] += 1
        summary["effects"].append({
            "event_ref": event_group[0]["id"], "kind": "triage", "status": "screened_out",
            "event_refs": [item["id"] for item in event_group],
            "reason": (verdict or {}).get("reason"),
        })
        return "skip", cost
    tally["escalated"] += 1
    return "judge", cost


def run_judgement(
    *,
    state_dir: Path,
    summary_dir: Path,
    judge_model_config: Path | None = None,
    verifier_model_config: Path | None = None,
    policy_path: Path | None = None,
    scheduler_db: Path | None = None,
    company_ref: str | None = None,
    event_refs: tuple[str, ...] | None = None,
    event_group_hash: str | None = None,
    max_events: int | None = None,
    per_company: int | None = None,
    dry_run: bool = False,
    judge_model: Any = None,
    verifier_model: Any = None,
    family_resolver: Any = None,
    now: datetime | None = None,
    budget_db: str | Path | None = None,
    triage_model: Any = None,
    pack_builder: Any = None,
) -> dict[str, Any]:
    """Judge one bounded batch.

    ``triage_model`` is the cheap screen (``event_triage``).  Built from the
    judge's own configuration -- same routing policy, the cheap tier -- when
    the judge is; a caller that hands in its own judge and no screen runs
    without one, exactly as before this stage existed.
    """

    state_dir = Path(state_dir).expanduser().resolve()
    summary_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    summary: dict[str, Any] = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "created_at": moment.isoformat(timespec="microseconds"),
        "status": "failed",
        "mode": "dry_run" if dry_run else "judge",
        "judgement_status": None,
        "ungranted": [],
        "pool": None,
        "candidates": 0,
        "judged": 0,
        "refused": 0,
        "decisions": {},
        "actions": {},
        "config_fingerprint": None,
        "reflections": 0,
        "reflections_refused": 0,
        "repeats": 0,
        "triage": {"mode": "off", "screened_out": 0, "escalated": 0,
                   "routes": {}, "cost_micros": 0},
        "followups": [],
        "effects": [],
        "failed_model_traces": [],
        "failure_reason": None,
        "cost_micros": 0,
        "formal_authority_writes": 0,
    }
    store = DaltonStore(str(state_dir / "core.sqlite"))
    try:
        missions = CoverageMissionAuthority(store)
        pointer = store.connection.execute(
            "SELECT mission_version_id FROM coverage_mission_pointer "
            "ORDER BY mission_ref LIMIT 1"
        ).fetchone()
        if pointer is None:
            summary.update({"status": "idle", "judgement_status": "no_mission"})
            return summary
        mission = missions.mission(pointer["mission_version_id"])
        summary["ungranted"] = missing_write_scopes(mission)
        if "deliverable" in summary["ungranted"]:
            summary.update({
                "status": "idle", "judgement_status": "ungranted",
                "failure_reason": "the mission does not grant deliverable writes; "
                                  "a judgement that can produce no note is not worth paying for",
            })
            return summary
        events = ResearchEventAuthority(store)
        judgements = EventJudgementAuthority(store)
        tracked = screen_passed_companies(missions, mission)
        if company_ref is not None:
            tracked = [ref for ref in tracked if ref == company_ref]
        # ADR-0010 backfill, on every child run before anything is paid for:
        # a judgement other than NO_CHANGE that has no governed follow-up yet
        # (written before this code, or its follow-up failed after the
        # judgement row) goes through the same call the forward path uses.
        # No model call, keyed writes, and never the reason a tick fails.
        summary["followup_sweep"] = _followup_sweep(
            store, judgements, mission, apply=not dry_run)
        day = moment.date().isoformat()
        budget_db = budget_db or _budget_db(judge_model_config, judge_model)
        state = pool_state(
            judgements, mission, day=day, budget_db=budget_db,
            scheduler_db=getattr(judge_model, "scheduler_db", None))
        summary["pool"] = state

        run_budget = _event_run_budget(judge_model_config, judge_model)
        effective_max_events = (
            max_events if max_events is not None else int(run_budget["max_events"])
        )
        effective_per_company = (
            per_company
            if per_company is not None
            else int(run_budget["max_events_per_company"])
        )

        allowed_versions = tuple(
            row["mission_version_id"] for row in store.connection.execute(
                "SELECT mission_version_id FROM coverage_mission_versions "
                "WHERE mission_ref=?", (mission["mission_ref"],)
            ).fetchall())
        batch: list[dict[str, Any]] = []
        if event_refs is not None:
            expected_refs = set(event_refs)
            current = unjudged_event_groups(
                events, judgements, company_ref=company_ref or "", limit=None,
                mission_version_refs=allowed_versions, newest_first=True, now=moment)
            matching = [group for group in current
                        if expected_refs.intersection(item["id"] for item in group)]
            target = matching[0] if len(matching) == 1 else []
            if target:
                judged_refs = {
                    row["event_ref"] for row in events.connection.execute(
                        "SELECT event_ref FROM event_judgements"
                    ).fetchall()
                }
                valid = (
                    len({item["id"] for item in target}) == len(target)
                    and {item["id"] for item in target} == expected_refs
                    and company_ref in tracked
                    and all(item["company_ref"] == company_ref for item in target)
                    and all(item.get("mission_version_ref") in allowed_versions
                            for item in target)
                    and _coherent_group(target, ClaimDocuments(events.connection))
                    and _closed_hk_week(buyback_group_key(target[0]), moment)
                    and incremental_group_hash(target) == event_group_hash
                    and all(item["id"] not in judged_refs for item in target)
                )
                if valid:
                    batch = [{"kind": "judge", "group": target}]
            if not batch and company_ref in tracked:
                # Batch F: a ticket may name a result event's bounded re-judgement.
                for job in result_jobs(events, company_ref=company_ref or "", state_dir=state_dir,
                                       now=moment, mission_version_refs=allowed_versions):
                    if ({item["id"] for item in job["group"]} == expected_refs
                            and job_group_hash(job) == event_group_hash):
                        batch = [job]
                        break
        else:
            for ref in tracked:
                owed = result_jobs(events, company_ref=ref, state_dir=state_dir, now=moment,
                                   mission_version_refs=allowed_versions)
                covered = {item for job in owed for item in job.get("new_refs") or ()}
                for group in unjudged_event_groups(
                        events, judgements, company_ref=ref,
                        limit=effective_per_company,
                        mission_version_refs=allowed_versions, now=moment,
                ):
                    if covered.intersection(item["id"] for item in group):
                        continue
                    batch.append({"kind": "judge", "group": group})
                batch.extend(owed)
        batch = batch[:effective_max_events]
        summary["candidates"] = len(batch)
        if not batch:
            summary.update({"status": "idle", "judgement_status": "nothing_unjudged"})
            return summary
        if dry_run:
            summary.update({"status": "succeeded", "judgement_status": "dry_run"})
            summary["rejudgements_owed"] = [
                {"event_ref": job["group"][0]["id"], **job["rejudge"]} for job in batch
                if job["kind"] == "rejudge"]
            return summary

        if judge_model is None:
            judge_model = _model(judge_model_config, state_dir, scheduler_db)
            # The screen routes through the judge's configuration under its
            # own purpose, which the pinned policy maps to the cheap tier.
            if triage_model is None:
                triage_model = judge_model
        if triage_model is not None:
            summary["triage"]["mode"] = "screen"
        verifier_model = verifier_model or _model(
            verifier_model_config, state_dir, scheduler_db
        )
        if judge_model is None or verifier_model is None:
            summary.update({
                "status": "idle", "judgement_status": "gated",
                "failure_reason": "the judgement lane needs a judge and an independent "
                                  "verifier configuration; it will not run on one",
            })
            return summary
        # Before anything is paid for. The route-level family check is the
        # guarantee, but it costs a pair of calls to reach; this costs a file
        # read and catches the misconfiguration that actually happens.
        shared = same_routing_policy(judge_model_config, verifier_model_config)
        if shared is not None:
            summary.update({
                "status": "idle", "judgement_status": "gated:same_family",
                "failure_reason": shared,
            })
            return summary
        fingerprint = config_fingerprint(judge_model_config, verifier_model_config)
        summary["config_fingerprint"] = fingerprint
        if family_resolver is None:
            config = json.loads(
                Path(judge_model_config).expanduser().read_text(encoding="utf-8")
            ) if judge_model_config else {}
            family_resolver = route_family_resolver(config.get("model_router_db"))

        policy = load_policy(policy_path) if policy_path else None
        source_projection = build_map(
            mission=mission,
            cadences=None if policy is None else policy["cadences"],
        )
        table = prompt_table(source_projection)
        deliverables = MissionDeliverableAuthority(store)
        playbook = _playbook(store, mission)
        forecast_models, model_versions = _forecast_models(store, tracked)
        admitter = research_admitter_for(store, mission)
        actor = mission["autonomy"]["automation_principal"]
        # W4: the price authority, read once per run rather than per event.
        # The buyback context compares what a company paid for its own shares
        # with what they cost now, and "what they cost now" is one row per
        # company however many events are in the batch. A Core with no price
        # series gets an empty map and the context block says so.
        prices, market_caps = _price_reads(store, tracked)

        if state.get("ledger_error"):
            summary.update({
                "status": "idle", "judgement_status": "gated:ledger_unreadable",
                "failure_reason": "the day ledger this pool is read from cannot be "
                                  f"read: {state['ledger_error']}",
            })
            return summary
        spent = 0
        claim_documents = ClaimDocuments(store.connection)
        # Four calls, not two: a divergence or a revise-shaped decision owes a
        # reflection and its verification as well. Reserving the pair only
        # would admit an event whose reflection then has nothing left to spend,
        # which is the shape where a decision is recorded without the account
        # of what we may have missed -- exactly the half the owner asked for.
        reservation = int(sum(
            Decimal(str(_call_budget(model, purpose)["max_cost_usd"]))
            for model, purpose in (
                (judge_model, PURPOSE),
                (verifier_model, VERIFIER_PURPOSE),
                (judge_model, REFLECTION_PURPOSE),
                (verifier_model, REFLECTION_VERIFIER_PURPOSE),
            )
        ) * 1_000_000)
        # The screen is paid for before any of those four, so an event that
        # cannot afford its screen and the judge behind it does not start.
        if triage_model is not None:
            reservation += int(Decimal(str(
                _call_budget(triage_model, TRIAGE_PURPOSE)["max_cost_usd"])) * 1_000_000)
        opener = _material_opener(state_dir)
        summary.setdefault("rejudged", 0)
        summary.setdefault("evidence_insufficient", 0)
        summary.setdefault("prompt_files", [])
        for job in batch:
            event_group = list(job["group"])
            rejudge = job.get("rejudge") if job["kind"] == "rejudge" else None
            if job["kind"] == "repeat":
                # An old result event processed again, nothing new: recorded
                # against the latest judgement of its occurrence, at no cost.
                for item in event_group:
                    if item["id"] in set(job.get("new_refs") or ()):
                        if _record_result_repeat(judgements, item, job["primary"], mission=mission,
                                                 actor_ref=actor,
                                                 reason="same result event already judged; no new material "
                                                        "or the re-judgement bound is reached") is not None:
                            summary["repeats"] += 1
                continue
            if budget_db is not None:
                # The ledger already holds every admission this run made, paid
                # or still open, and every one it never heard back from; re-read
                # it rather than adding what the calls reported.
                state = pool_state(
                    judgements, mission, day=day, budget_db=budget_db,
                    scheduler_db=getattr(judge_model, "scheduler_db", None))
                summary["pool"] = state
                remaining = state["remaining_micros"]
            else:
                remaining = state["remaining_micros"] - spent
            if rejudge is None:
                # A document already judged, and only ever judged NO_CHANGE, is not
                # paid for again: the member is recorded against the earlier
                # judgement at no cost and the rest of the group goes on.
                repeated = prior_no_change_judgements(store.connection, event_group, state_dir=state_dir)
                for item in event_group:
                    if item["id"] in repeated:
                        _record_repeat(judgements, item, repeated[item["id"]],
                                       mission=mission, actor_ref=actor)
                        summary["repeats"] += 1
                event_group = [item for item in event_group if item["id"] not in repeated]
            if not event_group:
                continue
            event = event_group[0]
            context = build_context(
                event=event, mission=mission, connection=store.connection,
                judgements=judgements,
                model_version=model_versions.get(event["company_ref"]),
                source_table=table,
                recent_events=events.events(company_ref=event["company_ref"], limit=40),
                source_keys=() if policy is None else sorted(policy["cadences"]),
                price=prices.get(event["company_ref"]),
                market_cap=market_caps.get(event["company_ref"]),
            )
            evidence_group = group_evidence_events(events, event_group)
            context["grouped_events"] = evidence_group
            context["grouped_by_document"] = len(evidence_group) > 1 and (
                event_group_key(event, claim_documents)[0] == "claim_document")
            # Batch F: the event's body, and for a result event the same-event
            # pack, the expectations baseline and the reaction.
            material = None
            if result_lane.is_result_event(event) or result_lane.is_document_event(event):
                material = _build_material(state_dir, store.connection, event, evidence_group,
                                           mission=mission, day=day, pack_builder=pack_builder,
                                           budget_chars=result_lane.PACK_BUDGET_CHARS)
                material["group_readable"] = result_lane.group_readable(state_dir, evidence_group[1:])
                context["result"] = material
            aliases = [item for item in event_group[1:] if not _is_judged(store.connection, item["id"])]
            if (material is not None and not material.get("body_read")
                    and result_lane.body_required(event) and rejudge is None
                    and not material.get("group_readable")):
                # 证据不足，安排读取: no model is paid to restate that the body
                # was not read; the request goes to the acquisition service and
                # the re-judgement waits for the material.
                decided = result_lane.gap_judgement(event, material)
                checked = {"status": "not_required", "verdict": "not_required",
                           "reason": "确定性判定：事件正文未读，只能「证据不足，安排读取」"}
                effect = {"kind": "research", "status": "material_requested",
                          **open_research_material(event, decided, context, opener),
                          **({"daily_digest": DAILY_DIGEST_MARKER})}
                written = judgements.record(
                    event=event, judgement=decided, verification=checked, effect=effect,
                    mission=mission, actor_ref=actor,
                    annotations={"result_material": _material_record(material, decided),
                                 "event_dates": material.get("dates"),
                                 "main_event_ref": event["id"]})
                _record_aliases(judgements, aliases, event, decided, checked, mission=mission,
                                actor_ref=actor, rejudgement_ref=None)
                summary["evidence_insufficient"] += 1
                summary["judged"] += 1
                summary["decisions"]["NO_CHANGE"] = summary["decisions"].get("NO_CHANGE", 0) + 1
                summary["actions"]["research"] = summary["actions"].get("research", 0) + 1
                summary["effects"].append({"event_ref": event["id"], "judgement_ref": written["id"], **effect})
                continue
            if remaining < reservation:
                summary["judgement_status"] = "skipped:pool_exhausted"
                break
            group_hash = (job_group_hash({**job, "group": evidence_group}) if rejudge
                          else incremental_group_hash(evidence_group))
            request_id = f"{fingerprint}{group_hash[:24]}"
            skip_triage = rejudge is not None or (material or {}).get("kind") == "result"
            if triage_model is not None and not skip_triage:
                passed, cost = _triage(
                    store, event_group=event_group, context=context,
                    model=triage_model, mission=mission, actor_ref=actor,
                    request_id=f"t{request_id}"[:31], day=day,
                    judgements=judgements, summary=summary,
                )
                spent += cost
                if passed == "stop":
                    summary["judgement_status"] = POOL_EXHAUSTED_STATUS
                    break
                if passed == "skip":
                    continue
            sink = _prompt_sink(summary_dir, event["id"] + (f"-r{rejudge['sequence']}" if rejudge else ""),
                                summary["prompt_files"])
            decided = judge(
                context, model=judge_model, mission=mission, request_id=request_id,
                prompt_sink=sink,
            )
            for smaller in (15_000, 8_000):
                # The pack is cut, not the contract: a prompt over the input
                # bound is rebuilt on a smaller pack before anything is paid.
                if (material is None or decided.get("status") != "refused" or decided.get("model")
                        or "exceeds the model input bound" not in str(decided.get("reason"))):
                    break
                readable = material.get("group_readable")
                material = _build_material(state_dir, store.connection, event, evidence_group,
                                           mission=mission, day=day, pack_builder=pack_builder,
                                           budget_chars=smaller)
                material["group_readable"] = readable
                context["result"] = material
                decided = judge(context, model=judge_model, mission=mission, request_id=request_id,
                                prompt_sink=sink)
            checked = verify(
                context, decided, model=verifier_model, mission=mission,
                request_id=f"v{request_id}"[:31], family_resolver=family_resolver,
                prompt_sink=sink,
            )
            for outcome in (decided, checked):
                if outcome.get("failure_trace") is not None:
                    summary["failed_model_traces"].append(outcome["failure_trace"])
            spent += judgements.record_spend(
                call=decided.get("model"), purpose="event_judgement",
                outcome=decided["status"], event_ref=event["id"], mission=mission, day=day,
            )
            spent += judgements.record_spend(
                call=checked.get("model"), purpose="event_judgement",
                outcome=f"verify:{checked['status']}", event_ref=event["id"],
                mission=mission, day=day,
            )
            if decided["status"] != "judged" or checked.get("verdict") != "pass":
                summary["refused"] += 1
                summary["effects"].append({
                    "event_ref": event["id"], "status": "refused",
                    "reason": decided.get("reason") or checked.get("reason")
                    or "the verifier rejected the decision",
                    "findings": checked.get("findings") or [],
                })
                # A verifier that shares the producer's family will do so for
                # every event in the batch. Stopping here costs one pair
                # instead of eight, and the run says which half is wrong
                # rather than reporting eight refusals nobody can act on.
                if "model_family_not_independent" in str(checked.get("reason") or ""):
                    summary["judgement_status"] = "gated:same_family"
                    break
                # C2: the same reasoning for a spent pool. This lane keeps its
                # own book of what it spent and gates on it above; the day
                # ledger's event_response pool is the same money seen from the
                # other side, and it can refuse first -- a hand-run or a
                # replayed call moves one book and not the other. Whichever
                # gate says stop, the word is the same one.
                if POOL_EXHAUSTED_STATUS in {
                    decided.get("lane_status"), checked.get("lane_status")
                }:
                    summary["judgement_status"] = POOL_EXHAUSTED_STATUS
                    break
                continue
            effect = apply_effect(
                event=event, judgement=decided, context=context, mission=mission,
                playbook=playbook, deliverables=deliverables,
                forecast_models=forecast_models,
                model_version=model_versions.get(event["company_ref"]),
                research_admitter=admitter, actor_ref=actor,
                material_opener=opener,
            )
            written = judgements.record(
                event=event, judgement=decided, verification=checked,
                effect=effect, mission=mission, actor_ref=actor,
                annotations={"event_dates": result_lane.event_dates(event)} if material is None else {
                    "result_material": _material_record(material, decided),
                    "event_dates": material.get("dates"), "main_event_ref": event["id"]},
                rejudgement=rejudge,
            )
            if rejudge is not None:
                summary["rejudged"] += 1
            _record_aliases(judgements, aliases, event, decided, checked, mission=mission,
                            actor_ref=actor, rejudgement_ref=written["id"] if rejudge else None)
            # The owner's third instruction: when we changed our mind, or when
            # the price kept running against us, write down what we expected
            # and what we may have missed -- including when the decision was to
            # hold. The reflection changes nothing; it is attached to the
            # candidate so the person deciding sees both.
            reflection_ref = None
            if reflection_is_owed(event, decided):
                thought = reflect(context, decided, model=judge_model,
                                  mission=mission, request_id=f"f{request_id}"[:31])
                reviewed = verify_reflection(
                    context, thought, model=verifier_model, mission=mission,
                    request_id=f"fv{request_id}"[:30], family_resolver=family_resolver,
                )
                for outcome in (thought, reviewed):
                    if outcome.get("failure_trace") is not None:
                        summary["failed_model_traces"].append(outcome["failure_trace"])
                spent += judgements.record_spend(
                    call=thought.get("model"), purpose="thesis_reflection",
                    outcome=thought["status"], event_ref=event["id"], mission=mission,
                    day=day,
                )
                spent += judgements.record_spend(
                    call=reviewed.get("model"), purpose="thesis_reflection",
                    outcome=f"verify:{reviewed['status']}", event_ref=event["id"],
                    mission=mission, day=day,
                )
                if thought["status"] == "reflected" and reviewed.get("verdict") == "pass":
                    recorded = judgements.record_reflection(
                        judgement=written, event=event, reflection=thought,
                        verification=reviewed, mission=mission, actor_ref=actor,
                    )
                    reflection_ref = recorded["id"]
                    summary["reflections"] += 1
                    summary["followups"].append({
                        "reflection_ref": reflection_ref,
                        "tracking": recorded["followup_tracking"],
                        "research": recorded["followup_research"],
                    })
                else:
                    summary["reflections_refused"] += 1
                    summary["effects"].append({
                        "event_ref": event["id"], "kind": "reflection",
                        "status": "refused",
                        "reason": thought.get("reason") or reviewed.get("reason")
                        or "the verifier rejected the reflection",
                    })
            # ADR-0010: every decision other than NO_CHANGE leaves a governed
            # follow-up -- a candidate against each named current thesis, a
            # forecast proposal when the model did not move, or a labelled
            # notice for the owner. Before this, only ``revise_thesis`` could
            # reach a candidate and a THESIS_WEAKENED ``note`` or ``research``
            # left nothing a person would ever be asked about.
            effect = dict(effect)
            effect.pop("theses", None)
            try:
                followup = record_judgement_followups(
                    judgements, judgement_ref=written["id"], event_ref=event["id"],
                    company_ref=event["company_ref"], judgement=decided, effect=effect,
                    mission=mission, actor_ref=actor,
                    model_version_ref=context.get("model_version_ref"),
                    reflection_ref=reflection_ref,
                )
            except EventJudgementError as exc:
                # The judgement is already recorded; the sweep at the top of
                # the next tick retries the follow-up through this same call.
                followup = {"status": "failed", "candidates": [], "proposal_ref": None,
                            "notice_ref": None, "reason": f"{type(exc).__name__}: {exc}"}
            if followup["candidates"]:
                effect["candidates"] = followup["candidates"]
            if followup["proposal_ref"] is not None:
                effect["proposal_ref"] = followup["proposal_ref"]
            if followup["status"] != "not_owed":
                effect["followup"] = followup
            summary["judged"] += 1
            summary["decisions"][decided["decision"]] = (
                summary["decisions"].get(decided["decision"], 0) + 1
            )
            summary["actions"][decided["action"]] = (
                summary["actions"].get(decided["action"], 0) + 1
            )
            summary["effects"].append({
                "event_ref": event["id"], "judgement_ref": written["id"], **effect,
            })
        summary["cost_micros"] = spent
        summary["formal_authority_writes"] = summary["judged"] + summary["repeats"]
        if summary["judged"] == 0 and summary["refused"]:
            reasons = [str(item.get("reason") or "judgement contract refused")
                       for item in summary["effects"]
                       if item.get("status") == "refused"][:3]
            summary["status"] = "failed"
            summary["judgement_status"] = summary["judgement_status"] or "refused"
            summary["failure_reason"] = (
                "all attempted event judgements were refused: " + "; ".join(reasons)
            )[:500]
        else:
            summary["status"] = "succeeded"
            summary["judgement_status"] = summary["judgement_status"] or (
                "partial" if summary["refused"] else
                "screened_out" if (summary["judged"] == 0 and not summary["repeats"]
                                   and summary["triage"]["screened_out"]) else
                "judged"
            )
        return summary
    except EventJudgementError as exc:
        summary["failure_reason"] = f"{type(exc).__name__}: {exc}"
        return summary
    except Exception as exc:  # unexpected: record for the parent, then surface
        summary["failure_reason"] = f"unexpected {type(exc).__name__}: {exc}"
        raise
    finally:
        _write_owner_only(summary_dir / "summary.json", summary)
        store.close()


def _latest_model_ref(store: DaltonStore) -> Callable[[str], str | None]:
    from .model_forecast_driver import ForecastModelAuthority

    authority = ForecastModelAuthority(store)

    def latest(company_ref: str) -> str | None:
        version = authority.latest(company_ref)
        return None if not version else version.get("id")

    return latest


def _followup_sweep(
    store: DaltonStore, judgements: EventJudgementAuthority, mission: Any, *, apply: bool,
) -> dict[str, Any]:
    try:
        report = sweep_judgement_followups(
            judgements, mission, actor_ref=mission["autonomy"]["automation_principal"],
            model_version_for=_latest_model_ref(store), apply=apply,
        )
    except Exception as exc:  # noqa: BLE001 - the sweep never takes the tick down
        return {"status": "failed", "reason": f"{type(exc).__name__}: {exc}"}
    return {"status": "ok", **report}


def run_followup_backfill(
    *, state_dir: str | Path, apply: bool = False,
) -> dict[str, Any]:
    """Owner entry point: the same sweep, alone, dry-run unless ``apply``.

    The dry run opens ``core.sqlite`` read-only (``mode=ro``) and writes
    nothing, not even a schema; ``apply`` goes through ``DaltonStore`` and
    the same keyed writes the judgement child makes.
    """

    core = Path(state_dir).expanduser().resolve() / "core.sqlite"
    if not apply:
        import sqlite3

        connection = sqlite3.connect(f"file:{core}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            reader = EventJudgementAuthority.reader(connection)

            def latest(company_ref: str) -> str | None:
                row = connection.execute(
                    "SELECT version_id FROM forecast_model_versions WHERE company_ref=? "
                    "ORDER BY version_number DESC LIMIT 1", (company_ref,)).fetchone()
                return None if row is None else row["version_id"]

            report = sweep_judgement_followups(
                reader, {}, actor_ref="dry-run", model_version_for=latest, apply=False)
        finally:
            connection.close()
        return {"status": "ok", **report}
    store = DaltonStore(str(core))
    try:
        pointer = store.connection.execute(
            "SELECT mission_version_id FROM coverage_mission_pointer "
            "ORDER BY mission_ref LIMIT 1"
        ).fetchone()
        if pointer is None:
            return {"status": "idle", "reason": "no mission"}
        mission = CoverageMissionAuthority(store).mission(pointer["mission_version_id"])
        return _followup_sweep(store, EventJudgementAuthority(store), mission, apply=True)
    finally:
        store.close()


def _price_reads(
    store: DaltonStore, companies: list[str]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The latest close and market capitalisation per company, or nothing.

    Read by name and defensively: the price lane is a different slice and a
    Core that has not run it has no such table. A judgement that failed because
    the price authority was absent would be the market data deciding whether
    the brain gets to think.
    """

    try:
        from .market_price import MarketPriceSeriesAuthority
    except ImportError:  # pragma: no cover - the module ships with the package
        return {}, {}
    closes: dict[str, Any] = {}
    caps: dict[str, Any] = {}
    try:
        prices = MarketPriceSeriesAuthority(store)
    except Exception:  # noqa: BLE001 - no price schema on this Core
        return {}, {}
    for ref in companies:
        try:
            close = prices.latest_close(ref)
            cap = prices.latest_observation(ref, "market_cap")
        except Exception:  # noqa: BLE001 - one company, not the run
            continue
        if close is not None:
            closes[ref] = close
        if cap is not None:
            caps[ref] = cap
    return closes, caps


def _playbook(store: DaltonStore, mission: Any) -> dict[str, Any] | None:
    from .research_playbook import read_exact_playbook_version

    try:
        return read_exact_playbook_version(
            store.connection, mission["bindings"]["playbook_version"]["ref"]
        )
    except Exception:  # noqa: BLE001 - no playbook means no note, not a crash
        return None


def _forecast_models(store: DaltonStore, companies: list[str]) -> tuple[Any, dict[str, Any]]:
    from .model_forecast_driver import ForecastModelAuthority

    authority = ForecastModelAuthority(store)
    versions: dict[str, Any] = {}
    for ref in companies:
        try:
            latest = authority.latest(ref)
        except Exception:  # noqa: BLE001
            latest = None
        if latest is not None:
            versions[ref] = latest
    return authority, versions


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--summary-dir", type=Path, help="defaults to the state dir")
    parser.add_argument("--judge-model-config", type=Path)
    parser.add_argument("--verifier-model-config", type=Path)
    parser.add_argument("--tracking-policy", type=Path)
    parser.add_argument("--scheduler", type=Path)
    parser.add_argument("--company-ref")
    parser.add_argument("--event-ref", action="append", dest="event_refs")
    parser.add_argument("--event-group-hash")
    parser.add_argument("--max-events", type=int)
    parser.add_argument("--per-company", type=int)
    parser.add_argument("--dry-run", action="store_true", help="count and stop; no calls")
    parser.add_argument(
        "--followups-only", action="store_true",
        help="ADR-0010 backfill: report judgements that owe a governed follow-up "
             "(read-only); add --apply to write them. No model call either way.")
    parser.add_argument("--apply", action="store_true",
                        help="with --followups-only: write the follow-ups")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.followups_only:
        report = run_followup_backfill(state_dir=args.state_dir, apply=args.apply)
        if not args.quiet:
            print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=1))
        return 0 if report.get("status") in ("ok", "idle") and not report.get("errors") else 1
    summary = run_judgement(
        state_dir=args.state_dir,
        summary_dir=args.summary_dir if args.summary_dir is not None else args.state_dir,
        judge_model_config=args.judge_model_config,
        verifier_model_config=args.verifier_model_config,
        policy_path=args.tracking_policy,
        scheduler_db=args.scheduler,
        company_ref=args.company_ref,
        event_refs=None if args.event_refs is None else tuple(args.event_refs),
        event_group_hash=args.event_group_hash,
        max_events=args.max_events,
        per_company=args.per_company,
        dry_run=args.dry_run,
    )
    if not args.quiet:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=1))
    return 0 if summary["status"] in ("succeeded", "idle") else 1


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())


__all__ = [
    "ClaimDocuments",
    "JUDGE_MODEL_CONFIG",
    "MAX_DOCUMENT_CLAIMS",
    "config_fingerprint",
    "same_routing_policy",
    "MAX_COST_USD",
    "MAX_EVENTS_PER_COMPANY",
    "MAX_EVENTS_PER_RUN",
    "VERIFIER_MODEL_CONFIG",
    "build_parser",
    "main",
    "missing_write_scopes",
    "research_admitter_for",
    "run_followup_backfill",
    "run_judgement",
    "unjudged_events",
    "result_jobs",
    "job_group_hash",
]
