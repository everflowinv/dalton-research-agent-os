"""Batch E: the material acquisition service, the one consumer of the MaterialRequest ledger.

The research capability diagnosis (2026-10-09) found every stage of the system
ending its search for material in a different dead end: the planner's
inquiries that no template could bind, the Dossier's repair targets, the
research task's ``no_bindable_template``, an event judgement's ``action=research``,
a product's 「材料缺少」.  The owner's decision was one shared service:
「所有环节共用的材料获取服务」.  Every stage that lacks something opens a
MaterialRequest (:mod:`material_requests`); this module works the request's
**source ladder** and closes it.

``serve(state_dir, request_id)``
    Runs the ladder in the request's ``source_priority`` order (default
    :data:`material_requests.DEFAULT_SOURCE_PRIORITY`):

    1. ``indexed_corpus`` -- topic retrieval over the evidence index
       (``evidence_search`` / ``documents`` with the request's need and period);
    2. ``sales_notes``   -- the indexed sales notes, then the raw notes index
       the feed lane reads (a matching note not yet acquired is waited for);
    3. ``alphaengine``   -- a targeted ``search_library`` (+ the coordinator's
       budgeted ``get_document``) for the company and the spec the need maps
       to (transcripts, broker reports);
    4. ``sec_edgar``     -- XBRL company facts already in Core for a data item,
       else a targeted filings-index pass (8-K Item 2.02 with its exhibits,
       the 10-K) through the SEC coordinator;
    5. ``guidepoint``    -- a targeted Guidepoint library search;
    6. ``web``           -- a targeted web search, whose cited pages the
       public-web fetch lane acquires.

    It stops at the first rung whose results meet the request's
    ``acceptance`` (:func:`evaluate_acceptance`).  Every rung's attempt --
    source, query, outcome, calls, cost -- is recorded with
    ``record_attempt``; found material is linked to the request
    (``material_request_arrivals``, the ``material_arrived`` event) and the
    request is closed ``found`` or ``partial``.  All rungs failing closes it
    ``exhausted`` with the attempt list; a passed deadline closes it
    ``timeout``.

**No parallel storage.**  The external rungs never fetch anything themselves.
A *targeted query* (``material_targeted_queries``) asks the source's own
coordinator to run its own governed search for this company now, bypassing its
cadence -- with its own launcher, mission grant, connector health hold and
``connector_rate_policy`` call window, its own code-owned query forms (the Core
never invents a connector query), and its own acquisition path.  The
coordinator appends what it did (``material_targeted_query_events``); the
documents it acquires arrive as ordinary acquisition manifests, which the
evidence index picks up; this module only re-evaluates acceptance over the
index.  A rung given manifest paths directly (a test's fake connector)
registers them through :func:`evidence_index.index_acquired_manifest`, the same
path an acquisition lane's completion hook uses.

**Idempotent, negatively cached.**  A request is its digest (subject, need,
period): two products asking for the same transcript share one request.  An
``exhausted`` request is not retried for :data:`NEGATIVE_CACHE_DAYS` days
unless a new discovery batch (or a new indexed arrival) for that company came
in since it was closed; then it is reopened (``material_requests.reopen``) and
the ladder runs again.

**Budget.**  The ``material_acquisition`` pool (:mod:`budget_pools`): a day
cap in dollars and calls, and a per-request cap from the request's
``budget``.  A rung whose estimated calls would cross either cap stops the
ladder (``budget_blocked``) and the owner sees one card with the amount to
approve.  Per-source call windows still bind at launch, in the coordinator.

**The owner door** (:func:`owner_door_cards`) is reached in exactly three
situations: every source exhausted (with 「已尝试：…」), a budget cap reached
(with the cost to approve), a source needing a login or an approval.
Everything else -- holds, waits, timeouts, lane-owned data -- is automatic.

Converters (:func:`open_for_inquiry`, :func:`open_for_repair_target`,
:func:`open_for_rewrite_flag`, :func:`open_for_effect`) turn the old dead ends
into requests.  The lane (:mod:`material_acquisition_lane`) serves the oldest
open requests within budget each tick, highest priority first.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol

from . import material_requests as mr

SCHEMA_VERSION = "material-acquisition:0.1"
_SCHEMA_PATH = Path(__file__).with_name("material_acquisition_schema.sql")
ACTOR_REF = "automation:material-acquisition"
HELD_FILING_ANSWER_WINDOW_RULE_REF = "rule:unperioded-filing-answer-window:0.1"

#: The default ladder (the diagnosis's order).
LADDER: tuple[str, ...] = mr.DEFAULT_SOURCE_PRIORITY
#: Sources that are not a place to look but a lane that owns the data
#: (``material_sufficiency.SOURCE_PRIORITY``): the request is satisfied when
#: that lane has produced it, and otherwise waits -- it times out, it is never
#: "exhausted", and it never reaches the owner.
LANE_OWNED: Mapping[str, str] = {
    "consensus_vendor": "consensus_fresh",
    "forecast_model_lane": "model_eps",
    "market_price_lane": "market_block",
}
SOURCE_REFS: Mapping[str, str] = {
    "indexed_corpus": "source:evidence-index",
    "sales_notes": "source:sales-notes",
    "alphaengine": "source:alphaengine",
    "sec_edgar": "source:sec-edgar",
    "guidepoint": "source:guidepoint",
    "web": "source:web-search",
}
#: The rung each source ref is drained by (the reverse of ``SOURCE_REFS``).
SOURCE_FOR_REF: Mapping[str, str] = {ref: name for name, ref in SOURCE_REFS.items()}

NEGATIVE_CACHE_DAYS = 7
#: How long a targeted query may go unanswered by its coordinator before the
#: rung is given up for this round (the source is not installed here, or its
#: lane is down).
RUNG_WAIT_HOURS = 3
#: After a targeted search settles, how long its acquisitions get to land and
#: be indexed before the rung's result is read as final.
SETTLE_GRACE_MINUTES = 15
#: Calls one targeted query is expected to take (search + document pages),
#: what the budget is checked against before it is asked.
CALL_ESTIMATES: Mapping[str, int] = {
    "indexed_corpus": 0, "sales_notes": 0, "alphaengine": 3, "sec_edgar": 1, "guidepoint": 1, "web": 3,
}
#: Dollars per call where a source is metered per call (web search grounding).
#: AlphaEngine and Guidepoint are subscriptions bounded by their call windows;
#: SEC is free public HTTPS.
USD_PER_CALL: Mapping[str, Decimal] = {"web": Decimal("0.035")}
#: Outcomes that end a rung for this round.
DONE_OUTCOMES = frozenset({"accepted", "partial", "empty", "not_applicable", "not_connected",
                           "needs_credentials", "error", "unsupported"})
#: Outcomes that leave a rung to be tried again (the source is held, or busy,
#: or the data belongs to another lane that has not produced it yet).
DEFERRED_OUTCOMES = frozenset({"health_hold", "rate_limited", "waiting_on_lane", "budget_blocked"})
#: Attempt outcomes that are real tries (what 「已尝试」 lists).
TRIED_OUTCOMES = frozenset({"accepted", "partial", "empty", "dispatched", "needs_credentials", "error"})

#: A request's need -> AlphaEngine discovery spec (``alphaengine_query_forms``).
ALPHAENGINE_SPECS: Mapping[str, str] = {
    "transcript": "earnings-call-transcripts",
    "broker_report": "sell-side-reports",
    "sales_note": "sell-side-reports",
}
#: A request's need -> SEC filings-index spec (``sec_plan_specs``).
SEC_SPECS: Mapping[str, str] = {
    "sec_exhibit": "current-report-8k",
    "sec_8k": "current-report-8k",
    "sec_10k": "annual-report-10k",
    "sec_10q": "quarterly-report-10q",
}
#: Data-item phrasings: what a passage says when it reports the item.  Keyed
#: by a normalised item name; the item's own words are always included.
DATA_ITEM_TERMS: Mapping[str, tuple[str, ...]] = {
    "bookings": ("bookings", "new bookings", "book-to-bill"),
    "revenue": ("revenue", "revenues", "net revenue", "sales"),
    "eps": ("EPS", "earnings per share", "diluted EPS"),
    "guidance": ("guidance", "outlook", "expects"),
    "operating_margin": ("operating margin", "operating income"),
    "operating_income": ("operating income", "operating profit"),
    "net_income": ("net income", "net earnings"),
    "free_cash_flow": ("free cash flow",),
    "capex": ("capital expenditures", "capex", "purchases of property"),
    "headcount": ("headcount", "employees"),
    "utilization": ("utilization", "utilisation"),
    "backlog": ("backlog", "remaining performance obligations", "RPO"),
    "consensus_estimates": ("consensus", "estimates"),
}
#: Data item -> XBRL concepts in Core's statement lines.
XBRL_CONCEPTS: Mapping[str, tuple[str, ...]] = {
    "revenue": ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"),
    "eps": ("EarningsPerShareDiluted", "EarningsPerShareBasic"),
    "net_income": ("NetIncomeLoss",),
    "operating_income": ("OperatingIncomeLoss",),
    "capex": ("PaymentsToAcquirePropertyPlantAndEquipment",),
    "free_cash_flow": ("NetCashProvidedByUsedInOperatingActivities",),
}
_NUMBER_RE = re.compile(r"\d")
_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September",
           "October", "November", "December")
_QUARTER_WORDS = {1: "first", 2: "second", 3: "third", 4: "fourth"}
_FY_PERIOD_RE = re.compile(r"FY(\d{2,4})[-\s]?Q([1-4])", re.I)
_REQUEST_ID_RE = re.compile(r"material-request:[0-9a-f]{32}")


class MaterialAcquisitionError(ValueError):
    """A request or an effect this service cannot work on."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            moment = datetime.fromisoformat(value[:10])
        except ValueError:
            return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _micros(usd: Any) -> int:
    return int((Decimal(str(usd)) * 1_000_000).to_integral_value())


def _usd(micros: int) -> str:
    return f"{Decimal(int(micros)) / Decimal(1_000_000):.2f}"


# ---------------------------------------------------------------------------
# storage: the service's own tables beside the request ledger
# ---------------------------------------------------------------------------


@contextmanager
def _writer(state_dir: str | Path) -> Iterator[sqlite3.Connection]:
    path = mr.ledger_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
    finally:
        connection.close()


@contextmanager
def _reader(state_dir: str | Path) -> Iterator[sqlite3.Connection | None]:
    path = mr.ledger_path(state_dir)
    if not path.is_file():
        yield None
        return
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    except sqlite3.Error:
        yield None
        return
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


def _rows(connection: sqlite3.Connection | None, sql: str, params: Sequence[Any] = (), *, deadline: float | None = None) -> list[sqlite3.Row]:
    if connection is None:
        return []
    try:
        from .source_discovery_read_budget import bounded_rows
        return bounded_rows(connection, sql, tuple(params), deadline=deadline)
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc) or "no such column" in str(exc):
            return []
        raise


@contextmanager
def _core(state_dir: str | Path) -> Iterator[sqlite3.Connection | None]:
    from .material_sufficiency import open_core_ro

    connection = open_core_ro(state_dir)
    try:
        yield connection
    finally:
        if connection is not None:
            connection.close()


# ---------------------------------------------------------------------------
# periods and queries (deterministic)
# ---------------------------------------------------------------------------


def _day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def period_window(period: str | None, *, now: str | None = None) -> dict[str, Any]:
    """What a request's ``period`` means: a date window, label and text tokens.

    The period vocabulary is the one :mod:`material_sufficiency` writes
    (``quarter_ending:2026-08-31``, ``latest_quarter_as_of:2026-10``,
    ``fiscal_year_as_of:2026``, ``183d_to:2026-10``, ``week:2026-W41``,
    ``as_of:2026-10-09``, ``current``) plus fiscal ids (``FY2026-Q4``).
    """

    today = (_parse_ts(now) or datetime.now(timezone.utc)).date()
    out: dict[str, Any] = {"date_from": None, "date_to": None, "label": None, "tokens": [], "quarter_end": None}
    text = str(period or "").strip()
    if not text or text == "current":
        out["date_from"] = (today - timedelta(days=365)).isoformat()
        return out
    kind, _, value = text.partition(":")
    # Event/document period namespaces identify the request, not public dates.
    if kind in {"event", "event_document"} and value:
        return out
    if kind == "quarter_ending" and _day(value):
        end = _day(value)
        out.update(date_from=(end + timedelta(days=1)).isoformat(),
                   date_to=min(end + timedelta(days=130), max(today, end + timedelta(days=1))).isoformat(),
                   quarter_end=end.isoformat(),
                   label=f"截至 {end.isoformat()} 的季度")
        month = _MONTHS[end.month - 1]
        out["tokens"] = [f"{month} {end.day}, {end.year}", f"{month} {end.year}",
                         f"quarter ended {month}", end.isoformat()]
        return out
    if kind == "latest_quarter_as_of":
        anchor = _day(value + "-01") if len(value) == 7 else _day(value)
        anchor = anchor or today
        out.update(date_from=(anchor - timedelta(days=100)).isoformat(), label="最近一个已报告季度")
        return out
    if kind == "fiscal_year_as_of":
        out.update(date_from=(today - timedelta(days=430)).isoformat(), label=f"{value} 财年")
        out["tokens"] = [f"fiscal {value}", f"FY{value[-2:]}", value]
        return out
    match = re.fullmatch(r"(\d{1,4})d_to:(\d{4}-\d{2})", text)
    if match:
        out.update(date_from=(today - timedelta(days=int(match.group(1)))).isoformat(),
                   label=f"近 {match.group(1)} 天")
        return out
    if kind == "week":
        out.update(date_from=(today - timedelta(days=14)).isoformat(), label=f"{value} 周")
        return out
    if kind == "as_of" and _day(value):
        anchor = _day(value)
        out.update(date_from=(anchor - timedelta(days=120)).isoformat(), date_to=anchor.isoformat(),
                   label=f"截至 {anchor.isoformat()}")
        return out
    fiscal = _FY_PERIOD_RE.search(text)
    if fiscal:
        year = fiscal.group(1)
        year4 = year if len(year) == 4 else f"20{year}"
        quarter = int(fiscal.group(2))
        out.update(label=f"Q{quarter} {year4}")
        out["tokens"] = [f"Q{quarter} {year4}", f"Q{quarter}", f"{_QUARTER_WORDS[quarter]} quarter",
                         f"FY{year4[-2:]}", f"fiscal {year4}"]
        return out
    out["label"] = text
    return out


def _item_key(data_item: str | None) -> str:
    text = re.sub(r"[^a-z0-9]+", "_", str(data_item or "").lower()).strip("_")
    if text.startswith("check_"):
        text = text[len("check_"):]
    for key in DATA_ITEM_TERMS:
        if text == key or text.startswith(key) or key in text.split("_"):
            return key
    return text


def data_item_terms(data_item: str | None) -> list[str]:
    """The words a passage uses for the data item (its own words first)."""

    from .forecast_driver_retrieval import terms as driver_terms
    explicit = driver_terms(data_item)
    if explicit is not None:
        return explicit
    raw = str(data_item or "").replace("_", " ").strip()
    if raw.lower().startswith("check:"):
        raw = raw[len("check:"):]
    terms = [raw] if raw else []
    for term in DATA_ITEM_TERMS.get(_item_key(data_item), ()):
        if term.lower() not in {t.lower() for t in terms}:
            terms.append(term)
    return terms[:4]


def build_queries(request: Mapping[str, Any], identity: Mapping[str, Any] | None = None, *,
                  now: str | None = None) -> list[str]:
    """Two or three phrasings for the request, deterministic in need/subject/period.

    The conventions are :mod:`alphaengine_query_forms`': the issuer's name and
    the quarter the way a vendor titles the document first ("Accenture Q4
    2026"), then the longer phrasings.  For a data item each phrasing names
    the item a different way (``bookings`` / ``new bookings`` /
    ``book-to-bill``) so one wording that misses does not end the rung.
    """

    identity = identity or {}
    need = request.get("need") or {}
    name = str(identity.get("name") or identity.get("ticker") or request.get("subject") or "").strip()
    ticker = str(identity.get("ticker") or name).strip()
    window = period_window(request.get("period"), now=now)
    label = window.get("label") or ""
    period_words = " ".join(window["tokens"][:1]) if window["tokens"] else ""
    kind = need.get("kind")
    out: list[str] = []
    if kind == "document":
        kinds = list(need.get("doc_kinds") or [])
        primary = kinds[0] if kinds else "document"
        if primary == "transcript":
            out = [f"{name} {period_words or label} earnings call", f"{name} earnings call transcript",
                   f"{ticker} quarterly results conference call"]
        elif primary in ("sec_exhibit", "sec_8k"):
            out = [f"{name} results press release {period_words}", f"{ticker} 8-K exhibit 99.1 earnings release",
                   f"{name} reports quarter results"]
        elif primary in ("sec_10k", "sec_10q"):
            form = "10-K annual report" if primary == "sec_10k" else "10-Q quarterly report"
            out = [f"{name} {form}", f"{ticker} {form} {period_words}"]
        elif primary in ("broker_report", "sales_note"):
            out = [f"{ticker} analyst research report", f"{name} equity research report",
                   f"{name} rating price target"]
        elif primary == "expert_call":
            out = [f"{name} expert call", f"{name} customer channel check"]
        else:
            out = [f"{name} {primary.replace('_', ' ')}", f"{ticker} {primary.replace('_', ' ')}"]
    elif kind in ("data", "model"):
        from .forecast_driver_retrieval import request_terms
        terms = request_terms(request) or data_item_terms(need.get("data_item"))
        first = terms[0] if terms else str(need.get("data_item") or "")
        out = [f"{name} {first} {period_words or label}".strip()]
        for term in terms[1:3]:
            out.append(f"{ticker} {term} {period_words}".strip())
        if len(out) < 2:
            out.append(f"{ticker} {first}")
    else:
        from .material_retrieval import contract
        retrieval = contract(request)
        if retrieval is not None:
            form_words = " ".join(k.replace("sec_", "").replace("10k", "10-K").replace("10q", "10-Q").replace("8k", "8-K")
                                  for k in retrieval["doc_kinds"])
            # Connector searches retain the issuer/form, while local passage
            # matching below uses each target's original-language terms.
            out = [f"{name} {form_words} {g['terms'][0]} {period_words or label}".strip()
                   for g in retrieval["groups"]]
            return list(dict.fromkeys(out))[:3]
        question = str(need.get("question") or "").strip()
        out = [question[:300], f"{name} {question[:200]}"]
        keywords = [word for word in re.findall(r"[A-Za-z][A-Za-z0-9\-]{3,}|[一-鿿]{2,}", question)][:6]
        if keywords:
            out.append(f"{ticker} " + " ".join(keywords))
    seen: list[str] = []
    for item in out:
        text = re.sub(r"\s+", " ", item).strip()[:390]
        if text and text.lower() not in {s.lower() for s in seen}:
            seen.append(text)
    return seen[:3]


# ---------------------------------------------------------------------------
# identity, mission, acceptance
# ---------------------------------------------------------------------------


def identity_for(state_dir: str | Path, subject: str) -> dict[str, Any]:
    """Ticker, name and aliases of a request's subject (read-only)."""

    from .material_sufficiency import company_identity

    with _core(state_dir) as connection:
        try:
            return company_identity(connection, subject, state_dir)
        except Exception:  # noqa: BLE001 - an unreadable Core is a subject with no aliases
            return {"company_ref": subject, "ticker": None, "name": subject, "aliases": [], "_patterns": []}


def mission_universe(state_dir: str | Path) -> dict[str, dict[str, Any]]:
    """``company_ref -> universe member`` of the active mission (read-only)."""

    from .material_sufficiency import mission

    with _core(state_dir) as connection:
        try:
            record = mission(connection)
        except sqlite3.Error:
            record = None
    return {str(member.get("company_ref")): dict(member) for member in (record or {}).get("universe") or ()
            if isinstance(member, Mapping)}


def _is_company(subject: str) -> bool:
    return not str(subject).startswith(("industry:", "industry-"))


def _unperioded_filing_answer(request: Mapping[str, Any]) -> bool:
    from .material_retrieval import filing_kinds
    return (not str(request.get("period") or "").strip()
            and (request.get("need") or {}).get("kind") == "answer"
            and bool(filing_kinds(request)))


def _window_with_acceptance(request: Mapping[str, Any], *, now: str | None) -> tuple[str | None, str | None]:
    window = period_window(request.get("period"), now=now)
    acceptance = request.get("acceptance") or {}
    today = (_parse_ts(now) or datetime.now(timezone.utc)).date()
    date_from, date_to = window["date_from"], window["date_to"]
    if _unperioded_filing_answer(request):
        # A filing question without a period is not a rolling publication
        # year. Explicit bounds below and fiscal/version proofs still apply.
        date_from, date_to = None, None
    after = _day(acceptance.get("published_after"))
    if after is not None:
        candidate = (after + timedelta(days=1)).isoformat()
        date_from = max(date_from or candidate, candidate)
    if acceptance.get("max_age_days"):
        candidate = (today - timedelta(days=int(acceptance["max_age_days"]))).isoformat()
        date_from = max(date_from or candidate, candidate)
    if acceptance.get("date_from"):
        date_from = max(date_from or str(acceptance["date_from"])[:10], str(acceptance["date_from"])[:10])
    if acceptance.get("date_to"):
        date_to = min(date_to or str(acceptance["date_to"])[:10], str(acceptance["date_to"])[:10])
    return date_from, date_to


def _doc_item(doc: Mapping[str, Any], *, source: str | None = None) -> dict[str, Any]:
    return {key: doc.get(key) for key in ("document_ref", "kind", "title", "house", "date", "period")
            if doc.get(key) is not None} | ({"source": source} if source else {})


def _passage_item(passage: Mapping[str, Any]) -> dict[str, Any]:
    return {"document_ref": passage.get("document_ref"), "kind": passage.get("kind"),
            "title": passage.get("title"), "date": passage.get("date"),
            "passage_ref": passage.get("passage_ref"), "snippet": str(passage.get("snippet") or "")[:240]}


def evaluate_acceptance(state_dir: str | Path, request: Mapping[str, Any], *,
                        identity: Mapping[str, Any] | None = None, kinds: Sequence[str] | None = None,
                        now: str | None = None) -> dict[str, Any]:
    """Does the evidence index now hold what the request accepts?

    Returns ``{"met", "partial", "items", "reason"}``.  The rules, by need:

    ``document``  ``min_documents`` (default 1) indexed documents of the
                  need's kinds about the subject (``about_company``, default
                  on) dated inside the period window (``published_after``,
                  ``max_age_days`` narrow it); fewer than required but at
                  least one is ``partial``.
    ``data``      a passage about the subject that names the data item (any
                  of its phrasings), carries a number, and is dated inside
                  the period window or names the period; such a passage
                  outside the window is ``partial``.
    ``answer``    ``min_passages`` (default 2) passages covering at least
                  ``min_coverage`` (default 0.5) of the question's terms.
    ``model``     never met here -- a model is a lane's output (``LANE_OWNED``).

    ``kinds`` restricts the evidence considered (the sales-notes rung reads
    only sales notes).  Read-only; a blank workspace is ``met: False``.
    """

    from . import evidence_index
    from .material_sufficiency import BROKER_ABOUTNESS, aboutness

    need = request.get("need") or {}
    acceptance = request.get("acceptance") or {}
    subject = str(request.get("subject") or "")
    company = subject if _is_company(subject) else None
    identity = identity if identity is not None else identity_for(state_dir, subject)
    date_from, date_to = _window_with_acceptance(request, now=now)
    kind = need.get("kind")
    if acceptance.get("numeric_contract_ref") == "directed-document-numeric:0.1":
        # Related passages are acquisition inputs, not a verified numeric answer.
        # Only the source-bound Figure/canonical Claim executor closes this request.
        return {"met": False, "partial": False, "items": [],
                "reason": "仍需从原文核验所缺数字，并形成正式研究结论；目前取得的相关段落尚未满足这项需求"}
    if kind == "model":
        return {"met": False, "partial": False, "items": [], "reason": "模型数据由预测模型更新，不从资料中检索"}
    if kind == "document" and acceptance.get("unit_proof_rule_ref"):
        from .sec_filing_units import unit_requirement_status
        return unit_requirement_status(state_dir, request)
    if kind == "document" and acceptance.get("document_ref"):
        # F's event body: this exact document, read into the index.
        ref = str(acceptance["document_ref"])
        try:
            passages = evidence_index.passages_for_document(state_dir, ref, limit=1)
        except Exception:  # noqa: BLE001
            passages = []
        if passages:
            return {"met": True, "partial": False, "items": [_doc_item(passages[0])],
                    "reason": "事件正文已读入全文证据索引"}
        return {"met": False, "partial": False, "items": [], "reason": "事件正文还没有读入全文证据索引"}
    if kind == "document":
        wanted = [k for k in (need.get("doc_kinds") or ()) if not kinds or k in kinds]
        if not wanted:
            return {"met": False, "partial": False, "items": [], "reason": "这一级来源不提供这类文档"}
        try:
            docs = evidence_index.documents(state_dir, company_ref=company, kinds=wanted, date_from=date_from,
                                            date_to=date_to, limit=200)
        except Exception as exc:  # noqa: BLE001 - a broken index reads as nothing found
            return {"met": False, "partial": False, "items": [], "reason": f"索引读取失败：{type(exc).__name__}"}
        if company and acceptance.get("about_company", True):
            threshold = float(acceptance.get("aboutness", BROKER_ABOUTNESS))
            docs = [doc for doc in docs if aboutness(doc, identity) >= threshold]
        seen, unique = set(), []
        for doc in docs:
            if doc.get("text_sha256") in seen:
                continue
            seen.add(doc.get("text_sha256"))
            unique.append(doc)
        needed = max(1, int(acceptance.get("min_documents") or 1))
        items = [_doc_item(doc) for doc in unique[:max(needed, 5)]]
        if len(unique) >= needed:
            return {"met": True, "partial": False, "items": items,
                    "reason": f"索引中有 {len(unique)} 份符合要求的文档"}
        return {"met": False, "partial": bool(unique), "items": items,
                "reason": (f"只有 {len(unique)} 份，少于要求的 {needed} 份" if unique else "索引中没有符合要求的文档")}
    queries = build_queries(request, identity, now=now)
    if kind == "data":
        from .forecast_driver_retrieval import request_terms
        terms = [t.lower() for t in (request_terms(request) or data_item_terms(need.get("data_item")))]
        tokens = [t.lower() for t in period_window(request.get("period"), now=now)["tokens"]]
        good, near = [], []
        for query in queries:
            try:
                found = evidence_index.evidence_search(state_dir, query, company_ref=company, kinds=kinds or None,
                                                       limit=20)
            except Exception:  # noqa: BLE001
                continue
            for item in found.get("results") or ():
                text = str(item.get("text") or "").lower()
                if not any(term in text for term in terms) or not _NUMBER_RE.search(text):
                    continue
                dated = str(item.get("date") or "")
                in_window = bool(dated) and (not date_from or dated >= date_from) and (not date_to or dated <= date_to)
                names_period = any(token in text for token in tokens)
                (good if (in_window or names_period) else near).append(item)
            if good:
                break
        needed = max(1, int(acceptance.get("min_passages") or 1))
        unique = list({item["passage_ref"]: item for item in good}.values())
        if len(unique) >= needed:
            return {"met": True, "partial": False, "items": [_passage_item(p) for p in unique[:5]],
                    "reason": "索引中有段落给出了该数据项（含数字与期间）"}
        partial = unique or near
        return {"met": False, "partial": bool(partial), "items": [_passage_item(p) for p in partial[:5]],
                "reason": "只找到其他期间提到该数据项的段落" if partial else "索引中没有给出该数据项的段落"}
    from .material_retrieval import contract, issuer_matches, phrase_matches
    retrieval = contract(request)
    if retrieval is not None:
        required_kinds = retrieval["doc_kinds"]
        allowed = [k for k in required_kinds if not kinds or k in kinds] if required_kinds else list(kinds or [])
        if required_kinds and not allowed:
            return {"met": False, "partial": False, "items": [], "reason": "这个来源不提供要求的申报类型",
                    "missing_targets": [g["target"] for g in retrieval["groups"]]}
        hits, target_hits = {}, {}
        minimum = float(acceptance.get("min_coverage") or 0.5)
        for group in retrieval["groups"]:
            collected = set()
            for term in group["terms"]:
                found = evidence_index.evidence_search(state_dir, term, company_ref=company,
                    kinds=allowed or None, date_from=date_from, date_to=date_to, limit=20)
                for item in found.get("results") or ():
                    if (float((item.get("score_parts") or {}).get("coverage") or 0) < minimum
                            or not phrase_matches(item.get("text") or "", term)
                            or not issuer_matches(item, company, identity)
                            or (retrieval.get("selected_document") is not None and
                                item.get("document_ref") != retrieval["selected_document"]["document_ref"])):
                        continue
                    ref = item["passage_ref"]
                    collected.add(ref); hits.setdefault(ref, item)
            target_hits[group["target"]] = sorted(collected)
        missing = [key for key, refs in target_hits.items() if not refs]
        needed = max(1, int(acceptance.get("min_passages") or 2))
        met = retrieval["scope_complete"] and not missing and len(hits) >= needed
        return {"met": met, "partial": bool(hits) and not met,
                "items": [_passage_item(p) | {"document_sha256": p.get("document_sha256")}
                          for p in list(hits.values())[:12]],
                "target_passages": target_hits, "missing_targets": missing,
                "retrieval_contract_hash": retrieval["content_hash"],
                "reason": ("各项所需原文段落已取得；研究答案仍由后续阅读核验" if met else
                           "仍缺原文项目：" + "、".join(missing) if missing else
                           f"已覆盖各项，但只有 {len(hits)} 段原文，少于要求的 {needed} 段" if len(hits) < needed else
                           "已找到相关原文；选定原文的确切版本仍需由正式阅读核验" if retrieval.get("selected_document") else
                           "已找到相关原文；问题指定的财政期间仍需核对" if retrieval["period_unproved"] else
                           "已找到问题中的财务项目原文；复合问题的其他要求仍需逐项核对")}


    from .material_retrieval import filing_kinds
    required_filings = filing_kinds(request)
    if required_filings:
        kinds = [k for k in required_filings if not kinds or k in kinds]
        if not kinds:
            return {"met": False, "partial": False, "items": [], "reason": "这个来源不提供要求的申报类型"}
    # answer
    min_coverage = float(acceptance.get("min_coverage") or 0.5)
    hits: dict[str, dict[str, Any]] = {}
    for query in queries:
        try:
            found = evidence_index.evidence_search(state_dir, query, company_ref=company, kinds=kinds or None,
                                                   date_from=date_from, date_to=date_to, limit=10)
        except Exception:  # noqa: BLE001
            continue
        for item in found.get("results") or ():
            if (float((item.get("score_parts") or {}).get("coverage") or 0) >= min_coverage
                    and (not required_filings or issuer_matches(item, company, identity))):
                hits.setdefault(item["passage_ref"], item)
    needed = max(1, int(acceptance.get("min_passages") or 2))
    items = [_passage_item(p) for p in list(hits.values())[:6]]
    period_unproved = bool(re.search(r"\bFY\s*\d{2,4}\b|\bQ[1-4]\b|20\d{2}|第[一二三四1234]季度|[一二三四]季度",
                                     str(need.get("question") or ""), re.I))
    if len(hits) >= needed and not period_unproved and len(required_filings) <= 1:
        return {"met": True, "partial": False, "items": items, "reason": f"索引中有 {len(hits)} 段相关材料"}
    return {"met": False, "partial": bool(hits), "items": items,
            "reason": ("已找到相关段落，但问题指定的财政期间或多种申报类型仍需核对"
                           if hits and (period_unproved or len(required_filings) > 1) else
                           "相关材料不足" if hits else "索引中没有相关材料")}


# ---------------------------------------------------------------------------
# rungs
# ---------------------------------------------------------------------------


@dataclass
class RungResult:
    """What one rung of the ladder came to."""

    outcome: str
    query: Any = None
    items: list[dict[str, Any]] = field(default_factory=list)
    calls: int = 0
    cost_micros: int = 0
    detail: str | None = None
    manifests: list[str] = field(default_factory=list)


@dataclass
class RungContext:
    state_dir: Path
    request: Mapping[str, Any]
    identity: Mapping[str, Any]
    queries: list[str]
    now: str
    previous: Mapping[str, Any] | None = None


class Rung(Protocol):
    name: str
    costed: bool

    def applies(self, request: Mapping[str, Any]) -> bool: ...

    def run(self, ctx: RungContext) -> RungResult: ...

    def settle(self, ctx: RungContext) -> RungResult: ...


def _acceptance_result(ctx: RungContext, *, kinds: Sequence[str] | None = None,
                       query: Any = None) -> RungResult:
    verdict = evaluate_acceptance(ctx.state_dir, ctx.request, identity=ctx.identity, kinds=kinds, now=ctx.now)
    outcome = "accepted" if verdict["met"] else ("partial" if verdict["partial"] else "empty")
    return RungResult(outcome, query=query if query is not None else ctx.queries, items=verdict["items"],
                      detail=verdict["reason"])


class IndexedCorpusRung:
    """Rung 1: topic retrieval over everything already acquired and indexed."""

    name = "indexed_corpus"
    costed = False

    def applies(self, request: Mapping[str, Any]) -> bool:
        return (request.get("need") or {}).get("kind") != "model"

    def run(self, ctx: RungContext) -> RungResult:
        return _acceptance_result(ctx)

    settle = run


class SalesNotesRung:
    """Rung 2: the sales notes -- indexed first, then the raw notes index.

    The notes a human already receives are read by the feed lane, which
    acquires every note it attributes to a covered company.  A note in the raw
    index that matches the request but is not indexed yet is therefore one
    the feed lane will deliver: the rung waits for it (``dispatched``) rather
    than reading the mail itself.
    """

    name = "sales_notes"
    costed = False
    KINDS = ("sales_note",)

    def __init__(self, digest_dir: Callable[[Path], Path | None] | None = None) -> None:
        self._digest_dir = digest_dir or (lambda state: state / "feeds" / "market-digest-output")

    def applies(self, request: Mapping[str, Any]) -> bool:
        need = request.get("need") or {}
        if need.get("kind") == "document":
            return bool({"broker_report", "sales_note"} & set(need.get("doc_kinds") or ()))
        return need.get("kind") in ("data", "answer")

    def _as_sales_note(self, request: Mapping[str, Any]) -> dict[str, Any]:
        need = dict(request.get("need") or {})
        if need.get("kind") == "document":
            need["doc_kinds"] = ["sales_note"]
        return {**request, "need": need}

    def run(self, ctx: RungContext) -> RungResult:
        local = _acceptance_result(RungContext(ctx.state_dir, self._as_sales_note(ctx.request), ctx.identity,
                                               ctx.queries, ctx.now), kinds=self.KINDS)
        if local.outcome == "accepted":
            return local
        raw = self._raw_matches(ctx)
        if raw:
            return RungResult("dispatched", query=ctx.queries, items=raw[:10],
                              detail=f"销售快报原始目录里有 {len(raw)} 份匹配的快报，等待资料流入库")
        return local

    def settle(self, ctx: RungContext) -> RungResult:
        local = _acceptance_result(RungContext(ctx.state_dir, self._as_sales_note(ctx.request), ctx.identity,
                                               ctx.queries, ctx.now), kinds=self.KINDS)
        if local.outcome == "accepted":
            return local
        if _age_hours(ctx.previous, ctx.now) >= RUNG_WAIT_HOURS:
            return RungResult(local.outcome, query=ctx.queries, items=local.items,
                              detail="匹配的快报在等待期内没有入库")
        return RungResult("waiting")

    def _raw_matches(self, ctx: RungContext) -> list[dict[str, Any]]:
        from .material_sufficiency import mentions

        directory = self._digest_dir(ctx.state_dir)
        if directory is None or not Path(directory).is_dir():
            return []
        date_from, _ = _window_with_acceptance(ctx.request, now=ctx.now)
        today = (_parse_ts(ctx.now) or datetime.now(timezone.utc)).date()
        since = date_from or (today - timedelta(days=90)).isoformat()
        try:
            from .sales_notes_core import enumerate_notes

            notes, _ = enumerate_notes(directory, since=since[:10], until=today.isoformat())
        except Exception:  # noqa: BLE001 - an unreadable mail directory is no notes
            return []
        need = ctx.request.get("need") or {}
        terms = [t.lower() for t in data_item_terms(need.get("data_item"))] if need.get("kind") == "data" else []
        out = []
        for note in notes:
            subject = str(note.get("subject") or note.get("title") or "")
            if not mentions(ctx.identity, subject):
                continue
            if terms and not any(term in subject.lower() for term in terms):
                continue
            out.append({"document_ref": note.get("note_id") or note.get("document_ref"), "kind": "sales_note",
                        "title": subject[:200], "date": str(note.get("sent_at") or "")[:10]})
        return out


def _age_hours(previous: Mapping[str, Any] | None, now: str) -> float:
    started = _parse_ts((previous or {}).get("created_at"))
    current = _parse_ts(now)
    if started is None or current is None:
        return 0.0
    return (current - started).total_seconds() / 3600.0


class TargetedSourceRung:
    """Rungs 3-6: ask the source's own coordinator to search for this company now.

    ``run`` checks the source's health hold (batch aa) and maps the need to a
    spec of the source's plan, then appends a targeted query; the coordinator
    drains it on its next tick through its own governed launcher, mission
    grant and call window, and records what it did.  ``settle`` reads that
    record and, once the search and its acquisitions have settled,
    re-evaluates acceptance over the index.
    """

    costed = True

    def __init__(self, name: str) -> None:
        if name not in ("alphaengine", "sec_edgar", "guidepoint", "web"):
            raise ValueError(f"no targeted rung for {name}")
        self.name = name
        self.source_ref = SOURCE_REFS[name]

    # -- what the source can be asked for ------------------------------------
    def spec_for(self, request: Mapping[str, Any]) -> str | None:
        need = request.get("need") or {}
        kinds = list(need.get("doc_kinds") or ())
        kind = need.get("kind")
        from .material_retrieval import filing_kinds
        required_filings = filing_kinds(request) if kind == "answer" else []
        if required_filings:
            if self.name == "sec_edgar":
                return next((SEC_SPECS[k] for k in required_filings if k in SEC_SPECS), None)
            if self.name in {"alphaengine", "guidepoint"}:
                return None
        if self.name == "alphaengine":
            if kind == "document":
                return next((ALPHAENGINE_SPECS[k] for k in kinds if k in ALPHAENGINE_SPECS), None)
            if kind == "data":
                return ("sell-side-reports" if _item_key(need.get("data_item")) == "consensus_estimates"
                        else "earnings-call-transcripts")
            if kind == "answer":
                return "earnings-call-transcripts"
            return None
        if self.name == "sec_edgar":
            if kind == "document":
                return next((SEC_SPECS[k] for k in kinds if k in SEC_SPECS), None)
            if kind == "data":
                return "current-report-8k"
            return None
        if self.name == "guidepoint":
            if kind == "document":
                return "expert" if "expert_call" in kinds else None
            return "expert" if kind == "answer" else None
        # web
        if kind == "model":
            return None
        return "web"

    def applies(self, request: Mapping[str, Any]) -> bool:
        if (request.get("need") or {}).get("kind") == "model":
            return False
        return self.spec_for(request) is not None or (
            self.name == "sec_edgar" and (request.get("need") or {}).get("kind") == "data")

    def estimate_calls(self, request: Mapping[str, Any]) -> int:
        return CALL_ESTIMATES[self.name]

    # -- the ladder's calls --------------------------------------------------
    def run(self, ctx: RungContext) -> RungResult:
        request = ctx.request
        need = request.get("need") or {}
        if self.name == "sec_edgar" and need.get("kind") == "data":
            facts = xbrl_facts(ctx.state_dir, str(request.get("subject") or ""), need.get("data_item"),
                               request.get("period"), now=ctx.now)
            if facts["met"]:
                return RungResult("accepted", query=facts["query"], items=facts["items"],
                                  detail="Core 里已申报的 XBRL 财务数据给出了该数据项")
        hold = _health_hold(ctx.state_dir, self.source_ref)
        if hold is not None:
            return RungResult("health_hold", query=ctx.queries,
                              detail=f"{self.source_ref} 登录失效暂停至 {hold.get('hold_until') or '探测通过'}")
        spec_ref = self.spec_for(request)
        if spec_ref is None:
            return RungResult("not_applicable", detail="这个来源没有对应这类材料的检索")
        subject = str(request.get("subject") or "")
        if not _is_company(subject):
            return RungResult("not_applicable", detail="行业问题没有对应公司，这个来源无法按公司检索")
        query = enqueue_targeted_query(ctx.state_dir, request_id=str(request["id"]), source=self.name,
                                       company_ref=subject, spec_ref=spec_ref, phrasings=ctx.queries,
                                       period=request.get("period"), now=ctx.now)
        calls = self.estimate_calls(request) if query.get("write_status") == "fresh" else 0
        cost = int(USD_PER_CALL.get(self.name, Decimal(0)) * calls * 1_000_000)
        return RungResult("dispatched", query={"spec_ref": spec_ref, "phrasings": ctx.queries,
                                               "targeted_query": query["query_id"]},
                          calls=calls, cost_micros=cost,
                          detail=f"已请求 {self.source_ref} 按 {spec_ref} 为这家公司立即检索")

    def settle(self, ctx: RungContext) -> RungResult:
        query_id = _targeted_query_id(ctx.previous)
        status = targeted_query_status(ctx.state_dir, query_id) if query_id else None
        age = _age_hours(ctx.previous, ctx.now)
        queue_wait = None
        if (self.name == "sec_edgar" and age >= RUNG_WAIT_HOURS
                and (status is None or status.get("status") is None
                     or (status.get("status") == "deferred" and status.get("kind") == "busy"))):
            from .material_source_queue_recovery import configured_wait
            queue_wait = configured_wait(ctx.state_dir, ctx.request, query_id, now=ctx.now)
        if queue_wait is not None:
            return RungResult("waiting", detail=("SEC 检索已获授权，仍在等待协调器执行"
                if queue_wait["status"] == "configured" else "SEC 检索配置暂未核实，等待下一轮复核"))
        if status is None or status.get("status") is None:
            if age >= RUNG_WAIT_HOURS:
                return RungResult("not_connected", query=ctx.queries,
                                  detail=f"{RUNG_WAIT_HOURS} 小时内没有协调器执行这个检索（本环境可能未接入该来源）")
            return RungResult("waiting")
        state = status["status"]
        reason = str(status.get("reason") or "")
        if state == "refused":
            return RungResult(_refusal_outcome(status), query=ctx.queries, detail=reason[:300] or None)
        if state == "deferred":
            if status.get("kind") == "health_hold":
                return RungResult("health_hold", query=ctx.queries, detail=reason[:300] or None)
            if age >= RUNG_WAIT_HOURS:
                return RungResult("rate_limited" if status.get("kind") == "budget" else "not_connected",
                                  query=ctx.queries, detail=reason[:300] or None)
            return RungResult("waiting")
        if state == "failed":
            return RungResult("error", query=ctx.queries, detail=reason[:300] or "检索失败")
        verdict = evaluate_acceptance(ctx.state_dir, ctx.request, identity=ctx.identity, now=ctx.now)
        if verdict["met"]:
            return RungResult("accepted", query=ctx.queries, items=verdict["items"], detail=verdict["reason"])
        if self.name=="sec_edgar" and query_id and age < RUNG_WAIT_HOURS:
            from .material_targeted_sec import archive_pending_for_material
            if archive_pending_for_material(ctx.state_dir,query_id):
                return RungResult("waiting",detail="SEC 原始索引已保存，等待其强绑定归档页正常执行")
        settled = _dispatch_settled(ctx.state_dir, status, now=ctx.now)
        # A finished search can leave original documents queued behind the
        # source's daily allowance. The search grace period is not an expiry
        # for that acquisition. Keep the original request deadline and never
        # label a pending download as a search that found nothing.
        deadline = _parse_ts(ctx.request.get("deadline"))
        current = _parse_ts(ctx.now)
        acquisition_wait_open = (current < deadline if current and deadline
                                 else age < mr.DEFAULT_DEADLINE_HOURS)
        if not settled and acquisition_wait_open:
            return RungResult("waiting", detail="检索已执行，等待候选原文取回并核对；无需重复检索")
        return RungResult("partial" if verdict["partial"] else "empty", query=ctx.queries,
                          items=verdict["items"], detail=verdict["reason"])


def _refusal_outcome(status: Mapping[str, Any]) -> str:
    kind = str(status.get("kind") or "")
    if kind in ("credentials", "governance", "not_authorized"):
        return "needs_credentials"
    if kind == "health_hold":
        return "health_hold"
    if kind == "budget":
        return "rate_limited"
    if kind in ("unconfigured", "no_plan", "not_in_plan", "no_spec"):
        return "not_connected"
    return "error"


def _targeted_query_id(previous: Mapping[str, Any] | None) -> str | None:
    query = (previous or {}).get("query")
    if isinstance(query, Mapping):
        return query.get("targeted_query")
    return None


class LaneOwnedRung:
    """Data another lane produces (consensus, our model, prices): satisfied when it has."""

    costed = False

    def __init__(self, name: str) -> None:
        self.name = name
        self.check = LANE_OWNED[name]

    def applies(self, request: Mapping[str, Any]) -> bool:
        return _is_company(str(request.get("subject") or ""))

    def run(self, ctx: RungContext) -> RungResult:
        from . import material_sufficiency as ms

        subject = str(ctx.request.get("subject") or "")
        as_of = ms.as_of_day((ctx.now or "")[:10] or None)
        try:
            info = ms.facts(ctx.state_dir, subject, as_of)
            item = ms._check(self.check, {}, state_dir=ctx.state_dir, company_ref=subject, as_of=as_of, info=info)
        except Exception as exc:  # noqa: BLE001
            return RungResult("waiting_on_lane", detail=f"检查失败：{type(exc).__name__}")
        if item.get("satisfied"):
            return RungResult("accepted", items=[{"ref": ref, "kind": self.check} for ref in item.get("evidence") or ()
                                                  if ref][:5], detail=item.get("detail"))
        return RungResult("waiting_on_lane", detail=item.get("detail"))

    settle = run


def default_rungs() -> dict[str, Any]:
    """The production ladder, one rung per source name."""

    rungs: dict[str, Any] = {"indexed_corpus": IndexedCorpusRung(), "sales_notes": SalesNotesRung()}
    for name in ("alphaengine", "sec_edgar", "guidepoint", "web"):
        rungs[name] = TargetedSourceRung(name)
    for name in LANE_OWNED:
        rungs[name] = LaneOwnedRung(name)
    return rungs


# ---------------------------------------------------------------------------
# XBRL company facts (SEC rung, data items)
# ---------------------------------------------------------------------------


def xbrl_facts(state_dir: str | Path, company_ref: str, data_item: str | None, period: str | None, *,
               now: str | None = None) -> dict[str, Any]:
    """Statement lines Core already holds for the data item and period (read-only)."""

    key = _item_key(data_item)
    concepts = list(XBRL_CONCEPTS.get(key, ()))
    terms = data_item_terms(data_item)
    window = period_window(period, now=now)
    query = {"concepts": concepts, "labels": terms[:2], "period_end": window.get("quarter_end")}
    with _core(state_dir) as connection:
        rows = []
        if concepts:
            rows = _rows(connection,
                         "SELECT l.line_id, l.concept, l.label, l.period_start, l.period_end, l.value, l.unit, "
                         "f.form, f.accession, f.filed FROM coverage_mission_statement_lines l JOIN "
                         "coverage_mission_statement_filings f ON f.ingest_id=l.ingest_id WHERE f.company_ref=? "
                         f"AND l.dimension_axis IS NULL AND l.concept IN ({','.join('?' * len(concepts))}) "
                         "ORDER BY l.period_end DESC LIMIT 40", (company_ref, *concepts))
        if not rows and terms:
            rows = _rows(connection,
                         "SELECT l.line_id, l.concept, l.label, l.period_start, l.period_end, l.value, l.unit, "
                         "f.form, f.accession, f.filed FROM coverage_mission_statement_lines l JOIN "
                         "coverage_mission_statement_filings f ON f.ingest_id=l.ingest_id WHERE f.company_ref=? "
                         "AND l.dimension_axis IS NULL AND lower(l.label) LIKE ? ORDER BY l.period_end DESC LIMIT 40",
                         (company_ref, f"%{terms[0].lower()}%"))
    wanted_end = window.get("quarter_end")
    matched = [dict(row) for row in rows if row["value"] not in (None, "")
               and (wanted_end is None or str(row["period_end"])[:10] == wanted_end)]
    items = [{"ref": row["line_id"], "kind": "xbrl_fact", "concept": row["concept"], "label": row["label"],
              "period_end": row["period_end"], "value": row["value"], "unit": row["unit"], "form": row["form"],
              "accession": row["accession"]} for row in matched[:5]]
    return {"met": bool(items), "items": items, "query": query}


# ---------------------------------------------------------------------------
# targeted queries (drained by the coordinators)
# ---------------------------------------------------------------------------


def ensure_schema(state_dir: str | Path) -> None:
    with _writer(state_dir):
        pass


class MaterialAcquisitionLedger:
    """The sidecar's owner for a deploy rehearsal: applies both schemas to one file.

    ``material-requests.sqlite`` holds the MaterialRequest ledger
    (:mod:`material_requests`, its schema inline) and this service's tables
    (``material_acquisition_schema.sql``).  Constructing this applies both,
    exactly as the first write of either module would.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self.connection.executescript(mr._SCHEMA)
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    def close(self) -> None:
        self.connection.close()


def enqueue_targeted_query(state_dir: str | Path, *, request_id: str, source: str, company_ref: str,
                           spec_ref: str | None, phrasings: Sequence[str] = (), period: str | None = None,
                           now: str | None = None) -> dict[str, Any]:
    """Append one targeted query; idempotent per (request, source, round start)."""

    stamp = now or _now()
    source_ref = SOURCE_REFS[source]
    for existing in pending_targeted_queries(state_dir, source_ref, limit=500):
        # A query the coordinator has not answered yet is still the request's
        # query: a rung retried after a hold must not queue a second search.
        if existing.get("request_id") == request_id and existing.get("spec_ref") == spec_ref:
            return {**existing, "write_status": "existing"}
    record = {"schema_version": SCHEMA_VERSION, "request_id": request_id, "source": source,
              "source_ref": source_ref, "company_ref": company_ref, "spec_ref": spec_ref,
              "phrasings": list(phrasings)[:3], "period": period, "created_at": stamp}
    query_id = "material-targeted-query:" + _sha({k: record[k] for k in (
        "request_id", "source", "company_ref", "spec_ref", "created_at")})[:32]
    with _writer(state_dir) as connection:
        connection.execute(
            "INSERT OR IGNORE INTO material_targeted_queries(query_id,request_id,source,source_ref,company_ref,"
            "spec_ref,record_json,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (query_id, request_id, source, source_ref, company_ref, spec_ref, _canonical(record), stamp))
    return {"query_id": query_id, **record, "write_status": "fresh"}


def pending_targeted_queries(state_dir: str | Path, source_ref: str, *, limit: int = 5, deadline: float | None = None) -> list[dict[str, Any]]:
    """What a coordinator of ``source_ref`` still owes: queries with no launch/refusal yet, oldest first.

    A ``deferred`` query (busy slot, call window full, held source) stays
    pending; the coordinator records why each time it could not run it.
    """

    with _reader(state_dir) as connection:
        rows = _rows(connection,
                     "SELECT q.query_id, q.record_json FROM material_targeted_queries q WHERE q.source_ref=? "
                     "AND NOT EXISTS (SELECT 1 FROM material_targeted_query_events e WHERE e.query_id=q.query_id "
                     "AND e.status IN ('launched','refused','failed')) ORDER BY q.created_at, q.query_id LIMIT ?",
                     (source_ref, max(1, int(limit))), deadline=deadline)
    return [{"query_id": row["query_id"], **json.loads(row["record_json"])} for row in rows]


def record_targeted_event(state_dir: str | Path, query_id: str, *, status: str, kind: str | None = None,
                          reason: str | None = None, dispatch_ref: str | None = None,
                          ticket_ref: str | None = None, extra: Mapping[str, Any] | None = None,
                          now: str | None = None) -> dict[str, Any]:
    """The coordinator's answer to one targeted query (``launched``/``refused``/``deferred``/``failed``)."""

    if status not in ("launched", "refused", "deferred", "failed"):
        raise MaterialAcquisitionError(f"unknown targeted query status {status!r}")
    stamp = now or _now()
    body = {"status": status, "kind": kind, "reason": (reason or "")[:500] or None, "dispatch_ref": dispatch_ref,
            "ticket_ref": ticket_ref, **dict(extra or {})}
    with _writer(state_dir) as connection:
        if connection.execute("SELECT 1 FROM material_targeted_queries WHERE query_id=?",
                              (query_id,)).fetchone() is None:
            raise MaterialAcquisitionError(f"no such targeted query: {query_id}")
        last = connection.execute(
            "SELECT status, record_json FROM material_targeted_query_events WHERE query_id=? "
            "ORDER BY event_id DESC LIMIT 1", (query_id,)).fetchone()
        if last is not None and last["status"] == status == "deferred" \
                and json.loads(last["record_json"]).get("reason") == body["reason"]:
            return {"query_id": query_id, **body, "write_status": "duplicate"}
        connection.execute(
            "INSERT INTO material_targeted_query_events(query_id,status,record_json,created_at) VALUES (?,?,?,?)",
            (query_id, status, _canonical(body), stamp))
    return {"query_id": query_id, **body, "created_at": stamp, "write_status": "fresh"}


def targeted_query_status(state_dir: str | Path, query_id: str) -> dict[str, Any] | None:
    with _reader(state_dir) as connection:
        row = _rows(connection, "SELECT record_json FROM material_targeted_queries WHERE query_id=?", (query_id,))
        if not row:
            return None
        events = _rows(connection, "SELECT status, record_json, created_at FROM material_targeted_query_events "
                                   "WHERE query_id=? ORDER BY event_id", (query_id,))
    record = json.loads(row[0]["record_json"])
    if not events:
        return {**record, "query_id": query_id, "status": None}
    last = events[-1]
    return {**record, **json.loads(last["record_json"]), "query_id": query_id, "status": last["status"],
            "event_at": last["created_at"]}


def _dispatch_settled(state_dir: str | Path, status: Mapping[str, Any], *, now: str) -> bool:
    """Has the targeted search finished and its acquisitions landed (read-only Core)?"""

    dispatch_ref = status.get("dispatch_ref")
    launched_at = _parse_ts(status.get("event_at"))
    current = _parse_ts(now) or datetime.now(timezone.utc)
    if not dispatch_ref:
        # A coordinator that launches without a Core dispatch (a test's fake,
        # or a lane that searches in-process) settles by the grace period.
        return launched_at is not None and current - launched_at >= timedelta(minutes=SETTLE_GRACE_MINUTES)
    with _core(state_dir) as connection:
        rows = _rows(connection, "SELECT * FROM coverage_mission_discovery_dispatches WHERE dispatch_id=?",
                     (dispatch_ref,))
        if not rows:
            return launched_at is not None and current - launched_at >= timedelta(hours=RUNG_WAIT_HOURS)
        dispatch = dict(rows[0])
        if dispatch["status"] == "launched":
            return False
        in_flight = _rows(connection,
                          "SELECT COUNT(*) FROM coverage_mission_discovered_documents d JOIN "
                          "coverage_mission_source_discoveries s ON s.record_id=d.discovery_ref WHERE "
                          "s.company_ref=? AND s.source_ref=? AND s.spec_ref=? AND s.query_hash=? AND "
                          "s.created_at>=? AND d.status IN ('discovered','acquisition_launched')",
                          (dispatch["company_ref"], dispatch["source_ref"], dispatch["spec_ref"],
                           dispatch["query_hash"], dispatch["created_at"]))
        if in_flight and int(in_flight[0][0]):
            return False
        updated = _parse_ts(dispatch.get("updated_at"))
    return updated is None or current - updated >= timedelta(minutes=SETTLE_GRACE_MINUTES)


def _health_hold(state_dir: str | Path, source_ref: str) -> dict[str, Any] | None:
    from .connector_health import HELD, read_record

    record = read_record(state_dir, source_ref)
    return record if record is not None and record.get("status") == HELD else None


# ---------------------------------------------------------------------------
# budget: the material_acquisition pool
# ---------------------------------------------------------------------------


def _approvals(state_dir: str | Path) -> list[dict[str, Any]]:
    """Owner approvals of extra spend (``approve_spend`` on a budget card), read-only."""

    with _core(state_dir) as connection:
        rows = _rows(connection, "SELECT card_ref, created_at FROM needs_human_acknowledgements "
                                 "WHERE choice='approve_spend' ORDER BY sequence")
    out = []
    for row in rows:
        parsed = parse_budget_card_ref(str(row["card_ref"]))
        if parsed is not None:
            out.append({**parsed, "approved_at": row["created_at"]})
    return out


def budget_card_ref(scope: str, key: str, usd_micros: int, calls: int, *,
                    basis: Mapping[str, Any] | None = None) -> str:
    # Approval amounts must cover fractional cents, not round them down.
    cents = (max(0, int(usd_micros)) + 9999) // 10000
    ref = f"material-budget:{scope}:{key}:{_usd(cents * 10000)}:{int(calls)}"
    return ref if basis is None else ref + ":basis:" + _sha(dict(basis))[:16]


def parse_budget_card_ref(ref: str) -> dict[str, Any] | None:
    match = re.fullmatch(r"material-budget:(pool|request):(.+):(\d+\.\d{2}):(\d+)(?::basis:[a-f0-9]{16})?", ref or "")
    if match is None:
        return None
    return {"scope": match.group(1), "key": match.group(2), "usd_micros": _micros(match.group(3)),
            "calls": int(match.group(4)), "card_ref": ref}


def pool_status(state_dir: str | Path, *, now: str | None = None) -> dict[str, Any]:
    """Today's ``material_acquisition`` pool: caps (+ approvals), spend, remaining."""

    from .budget_pools import material_acquisition_caps

    day = (now or _now())[:10]
    caps = material_acquisition_caps(None, state_dir)
    extra_usd = extra_calls = 0
    for approval in _approvals(state_dir):
        if approval["scope"] == "pool" and approval["key"] == day:
            extra_usd += approval["usd_micros"]
            extra_calls += approval["calls"]
    with _reader(state_dir) as connection:
        row = _rows(connection, "SELECT COALESCE(SUM(calls),0), COALESCE(SUM(cost_micros),0) "
                                "FROM material_acquisition_spend WHERE day=?", (day,))
    spent_calls, spent_micros = (int(row[0][0]), int(row[0][1])) if row else (0, 0)
    cap_micros = caps["usd_micros_per_day"] + extra_usd
    cap_calls = caps["calls_per_day"] + extra_calls
    return {"pool": caps["pool"], "day": day, "cap_usd_micros": cap_micros, "cap_calls": cap_calls,
            "spent_usd_micros": spent_micros, "spent_calls": spent_calls,
            "remaining_usd_micros": max(0, cap_micros - spent_micros), "remaining_calls": max(0, cap_calls - spent_calls),
            "approved_extra_usd_micros": extra_usd, "approved_extra_calls": extra_calls,
            "defaulted": caps["defaulted"], "source": caps["source"]}


def request_spend(state_dir: str | Path, request_id: str) -> dict[str, int]:
    with _reader(state_dir) as connection:
        row = _rows(connection, "SELECT COALESCE(SUM(calls),0), COALESCE(SUM(cost_micros),0) "
                                "FROM material_acquisition_spend WHERE request_id=?", (request_id,))
    return {"calls": int(row[0][0]), "cost_micros": int(row[0][1])} if row else {"calls": 0, "cost_micros": 0}


def request_caps(state_dir: str | Path, request: Mapping[str, Any]) -> dict[str, int]:
    from .budget_pools import MATERIAL_ACQUISITION_REQUEST_DEFAULTS

    budget = request.get("budget") or {}
    usd = budget.get("max_usd", MATERIAL_ACQUISITION_REQUEST_DEFAULTS["max_usd"])
    calls = budget.get("max_calls", MATERIAL_ACQUISITION_REQUEST_DEFAULTS["max_calls"])
    try:
        usd_micros, calls = _micros(usd), int(calls)
    except Exception:  # noqa: BLE001 - a malformed request cap is the default
        usd_micros = _micros(MATERIAL_ACQUISITION_REQUEST_DEFAULTS["max_usd"])
        calls = int(MATERIAL_ACQUISITION_REQUEST_DEFAULTS["max_calls"])
    for approval in _approvals(state_dir):
        if approval["scope"] == "request" and approval["key"] == request.get("id"):
            usd_micros += approval["usd_micros"]
            calls += approval["calls"]
    return {"usd_micros": usd_micros, "calls": calls}


def _budget_check(state_dir: str | Path, request: Mapping[str, Any], calls: int, cost_micros: int, *,
                  now: str) -> dict[str, Any] | None:
    """``None`` when the spend fits; else which cap it would cross and what to approve."""

    pool = pool_status(state_dir, now=now)
    if calls > pool["remaining_calls"] or cost_micros > pool["remaining_usd_micros"]:
        return {"scope": "pool", "key": pool["day"],
                "approve_calls": max(calls, 10), "approve_usd_micros": max(cost_micros, _micros("0.50")),
                "spent_calls": pool["spent_calls"], "cap_calls": pool["cap_calls"],
                "spent_usd_micros": pool["spent_usd_micros"], "cap_usd_micros": pool["cap_usd_micros"]}
    caps = request_caps(state_dir, request)
    spent = request_spend(state_dir, str(request["id"]))
    if spent["calls"] + calls > caps["calls"] or spent["cost_micros"] + cost_micros > caps["usd_micros"]:
        return {"scope": "request", "key": str(request["id"]),
                "approve_calls": max(0, spent["calls"] + calls - caps["calls"]),
                "approve_usd_micros": max(0, spent["cost_micros"] + cost_micros - caps["usd_micros"]),
                "spent_calls": spent["calls"], "cap_calls": caps["calls"],
                "spent_usd_micros": spent["cost_micros"], "cap_usd_micros": caps["usd_micros"]}
    return None


def _request_budget_proposal(request: Mapping[str, Any], blocked: Mapping[str, Any], *,
                             source: str, rungs: Mapping[str, Any]) -> dict[str, Any]:
    """Estimate the remaining round once; this is a proposal, never a grant."""
    attempts = _round_attempts(request)
    last = {str(a.get("source")): a for a in attempts}
    tried = {str(a.get("source")) for a in attempts if a.get("outcome") in TRIED_OUTCOMES}
    max_attempts = int((request.get("budget") or {}).get("max_attempts") or len(LADDER))
    sources = list(request.get("source_priority") or LADDER)
    sources = sources[sources.index(source):] if source in sources else [source]
    calls = cost = 0
    remaining = []
    for name in sources:
        rung = rungs.get(name)
        if (rung is None or not getattr(rung, "costed", False)
                or (last.get(name) or {}).get("outcome") in DONE_OUTCOMES | {"dispatched"}
                or not rung.applies(request)):
            continue
        if name not in tried and len(tried) >= max_attempts:
            continue
        tried.add(name)
        count = int(getattr(rung, "estimate_calls", lambda r: CALL_ESTIMATES.get(name, 1))(request))
        calls += count
        cost += int(USD_PER_CALL.get(name, Decimal(0)) * count * 1_000_000)
        remaining.append(name)
    return {**blocked,
            "approve_calls": max(0, int(blocked["spent_calls"]) + calls - int(blocked["cap_calls"])),
            "approve_usd_micros": max(0, int(blocked["spent_usd_micros"]) + cost
                                     - int(blocked["cap_usd_micros"])),
            "remaining_sources": remaining, "remaining_estimated_calls": calls,
            "remaining_estimated_usd_micros": cost}


def _record_spend(state_dir: str | Path, request_id: str, source: str, calls: int, cost_micros: int, *,
                  now: str, note: str | None = None) -> None:
    if not calls and not cost_micros:
        return
    with _writer(state_dir) as connection:
        connection.execute(
            "INSERT INTO material_acquisition_spend(day,request_id,source,calls,cost_micros,note,created_at) "
            "VALUES (?,?,?,?,?,?,?)", (now[:10], request_id, source, int(calls), int(cost_micros), note, now))


# ---------------------------------------------------------------------------
# arrivals: the request <-> document link (``material_arrived``)
# ---------------------------------------------------------------------------


def link_arrivals(state_dir: str | Path, request: Mapping[str, Any], items: Sequence[Mapping[str, Any]], *,
                  source: str, now: str) -> list[dict[str, Any]]:
    """Record which documents answered the request; one ``material_arrived`` row each."""

    from . import evidence_index

    company = str(request.get("subject") or "")
    linked: list[dict[str, Any]] = []
    picked: dict[str, Mapping[str, Any]] = {}
    for item in items:
        ref = item.get("document_ref") or (item.get("ref") if item.get("kind") == "xbrl_fact" else None)
        if ref and ref not in picked:
            picked[str(ref)] = item
    if not picked:
        return linked
    index = evidence_index.open_index_readonly(state_dir)
    try:
        with _writer(state_dir) as connection:
            for ref, item in picked.items():
                arrival = None
                if index is not None and item.get("kind") != "xbrl_fact":
                    try:
                        digest = item.get("document_sha256")
                        if digest is not None:
                            if re.fullmatch(r"[a-f0-9]{64}", str(digest)) is None:
                                raise MaterialAcquisitionError("indexed document version hash is invalid")
                            found = index.execute("SELECT MIN(arrival_id) FROM material_arrivals WHERE company_ref=? "
                                                  "AND document_ref=? AND digest=?", (company, ref, digest)).fetchone()
                        else:
                            # Preserve the old link contract for historical
                            # items that did not carry a source text version.
                            found = index.execute("SELECT MIN(arrival_id) FROM material_arrivals WHERE company_ref=? "
                                                  "AND document_ref=?", (company, ref)).fetchone()
                        arrival = found[0] if found else None
                    except sqlite3.Error:
                        if item.get("document_sha256") is not None:
                            raise
                        arrival = None
                if item.get("document_sha256") is not None and arrival is None:
                    raise MaterialAcquisitionError("the exact indexed document version has no matching arrival")
                body = {"event": "material_arrived", "request_id": request["id"], "document_ref": ref,
                        "kind": item.get("kind"), "passage_ref": item.get("passage_ref"), "source": source,
                        "need": request.get("need"), "period": request.get("period"),
                        "requesters": [r.get("lane") for r in request.get("requesters") or ()]}
                if item.get("document_sha256") is not None:
                    body["document_sha256"] = item["document_sha256"]
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO material_request_arrivals(request_id,company_ref,document_ref,arrival_id,"
                    "kind,source,record_json,created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (request["id"], company, ref, arrival, item.get("kind"), source, _canonical(body), now))
                if cursor.rowcount:
                    linked.append({**body, "arrival_id": arrival, "created_at": now})
    finally:
        if index is not None:
            index.close()
    return linked


def material_arrived(state_dir: str | Path, *, company_ref: str | None = None, request_id: str | None = None,
                     after_id: int = 0, limit: int = 500) -> list[dict[str, Any]]:
    """``material_arrived`` events after a watermark: which request each arrived document answered."""

    where, params = ["link_id > ?"], [int(after_id)]
    if company_ref:
        where.append("company_ref = ?")
        params.append(company_ref)
    if request_id:
        where.append("request_id = ?")
        params.append(request_id)
    with _reader(state_dir) as connection:
        rows = _rows(connection, f"SELECT * FROM material_request_arrivals WHERE {' AND '.join(where)} "
                                 "ORDER BY link_id LIMIT ?", (*params, max(1, min(int(limit), 5000))))
    return [{**json.loads(row["record_json"]), "link_id": row["link_id"], "arrival_id": row["arrival_id"],
             "company_ref": row["company_ref"], "created_at": row["created_at"]} for row in rows]


def requests_for_document(state_dir: str | Path, document_ref: str) -> list[str]:
    with _reader(state_dir) as connection:
        return [row[0] for row in _rows(connection, "SELECT request_id FROM material_request_arrivals "
                                                    "WHERE document_ref=? ORDER BY link_id", (document_ref,))]


# ---------------------------------------------------------------------------
# negative cache
# ---------------------------------------------------------------------------


def _closed_at(view: Mapping[str, Any]) -> str | None:
    closes = [event for event in view.get("events") or () if event.get("event_kind") == "closed"]
    return closes[-1]["created_at"] if closes else None


def new_material_since(state_dir: str | Path, company_ref: str, since: str) -> bool:
    """Did a discovery batch or an indexed arrival for the company come in after ``since``?"""

    from . import evidence_index

    with _core(state_dir) as connection:
        rows = _rows(connection, "SELECT 1 FROM coverage_mission_source_discoveries WHERE company_ref=? "
                                 "AND created_at>? LIMIT 1", (company_ref, since))
    if rows:
        return True
    index = evidence_index.open_index_readonly(state_dir)
    if index is None:
        return False
    try:
        return index.execute("SELECT 1 FROM material_arrivals WHERE company_ref=? AND backfill=0 AND indexed_at>? "
                             "LIMIT 1", (company_ref, since)).fetchone() is not None
    except sqlite3.Error:
        return False
    finally:
        index.close()


def retry_due(state_dir: str | Path, view: Mapping[str, Any], *, now: str) -> tuple[bool, str]:
    """Whether an ``exhausted`` request leaves the negative cache, and why."""

    if view.get("status") not in ("exhausted", "timeout"):
        return False, "not_exhausted"
    closed = _closed_at(view)
    if closed is None:
        return False, "not_closed"
    moment, current = _parse_ts(closed), _parse_ts(now)
    if moment is not None and current is not None and current - moment >= timedelta(days=NEGATIVE_CACHE_DAYS):
        return True, f"距上次来源用尽已满 {NEGATIVE_CACHE_DAYS} 天"
    if _is_company(str(view.get("subject") or "")) and new_material_since(state_dir, str(view["subject"]), closed):
        return True, "这家公司有新的资料批次到达"
    return False, "negative_cache"


# ---------------------------------------------------------------------------
# the ladder
# ---------------------------------------------------------------------------


def _round_attempts(view: Mapping[str, Any]) -> list[dict[str, Any]]:
    events = list(view.get("events") or ())
    start = max((i for i, e in enumerate(events) if e.get("event_kind") == "opened" and e.get("reopened")),
                default=-1)
    from .material_source_queue_recovery import carried_attempts
    return carried_attempts(events, start, request_ref=view.get("id"), request_hash=view.get("content_hash")) + [e for e in events[start + 1:] if e.get("event_kind") == "attempt"]


def _record(state_dir: Path, view: Mapping[str, Any], source: str, result: RungResult, *, now: str,
            status: str | None = None) -> None:
    detail = result.detail
    mr.record_attempt(state_dir, str(view["id"]), source=source, outcome=result.outcome, detail=detail,
                      items=result.items, status=status, cost_micros=result.cost_micros, query=result.query,
                      calls=result.calls, actor_ref=ACTOR_REF, now=now)


def _free_retrieval_recheck(state: Path, view: Mapping[str, Any], *, stamp: str) -> dict[str, Any] | None:
    """Re-evaluate a changed lexical contract once using held material only.

    The append-only request/attempt/spend history is retained. A closed miss
    stays closed after this free check; it never opens a fresh paid ladder.
    """
    from .material_retrieval import contract, filing_kinds, RULE_REF
    retrieval = contract(view)
    required_filings = filing_kinds(view)
    if (retrieval is None and not required_filings) or (view.get("acceptance") or {}).get("numeric_contract_ref"):
        return None
    retrieval_hash = (retrieval["content_hash"] if retrieval is not None else
                      _sha({"rule_ref": RULE_REF, "need": view.get("need"), "doc_kinds": required_filings}))
    marker = {"rule_ref": RULE_REF, "retrieval_contract_hash": retrieval_hash}
    if _unperioded_filing_answer(view):
        # Permit one free local recheck after the corrected window rule,
        # including requests that consumed the old lexical-contract recheck.
        marker["window_rule_ref"] = HELD_FILING_ANSWER_WINDOW_RULE_REF
    if any(e.get("event_kind") == "attempt" and e.get("query") == marker
           for e in view.get("events") or ()):
        return None
    closed = bool(view.get("closed") or view.get("status") in mr.TERMINAL)
    if closed and view.get("status") not in {"exhausted", "timeout", "partial"}:
        return None
    # Do the read before reopening. A broken index is not a negative finding
    # and cannot alter the request's terminal state.
    verdict = evaluate_acceptance(state, view, now=stamp)
    ensure_schema(state)
    # The links table permits append-only links for closed requests. Verify
    # and commit exact text versions before reopening or recording a marker:
    # a failed link must leave the original request eligible for a later read.
    linked = (link_arrivals(state, view, verdict["items"], source="indexed_corpus", now=stamp)
              if verdict["met"] or verdict["partial"] else [])
    if closed:
        mr.reopen(state, view["id"], reason="原文逐项检索规则已修正，仅免费复核已有资料",
                  actor_ref=ACTOR_REF, now=stamp)
    mr.record_attempt(state, view["id"], source="indexed_corpus",
        outcome="accepted" if verdict["met"] else "partial" if verdict["partial"] else "empty",
        detail=verdict["reason"], items=verdict["items"], query=marker,
        status="partial" if verdict["partial"] else None, actor_ref=ACTOR_REF, now=stamp)
    if verdict["met"]:
        mr.close(state, view["id"], status="found", reason=verdict["reason"],
                 items=verdict["items"], actor_ref=ACTOR_REF, now=stamp)
        return {"request_id": view["id"], "status": "found", "outcome": "found",
                "free_recheck": marker, "rungs": [{"source": "indexed_corpus", "outcome": "accepted"}],
                "arrived": linked, "acceptance": verdict}
    if closed:
        mr.close(state, view["id"], status=view["status"], reason=verdict["reason"],
                 items=verdict["items"], actor_ref=ACTOR_REF, now=stamp)
        return {"request_id": view["id"], "status": view["status"], "outcome": "cached",
                "free_recheck": marker, "rungs": [{"source": "indexed_corpus", "outcome": "partial" if verdict["partial"] else "empty"}],
                "acceptance": verdict, "arrived": linked}
    return None


def serve(state_dir: str | Path, request_id: str, *, rungs: Mapping[str, Any] | None = None,
          now: str | None = None, allow_costed: bool = True) -> dict[str, Any]:
    """Work one request's source ladder as far as it can go now.

    Returns ``{"request_id", "status", "outcome", "rungs": [...]}`` where
    ``outcome`` is ``found`` / ``partial`` / ``exhausted`` / ``timeout`` (the
    request was closed), ``waiting`` (a rung is waiting for its source),
    ``deferred`` (a source is held or busy; tried again next tick),
    ``budget_blocked``, ``cached`` (closed and still negatively cached) or
    ``missing``.  ``allow_costed=False`` runs only the free rungs (the
    lane's way of honouring an exhausted pool without a card per request).
    """

    state = Path(state_dir)
    stamp = now or _now()
    view = mr.request(state, request_id)
    if view is None:
        return {"request_id": request_id, "status": None, "outcome": "missing", "rungs": []}
    from .forecast_driver_retrieval import routing_view
    view = routing_view(view)
    from .material_source_queue_recovery import recover
    if recover(state, view, now=stamp) is not None:
        view = routing_view(mr.request(state, request_id))
    rechecked = _free_retrieval_recheck(state, view, stamp=stamp)
    if rechecked is not None:
        return rechecked
    view = routing_view(mr.request(state, request_id) or view)
    if view["closed"] or view["status"] in mr.TERMINAL:
        due, why = retry_due(state, view, now=stamp)
        if not due:
            return {"request_id": request_id, "status": view["status"], "outcome": "cached", "reason": why,
                    "rungs": []}
        mr.reopen(state, request_id, reason=why, actor_ref=ACTOR_REF, now=stamp)
        view = routing_view(mr.request(state, request_id))
    if (view.get("acceptance") or {}).get("numeric_contract_ref") == "directed-document-numeric:0.1":
        return {"request_id": request_id, "status": view["status"], "outcome": "waiting",
                "reason": "系统将继续核对所缺数字及出处，取得新的原文段落后再分析", "rungs": []}
    ensure_schema(state)
    rungs = dict(rungs) if rungs is not None else default_rungs()
    identity = identity_for(state, str(view["subject"]))
    queries = build_queries(view, identity, now=stamp)
    expired = bool(view.get("deadline")) and str(view["deadline"]) < stamp
    attempts = _round_attempts(view)
    last: dict[str, dict[str, Any]] = {}
    for attempt in attempts:
        last[str(attempt.get("source"))] = attempt
    trail: list[dict[str, Any]] = []
    partial_items: list[dict[str, Any]] = []
    deferred = False
    max_attempts = int((view.get("budget") or {}).get("max_attempts") or len(LADDER))
    # Dispatch and settlement are two records of one source attempt. Counting
    # both exhausted a six-source ladder before it ever reached public web.
    # Monetary and connector-call caps continue to use the unchanged spend ledger.
    tried_sources_this_round = {
        str(a.get("source")) for a in attempts if a.get("outcome") in TRIED_OUTCOMES
    }

    def finish(status: str, reason: str, items: Sequence[Mapping[str, Any]], source: str) -> dict[str, Any]:
        linked = link_arrivals(state, view, items, source=source, now=stamp) if status in ("found", "partial") else []
        mr.close(state, request_id, status=status, reason=reason, items=items, actor_ref=ACTOR_REF, now=stamp)
        return {"request_id": request_id, "status": status, "outcome": status, "rungs": trail,
                "arrived": linked}

    for source in list(view.get("source_priority") or LADDER):
        rung = rungs.get(source)
        previous = last.get(source)
        if rung is None:
            if previous is None:
                result = RungResult("unsupported", detail=f"没有名为 {source} 的来源")
                _record(state, view, source, result, now=stamp)
                trail.append({"source": source, "outcome": result.outcome})
            continue
        outcome_before = (previous or {}).get("outcome")
        # A previously inapplicable SEC rung may now have an explicit filing
        # target. No old charge is erased and normal budget checks still apply.
        from .material_retrieval import filing_kinds
        if (source == "sec_edgar" and outcome_before == "not_applicable"
                and filing_kinds(view) and rung.applies(view)):
            previous, outcome_before = None, None
        if outcome_before in DONE_OUTCOMES:
            if outcome_before == "partial":
                partial_items.extend(previous.get("items") or [])
            trail.append({"source": source, "outcome": outcome_before, "carried": True})
            continue
        ctx = RungContext(state, view, identity, queries, stamp, previous)
        pending = outcome_before == "dispatched"
        if pending:
            result = rung.settle(ctx)
        else:
            if not rung.applies(view):
                result = RungResult("not_applicable", detail="这个来源不提供这类材料")
                _record(state, view, source, result, now=stamp)
                trail.append({"source": source, "outcome": result.outcome})
                continue
            if getattr(rung, "costed", False):
                if expired:
                    trail.append({"source": source, "outcome": "skipped_expired"})
                    continue
                if not allow_costed:
                    trail.append({"source": source, "outcome": "skipped_pool"})
                    deferred = True
                    break
                if source not in tried_sources_this_round and len(tried_sources_this_round) >= max_attempts:
                    trail.append({"source": source, "outcome": "skipped_max_attempts"})
                    continue
                calls = int(getattr(rung, "estimate_calls", lambda r: CALL_ESTIMATES.get(source, 1))(view))
                cost = int(USD_PER_CALL.get(source, Decimal(0)) * calls * 1_000_000)
                blocked = _budget_check(state, view, calls, cost, now=stamp)
                if blocked is not None:
                    if blocked["scope"] == "request":
                        blocked = _request_budget_proposal(view, blocked, source=source, rungs=rungs)
                    result = RungResult("budget_blocked", query=queries, detail=_canonical(blocked))
                    if outcome_before != "budget_blocked" or (previous or {}).get("detail") != result.detail:
                        _record(state, view, source, result, now=stamp)
                    trail.append({"source": source, "outcome": "budget_blocked", "budget": blocked})
                    return {"request_id": request_id, "status": view["status"], "outcome": "budget_blocked",
                            "budget": blocked, "rungs": trail}
            try:
                result = rung.run(ctx)
            except Exception as exc:  # noqa: BLE001 - one rung's failure is an attempt, not a crash
                result = RungResult("error", query=queries, detail=f"{type(exc).__name__}: {exc}"[:500])
        if result.outcome == "waiting":
            trail.append({"source": source, "outcome": "waiting"})
            return {"request_id": request_id, "status": view["status"], "outcome": "waiting", "rungs": trail,
                    "reason": result.detail}
        for manifest in result.manifests:
            _register_manifest(state, manifest)
        if result.manifests and result.outcome in ("dispatched", "empty", "partial", "accepted"):
            # The acquisition landed through the normal manifest path; what
            # counts is what the index now holds.
            verdict = evaluate_acceptance(state, view, identity=identity, now=stamp)
            result.outcome = "accepted" if verdict["met"] else ("partial" if verdict["partial"] else "empty")
            result.items = verdict["items"] or result.items
            result.detail = verdict["reason"]
        if result.outcome in DEFERRED_OUTCOMES:
            if outcome_before != result.outcome:
                _record(state, view, source, result, now=stamp)
            trail.append({"source": source, "outcome": result.outcome})
            deferred = True
            continue
        if result.outcome == "dispatched":
            _record(state, view, source, result, now=stamp)
            _record_spend(state, request_id, source, result.calls, result.cost_micros, now=stamp,
                          note=_canonical(result.query)[:500])
            trail.append({"source": source, "outcome": "dispatched"})
            return {"request_id": request_id, "status": view["status"], "outcome": "waiting", "rungs": trail}
        if result.outcome in TRIED_OUTCOMES:
            tried_sources_this_round.add(source)
        if result.calls or result.cost_micros:
            _record_spend(state, request_id, source, result.calls, result.cost_micros, now=stamp)
        if result.outcome == "accepted":
            _record(state, view, source, result, now=stamp)
            trail.append({"source": source, "outcome": "accepted"})
            label = _source_label(source)
            return finish("found", f"在「{label}」找到：{result.detail or ''}"[:500], result.items, source)
        status = "partial" if result.outcome == "partial" else None
        _record(state, view, source, result, now=stamp, status=status)
        if result.outcome == "partial":
            partial_items.extend(result.items)
        trail.append({"source": source, "outcome": result.outcome})
    if expired:
        mr.close(state, request_id, status="timeout", reason="截止时间已过，按现有材料继续",
                 items=partial_items[:20], actor_ref=ACTOR_REF, now=stamp)
        return {"request_id": request_id, "status": "timeout", "outcome": "timeout", "rungs": trail}
    if deferred:
        return {"request_id": request_id, "status": view["status"], "outcome": "deferred", "rungs": trail}
    if partial_items:
        return finish("partial", "所有来源都尝试过，只找到部分材料", partial_items[:20], "ladder")
    refreshed = mr.request(state, request_id) or view
    tried_labels = tried_sources(refreshed)
    skipped = [item["source"] for item in trail if item["outcome"] == "skipped_max_attempts"]
    reason = (("本轮检索次数已用完，尚未检索：" + "、".join(_source_label(s) for s in skipped) + "。")
              if skipped else "所有来源都没有找到。")
    reason += ("已尝试：" + " → ".join(tried_labels)) if tried_labels else "没有可用的来源"
    mr.close(state, request_id, status="exhausted", reason=reason, actor_ref=ACTOR_REF, now=stamp)
    return {"request_id": request_id, "status": "exhausted", "outcome": "exhausted", "rungs": trail}


def _register_manifest(state_dir: Path, manifest: str | Path) -> None:
    from . import evidence_index

    try:
        evidence_index.index_acquired_manifest(state_dir, manifest)
    except Exception:  # noqa: BLE001 - the index lane picks it up next tick anyway
        pass


def _source_label(source: str) -> str:
    from .terminology import label

    return label("material_source", source, default=source)


def _outcome_label(outcome: str) -> str:
    from .terminology import label

    return label("material_outcome", outcome, default=outcome)


def tried_sources(view: Mapping[str, Any]) -> list[str]:
    """「已尝试」: each source really tried this round, in ladder order, with its result."""

    seen: dict[str, str] = {}
    for attempt in _round_attempts(view):
        outcome = str(attempt.get("outcome") or "")
        if outcome in ("not_applicable", "unsupported"):
            continue
        seen[str(attempt.get("source"))] = outcome
    return [f"{_source_label(source)}（{_outcome_label(outcome)}）" for source, outcome in seen.items()]


# ---------------------------------------------------------------------------
# conversions: the old dead ends now open requests
# ---------------------------------------------------------------------------


def _open(state_dir: str | Path, **kwargs: Any) -> dict[str, Any]:
    return mr.open_request(state_dir, actor_ref=ACTOR_REF, **kwargs)


def _converted(state_dir: str | Path, input_ref: str) -> str | None:
    with _reader(state_dir) as connection:
        rows = _rows(connection, "SELECT request_id FROM material_acquisition_conversions WHERE input_ref=?",
                     (input_ref,))
    return rows[0][0] if rows else None


def _note_conversion(state_dir: str | Path, input_ref: str, request_id: str | None,
                     record: Mapping[str, Any], now: str) -> None:
    with _writer(state_dir) as connection:
        connection.execute("INSERT OR IGNORE INTO material_acquisition_conversions(input_ref,request_id,record_json,"
                           "created_at) VALUES (?,?,?,?)", (input_ref, request_id, _canonical(dict(record)), now))


def open_for_inquiry(state_dir: str | Path, inquiry: Mapping[str, Any], *, plan_ref: str | None = None,
                     reason: str | None = None, industry_ref: str | None = None,
                     now: str | None = None) -> dict[str, Any]:
    """A planner inquiry no template could bind becomes a MaterialRequest (idempotent)."""

    from .research_planner import inquiry_material_request

    spec = inquiry_material_request(inquiry, plan_ref=plan_ref, reason=reason, industry_ref=industry_ref)
    if spec is None:
        raise MaterialAcquisitionError("this inquiry names no subject to look for material about")
    return _open(state_dir, now=now, **spec)


def open_for_repair_target(state_dir: str | Path, company_ref: str, target: Mapping[str, Any], *,
                           feedback_ref: str | None = None, product_ref: str | None = None,
                           now: str | None = None) -> dict[str, Any] | None:
    """A Dossier repair target that is a missing-material finding becomes a request.

    Writing fixes (a figure no cited row prints) are the drafter's to repair
    and open nothing; ``missing_evidence``, ``unsupported_slot`` and
    ``no_new_evidence`` findings are material the Dossier lacked.
    """

    code = str(target.get("code") or target.get("check") or "")
    if code not in REPAIR_MATERIAL_CODES:
        return None
    detail = str(target.get("detail") or "").strip()
    figure = str(target.get("figure") or "").strip()
    question = " ".join(part for part in (detail, f"({figure})" if figure else "") if part)[:1800]
    if not question:
        return None
    section = str(target.get("unit") or target.get("section") or target.get("slot_id") or code)[:500]
    return _open(state_dir, now=now,
                 requester={"lane": "company_dossier", "product_ref": product_ref,
                            "run_ref": str(target.get("id") or feedback_ref or "")[:500] or None, "section": section},
                 subject=company_ref, need={"kind": "answer", "question": question},
                 period=None, acceptance={"min_passages": 2})


REPAIR_MATERIAL_CODES = frozenset({"missing_evidence", "unsupported_slot", "no_new_evidence"})


def open_for_rewrite_flag(state_dir: str | Path, flag: Mapping[str, Any], *,
                          now: str | None = None) -> dict[str, Any] | None:
    """矛盾 4: an issuer-misattribution rewrite waits for official replacement material.

    The flag's first required kind (the subject's own 8-K exhibit 99.1 for the
    latest reported quarter) is requested from the official sources first --
    the index, SEC, then the company's own pages on the web.
    """

    subject = flag.get("subject_ref")
    if not isinstance(subject, str) or not subject:
        return None
    needs = [need for need in flag.get("requires_material") or () if isinstance(need, Mapping)]
    kinds = [str(need.get("kind")) for need in needs if need.get("kind") in ("sec_exhibit", "transcript", "sec_10q")]
    if not kinds:
        kinds = ["sec_exhibit"]
    stamp = now or _now()
    return _open(state_dir, now=stamp,
                 requester={"lane": "product_rewrite", "product_ref": flag.get("product_version_ref"),
                            "run_ref": flag.get("id"), "section": "replacement_material"},
                 subject=subject, need={"kind": "document", "doc_kinds": sorted(set(kinds))},
                 period=f"latest_quarter_as_of:{stamp[:7]}",
                 source_priority=["indexed_corpus", "sec_edgar", "alphaengine", "web"],
                 acceptance={"about_company": True, "max_age_days": 130})


#: What an effect from another batch (F's event judgement ``action=research``,
#: G's ask recovery) may say.  Closed so a caller cannot smuggle a ledger field.
EFFECT_FIELDS = frozenset({"subject_ref", "company_ref", "subject", "need", "question", "doc_kinds", "data_item",
                           "period",
                           "requester", "lane", "product_ref", "run_ref", "section", "source_priority",
                           "acceptance", "deadline_hours", "event_ref", "kind"})


def open_for_effect(state_dir: str | Path, effect: Mapping[str, Any], *, now: str | None = None) -> dict[str, Any]:
    """The entry point for F (event judgement ``action=research``) and G (ask续答).

    ``effect`` names a subject (``subject_ref`` or ``company_ref``) and what is
    missing: ``doc_kinds`` (a document), ``data_item`` (a figure) or
    ``question`` (a research question); optionally ``period``, a requester
    (``requester`` mapping, or ``lane``/``product_ref``/``run_ref``/``section``),
    ``source_priority``, ``acceptance`` and ``deadline_hours``.  Returns the
    request view (``write_status`` fresh / joined / duplicate).
    """

    if not isinstance(effect, Mapping):
        raise MaterialAcquisitionError("an effect is a mapping")
    unknown = set(effect) - EFFECT_FIELDS
    if unknown:
        raise MaterialAcquisitionError(f"unknown effect fields: {sorted(unknown)}")
    subject = effect.get("subject_ref") or effect.get("company_ref") or effect.get("subject")
    if not isinstance(subject, str) or not subject.strip():
        raise MaterialAcquisitionError("an effect names its subject_ref")
    if isinstance(effect.get("need"), Mapping):
        # The ledger's own shape (F's ``gap_need`` / ``research_need``, G's ask
        # door): passed through unchanged, so the digest is the one
        # ``material_requests.open_request`` would compute for the same need.
        need = dict(effect["need"])
    elif effect.get("question"):
        need = {"kind": "answer", "question": str(effect["question"])}
        if effect.get("doc_kinds"):
            need["doc_kinds"] = list(effect["doc_kinds"])
    elif effect.get("doc_kinds"):
        need = {"kind": "document", "doc_kinds": list(effect["doc_kinds"])}
    elif effect.get("data_item"):
        need = {"kind": "data", "data_item": str(effect["data_item"])}
    else:
        raise MaterialAcquisitionError("an effect names need, doc_kinds, data_item or question")
    who = dict(effect.get("requester") or {})
    for key in ("lane", "product_ref", "run_ref", "section"):
        if effect.get(key) is not None and key not in who:
            who[key] = effect[key]
    if effect.get("event_ref") and not who.get("run_ref"):
        who["run_ref"] = effect["event_ref"]
    who.setdefault("lane", "event_judgement")
    stamp = now or _now()
    deadline = None
    if effect.get("deadline_hours") is not None:
        hours = float(effect["deadline_hours"])
        if not 0 < hours <= 24 * 30:
            raise MaterialAcquisitionError("deadline_hours must be within 30 days")
        deadline = ((_parse_ts(stamp) or datetime.now(timezone.utc)) + timedelta(hours=hours)).isoformat(
            timespec="seconds")
    try:
        return _open(state_dir, now=stamp, requester=who, subject=subject.strip(), need=need,
                     period=effect.get("period"), source_priority=effect.get("source_priority"),
                     acceptance=effect.get("acceptance"), deadline=deadline)
    except mr.MaterialRequestError as exc:
        raise MaterialAcquisitionError(str(exc)) from exc


def convert_rewrite_flags(state_dir: str | Path, *, now: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """Open a request for every rewrite flag still waiting for its replacement material."""

    from .product_rewrite_flags import needs_rewrite

    stamp = now or _now()
    with _core(state_dir) as connection:
        if connection is None:
            return []
        try:
            flags = needs_rewrite(connection)
        except sqlite3.Error:
            return []
    out = []
    for flag in flags:
        if len(out) >= limit:
            break
        if flag.get("rewrite_gate") != "awaiting_material" or not flag.get("id"):
            continue
        key = f"rewrite-flag:{flag['id']}"
        if _converted(state_dir, key):
            continue
        opened = open_for_rewrite_flag(state_dir, flag, now=stamp)
        _note_conversion(state_dir, key, (opened or {}).get("id"), {"flag": flag.get("id")}, stamp)
        if opened:
            out.append({"input": key, "request_id": opened["id"], "write_status": opened.get("write_status")})
    return out


def convert_repair_targets(state_dir: str | Path, *, now: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """Open requests for the stable missing-material targets of each company's latest Dossier run."""

    from .dossier_repair_feedback import read_dossier_repair_feedback

    stamp = now or _now()
    try:
        feedback = read_dossier_repair_feedback(state_dir)
    except Exception:  # noqa: BLE001 - unreadable feedback converts nothing
        return []
    out = []
    for company_ref, item in sorted(feedback.items()):
        stable = set((item.get("material") or {}).get("stable_repair_target_keys") or ())
        for target in item.get("repair_targets") or ():
            if len(out) >= limit:
                return out
            key = f"repair-target:{target.get('id')}"
            if _converted(state_dir, key):
                continue
            from .dossier_repair_feedback import _target_key

            body = {k: v for k, v in target.items() if k not in ("id", "content_hash")}
            if stable and _target_key(company_ref, body) not in stable:
                continue
            opened = open_for_repair_target(state_dir, company_ref, target, feedback_ref=item.get("id"),
                                            product_ref=item.get("published_version_ref"), now=stamp)
            _note_conversion(state_dir, key, (opened or {}).get("id"), {"company_ref": company_ref}, stamp)
            if opened:
                out.append({"input": key, "request_id": opened["id"], "write_status": opened.get("write_status")})
    return out


# ---------------------------------------------------------------------------
# priority and the lane's tick
# ---------------------------------------------------------------------------

#: Requesters that are drafting (or answering) right now: a product waiting
#: on its sufficiency check, a rewrite waiting on replacement material, an
#: event judgement or an answer waiting to continue.
DRAFTING_LANES = frozenset({"initial_screen", "investment_memo", "dossier", "debate_map", "earnings_preview",
                            "event_judgement", "ask", "product_rewrite", "company_dossier"})


def priority_key(request: Mapping[str, Any], universe: Mapping[str, Mapping[str, Any]]) -> tuple[Any, ...]:
    """Lower first: drafting products, then P0 companies, then sufficiency-blocking, then oldest."""

    from .material_sufficiency import CHECK_LABELS

    requesters = [r for r in request.get("requesters") or () if isinstance(r, Mapping)]
    drafting = any(r.get("lane") in DRAFTING_LANES for r in requesters)
    member = universe.get(str(request.get("subject") or "")) or {}
    p0 = member.get("bootstrap_priority") == "P0"
    blocking = any(r.get("section") in CHECK_LABELS or r.get("lane") == "product_rewrite" for r in requesters)
    return (0 if drafting else 1, 0 if p0 else 1, 0 if blocking else 1, str(request.get("created_at") or ""),
            str(request.get("id") or ""))


_RETRY_CURSOR_ID = "retry-continuation"
_RETRY_CURSOR_SCHEMA = "material-retry-continuation:0.1"
_RETRY_PAGE_SIZE = 64


def _decode_retry_cursor(row):
    if row is None:
        return 0, None
    try:
        body = json.loads(row["record_json"])
        if (set(body) != {"schema_version", "cursor_id", "last_rowid", "request_ref", "revision", "updated_at"}
                or body["schema_version"] != _RETRY_CURSOR_SCHEMA
                or body["cursor_id"] not in (_RETRY_CURSOR_ID, _RETRY_CURSOR_ID+"-queue")
                or type(body["last_rowid"]) is not int or body["last_rowid"] <= 0
                or type(body["revision"]) is not int or body["revision"] <= 0
                or not _REQUEST_ID_RE.fullmatch(body["request_ref"])
                or _parse_ts(body["updated_at"]) is None
                or _sha(body) != row["content_hash"]):
            raise ValueError("cursor identity differs")
    except (ValueError, TypeError, KeyError) as exc:
        raise MaterialAcquisitionError("material retry cursor is invalid") from exc
    return body["last_rowid"], row["content_hash"]


def _retry_page(state_dir, *, deadline, recovery_lane=False):
    from .material_source_queue_recovery import LEGACY_TIMEOUT_DETAIL
    cursor_id = _RETRY_CURSOR_ID+("-queue" if recovery_lane else "")
    # IDs only: never unfold all historical request JSON before starting the
    # allowance. rowid is stable because the request ledger is append-only.
    with _reader(state_dir) as connection:
        if connection is None:
            return [], None
        try:
            cursor = connection.execute(
                "SELECT record_json,content_hash FROM material_acquisition_retry_cursor WHERE cursor_id=?",
                (cursor_id,)).fetchone()
        except sqlite3.OperationalError as exc:
            if str(exc) != "no such table: material_acquisition_retry_cursor":
                raise
            cursor = None
        last, digest = _decode_retry_cursor(cursor)
        if cursor is not None:
            if json.loads(cursor["record_json"])["cursor_id"] != cursor_id:
                raise MaterialAcquisitionError("material retry cursor lane differs")
            body = json.loads(cursor["record_json"])
            held = connection.execute("SELECT request_id FROM material_requests WHERE rowid=?", (last,)).fetchone()
            if held is None or held["request_id"] != body["request_ref"]:
                raise MaterialAcquisitionError("material retry cursor request differs")
        sql = ("SELECT r.rowid AS sweep_rowid,r.request_id,"
               "(SELECT status FROM material_request_events e WHERE e.request_id=r.request_id "
               "ORDER BY event_id DESC LIMIT 1) AS latest_status FROM material_requests r ")
        # Cheap indexed event preselection is not authority. Every selected
        # candidate is re-read and checked by the original strong recovery.
        clause = " AND latest_status IN ('exhausted','timeout')"
        queue_shape = ("(EXISTS (SELECT 1 FROM material_request_events e WHERE e.request_id=r.request_id "
                       "AND e.event_kind='attempt' AND json_extract(e.record_json,'$.source')='sec_edgar' "
                       "AND json_extract(e.record_json,'$.outcome')='not_connected' "
                       "AND json_extract(e.record_json,'$.detail')=?) OR EXISTS ("
                       "SELECT 1 FROM material_targeted_queries q JOIN material_targeted_query_events t ON t.query_id=q.query_id "
                       "WHERE q.request_id=r.request_id AND q.source_ref='source:sec-edgar' "
                       "AND t.status='refused' AND json_extract(t.record_json,'$.kind')='no_spec'))")
        # The same slow queue proof must not also occupy the normal-first
        # turn. Legacy-shaped requests still get ordinary retry_due checks in
        # their queue turn if strong special recovery does not qualify.
        clause += " AND "+("" if recovery_lane else "NOT ")+queue_shape
        extra = (LEGACY_TIMEOUT_DETAIL,)
        rows = _rows(connection, sql + "WHERE r.rowid>?"+clause+" ORDER BY r.rowid LIMIT ?",
                     (last, *extra, _RETRY_PAGE_SIZE), deadline=deadline)
        if len(rows) < _RETRY_PAGE_SIZE and last:
            rows += _rows(connection, sql + "WHERE r.rowid<=?"+clause+" ORDER BY r.rowid LIMIT ?",
                          (last, *extra, _RETRY_PAGE_SIZE-len(rows)), deadline=deadline)
        return rows, digest


def _claim_retry_turn(state_dir, row, *, expected_hash, now, recovery_lane=False):
    cursor_id = _RETRY_CURSOR_ID+("-queue" if recovery_lane else "")
    # Claim before strong evaluation. A slow/unverified item gets another
    # chance after a complete sweep; it cannot pin every future tick here.
    with _writer(state_dir) as connection:
        cursor = connection.execute(
            "SELECT record_json,content_hash FROM material_acquisition_retry_cursor WHERE cursor_id=?",
            (cursor_id,)).fetchone()
        if cursor is not None and json.loads(cursor["record_json"]).get("cursor_id") != cursor_id:
            raise MaterialAcquisitionError("material retry claim lane differs")
        _, held_hash = _decode_retry_cursor(cursor)
        if held_hash != expected_hash:
            return None
        held = connection.execute("SELECT request_id FROM material_requests WHERE rowid=?",
                                  (row["sweep_rowid"],)).fetchone()
        if held is None or held["request_id"] != row["request_id"]:
            raise MaterialAcquisitionError("material retry turn request differs")
        body = {"schema_version": _RETRY_CURSOR_SCHEMA, "cursor_id": cursor_id, "last_rowid": row["sweep_rowid"],
                "request_ref": row["request_id"], "revision": 1 if cursor is None else
                json.loads(cursor["record_json"])["revision"]+1, "updated_at": now}
        digest = _sha(body)
        connection.execute("INSERT INTO material_acquisition_retry_cursor VALUES(?,?,?) "
                           "ON CONFLICT(cursor_id) DO UPDATE SET record_json=excluded.record_json,content_hash=excluded.content_hash",
                           (cursor_id, _canonical(body), digest))
        return digest


def _retry_pass(state_dir, *, stamp, limit, deadline, recovery_lane=False, max_evaluations=None):
    from .material_source_queue_recovery import recover
    from .source_discovery_read_budget import SourceReadDeadline
    out = []
    if limit <= 0 or time.monotonic() >= deadline:
        return out
    try:
        page, cursor_hash = _retry_page(state_dir, deadline=deadline, recovery_lane=recovery_lane)
    except SourceReadDeadline:
        return []
    evaluations = 0
    for row in page:
        if (len(out) >= limit or time.monotonic() >= deadline
                or (max_evaluations is not None and evaluations >= max_evaluations)):
            break
        next_hash = _claim_retry_turn(state_dir, row, expected_hash=cursor_hash, now=stamp, recovery_lane=recovery_lane)
        if next_hash is None:
            break  # Another tick claimed this exact continuation.
        cursor_hash = next_hash
        if row["latest_status"] not in ("exhausted", "timeout"):
            continue
        evaluations += 1
        full = mr.request(state_dir, row["request_id"])
        if full is None or full["status"] not in ("exhausted", "timeout"):
            continue
        if time.monotonic() >= deadline:
            break
        # Already started strong verification and append-only authority work
        # finish normally. The deadline governs starting the next item.
        repaired = recover(state_dir, full, now=stamp)
        if repaired is not None:
            out.append({"request_id": row["request_id"], "reason": repaired["rule_ref"]})
            continue
        if time.monotonic() >= deadline:
            break
        due, why = retry_due(state_dir, full, now=stamp)
        if due:
            result = mr.reopen(state_dir, row["request_id"], reason=why, actor_ref=ACTOR_REF, now=stamp)
            if result["write_status"] == "fresh":
                out.append({"request_id": row["request_id"], "reason": why})
    return out



def _claim_retry_first(state_dir, *, now):
    """Persistently alternate the first class, including one-slot/slow ticks."""
    cursor_id = "retry-class-rotation"
    schema = "material-retry-class-rotation:0.1"
    with _writer(state_dir) as connection:
        row = connection.execute("SELECT record_json,content_hash FROM material_acquisition_retry_cursor WHERE cursor_id=?",
                                 (cursor_id,)).fetchone()
        prior = None
        if row is not None:
            try:
                prior = json.loads(row["record_json"])
                if (set(prior) != {"schema_version", "cursor_id", "next_queue_first", "revision", "updated_at"}
                        or prior["schema_version"] != schema or prior["cursor_id"] != cursor_id
                        or type(prior["next_queue_first"]) is not bool
                        or type(prior["revision"]) is not int or prior["revision"] <= 0
                        or _parse_ts(prior["updated_at"]) is None or _sha(prior) != row["content_hash"]):
                    raise ValueError("rotation differs")
            except (ValueError, TypeError, KeyError) as exc:
                raise MaterialAcquisitionError("material retry class rotation is invalid") from exc
        first = prior["next_queue_first"] if prior else False
        body = {"schema_version": schema, "cursor_id": cursor_id, "next_queue_first": not first,
                "revision": 1 if prior is None else prior["revision"]+1, "updated_at": now}
        connection.execute("INSERT INTO material_acquisition_retry_cursor VALUES(?,?,?) "
                           "ON CONFLICT(cursor_id) DO UPDATE SET record_json=excluded.record_json,content_hash=excluded.content_hash "
                           "WHERE material_acquisition_retry_cursor.content_hash=?",
                           (cursor_id, _canonical(body), _sha(body), None if row is None else row["content_hash"]))
        return first

def retry_sweep(state_dir: str | Path, *, now: str | None = None, limit: int = 3,
                deadline: float | None = None) -> list[dict[str, Any]]:
    """Bounded fair continuation; both classes share the original reopen cap.

    The first class rotates durably, even with one slot or a slow strong read.
    Unused opportunities return to the other class; independent persisted
    positions stop either class being starved by earlier arrivals. No source/call quota is changed.
    """
    stamp = now or _now()
    allowance = time.monotonic()+2.0
    deadline = allowance if deadline is None else min(deadline, allowance)
    if limit <= 0 or time.monotonic() >= deadline:
        return []
    queue_first = _claim_retry_first(state_dir,now=stamp)
    if queue_first:
        out = _retry_pass(state_dir,stamp=stamp,limit=limit,deadline=deadline,recovery_lane=True)
        out += _retry_pass(state_dir,stamp=stamp,limit=limit-len(out),deadline=deadline)
    else:
        out = _retry_pass(state_dir,stamp=stamp,limit=1,deadline=deadline,max_evaluations=1)
        out += _retry_pass(state_dir,stamp=stamp,limit=limit-len(out),deadline=deadline,recovery_lane=True)
        out += _retry_pass(state_dir,stamp=stamp,limit=limit-len(out),deadline=deadline)
    return out


_SERVICE_CURSOR_ID = "priority-round-robin"
_SERVICE_CURSOR_SCHEMA = "material-acquisition-service-cursor:0.1"


def _decode_service_cursor(row: sqlite3.Row | None) -> tuple[tuple[Any, ...] | None, str | None]:
    if row is None:
        return None, None
    try:
        record = json.loads(row["record_json"])
    except (TypeError, ValueError) as exc:
        raise MaterialAcquisitionError("material scheduling cursor JSON is invalid") from exc
    if not isinstance(record, dict):
        raise MaterialAcquisitionError("material scheduling cursor must be an object")
    if _sha(record) != row["content_hash"]:
        raise MaterialAcquisitionError("material scheduling cursor content hash differs")
    key = record.get("priority_key")
    if (set(record) != {"schema_version", "request_ref", "priority_key", "updated_at", "revision"}
            or record["schema_version"] != _SERVICE_CURSOR_SCHEMA
            or type(record["revision"]) is not int or not 0 < record["revision"] < 2**63
            or not isinstance(key, list) or len(key) != 5
            or any(type(value) is not int or value not in (0, 1) for value in key[:3])
            or not all(isinstance(value, str) for value in key[3:])
            or key[4] != record["request_ref"]
            or not _REQUEST_ID_RE.fullmatch(key[4])
            or not isinstance(record["updated_at"], str) or _parse_ts(record["updated_at"]) is None):
        raise MaterialAcquisitionError("material scheduling cursor identity or priority is invalid")
    return tuple(key), str(row["content_hash"])


def _service_order(state_dir: str | Path, pending: Sequence[Mapping[str, Any]],
                   universe: Mapping[str, Mapping[str, Any]]) -> tuple[list[Mapping[str, Any]], str | None]:
    """Resume the priority-ordered sweep even when its last request has closed."""
    with _reader(state_dir) as connection:
        if connection is None:
            raise MaterialAcquisitionError("material scheduling cursor ledger cannot be read")
        try:
            rows = connection.execute(
                "SELECT record_json,content_hash FROM material_acquisition_service_cursor WHERE cursor_id=?",
                (_SERVICE_CURSOR_ID,)).fetchall()
        except sqlite3.OperationalError as exc:
            # This table is additive: only its genuine pre-migration absence
            # starts a new sweep. Other schema/read errors must not reset it.
            if str(exc) != "no such table: material_acquisition_service_cursor":
                raise MaterialAcquisitionError(f"material scheduling cursor cannot be read: {exc}") from exc
            rows = []
    key, digest = _decode_service_cursor(rows[0] if rows else None)
    ordered = sorted(pending, key=lambda item: priority_key(item, universe))
    if key is None:
        return ordered, digest
    start = next((index for index, item in enumerate(ordered)
                  if priority_key(item, universe) > key), len(ordered))
    return ordered[start:] + ordered[:start], digest


def _advance_service_cursor(state_dir: str | Path, request: Mapping[str, Any],
                            universe: Mapping[str, Mapping[str, Any]], *,
                            expected_hash: str | None, now: str,
                            deadline: float | None = None,
                            expected_priority_hash: Any = ...) -> str | None:
    """Claim one service turn; concurrent stale ticks must not execute it."""
    record = {"schema_version": _SERVICE_CURSOR_SCHEMA, "request_ref": request["id"],
              "priority_key": list(priority_key(request, universe)), "updated_at": now}
    with _writer(state_dir) as connection:
        row = connection.execute("SELECT record_json,content_hash FROM material_acquisition_service_cursor "
                                 "WHERE cursor_id=?", (_SERVICE_CURSOR_ID,)).fetchone()
        _, current_hash = _decode_service_cursor(row)
        if current_hash != expected_hash:
            return None
        if expected_priority_hash is not ...:
            priority_row = connection.execute("SELECT record_json,content_hash FROM material_acquisition_priority_cursor WHERE cursor_id=?",
                                              (_PRIORITY_CURSOR_ID,)).fetchone()
            _, _, current_priority_hash = _decode_priority_cursor(priority_row)
            if current_priority_hash != expected_priority_hash:
                return None
        if deadline is not None and time.monotonic() >= deadline:
            return None
        record["revision"] = 1 if row is None else json.loads(row["record_json"])["revision"] + 1
        digest = _sha(record)
        connection.execute(
            "INSERT INTO material_acquisition_service_cursor(cursor_id,record_json,content_hash) VALUES (?,?,?) "
            "ON CONFLICT(cursor_id) DO UPDATE SET record_json=excluded.record_json,content_hash=excluded.content_hash",
            (_SERVICE_CURSOR_ID, _canonical(record), digest))
    return digest


_PRIORITY_CURSOR_ID = "current-product-gap"
_PRIORITY_CURSOR_SCHEMA = "material-acquisition-priority-cursor:0.1"


def _decode_priority_cursor(row: sqlite3.Row | None) -> tuple[tuple[Any, ...] | None, bool, str | None]:
    if row is None:
        return None, True, None
    try:
        record = json.loads(row["record_json"])
    except (TypeError, ValueError) as exc:
        raise MaterialAcquisitionError("material priority cursor JSON is invalid") from exc
    if (not isinstance(record, dict) or set(record) != {"schema_version", "priority_key", "next_priority_first", "revision", "updated_at"}
            or record["schema_version"] != _PRIORITY_CURSOR_SCHEMA or type(record["next_priority_first"]) is not bool
            or type(record["revision"]) is not int or not 0 < record["revision"] < 2**63
            or not isinstance(record["updated_at"], str) or _parse_ts(record["updated_at"]) is None
            or _sha(record) != row["content_hash"]):
        raise MaterialAcquisitionError("material priority cursor hash or fields differ")
    key = record["priority_key"]
    if key is not None:
        # Reuse the ordinary cursor's exact request/key validation.
        wire = {"schema_version": _SERVICE_CURSOR_SCHEMA, "request_ref": key[-1] if isinstance(key, list) and key else None,
                "priority_key": key, "revision": record["revision"], "updated_at": record["updated_at"]}
        key, _ = _decode_service_cursor({"record_json": _canonical(wire), "content_hash": _sha(wire)})
    return key, record["next_priority_first"], str(row["content_hash"])


def _priority_service_order(state_dir, pending, universe):
    with _reader(state_dir) as connection:
        if connection is None:
            raise MaterialAcquisitionError("material priority cursor cannot be read")
        try:
            row = connection.execute("SELECT record_json,content_hash FROM material_acquisition_priority_cursor WHERE cursor_id=?",
                                     (_PRIORITY_CURSOR_ID,)).fetchone()
        except sqlite3.OperationalError as exc:
            if str(exc) != "no such table: material_acquisition_priority_cursor":
                raise MaterialAcquisitionError(f"material priority cursor cannot be read: {exc}") from exc
            row = None
    key, first, digest = _decode_priority_cursor(row)
    ordered = sorted(pending, key=lambda v: priority_key(v, universe))
    cut = 0 if key is None else next((i for i, v in enumerate(ordered) if priority_key(v, universe) > key), len(ordered))
    return ordered[cut:] + ordered[:cut], first, digest


def _advance_priority_turn(state_dir, request, universe, *, expected_hash, expected_priority_hash,
                           priority, flip_order, now, deadline, priority_proof=None):
    """One atomic CAS across both cursors; a losing tick must never serve."""
    with _writer(state_dir) as connection:
        normal_row = connection.execute("SELECT record_json,content_hash FROM material_acquisition_service_cursor WHERE cursor_id=?",
                                        (_SERVICE_CURSOR_ID,)).fetchone()
        _, normal_hash = _decode_service_cursor(normal_row)
        row = connection.execute("SELECT record_json,content_hash FROM material_acquisition_priority_cursor WHERE cursor_id=?",
                                 (_PRIORITY_CURSOR_ID,)).fetchone()
        key, first, priority_hash = _decode_priority_cursor(row)
        if normal_hash != expected_hash or priority_hash != expected_priority_hash or time.monotonic() >= deadline:
            return None
        if priority:
            from .material_current_gap_priority import still_current
            if not still_current(state_dir, priority_proof or {}, deadline=deadline):
                return None
            source = connection.execute("SELECT * FROM material_requests WHERE request_id=?", (request["id"],)).fetchone()
            if source is None or source["content_hash"] != priority_proof["request_hash"]:
                return None
            status, closed, _ = mr._status_of(connection, request["id"])
            if status not in mr.ACTIVE or closed:
                return None
        if time.monotonic() >= deadline:
            return None
        if not priority:
            wire = {"schema_version": _SERVICE_CURSOR_SCHEMA, "request_ref": request["id"],
                    "priority_key": list(priority_key(request, universe)), "updated_at": now,
                    "revision": 1 if normal_row is None else json.loads(normal_row["record_json"])["revision"] + 1}
            normal_hash = _sha(wire)
            connection.execute("INSERT INTO material_acquisition_service_cursor VALUES(?,?,?) "
                "ON CONFLICT(cursor_id) DO UPDATE SET record_json=excluded.record_json,content_hash=excluded.content_hash",
                (_SERVICE_CURSOR_ID, _canonical(wire), normal_hash))
        wire = {"schema_version": _PRIORITY_CURSOR_SCHEMA,
                "priority_key": list(priority_key(request, universe)) if priority else None if key is None else list(key),
                "next_priority_first": not first if flip_order else first,
                "updated_at": now, "revision": 1 if row is None else json.loads(row["record_json"])["revision"] + 1}
        priority_hash = _sha(wire)
        connection.execute("INSERT INTO material_acquisition_priority_cursor VALUES(?,?,?) "
            "ON CONFLICT(cursor_id) DO UPDATE SET record_json=excluded.record_json,content_hash=excluded.content_hash",
            (_PRIORITY_CURSOR_ID, _canonical(wire), priority_hash))
    return normal_hash, priority_hash


_SERVICE_REASONS = {
    "waiting": "已选择，等待来源或数字核对", "deferred": "已选择，来源暂时等待",
    "budget_blocked": "已选择，现有额度门未通过", "error": "已选择，材料处理发生异常",
    "found": "已有合格材料", "partial": "已有部分材料，缺项保留", "exhausted": "当前来源已用尽",
    "timeout": "当前材料等待已到期", "cached": "沿用当前等待或终态记录", "missing": "请求已不存在",
}


def tick(state_dir: str | Path, *, max_requests: int = 4, max_seconds: float = 10.0, now: str | None = None,
         rungs: Mapping[str, Any] | None = None, convert: bool = True) -> dict[str, Any]:
    """One bounded pass over a durable priority-ordered round-robin sweep."""

    started = time.monotonic()
    stamp = now or _now()
    state = Path(state_dir)
    summary: dict[str, Any] = {"converted": 0, "reopened": 0, "served": 0, "found": 0, "partial": 0,
                               "exhausted": 0, "timeout": 0, "waiting": 0, "deferred": 0, "budget_blocked": 0,
                               "open": 0, "results": []}
    if convert:
        converted = []
        for sweep in (convert_rewrite_flags, convert_repair_targets):
            try:
                converted += sweep(state, now=stamp)
            except Exception as exc:  # noqa: BLE001 - a broken input converts nothing this tick
                summary.setdefault("conversion_errors", []).append(f"{sweep.__name__}: {type(exc).__name__}")
        summary["converted"] = sum(1 for item in converted if item.get("write_status") == "fresh")
    if mr.ledger_path(state).is_file():
        summary["reopened"] = len(retry_sweep(state, now=stamp, deadline=started+max_seconds))
    pending = mr.open_requests(state, limit=5000, now=stamp)
    summary["open"] = len(pending)
    if not pending:
        return summary
    universe = mission_universe(state)
    pending, cursor_hash = _service_order(state, pending, universe)
    pool = pool_status(state, now=stamp)
    # The first request that reaches a costed rung on a spent pool records
    # ``budget_blocked`` (that is what raises the one pool card); the rest of
    # the tick then runs only the free rungs.
    allow_costed = True
    summary["pool"] = {k: pool[k] for k in ("day", "spent_calls", "cap_calls", "spent_usd_micros", "cap_usd_micros")}
    from .material_current_gap_priority import priority_gaps
    proofs, priority_info = priority_gaps(state, pending,
        deadline=min(started + max_seconds, time.monotonic() + 2.0))
    summary["priority"] = priority_info
    cap = max(1, int(max_requests))
    # Even a no-signal tick must notice a concurrent priority claim. This is
    # a CAS coordination read only; its ordinary order and quota are unchanged.
    _, _, priority_hash = _priority_service_order(state, [], universe)
    choices = [(view, False) for view in pending[:cap]]
    if proofs:
        urgent, first, priority_hash = _priority_service_order(state, [v for v in pending if v["id"] in proofs], universe)
        chosen = urgent[0]
        ordinary = [v for v in pending if v["id"] not in proofs]
        if cap == 1:
            choices = [(chosen, True)] if first or not ordinary else [(ordinary[0], False)]
        else:
            choices = [(v, False) for v in ordinary[:cap - 1]]
            choices = ([(chosen, True)] + choices) if first else (choices + [(chosen, True)])
    for turn, (view, is_priority) in enumerate(choices):
        if time.monotonic() - started >= max_seconds:
            summary["out_of_time"] = True
            break
        if proofs:
            claimed = _advance_priority_turn(state, view, universe, expected_hash=cursor_hash,
                expected_priority_hash=priority_hash, priority=is_priority, flip_order=turn == 0,
                now=stamp, deadline=started + max_seconds,
                priority_proof=proofs[view["id"]] if is_priority else None)
            claim_failed = claimed is None
            next_cursor_hash = cursor_hash if claimed is None else claimed[0]
            if claimed is not None:
                priority_hash = claimed[1]
        else:
            next_cursor_hash = _advance_service_cursor(
                state, view, universe, expected_hash=cursor_hash, now=stamp,
                deadline=started + max_seconds, expected_priority_hash=priority_hash)
            claim_failed = next_cursor_hash is None
        if claim_failed:
            summary["out_of_time" if time.monotonic() - started >= max_seconds
                    else "scheduling_busy"] = True
            break
        cursor_hash = next_cursor_hash
        try:
            result = serve(state, view["id"], rungs=rungs, now=stamp, allow_costed=allow_costed)
        except Exception as exc:  # noqa: BLE001 - one request's failure is not the tick's
            result = {"request_id": view["id"], "outcome": "error", "reason": f"{type(exc).__name__}: {exc}"[:300]}
        summary["served"] += 1
        outcome = str(result.get("outcome"))
        if outcome in summary and isinstance(summary[outcome], int):
            summary[outcome] += 1
        if outcome == "budget_blocked" and (result.get("budget") or {}).get("scope") == "pool":
            allow_costed = False
        diagnostic = {k: result.get(k) for k in ("request_id", "outcome", "status")}
        diagnostic["request_id"] = view["id"]
        diagnostic.update(selection_class="current_product_gap" if is_priority else "ordinary_round_robin",
                          reason_code=outcome, reason=_SERVICE_REASONS.get(outcome, "已选择，结果待核对"))
        if is_priority:
            diagnostic["priority_proof_hash"] = proofs[view["id"]]["content_hash"]
        if outcome == "error":
            diagnostic["error_type"] = str(result.get("reason") or "").split(":", 1)[0][:64]
        summary["results"].append(diagnostic)
    return summary


# ---------------------------------------------------------------------------
# the owner door: exactly three situations
# ---------------------------------------------------------------------------

CARD_KINDS = ("material_sources_exhausted", "material_budget_exceeded", "material_credentials_needed")


def _need_text(request: Mapping[str, Any]) -> str:
    from .material_sufficiency import CHECK_LABELS

    need = request.get("need") or {}
    section = next((r.get("section") for r in request.get("requesters") or () if r.get("section")), None)
    if section in CHECK_LABELS:
        return CHECK_LABELS[section]
    if need.get("kind") == "document":
        from .evidence_corpus import KIND_LABELS

        return "、".join(KIND_LABELS.get(k, k) for k in need.get("doc_kinds") or ())
    if need.get("kind") in ("data", "model"):
        return f"数据项「{str(need.get('data_item') or '').replace('check:', '')}」"
    question = re.split(r"\s+-- (?:wants|terms):", str(need.get("question") or ""), maxsplit=1)[0]
    return f"问题「{question[:60]}」"


def owner_door_cards(state_dir: str | Path, *, now: str | None = None,
                     company_labels: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """The needs-human cards this service raises (read-only), as plain dicts.

    ``needs_human`` wraps each in its card shape.  One card per company for
    requests exhausted in the last :data:`NEGATIVE_CACHE_DAYS` days, one per
    source that needs a login/approval, one for the day's pool and one per
    request whose own cap stopped it.
    """

    stamp = now or _now()
    labels = dict(company_labels or {})
    if not mr.ledger_path(state_dir).is_file():
        return []
    every = mr.open_requests(state_dir, statuses=None, limit=5000, now=stamp)
    cutoff = ((_parse_ts(stamp) or datetime.now(timezone.utc)) - timedelta(days=NEGATIVE_CACHE_DAYS)).isoformat()
    cards: list[dict[str, Any]] = []
    exhausted: dict[str, list[dict[str, Any]]] = {}
    credentials: dict[str, list[dict[str, Any]]] = {}
    budget_pool: list[dict[str, Any]] = []
    budget_request: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for summary in every:
        if summary["status"] not in ("exhausted", "open", "partial"):
            continue
        view = mr.request(state_dir, summary["id"]) or summary
        attempts = _round_attempts(view)
        if summary["status"] == "exhausted" and str(summary.get("last_event_at") or "") >= cutoff:
            exhausted.setdefault(str(view["subject"]), []).append(view)
        for attempt in attempts:
            if attempt.get("outcome") == "needs_credentials" and str(attempt.get("created_at") or "") >= cutoff:
                credentials.setdefault(str(attempt.get("source")), []).append(view)
        if summary["status"] in mr.ACTIVE and not summary.get("closed") and attempts \
                and attempts[-1].get("outcome") == "budget_blocked":
            try:
                blocked = json.loads(attempts[-1].get("detail") or "{}")
            except ValueError:
                blocked = {}
            if blocked.get("scope") == "pool" and blocked.get("key") == stamp[:10]:
                budget_pool.append(view)
            elif blocked.get("scope") == "request":
                # The last attempt predates a possible approval. Render from
                # current caps, not that stale refusal; no request is dispatched.
                source = str(attempts[-1].get("source") or "")
                rungs = default_rungs()
                rung = rungs.get(source)
                if rung is not None and rung.applies(view):
                    calls = int(rung.estimate_calls(view))
                    cost = int(USD_PER_CALL.get(source, Decimal(0)) * calls * 1_000_000)
                    caps, spent = request_caps(state_dir, view), request_spend(state_dir, view["id"])
                    if spent["calls"] + calls <= caps["calls"] and spent["cost_micros"] + cost <= caps["usd_micros"]:
                        continue
                    blocked = {**blocked, "spent_calls": spent["calls"], "spent_usd_micros": spent["cost_micros"],
                               "cap_calls": caps["calls"], "cap_usd_micros": caps["usd_micros"]}
                    blocked = _request_budget_proposal(view, blocked, source=source, rungs=rungs)
                budget_request.append((view, blocked))
    for subject, views in sorted(exhausted.items()):
        name = labels.get(subject) or subject
        lines = [f"{_need_text(v)}（{v.get('period') or '不限期间'}）——已尝试：" + "；".join(tried_sources(v))
                 for v in views[:6]]
        cards.append({
            "kind": "material_sources_exhausted", "ref": f"material-exhausted:{subject}",
            "at": max(str(v.get("last_event_at") or "") for v in views),
            "title": f"{name}：{len(views)} 项材料暂未补齐",
            "why": "本轮检索尚未补齐这些材料，以下记录说明各来源的处理情况：" + "\n".join(lines),
            "action": (f"系统会在 {NEGATIVE_CACHE_DAYS} 天后或这家公司有新资料到达时自动重试。"
                       "已有相关材料时，也可以请管理员补录。"),
            "consequence": "相关章节按现有材料起草，并在正文注明缺少的材料和已尝试的来源。",
            "where": "无需操作，系统会自动重试",
            "detail": {"company_ref": subject, "request_refs": [v["id"] for v in views],
                       "requests": [{"id": v["id"], "need": v.get("need"), "period": v.get("period"),
                                     "tried": tried_sources(v)} for v in views[:20]]},
        })
    for source, views in sorted(credentials.items()):
        label = _source_label(source)
        cards.append({
            "kind": "material_credentials_needed", "ref": f"material-credentials:{source}",
            "at": max(str(v.get("last_event_at") or "") for v in views),
            "title": f"{label}需要登录或授权，材料获取跳过了这个来源",
            "why": (f"{len(views)} 项材料请求在检索「{label}」时被拒绝：这个来源尚未批准或登录已失效。"
                    "其余来源照常检索。"),
            "action": f"请在本机完成「{label}」的登录或批准；恢复后系统自动把这个来源加回检索顺序。",
            "consequence": f"不处理，需要「{label}」的材料只能从其他来源获取，可能一直缺少。",
            "where": "本机（来源登录或批准）",
            "detail": {"source": source, "source_ref": SOURCE_REFS.get(source),
                       "request_refs": [v["id"] for v in views][:50]},
        })
    if budget_pool:
        pool = pool_status(state_dir, now=stamp)
        calls = max(10, CALL_ESTIMATES["alphaengine"] * len(budget_pool))
        usd = max(_micros("0.50"), int(USD_PER_CALL["web"] * calls * 1_000_000))
        ref = budget_card_ref("pool", pool["day"], usd, calls)
        cards.append({
            "kind": "material_budget_exceeded", "ref": ref, "at": stamp,
            "title": f"今天的材料获取额度已用完（{len(budget_pool)} 项请求在等待）",
            "why": (f"今天已用 {pool['spent_calls']} 次检索、约 {_usd(pool['spent_usd_micros'])} 美元，"
                    f"上限 {pool['cap_calls']} 次、{_usd(pool['cap_usd_micros'])} 美元。"
                    "已入库资料的检索不受影响，外部来源的检索暂停到明天。"),
            "action": f"如需今天继续，可以批准追加 {_usd(usd)} 美元（约 {calls} 次检索）。",
            "consequence": "不处理，等待中的请求明天自动继续。",
            "where": "本卡片",
            "detail": {"scope": "pool", "day": pool["day"], "approve_usd": _usd(usd), "approve_calls": calls,
                       "request_refs": [v["id"] for v in budget_pool][:50]},
        })
    for view, blocked in budget_request:
        usd = int(blocked.get("approve_usd_micros", _micros("0.50")))
        calls = int(blocked.get("approve_calls", 3))
        ref = budget_card_ref("request", view["id"], usd, calls, basis={
            key: blocked.get(key) for key in ("cap_calls", "cap_usd_micros", "spent_calls",
                                             "spent_usd_micros", "remaining_sources")})
        approved = [a for a in _approvals(state_dir) if a["scope"] == "request" and a["key"] == view["id"]]
        prior_calls = sum(a["calls"] for a in approved)
        prior_usd = sum(a["usd_micros"] for a in approved)
        remaining_usd = max(0, int(blocked.get("cap_usd_micros") or 0) - int(blocked.get("spent_usd_micros") or 0))
        money = parse_budget_card_ref(ref)["usd_micros"]
        shortage = "检索次数" if not money else "金额" if not calls else "检索次数和金额"
        approval_text = (f"批准追加 {calls} 次检索；金额上限保持不变"
                         if not money else f"批准追加 {_usd(money)} 美元和 {calls} 次检索"
                         if calls else f"批准追加 {_usd(money)} 美元；检索次数上限保持不变")
        prior_text = (f"此前已批准追加 {_usd(prior_usd)} 美元、{prior_calls} 次检索，已计入当前上限。"
                      if approved else "")
        source_text = "、".join(_source_label(s) for s in blocked.get("remaining_sources") or ())
        name = labels.get(str(view["subject"])) or view["subject"]
        cards.append({
            "kind": "material_budget_exceeded", "ref": ref,
            "at": str(view.get("last_event_at") or stamp),
            "title": f"{name}：{_need_text(view)}的{shortage}额度不足",
            "why": (prior_text + f"这项请求累计已用 {blocked.get('spent_calls')} 次检索、"
                    f"{Decimal(int(blocked.get('spent_usd_micros') or 0)) / 1_000_000:.3f} 美元；"
                    f"当前上限 {blocked.get('cap_calls')} 次、{_usd(blocked.get('cap_usd_micros') or 0)} 美元。"
                    f"金额还剩 {Decimal(remaining_usd) / 1_000_000:.3f} 美元。"
                    + (f"本轮尚待检索：{source_text}。" if source_text else "") +
                    "已尝试：" + "；".join(tried_sources(view))),
            "action": approval_text + "。追加建议覆盖本轮剩余来源，找到所需材料即停止。",
            "consequence": "不处理，到截止时间后按现有材料起草，并注明这项材料缺少。",
            "where": "本卡片",
            "detail": {"scope": "request", "request_ref": view["id"], "company_ref": view["subject"],
                       "approve_usd": _usd(money), "approve_calls": calls,
                       "approved_extra_usd": _usd(prior_usd), "approved_extra_calls": prior_calls,
                       "remaining_sources": blocked.get("remaining_sources") or []},
        })
    return cards


__all__ = [
    "CALL_ESTIMATES", "CARD_KINDS", "DRAFTING_LANES", "EFFECT_FIELDS", "IndexedCorpusRung", "LADDER", "LANE_OWNED",
    "LaneOwnedRung", "MaterialAcquisitionError", "NEGATIVE_CACHE_DAYS", "REPAIR_MATERIAL_CODES", "RUNG_WAIT_HOURS",
    "RungContext", "RungResult", "SCHEMA_VERSION", "SOURCE_FOR_REF", "SOURCE_REFS", "SalesNotesRung",
    "TargetedSourceRung", "budget_card_ref", "build_queries", "convert_repair_targets", "convert_rewrite_flags",
    "data_item_terms", "default_rungs", "enqueue_targeted_query", "ensure_schema", "evaluate_acceptance",
    "identity_for", "link_arrivals", "material_arrived", "new_material_since", "open_for_effect",
    "open_for_inquiry", "open_for_repair_target", "open_for_rewrite_flag", "owner_door_cards",
    "parse_budget_card_ref", "pending_targeted_queries", "period_window", "pool_status", "priority_key",
    "record_targeted_event", "request_caps", "request_spend", "requests_for_document", "retry_due",
    "retry_sweep", "serve", "targeted_query_status", "tick", "tried_sources", "xbrl_facts",
]
