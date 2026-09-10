"""P14a: one ledger for everything that happened to a company we cover.

Before this, "something happened" was five different shapes in five different
tables -- a discovered document, a new Claim, a reconciliation row, a price
that moved, a date on a calendar -- and none of them was addressable.  You
could not ask "what has ACN done this week", you could not ask "why did we not
change our mind about it", and nothing could be *judged*, because a judgement
has to be about one thing with a name.

So an event is one thing with a name.  It is append-only, content-hashed, and
idempotent on (company, kind, payload): an emitter that re-reads the same row
tomorrow gets ``duplicate`` and costs a read, which is what lets the emitters
be dumb, stateless scans instead of cursor-keeping machines with their own
consistency problem.

Three properties are load-bearing and are enforced here rather than trusted:

- **The payload is typed per kind and closed.**  A ``price_move`` carries a
  return and the basket it is relative to; a ``news`` carries a document and
  the discovery that found it.  An emitter that invents a field is refused,
  because the judgement prompt renders these fields and a field nobody
  declared is a field nobody verified.
- **Every event carries an evidence tier.**  A sales note and a 10-Q are not
  the same kind of fact and the brain has to see the difference *in the
  prompt*, not infer it from a source slug it may not recognise.  The tier is
  derived from the source when the source is known and is otherwise required
  from the caller; it is never guessed.
- **This module decides nothing.**  It records that something happened.
  Whether anything should change because of it is ``event_judgement``'s
  question, and ADR-0008 is the reason the two are different modules: an
  authority that publishes because it noticed something produces noise and
  destroys the meaning of its own chain.

``record_event`` is the entry point other lanes call -- C1's catalyst calendar
with ``kind="calendar"``, and anything else that learns a dated fact about a
covered company.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .store import canonical_json, content_hash

SCHEMA_VERSION = "0.1"
_SCHEMA_PATH = Path(__file__).with_name("research_event_schema.sql")

# The mission word that authorises writing to this ledger.  P11d's MarketEvent
# was folded into ResearchEvent (parallel-development-plan v1.0), so the scope
# that was reserved for the one covers the other; a mission that grants
# ``market_event`` grants the event ledger.
WRITE_SCOPE = "market_event"

# Closed.  A kind is how the payload is shaped and how the prompt renders it,
# so a sixth word is a contract change and not a configuration change.
#
# The last three arrived with the owner's second instruction (2026-09-09): a
# sales note, a crowd post and an expert excerpt say different things about
# the *market's* view than a sell-side report does, and collapsing them into
# ``news`` would have thrown away exactly the distinction they are read for.
EVENT_KINDS: tuple[str, ...] = (
    "price_move",
    # The owner's third instruction (2026-09-09): agreeing with the market is
    # worth nothing. A divergence is the price running *against* what our
    # thesis implies over a window -- the case where the interesting question
    # is not "what happened today" but "what did we miss".
    "price_divergence",
    "news",
    "filing",
    "transcript",
    "rating_change",
    "calendar",
    "reconciliation",
    "claim",
    "sales_note",
    "crowd_post",
    "expert_excerpt",
    # S5 (the ongoing-tracking sources).  A ``filing`` says a document
    # appeared; these three say *what was in it*, because who bought, who
    # sold and how much of the company they now hold is the whole reason to
    # read an ownership filing and none of it survives a document-shaped
    # event.  They are their own kinds rather than ``filing`` with a richer
    # payload for the same reason ``sales_note`` is not ``news``: the
    # judgement prompt renders these fields, and a director's sale and a
    # 10-K's arrival are not the same question.
    "insider_transaction",
    "ownership_change",
    "holdings_change",
    # A company's own IR page moved.  Not a filing -- nothing was filed with
    # anybody and a marketing page is edited without a revision history -- so
    # it carries a diff hash instead of an accession, and the tier says
    # ``management_direct`` rather than ``primary_filing``.
    "ir_page_change",
)

# How much a reader should believe one event before reading it.  Ordered best
# first; the order is the Playbook's evidence discipline (一手 filing > 管理层
# 原话 > 专家 > 卖方 > 新闻 > 大众), written down where the prompt can print it.
EVIDENCE_TIERS: tuple[str, ...] = (
    "primary_filing",
    "management_direct",
    "expert_network",
    "sell_side",
    "vendor_note",
    "internal_wiki",
    "market_price",
    "derived",
    "news_media",
    "crowd",
)
_TIER_RANK = {tier: rank for rank, tier in enumerate(EVIDENCE_TIERS)}

# What each kind's payload is, exactly.  Optional fields may be ``None``; a
# field outside the set is a refusal.
PAYLOAD_FIELDS: Mapping[str, frozenset[str]] = MappingProxyType({
    "price_divergence": frozenset({
        "window_days", "from_date", "as_of", "cumulative_return_percent",
        "basket_return_percent", "excess_vs_basket_percent", "basket_members",
        "thesis_ref", "thesis_stance", "divergence_percent", "threshold_percent",
        "price_version_ref",
    }),
    "price_move": frozenset({
        "as_of", "close", "previous_close", "return_percent", "direction",
        "basket_return_percent", "excess_vs_basket_percent", "basket_members",
        "benchmark_ref", "benchmark_return_percent", "excess_vs_benchmark_percent",
        "trigger", "threshold_percent", "price_version_ref", "invocation_ref",
    }),
    "news": frozenset({"document_ref", "source_ref", "spec_ref", "discovery_ref", "title", "host"}),
    "filing": frozenset({"document_ref", "source_ref", "spec_ref", "discovery_ref", "title", "host"}),
    "transcript": frozenset({"document_ref", "source_ref", "spec_ref", "discovery_ref", "title", "host"}),
    "sales_note": frozenset({"document_ref", "source_ref", "spec_ref", "discovery_ref", "title", "host"}),
    "crowd_post": frozenset({"document_ref", "source_ref", "spec_ref", "discovery_ref", "title", "host"}),
    "expert_excerpt": frozenset({"document_ref", "source_ref", "spec_ref", "discovery_ref", "title", "host"}),
    "rating_change": frozenset({
        "document_ref", "source_ref", "broker", "from_rating", "to_rating", "price_target",
    }),
    "calendar": frozenset({
        "event_kind", "expected_date", "confirmed", "calendar_version_ref", "source_ref",
    }),
    "reconciliation": frozenset({
        "reconciliation_ref", "metric_ref", "period_end", "deviation_percent", "band",
        "forecast_line_version_ref", "claim_version_ref",
    }),
    "claim": frozenset({
        "claim_version_ref", "claim_ref", "metric_ref", "period", "statement", "source_ref",
    }),
    # S5.  Every one of these carries the accession *and* the hash of the
    # bytes it was parsed from, because a figure about a person's holding
    # that cannot be taken back to a filing is a rumour with a citation
    # attached.  ``event_key`` is the emitter's own deterministic name for
    # the row, so a lane that re-reads yesterday's filing marks it emitted
    # without having to diff the payload it just built.
    "insider_transaction": frozenset({
        "accession", "form", "owner_name", "owner_cik", "role",
        "transaction_code", "transaction_meaning", "transaction_date",
        "security_title", "shares", "price_per_share", "acquired_disposed",
        "shares_owned_following", "direct_or_indirect", "issuer_name",
        "invocation_ref", "artifact_hash", "event_key",
    }),
    "ownership_change": frozenset({
        "accession", "form", "is_amendment", "amendment_no",
        "reporting_person", "person_cik", "person_type", "percent_of_class",
        "aggregate_shares", "sole_voting_power", "shared_voting_power",
        "event_date", "security_class", "cusip", "purpose_text_hash",
        "invocation_ref", "artifact_hash", "event_key",
    }),
    "holdings_change": frozenset({
        "accession", "form", "holder_name", "holder_cik", "quarter",
        "prior_quarter", "cusip", "issuer_name", "title_of_class", "put_call",
        "action", "shares", "prior_shares", "share_change", "value_usd",
        "prior_value_usd", "value_unit", "value_unit_basis",
        "invocation_ref", "artifact_hash", "event_key",
    }),
    "ir_page_change": frozenset({
        "watch_ref", "url", "host", "diff_hash", "previous_snapshot_hash",
        "current_snapshot_hash", "changed_at", "added_line_count",
        "removed_line_count", "title", "excerpt", "artifact_hash",
        "invocation_ref", "event_key",
    }),
})

# The tier a kind carries when nothing more specific is known.  ``news`` has
# none on purpose: a document's tier depends on who wrote it, so the emitter
# resolves it from the source and a caller that cannot must say which it is.
DEFAULT_TIER_BY_KIND: Mapping[str, str] = MappingProxyType({
    "price_move": "market_price",
    "price_divergence": "market_price",
    "filing": "primary_filing",
    "transcript": "management_direct",
    "rating_change": "sell_side",
    "calendar": "derived",
    "reconciliation": "derived",
    "claim": "derived",
    "sales_note": "vendor_note",
    "crowd_post": "crowd",
    "expert_excerpt": "expert_network",
    "news": "news_media",
    # Filed with a regulator, by name, under penalty.  ``primary_filing`` is
    # how well attested it is and is not a licence to read it as a statement
    # of what the business earned: see ``sec_ownership_core.OWNERSHIP_GRADE``,
    # which is the word that keeps these out of the figure path.
    "insider_transaction": "primary_filing",
    "ownership_change": "primary_filing",
    "holdings_change": "primary_filing",
    # The company speaking in its own voice on its own site.
    "ir_page_change": "management_direct",
})

MAX_PAYLOAD_TEXT = 600
MAX_SOURCE_REFS = 12
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class ResearchEventError(RuntimeError):
    """Base error for the research event ledger."""


class ResearchEventValidationError(ResearchEventError, ValueError):
    """A request does not satisfy the closed contract."""


class ResearchEventConflict(ResearchEventError):
    """A request conflicts with the immutable ledger."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any, name: str, *, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResearchEventValidationError(f"{name} must be non-empty text")
    value = value.strip()
    if len(value) > maximum:
        raise ResearchEventValidationError(f"{name} must be at most {maximum} characters")
    return value


def _vocabulary(value: Any, allowed: Sequence[str], name: str) -> str:
    value = _text(value, name)
    if value not in allowed:
        raise ResearchEventValidationError(f"{name} must be one of {list(allowed)}")
    return value


def rfc3339(value: Any, name: str = "occurred_at") -> str:
    """A moment, in UTC, with a timezone on it.

    A naive timestamp is refused rather than assumed to be UTC: half of this
    system's inputs are dated in a market's local time, and an event ledger
    whose ordering is a guess cannot answer "what did we know when".
    """

    value = _text(value, name, maximum=64)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ResearchEventValidationError(f"{name} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise ResearchEventValidationError(f"{name} must carry a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def day_start(value: str) -> str:
    """The RFC3339 midnight of an ISO date, so a dated fact can be an event."""

    return f"{date.fromisoformat(value).isoformat()}T00:00:00+00:00"


def validate_payload(kind: str, payload: Any) -> dict[str, Any]:
    """One kind's payload, closed and bounded.

    Values are text, integers, booleans or absent.  Nothing nested: a payload
    that can hold a document can hold a document's worth of unverified prose,
    and this ledger is an index of what happened, not a second copy of it.
    """

    fields = PAYLOAD_FIELDS[_vocabulary(kind, EVENT_KINDS, "kind")]
    if not isinstance(payload, Mapping):
        raise ResearchEventValidationError("payload must be an object")
    unknown = sorted(set(payload) - fields)
    if unknown:
        raise ResearchEventValidationError(
            f"{kind} payload has fields the contract does not declare: {unknown}"
        )
    result: dict[str, Any] = {}
    for field in sorted(fields):
        value = payload.get(field)
        if value is None:
            result[field] = None
        elif isinstance(value, bool) or isinstance(value, int):
            result[field] = value
        elif isinstance(value, str):
            result[field] = _text(value, f"payload.{field}", maximum=MAX_PAYLOAD_TEXT)
        else:
            raise ResearchEventValidationError(
                f"payload.{field} must be text, an integer, a boolean or null"
            )
    if all(value is None for value in result.values()):
        raise ResearchEventValidationError(f"a {kind} payload that says nothing is not an event")
    return result


def payload_hash(payload: Mapping[str, Any]) -> str:
    return content_hash(dict(payload))


def event_ref_for(company_ref: str, kind: str, digest: str) -> str:
    """The name of one event.

    Deliberately not a function of when it was recorded: the same fact read
    twice is the same event, and that is the whole of the idempotency rule.
    """

    return "research-event:" + content_hash({
        "company_ref": company_ref, "kind": kind, "payload_hash": digest,
    })[:32]


def worst_tier(tiers: Sequence[str]) -> str:
    """The weakest tier in a set -- how a batch of events should be believed."""

    known = [tier for tier in tiers if tier in _TIER_RANK]
    if not known:
        return "news_media"
    return max(known, key=lambda tier: _TIER_RANK[tier])


def _decode(row: sqlite3.Row | None, name: str) -> dict[str, Any]:
    if row is None:
        raise ResearchEventConflict(f"{name} is missing")
    try:
        wire = json.loads(row["record_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise ResearchEventConflict(f"{name} record_json is invalid") from exc
    if not isinstance(wire, dict) or canonical_json(wire) != row["record_json"]:
        raise ResearchEventConflict(f"{name} record_json is not canonical")
    body = dict(wire)
    asserted = body.pop("content_hash", None)
    if asserted != content_hash(body) or asserted != row["content_hash"]:
        raise ResearchEventConflict(f"{name} content hash drifted")
    for column, key in (
        ("event_id", "id"), ("company_ref", "company_ref"), ("kind", "kind"),
        ("occurred_at", "occurred_at"), ("evidence_tier", "evidence_tier"),
        ("payload_hash", "payload_hash"), ("actor_ref", "actor_ref"),
        ("created_at", "created_at"),
    ):
        if wire.get(key) != row[column]:
            raise ResearchEventConflict(f"{name} identity column {column} drifted")
    return wire


class ResearchEventAuthority:
    """Append-only, content-hashed, idempotent on what the event says."""

    def __init__(self, store: Any) -> None:
        if not hasattr(store, "connection") or not hasattr(store, "_transaction"):
            raise TypeError("ResearchEventAuthority requires a DaltonStore")
        self.store = store
        self.connection: sqlite3.Connection = store.connection
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    # -- reading -----------------------------------------------------------

    def event(self, event_ref: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM research_events WHERE event_id=?",
            (_text(event_ref, "event_ref"),),
        ).fetchone()
        return None if row is None else _decode(row, f"ResearchEvent {event_ref}")

    def events(
        self,
        *,
        company_ref: str | None = None,
        kind: str | None = None,
        since: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM research_events WHERE 1=1"
        params: list[Any] = []
        if company_ref is not None:
            query += " AND company_ref=?"
            params.append(_text(company_ref, "company_ref"))
        if kind is not None:
            query += " AND kind=?"
            params.append(_vocabulary(kind, EVENT_KINDS, "kind"))
        if since is not None:
            query += " AND occurred_at>=?"
            params.append(rfc3339(since, "since"))
        query += " ORDER BY occurred_at DESC, event_id DESC LIMIT ?"
        params.append(max(1, int(limit)))
        return [
            _decode(row, "ResearchEvent")
            for row in self.connection.execute(query, params).fetchall()
        ]

    def holds(self, company_ref: str, kind: str, digest: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM research_events WHERE company_ref=? AND kind=? AND payload_hash=?",
            (company_ref, kind, digest),
        ).fetchone()
        return row is not None

    def latest_occurred_at(self, company_ref: str, kind: str) -> str | None:
        """The newest event of one kind, which is how an emitter bounds a scan."""

        row = self.connection.execute(
            "SELECT MAX(occurred_at) AS newest FROM research_events "
            "WHERE company_ref=? AND kind=?",
            (_text(company_ref, "company_ref"), _vocabulary(kind, EVENT_KINDS, "kind")),
        ).fetchone()
        return None if row is None else row["newest"]

    def counts(self, company_ref: str) -> dict[str, int]:
        """Per-kind event counts for one company; the active-coverage figures."""

        rows = self.connection.execute(
            "SELECT kind, COUNT(*) AS n FROM research_events WHERE company_ref=? GROUP BY kind",
            (_text(company_ref, "company_ref"),),
        ).fetchall()
        return {row["kind"]: int(row["n"]) for row in rows}

    # -- writing -----------------------------------------------------------

    def record(
        self,
        *,
        company_ref: str,
        kind: str,
        occurred_at: str,
        source_refs: Sequence[str],
        payload: Mapping[str, Any],
        evidence_tier: str | None,
        mission: Mapping[str, Any],
        actor_ref: str,
    ) -> dict[str, Any]:
        """Record one event, or say it was already recorded.

        Returns the record with ``status`` ``fresh`` or ``duplicate``.  A
        duplicate is the resting state of every emitter and is not an error.
        """

        company_ref = _text(company_ref, "company_ref")
        kind = _vocabulary(kind, EVENT_KINDS, "kind")
        occurred_at = rfc3339(occurred_at)
        actor_ref = _text(actor_ref, "actor_ref")
        if not (actor_ref.startswith("human:") or actor_ref.startswith("automation:")):
            raise ResearchEventValidationError("actor_ref must be a human: or automation: principal")
        if actor_ref.startswith("automation:"):
            if actor_ref != mission["autonomy"]["automation_principal"]:
                raise ResearchEventConflict("automation actor is not the mission principal")
            if WRITE_SCOPE not in mission["autonomy"]["may_write"]:
                raise ResearchEventConflict(
                    f"mission does not grant {WRITE_SCOPE} writes to automation"
                )
        if not any(member["company_ref"] == company_ref for member in mission["universe"]):
            raise ResearchEventConflict("company is not in the mission universe")
        if not isinstance(source_refs, Sequence) or isinstance(source_refs, (str, bytes)):
            raise ResearchEventValidationError("source_refs must be a list")
        refs = [_text(ref, "source_refs[]") for ref in source_refs][:MAX_SOURCE_REFS]
        if not refs:
            raise ResearchEventValidationError(
                "an event with no source ref cannot be checked and is refused"
            )
        body = validate_payload(kind, payload)
        digest = payload_hash(body)
        tier = _vocabulary(
            evidence_tier or DEFAULT_TIER_BY_KIND[kind], EVIDENCE_TIERS, "evidence_tier"
        )
        event_id = event_ref_for(company_ref, kind, digest)
        record = {
            "schema_version": SCHEMA_VERSION,
            "id": event_id,
            "created_at": _now(),
            "company_ref": company_ref,
            "kind": kind,
            "occurred_at": occurred_at,
            "evidence_tier": tier,
            "source_refs": list(dict.fromkeys(refs)),
            "payload": body,
            "payload_hash": digest,
            "mission_version_ref": mission["id"],
            "mission_version_hash": mission["content_hash"],
            "actor_ref": actor_ref,
        }
        record["content_hash"] = content_hash(record)
        with self.store._transaction() as cur:
            existing = cur.execute(
                "SELECT * FROM research_events WHERE company_ref=? AND kind=? AND payload_hash=?",
                (company_ref, kind, digest),
            ).fetchone()
            if existing is not None:
                return {**_decode(existing, "ResearchEvent"), "status": "duplicate"}
            cur.execute(
                "INSERT INTO research_events(event_id,company_ref,kind,occurred_at,evidence_tier,"
                "payload_hash,mission_version_ref,record_json,content_hash,actor_ref,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    event_id, company_ref, kind, occurred_at, tier, digest, mission["id"],
                    canonical_json(record), record["content_hash"], actor_ref,
                    record["created_at"],
                ),
            )
            written = _decode(
                cur.execute(
                    "SELECT * FROM research_events WHERE event_id=?", (event_id,)
                ).fetchone(),
                "ResearchEvent",
            )
        return {**written, "status": "fresh"}


def record_event(
    authority: ResearchEventAuthority,
    *,
    company_ref: str,
    kind: str,
    occurred_at: str,
    source_refs: Sequence[str],
    payload: Mapping[str, Any],
    mission: Mapping[str, Any],
    actor_ref: str,
    evidence_tier: str | None = None,
) -> dict[str, Any]:
    """The entry point another lane calls to say something happened.

    C1's catalyst calendar calls it with ``kind="calendar"``; anything else
    that learns a dated fact about a covered company calls it with its own
    kind.  It is a function rather than a method so the caller does not have
    to hold the authority's type -- only a thing that records events.
    """

    return authority.record(
        company_ref=company_ref, kind=kind, occurred_at=occurred_at,
        source_refs=source_refs, payload=payload, evidence_tier=evidence_tier,
        mission=mission, actor_ref=actor_ref,
    )


# ---------------------------------------------------------------------------
# emitters: stateless scans over ledgers that already exist
# ---------------------------------------------------------------------------
#
# None of these keeps a cursor.  A cursor is a second ledger with its own
# consistency problem, and this one does not need it: the event id is a
# function of what the event says, so re-reading yesterday's rows costs a
# lookup and writes nothing.  What bounds the work is a lookback window, and
# the window is generous rather than exact because missing an event is worse
# than re-deciding one is a duplicate.

DEFAULT_LOOKBACK_DAYS = 7
MAX_EVENTS_PER_SCAN = 60

# Which discovery spec produces which kind of event, and how much a reader
# should believe it.  A spec that is not named here is ``news`` at news tier:
# an unrecognised source is not promoted by not being recognised.
SPEC_EVENT_KINDS: Mapping[str, tuple[str, str]] = MappingProxyType({
    "earnings-call-transcripts": ("transcript", "management_direct"),
    "annual-report-10k": ("filing", "primary_filing"),
    "annual-reports": ("filing", "primary_filing"),
    "sell-side-reports": ("news", "sell_side"),
    "broker-research": ("news", "sell_side"),
    "sales-notes": ("sales_note", "vendor_note"),
    "company-wiki": ("expert_excerpt", "internal_wiki"),
    "expert-transcripts": ("expert_excerpt", "expert_network"),
    "guidepoint-library": ("expert_excerpt", "expert_network"),
    "x-timeline": ("crowd_post", "crowd"),
    "xueqiu-posts": ("crowd_post", "crowd"),
    "employee-reviews": ("crowd_post", "crowd"),
})
# The same question answered from the source when the spec is unfamiliar,
# which is the common case while the S-line connectors are still landing.
SOURCE_EVENT_KINDS: Mapping[str, tuple[str, str]] = MappingProxyType({
    "source:sec-edgar": ("filing", "primary_filing"),
    "source:alphaengine": ("news", "sell_side"),
    "source:guidepoint": ("expert_excerpt", "expert_network"),
    "source:sales-notes": ("sales_note", "vendor_note"),
    "source:company-wiki": ("expert_excerpt", "internal_wiki"),
    "source:x": ("crowd_post", "crowd"),
    "source:xueqiu": ("crowd_post", "crowd"),
    "source:employee-reviews": ("crowd_post", "crowd"),
    "source:web-search": ("news", "news_media"),
    "source:public-web": ("news", "news_media"),
})


def classify_document(spec_ref: str | None, source_ref: str | None) -> tuple[str, str]:
    """(kind, evidence_tier) for one discovered document.

    The spec is asked first because it is the more specific fact -- the same
    AlphaEngine library holds sell-side reports and management minutes -- and
    the source is the fallback.  Neither known means ``news`` at news tier.
    """

    if spec_ref and spec_ref in SPEC_EVENT_KINDS:
        return SPEC_EVENT_KINDS[spec_ref]
    if source_ref and source_ref in SOURCE_EVENT_KINDS:
        return SOURCE_EVENT_KINDS[source_ref]
    return ("news", "news_media")


def _lookback(now: datetime, days: int) -> str:
    return (now - timedelta(days=max(1, days))).isoformat(timespec="microseconds")


def document_event_candidates(
    connection: sqlite3.Connection,
    *,
    company_ref: str,
    mission_ref: str,
    now: datetime,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    limit: int = MAX_EVENTS_PER_SCAN,
) -> list[dict[str, Any]]:
    """Documents this company acquired recently, as event bodies.

    Counted across every version of the mission, for the reason the stage
    checklist found the hard way: carry-forward does not copy a document whose
    work is finished, so counting only the current version makes a company's
    history *fall* when the work on it completes.
    """

    since = _lookback(now, lookback_days)
    rows = connection.execute(
        "SELECT d.document_ref AS document_ref, d.source_ref AS source_ref, "
        "d.host AS host, d.created_at AS created_at, d.record_id AS record_id, "
        "s.spec_ref AS spec_ref, s.record_id AS discovery_ref "
        "FROM coverage_mission_discovered_documents d "
        "JOIN coverage_mission_source_discoveries s ON s.record_id=d.discovery_ref "
        "WHERE d.company_ref=? AND d.created_at>=? AND d.status IN "
        "('acquired','already_in_authority') AND d.mission_version_ref IN "
        "(SELECT mission_version_id FROM coverage_mission_versions WHERE mission_ref=?) "
        # Oldest first inside the window. Newest first with a LIMIT silently
        # drops the tail for ever: the rows past the limit are never reached
        # again, because tomorrow's scan finds the same newest rows and stops
        # in the same place. Oldest first makes every run monotone -- what was
        # recorded is a duplicate next time and costs a lookup, so the window
        # is worked through rather than skimmed.
        "ORDER BY d.created_at ASC, d.record_id ASC LIMIT ?",
        (company_ref, since, mission_ref, int(limit)),
    ).fetchall()
    seen: set[str] = set()
    candidates: list[dict[str, Any]] = []
    for row in rows:
        if row["document_ref"] in seen:
            continue
        seen.add(row["document_ref"])
        kind, tier = classify_document(row["spec_ref"], row["source_ref"])
        candidates.append({
            "kind": kind,
            "evidence_tier": tier,
            "occurred_at": rfc3339(row["created_at"]),
            "source_refs": [row["source_ref"], row["document_ref"]],
            "payload": {
                "document_ref": row["document_ref"],
                "source_ref": row["source_ref"],
                "spec_ref": row["spec_ref"],
                "discovery_ref": row["discovery_ref"],
                "title": None,
                "host": row["host"],
            },
        })
    return candidates


def claim_event_candidates(
    connection: sqlite3.Connection,
    *,
    company_ref: str,
    now: datetime,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    limit: int = MAX_EVENTS_PER_SCAN,
) -> list[dict[str, Any]]:
    """Claims admitted for this company recently, as event bodies.

    A retired Claim never becomes an event: P10b decided it was wrong, and an
    event ledger that carries it would put a disowned fact in front of the
    judgement prompt with nothing marking it as disowned.
    """

    since = _lookback(now, lookback_days)
    try:
        retired = {
            row["claim_version_ref"]
            for row in connection.execute(
                "SELECT claim_version_ref FROM claim_retirement_decisions WHERE decision='retired'"
            ).fetchall()
        }
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        retired = set()
    rows = connection.execute(
        "SELECT claim_version_id AS id, claim_ref, claim_json, created_at FROM claim_versions "
        "WHERE json_extract(claim_json,'$.subject_ref')=? AND created_at>=? "
        # Oldest first, for the reason above.
        "ORDER BY created_at ASC, claim_version_id ASC LIMIT ?",
        (company_ref, since, int(limit)),
    ).fetchall()
    candidates: list[dict[str, Any]] = []
    for row in rows:
        if row["id"] in retired:
            continue
        try:
            claim = json.loads(row["claim_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        statement = claim.get("normalized_statement") or claim.get("statement")
        candidates.append({
            "kind": "claim",
            "evidence_tier": "derived",
            "occurred_at": rfc3339(row["created_at"]),
            "source_refs": [row["id"]],
            "payload": {
                "claim_version_ref": row["id"],
                "claim_ref": row["claim_ref"],
                "metric_ref": claim.get("metric_ref") or claim.get("aspect"),
                "period": claim.get("period"),
                "statement": None if statement is None else str(statement)[:MAX_PAYLOAD_TEXT],
                "source_ref": claim.get("source_ref"),
            },
        })
    return candidates


def reconciliation_event_candidates(
    connection: sqlite3.Connection,
    *,
    company_ref: str,
    now: datetime,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    limit: int = MAX_EVENTS_PER_SCAN,
) -> list[dict[str, Any]]:
    """Forecast-versus-actual rows for this company, as event bodies."""

    since = _lookback(now, lookback_days)
    try:
        rows = connection.execute(
            "SELECT reconciliation_id, metric_ref, period_end, band, record_json, "
            "forecast_line_version_ref, claim_version_ref, created_at "
            "FROM forecast_reconciliations WHERE subject_ref=? AND created_at>=? "
            "ORDER BY created_at ASC, reconciliation_id ASC LIMIT ?",
            (company_ref, since, int(limit)),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        return []
    candidates: list[dict[str, Any]] = []
    for row in rows:
        try:
            record = json.loads(row["record_json"])
        except (TypeError, json.JSONDecodeError):
            record = {}
        candidates.append({
            "kind": "reconciliation",
            "evidence_tier": "derived",
            "occurred_at": rfc3339(row["created_at"]),
            "source_refs": [row["reconciliation_id"]],
            "payload": {
                "reconciliation_ref": row["reconciliation_id"],
                "metric_ref": row["metric_ref"],
                "period_end": row["period_end"],
                "deviation_percent": str(record.get("deviation_percent"))
                if record.get("deviation_percent") is not None else None,
                "band": row["band"],
                "forecast_line_version_ref": row["forecast_line_version_ref"],
                "claim_version_ref": row["claim_version_ref"],
            },
        })
    return candidates


__all__ = [
    "DEFAULT_LOOKBACK_DAYS",
    "DEFAULT_TIER_BY_KIND",
    "EVENT_KINDS",
    "EVIDENCE_TIERS",
    "MAX_EVENTS_PER_SCAN",
    "PAYLOAD_FIELDS",
    "SCHEMA_VERSION",
    "SOURCE_EVENT_KINDS",
    "SPEC_EVENT_KINDS",
    "WRITE_SCOPE",
    "ResearchEventAuthority",
    "ResearchEventConflict",
    "ResearchEventError",
    "ResearchEventValidationError",
    "claim_event_candidates",
    "classify_document",
    "day_start",
    "document_event_candidates",
    "event_ref_for",
    "payload_hash",
    "reconciliation_event_candidates",
    "record_event",
    "rfc3339",
    "validate_payload",
    "worst_tier",
]
