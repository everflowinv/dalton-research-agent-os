"""Batch F: judging a result-type event the way an analyst does.

The research capability diagnosis (2026-10-09, section 四) found the ACN Q4
8-K judged from 11 income-statement numbers, two theses and twelve Claims --
no body text, no consensus, no prior guidance, no preview -- and answered
NO_CHANGE / research with a question nothing could bind.  The exhibit, the Q4
call, a BofA sales note and the GS preview were all in the library.  This
module is what the judge now reads for a result-type event (8-K Item 2.02 /
earnings release, its call, a guidance change), and the contract it answers
to.  Deterministic and free: no model call, read-only on every store.

What it builds (``build_result_material``)
-----------------------------------------
* **the same-event pack** -- ``material_pack.build(..., "event_judgement",
  topics=...)`` with ``as_of`` = event day + 3, so every section reads the
  event window [day - 3, day + 3] (8-K exhibit text, the call, broker flash
  notes, sales notes, news), plus previews from the 14 days before;
* **the expectations baseline** -- pre-event consensus
  (``consensus_estimate.period_consensus(as_of=day - 1)``) for the reported
  period and the next guidance periods, the company's prior guidance (the
  previous release's outlook, else the guided range the release itself
  restates), and the previews in the pack;
* **the actuals** -- the release's own figures, read deterministically from
  the exhibit (``earnings_release_outlook``: stored record, else the intake's
  spool bytes, else the indexed exhibit text);
* **the market reaction** -- the event day's close-to-close move against SPY
  (or another broad index) and the covered basket, from the price authority;
  「无基准」 when neither is held.

Every figure is tagged ``[N…]`` (bound to a ``pack-json:`` pointer) or
``[P…]`` (bound to a ``doc-passage:`` ref), continuing the pack's own tag
numbering, so a judgement can cite them and a summary can publish them.

The contract (``result_contract_problems``)
------------------------------------------
The judge first fills 「实际 vs 预期」 (revenue, local-currency growth,
bookings / book-to-bill where the company reports them, margin, EPS,
next-quarter and full-year guidance against prior guidance and consensus),
every figure carrying the tag it came from; then the market reaction against a
benchmark; then the thesis impact.  NO_CHANGE on a beat or a miss must say why
the beat or miss does not change the thesis.  When the event's own body was
not read the only answer is 「证据不足，安排读取」 (NO_CHANGE / research with
``evidence_status: insufficient``) and a MaterialRequest; the lane records
that deterministically, without paying for a model.

Re-judgement (``rejudgement_reason``)
------------------------------------
A result event is judged again -- at most :data:`MAX_REJUDGEMENTS` times --
when the body its last judgement lacked has arrived (``material_arrived``; a
judgement written before this batch, which never read a body, counts as
lacking it), or when a same-event document arrives within
:data:`REJUDGE_WINDOW_HOURS` of the moment the event became known
(``same_event_document_within_72h``).  The reason is recorded on the
re-judgement.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

SCHEMA_VERSION = "event-result:0.1"
RESULT_CONTRACT_VERSION = "event-result-contract:f1"
EVENT_WINDOW_DAYS = 3
PREVIEW_DAYS = 14
REJUDGE_WINDOW_HOURS = 72
MAX_REJUDGEMENTS = 2
#: An event first seen this many days after it happened is an old event being
#: processed now; its summary says so.
REPROCESSED_AFTER_DAYS = 7
#: How far back the re-judgement scan looks for result events.
REJUDGE_LOOKBACK_DAYS = 30
PACK_BUDGET_CHARS = 40_000
BODY_SECTION_CHARS = 12_000
DOCUMENT_BODY_CHARS = 6_000

RESULT_EVENT_KINDS: frozenset[str] = frozenset({"earnings_release", "transcript"})
#: Kinds whose event *is* a document: the judge must read its body.
DOCUMENT_EVENT_KINDS: frozenset[str] = frozenset({
    "earnings_release", "transcript", "filing", "sales_note", "expert_excerpt", "news",
})
#: Tiers whose unread document may not be judged on its title: the deterministic
#: 「证据不足，安排读取」 applies.  News and crowd keep the old reading.
BODY_REQUIRED_TIERS: frozenset[str] = frozenset({
    "primary_filing", "management_direct", "expert_network", "sell_side", "vendor_note",
})
TRANSCRIPT_SPECS: frozenset[str] = frozenset({"earnings-call-transcripts"})

#: The table's rows, in the order an analyst reads them.
ROW_KEYS: tuple[str, ...] = (
    "revenue", "revenue_growth_lc", "bookings", "book_to_bill", "operating_margin", "eps",
    "guidance_next_quarter", "guidance_full_year",
)
REQUIRED_ROWS: tuple[str, ...] = (
    "revenue", "revenue_growth_lc", "operating_margin", "eps", "guidance_next_quarter",
    "guidance_full_year",
)
#: Required when the release reports them ("where applicable").
APPLICABLE_ROWS: tuple[str, ...] = ("bookings", "book_to_bill")
ROW_LABELS: Mapping[str, str] = {
    "revenue": "收入", "revenue_growth_lc": "本币收入增速", "bookings": "新签订单",
    "book_to_bill": "订单收入比", "operating_margin": "营业利润率", "eps": "每股收益",
    "guidance_next_quarter": "下季度指引", "guidance_full_year": "全年指引",
}
COMPARISONS: tuple[str, ...] = (
    "beat", "miss", "inline", "above_range", "within_range", "below_range", "n/a",
)
COMPARISON_LABELS: Mapping[str, str] = {
    "beat": "高于预期", "miss": "低于预期", "inline": "符合预期", "above_range": "高于指引区间",
    "within_range": "落在指引区间内", "below_range": "低于指引区间", "n/a": "无可比基准",
}
SURPRISES: frozenset[str] = frozenset({"beat", "miss", "above_range", "below_range"})
EVIDENCE_STATUSES: tuple[str, ...] = ("read", "insufficient")
VERDICT_EVIDENCE_INSUFFICIENT = "evidence_insufficient"
VERDICT_LABELS: Mapping[str, str] = {
    VERDICT_EVIDENCE_INSUFFICIENT: "证据不足，安排读取",
    "NO_CHANGE": "维持基准判断", "THESIS_STRENGTHENED": "核心论点增强",
    "THESIS_WEAKENED": "核心论点受损", "THESIS_BROKEN": "核心论点被推翻",
    "NEW_THESIS": "形成新的投资论点",
}
NO_BENCHMARK = "无基准"
REJUDGE_REASONS: Mapping[str, str] = {
    "material_arrived": "上次研判缺的正文已入库，按新材料重判",
    "legacy_judgement_without_body": "上次研判没有读到事件正文（批次 F 之前的研判），正文已入库，重判",
    "same_event_document_within_72h": "事件可得后 72 小时内到达同一事件的新文档，重判",
}
MIN_EXPLANATION_CHARS = 20
MAX_CELL_CHARS = 300
MAX_TEXT_CHARS = 1200


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _dec(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _plain(value: Decimal | None, quantum: str | None = None) -> str | None:
    if value is None:
        return None
    if quantum:
        value = value.quantize(Decimal(quantum))
    text = format(value, "f")
    return text


def _bare(token: str) -> str | None:
    """``$18.68`` / ``7%`` / ``-1.2`` / ``22,170`` -> ``18.68`` / ``7`` / ``1.2`` / ``22170``."""

    value = _dec(str(token).strip().lstrip("$€£+-").rstrip("%").replace(",", ""))
    if value is None:
        return None
    value = value.copy_abs()
    return format(value.quantize(Decimal(1)) if value == value.to_integral() else value.normalize(), "f")


def _day(value: Any) -> str | None:
    text = str(value or "")[:10]
    try:
        date.fromisoformat(text)
    except ValueError:
        return None
    return text


def _shift(day: str, days: int) -> str:
    return (date.fromisoformat(day) + timedelta(days=days)).isoformat()


def _ts(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _query(connection: Any, sql: str, params: Sequence[Any] = ()) -> list[Any]:
    if connection is None:
        return []
    try:
        return connection.execute(sql, tuple(params)).fetchall()
    except sqlite3.Error:
        return []


def _table_exists(connection: Any, name: str) -> bool:
    return bool(_query(connection, "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)))


def _row_json(row: Any, key: str = "record_json") -> dict[str, Any] | None:
    try:
        value = json.loads(row[key])
    except (TypeError, ValueError, KeyError, IndexError):
        return None
    return value if isinstance(value, dict) else None


# ---------------------------------------------------------------------------
# what kind of event this is, and when it happened
# ---------------------------------------------------------------------------


def is_result_event(event: Mapping[str, Any]) -> bool:
    """An earnings release (8-K 2.02) or the company's own results call."""

    kind = event.get("kind")
    if kind == "earnings_release":
        return True
    if kind == "transcript":
        return (event.get("payload") or {}).get("spec_ref") in TRANSCRIPT_SPECS
    if kind == "filing":
        title = str((event.get("payload") or {}).get("title") or "")
        return bool(re.search(r"(?i)item\s*2\.02|results of operations", title))
    return False


def is_document_event(event: Mapping[str, Any]) -> bool:
    payload = event.get("payload") or {}
    return event.get("kind") in DOCUMENT_EVENT_KINDS and bool(payload.get("document_ref"))


def group_readable(state_dir: str | Path | None, members: Sequence[Mapping[str, Any]]) -> bool:
    """Whether a group's other inputs give the judge something to read.

    A Claim carries its statement; an input whose tier does not require a body
    is read on its payload as before; a document input counts when its body is
    in the index.
    """

    for item in members:
        if not body_required(item):
            return True
        if body_document(state_dir, item, None, None) is not None:
            return True
    return False


def body_required(event: Mapping[str, Any]) -> bool:
    """A document event whose unread body forbids judging it on its title."""

    return is_document_event(event) and (
        is_result_event(event) or event.get("evidence_tier") in BODY_REQUIRED_TIERS)


def _release_period(value: Any) -> str | None:
    found = re.fullmatch(r"FY(\d{4})-?Q([1-4])", str(value or ""))
    return f"FY{found.group(1)}Q{found.group(2)}" if found else None


def next_periods(period: str | None) -> tuple[str | None, str | None]:
    """``FY2026Q4`` -> (``FY2027Q1``, ``FY2027``); ``FY2026Q2`` -> (``FY2026Q3``, ``FY2026``)."""

    found = re.fullmatch(r"FY(\d{4})Q([1-4])", str(period or ""))
    if not found:
        return None, None
    year, quarter = int(found.group(1)), int(found.group(2))
    if quarter == 4:
        return f"FY{year + 1}Q1", f"FY{year + 1}"
    return f"FY{year}Q{quarter + 1}", f"FY{year}"


def _release_events(connection: Any, company_ref: str) -> list[dict[str, Any]]:
    rows = _query(connection, "SELECT record_json FROM research_events WHERE company_ref=? "
                              "AND kind='earnings_release' ORDER BY occurred_at, event_id", (company_ref,))
    return [record for record in (_row_json(row) for row in rows) if record]


def _indexed_document(state_dir: str | Path | None, document_ref: Any) -> dict[str, Any] | None:
    if state_dir is None or not isinstance(document_ref, str) or not document_ref:
        return None
    from . import evidence_index

    connection = evidence_index.open_index_readonly(state_dir)
    if connection is None:
        return None
    try:
        row = connection.execute(
            "SELECT document_ref, kind, document_date, title, house, indexed_at, acquired_at, passage_count "
            "FROM evidence_documents WHERE document_ref=? AND superseded_by IS NULL "
            "ORDER BY doc_id DESC LIMIT 1", (document_ref,)).fetchone()
    except sqlite3.Error:
        row = None
    finally:
        connection.close()
    return None if row is None else {key: row[key] for key in row.keys()}


def result_anchor(connection: Any, event: Mapping[str, Any], *,
                  state_dir: str | Path | None = None) -> dict[str, Any] | None:
    """The one occurrence a result-type event belongs to: day, period, accession.

    A release anchors itself (its report date).  A results call anchors to the
    company's release within ±3 days of the call's document date, else to
    itself, so the release and its call are one event.
    """

    if not is_result_event(event):
        return None
    payload = event.get("payload") or {}
    company_ref = str(event.get("company_ref"))
    if state_dir is None:
        state_dir = state_dir_of(connection)
    if event.get("kind") == "earnings_release":
        day = _day(payload.get("report_date")) or _day(event.get("occurred_at"))
        return {"day": day, "period": _release_period(payload.get("period")),
                "period_end": payload.get("period_end"), "accession": payload.get("accession"),
                "release_event_ref": event.get("id"), "basis": "release"}
    document = _indexed_document(state_dir, payload.get("document_ref"))
    day = _day((document or {}).get("document_date")) or _day(event.get("occurred_at"))
    if day is None:
        return None
    occurred = _day(event.get("occurred_at"))
    if document is not None and occurred and abs(
            (date.fromisoformat(occurred) - date.fromisoformat(day)).days) > REPROCESSED_AFTER_DAYS:
        # A call found long after it happened (a backfill) is a document to
        # read, not this quarter's result event.
        return None
    best = None
    for release in _release_events(connection, company_ref):
        release_day = _day((release.get("payload") or {}).get("report_date")) or _day(release.get("occurred_at"))
        if release_day is None:
            continue
        distance = abs((date.fromisoformat(release_day) - date.fromisoformat(day)).days)
        if distance <= EVENT_WINDOW_DAYS and (best is None or distance < best[0]):
            best = (distance, release, release_day)
    if best is not None:
        release = best[1]
        rp = release.get("payload") or {}
        return {"day": best[2], "period": _release_period(rp.get("period")), "period_end": rp.get("period_end"),
                "accession": rp.get("accession"), "release_event_ref": release.get("id"),
                "basis": "call_matched_to_release"}
    return {"day": day, "period": None, "period_end": None, "accession": None,
            "release_event_ref": None, "basis": "call_alone",
            "document_ref": payload.get("document_ref")}


def state_dir_of(connection: Any) -> Path | None:
    """The state directory of a Core connection (``core.sqlite``'s folder), or None."""

    for row in _query(connection, "PRAGMA database_list"):
        try:
            name, path = row[1], row[2]
        except (IndexError, TypeError):
            continue
        if name == "main" and path:
            return Path(path).parent
    return None


def result_key(connection: Any, event: Mapping[str, Any], *,
               state_dir: str | Path | None = None) -> tuple[str, ...] | None:
    anchor = result_anchor(connection, event, state_dir=state_dir)
    if anchor is None or not anchor.get("day"):
        return None
    if anchor.get("basis") == "call_alone":
        # A call with no release beside it is its own occurrence: calls of
        # different quarters found on one day are never one event.
        return ("result_event", str(event.get("company_ref")), str(anchor["day"]), "call",
                str(anchor.get("document_ref") or event.get("id")))
    return ("result_event", str(event.get("company_ref")), str(anchor["day"]))


def event_dates(event: Mapping[str, Any], *, anchor: Mapping[str, Any] | None = None,
                judged_at: str | None = None) -> dict[str, Any]:
    """Original event date and information-available date, kept apart.

    ``occurred_on`` is when it happened (a release's report date); the
    information became available to us when the event was recorded
    (``created_at``).  An event first seen more than a week after it happened
    is an old event processed now, and says so.
    """

    occurred = (anchor or {}).get("day") or _day(event.get("occurred_at"))
    available = event.get("created_at")
    lag = None
    if occurred and _day(available):
        lag = (date.fromisoformat(_day(available)) - date.fromisoformat(occurred)).days
    return {"occurred_on": occurred, "information_available_at": available,
            "information_available_on": _day(available), "lag_days": lag,
            "reprocessed_old_event": bool(lag is not None and lag > REPROCESSED_AFTER_DAYS),
            **({"judged_at": judged_at} if judged_at else {})}


def dates_line(dates: Mapping[str, Any]) -> str:
    line = (f"事件原始日期 {dates.get('occurred_on') or '未知'}；信息可得日期 "
            f"{dates.get('information_available_on') or '未知'}")
    if dates.get("reprocessed_old_event"):
        line += f"（旧事件重处理：晚 {dates.get('lag_days')} 天才进入研判）"
    return line


# ---------------------------------------------------------------------------
# numbers: formatting and tagging
# ---------------------------------------------------------------------------


_SCALE_SUFFIX = {"billion": "B", "million": "M", "thousand": "K"}


def display_figure(value: Any, *, unit: str | None, scale: str | None, currency: str | None) -> str | None:
    number = _dec(value)
    if number is None:
        return None
    text = str(value)
    if unit == "percent":
        return f"{text}%"
    if unit == "bps":
        return f"{text}bps"
    if unit == "ratio":
        return text
    symbol = "$" if currency == "USD" else ("€" if currency == "EUR" else ("£" if currency == "GBP" else ""))
    return f"{symbol}{text}{_SCALE_SUFFIX.get(scale or '', '')}"


def display_item(item: Mapping[str, Any]) -> str | None:
    kw = {"unit": item.get("unit"), "scale": item.get("scale"), "currency": item.get("currency")}
    if item.get("low") is not None and item.get("high") is not None:
        low, high = display_figure(item["low"], **kw), display_figure(item["high"], **kw)
        return None if low is None or high is None else f"{low}–{high}"
    return display_figure(item.get("value"), **kw)


class Tagger:
    """New ``[N…]`` / ``[P…]`` tags continuing a pack's numbering."""

    def __init__(self, pack: Any, *, root: str = "event_result") -> None:
        self.pack = pack
        self.root = root
        self.tags: dict[str, str] = dict(getattr(pack, "tags", {}) or {})
        self.texts: dict[str, str] = {}
        self.json: dict[str, Any] = {}
        self._next = {"N": 1, "P": 1}
        for tag in self.tags:
            found = re.fullmatch(r"([NP])(\d+)", tag)
            if found:
                self._next[found.group(1)] = max(self._next[found.group(1)], int(found.group(2)) + 1)
        self._number_meta = dict(((getattr(pack, "json", None) or {}).get("number_tags") or {}))
        for tag, meta in self._number_meta.items():
            self.texts[tag] = " ".join(str(meta.get(key)) for key in ("display", "value") if meta.get(key) is not None)
        self._by_pointer: dict[str, str] = {}
        self.added: list[dict[str, Any]] = []

    def number(self, path: Sequence[Any], value: Any, display: str) -> str:
        pointer = "/" + "/".join(str(part).replace("~", "~0").replace("/", "~1") for part in (self.root, *path))
        tag = self._by_pointer.get(pointer)
        if tag is None:
            tag = f"N{self._next['N']}"
            self._next["N"] += 1
            self._by_pointer[pointer] = tag
            self.tags[tag] = "pack-json:" + pointer
            self.texts[tag] = f"{display} {value}"
            node = self.json
            for part in path[:-1]:
                node = node.setdefault(str(part), {})
            node[str(path[-1])] = value
        return tag

    def passage(self, ref: str, text: str, meta: Mapping[str, Any] | None = None) -> str:
        for tag, bound in self.tags.items():
            if bound == ref and tag.startswith("P"):
                self.texts.setdefault(tag, text)
                return tag
        tag = f"P{self._next['P']}"
        self._next["P"] += 1
        self.tags[tag] = ref
        self.texts[tag] = text
        self.added.append({"tag": tag, "ref": ref, "text": text, **dict(meta or {})})
        return tag

    def added_lines(self, heading: str) -> list[str]:
        """The passages tagged here that the pack did not print: printed, so every tag is readable."""

        if not self.added:
            return []
        lines = ["", heading]
        for item in self.added:
            lines.append(f"[{item['tag']}] {item.get('kind_label') or item.get('kind') or '原文'} · "
                         f"{item.get('date') or ''} · 《{item.get('title') or ''}》")
            lines.append(str(item["text"]).strip())
        self.added = []
        return lines


# ---------------------------------------------------------------------------
# the release's own figures, and the guidance before it
# ---------------------------------------------------------------------------


def _fye_month(connection: Any, company_ref: str, period_end: Any) -> int | None:
    try:
        from .financial_period import fiscal_year_end

        found = fiscal_year_end(connection, company_ref) if connection is not None else None
        if found:
            return int(found["month"])
    except Exception:  # noqa: BLE001 - the month only sharpens period labels
        pass
    return None


def _period_dict(label: str | None) -> dict[str, Any] | None:
    found = re.fullmatch(r"FY(\d{4})Q([1-4])", str(label or ""))
    if not found:
        return None
    return {"kind": "quarter", "fiscal_year": int(found.group(1)), "fiscal_quarter": int(found.group(2))}


def reconstruct_text(passages: Sequence[Mapping[str, Any]]) -> str:
    """A document's text from its indexed passages (overlaps folded)."""

    text = ""
    for item in sorted(passages, key=lambda p: int(p.get("start") or 0)):
        start = int(item.get("start") or 0)
        body = str(item.get("text") or "")
        if start <= len(text):
            text = text[:start] + body
        else:
            text += " " * (start - len(text)) + body
    return text


def _parse_text(text: str, *, fye_month: int | None, period: str | None) -> list[dict[str, Any]]:
    from .earnings_release_outlook import parse_release

    # The index keeps the blank line between an outlook table's label and its
    # value; the outlook parser reads label/value pairs on adjacent lines.
    collapsed = re.sub(r"[ \t]*\n\s*\n+", "\n", text)
    try:
        return parse_release(collapsed, fye_month=fye_month, release_period=_period_dict(period))
    except Exception:  # noqa: BLE001 - an unparseable exhibit is no items
        return []


def release_items(connection: Any, state_dir: str | Path | None, *, company_ref: str,
                  accession: str | None, period: str | None, period_end: Any,
                  body_passages: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """The release's typed figures: stored outlook record, spool bytes, or the indexed text."""

    fye = _fye_month(connection, company_ref, period_end)
    if accession and _table_exists(connection, "earnings_release_outlooks"):
        from .earnings_release_outlook import PARSER_VERSION

        for row in _query(connection, "SELECT record_json FROM earnings_release_outlooks WHERE accession=? "
                                      "AND parser_version=? ORDER BY created_at DESC", (accession, PARSER_VERSION)):
            record = _row_json(row)
            if record and record.get("items"):
                return {"items": list(record["items"]), "source": "stored_outlook",
                        "ref": record.get("id"), "report_date": record.get("report_date")}
    if accession and state_dir is not None:
        try:
            from .earnings_release_outlook import exhibit_bytes, intake_reads, outlook_record

            for read in intake_reads(connection, company_ref):
                if read.get("accession") != accession:
                    continue
                raw, _reason = exhibit_bytes(state_dir, str(read["body_sha256"]))
                if raw is None:
                    continue
                body = outlook_record(read, raw, fye_month=fye)
                if body.get("items"):
                    return {"items": list(body["items"]), "source": "exhibit_spool",
                            "ref": f"sec:filing:{accession}", "report_date": body.get("report_date")}
        except Exception:  # noqa: BLE001 - fall through to the indexed text
            pass
    if body_passages:
        items = _parse_text(reconstruct_text(body_passages), fye_month=fye, period=period)
        if items:
            return {"items": items, "source": "indexed_exhibit_text",
                    "ref": str(body_passages[0].get("document_ref")), "report_date": None}
    return {"items": [], "source": None, "ref": None, "report_date": None,
            "reason": "没有读到业绩稿里的收入、订单或指引句子"}


def prior_guidance_items(connection: Any, state_dir: str | Path | None, *, company_ref: str,
                         before: str, identity: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The previous release's outlook: what the company guided before this event."""

    try:
        from .earnings_release_outlook import exhibit_bytes, intake_reads, outlook_record

        reads = [read for read in intake_reads(connection, company_ref)
                 if str(read.get("report_date") or "") < before]
        reads.sort(key=lambda read: str(read.get("report_date") or ""), reverse=True)
        fye = _fye_month(connection, company_ref, None)
        for read in reads[:2]:
            if state_dir is None:
                break
            raw, _reason = exhibit_bytes(state_dir, str(read["body_sha256"]))
            if raw is None:
                continue
            body = outlook_record(read, raw, fye_month=fye)
            guidance = [item for item in body.get("items") or () if item.get("kind") == "guidance"]
            if guidance:
                return {"items": guidance, "source": "prior_release", "report_date": read.get("report_date"),
                        "ref": f"sec:filing:{read.get('accession')}"}
    except Exception:  # noqa: BLE001
        pass
    if state_dir is not None and identity is not None:
        try:
            from . import evidence_index
            from .material_sufficiency import own_documents

            docs = own_documents(state_dir, company_ref, identity, ["sec_exhibit"], as_of=_shift(before, -1),
                                 days=120)
            for doc in docs[:2]:
                passages = evidence_index.passages_for_document(state_dir, doc["document_ref"])
                items = [item for item in _parse_text(reconstruct_text(passages),
                                                      fye_month=_fye_month(connection, company_ref, None),
                                                      period=None)
                         if item.get("kind") == "guidance"]
                if items:
                    return {"items": items, "source": "prior_release_indexed", "report_date": doc.get("date"),
                            "ref": doc["document_ref"], "passages": passages}
        except Exception:  # noqa: BLE001
            pass
    return {"items": [], "source": None, "reason": "没有找到上一份业绩稿的指引"}


# ---------------------------------------------------------------------------
# the market's reaction
# ---------------------------------------------------------------------------


def _bars(connection: Any, company_ref: str) -> list[dict[str, Any]]:
    try:
        from .market_data_block import latest_series

        series = latest_series(connection, company_ref)
    except Exception:  # noqa: BLE001
        series = None
    bars = [bar for bar in (series or {}).get("bars") or () if _dec(bar.get("close")) is not None]
    bars.sort(key=lambda bar: bar["date"])
    return bars


def _move(bars: Sequence[Mapping[str, Any]], day: str) -> dict[str, Any] | None:
    for index, bar in enumerate(bars):
        if bar["date"] >= day and index > 0:
            prev = bars[index - 1]
            close, before = _dec(bar["close"]), _dec(prev["close"])
            if not before:
                return None
            return {"date": bar["date"], "close": bar["close"], "previous_date": prev["date"],
                    "previous_close": prev["close"],
                    "percent": _plain((close / before - 1) * 100, "0.01")}
    return None


def market_reaction(connection: Any, company_ref: str, day: str, *,
                    basket: Sequence[str] = ()) -> dict[str, Any]:
    """The event day's close-to-close move, against a broad index and the basket."""

    from .market_data_block import BENCHMARK_TICKERS

    own = _move(_bars(connection, company_ref), day)
    out: dict[str, Any] = {"day": day, "status": "ok" if own else "unavailable", "move": own,
                           "benchmark": {"status": "unavailable", "label": NO_BENCHMARK},
                           "basket": {"status": "unavailable", "label": NO_BENCHMARK}}
    if own is None:
        out["reason"] = "价格库里没有事件日前后的收盘价"
        return out
    held = {}
    for row in _query(connection, "SELECT DISTINCT ticker, company_ref FROM market_price_series_versions"):
        held[row["ticker"]] = row["company_ref"]
    ticker = next((t for t in BENCHMARK_TICKERS if t in held), None)
    if ticker is not None:
        bench = _move(_bars(connection, held[ticker]), day)
        if bench is not None and bench["date"] == own["date"]:
            out["benchmark"] = {"status": "available", "ticker": ticker, "percent": bench["percent"],
                                "relative_pp": _plain(_dec(own["percent"]) - _dec(bench["percent"]), "0.01")}
    else:
        out["benchmark"]["reason"] = "价格库里没有 SPY 或其他宽基指数序列"
    peers = []
    for ref in basket:
        if ref == company_ref:
            continue
        moved = _move(_bars(connection, ref), day)
        if moved is not None and moved["date"] == own["date"]:
            peers.append(_dec(moved["percent"]))
    if peers:
        mean = sum(peers) / len(peers)
        out["basket"] = {"status": "available", "members": len(peers), "percent": _plain(mean, "0.01"),
                         "relative_pp": _plain(_dec(own["percent"]) - mean, "0.01")}
    else:
        out["basket"]["reason"] = "同组公司没有同一交易日的收盘价"
    return out


# ---------------------------------------------------------------------------
# building what the judge reads
# ---------------------------------------------------------------------------


def _identity(state_dir: str | Path, company_ref: str) -> dict[str, Any]:
    from . import material_sufficiency as ms

    connection = ms.open_core_ro(state_dir)
    try:
        return ms.company_identity(connection, company_ref, state_dir)
    finally:
        if connection is not None:
            connection.close()


def body_document(state_dir: str | Path | None, event: Mapping[str, Any], anchor: Mapping[str, Any] | None,
                  identity: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The indexed document that *is* this event's body (exhibit 99.1 for a release)."""

    if state_dir is None:
        return None
    payload = event.get("payload") or {}
    if event.get("kind") == "earnings_release" and payload.get("accession"):
        from . import evidence_index

        accession = str(payload["accession"])
        day = (anchor or {}).get("day") or _day(event.get("occurred_at"))
        try:
            docs = evidence_index.documents(
                state_dir, company_ref=str(event.get("company_ref")), kinds=["sec_exhibit"],
                date_from=_shift(day, -EVENT_WINDOW_DAYS) if day else None,
                date_to=_shift(day, EVENT_WINDOW_DAYS) if day else None, limit=50)
        except Exception:  # noqa: BLE001
            docs = []
        for doc in docs:
            if accession in str(doc.get("document_ref")):
                return doc
        return None
    found = _indexed_document(state_dir, payload.get("document_ref"))
    if found is not None and found.get("passage_count"):
        return {**found, "date": found.get("document_date")}
    return None


def _body_passages(state_dir: str | Path, document_ref: str) -> list[dict[str, Any]]:
    from . import evidence_index

    try:
        return evidence_index.passages_for_document(state_dir, document_ref)
    except Exception:  # noqa: BLE001
        return []


def _passage_for(passages: Sequence[Mapping[str, Any]], needles: Sequence[str]) -> Mapping[str, Any] | None:
    wanted = [needle for needle in needles if needle]
    if not wanted:
        return None
    for item in passages:
        text = str(item.get("text") or "")
        if all(needle in text for needle in wanted):
            return item
    return None


def _digits(item: Mapping[str, Any]) -> list[str]:
    out = []
    for key in ("value", "low", "high"):
        value = item.get(key)
        if value is not None:
            out.append(str(value))
    return out


def _consensus_rows(connection: Any, company_ref: str, as_of: str) -> list[dict[str, Any]]:
    try:
        from .consensus_estimate import period_consensus

        return period_consensus(SimpleNamespace(connection=connection), company_ref, as_of=as_of)
    except Exception:  # noqa: BLE001
        return []


def _bn(value: Any) -> str | None:
    number = _dec(value)
    return None if number is None else _plain(number / Decimal(10 ** 9), "0.01") + "bn"


def build_result_material(
    state_dir: str | Path | None,
    connection: Any,
    event: Mapping[str, Any],
    *,
    group: Sequence[Mapping[str, Any]] = (),
    mission: Mapping[str, Any] | None = None,
    today: str | None = None,
    pack_builder: Callable[..., Any] | None = None,
    numbers_fn: Callable[..., Mapping[str, Any]] | None = None,
    budget_chars: int = PACK_BUDGET_CHARS,
) -> dict[str, Any]:
    """Everything a result-type (or document-type) event's judge reads, tagged.

    Returns the ``context["result"]`` block: ``lines`` for the prompt, the tag
    map, the deterministic draft rows, the reaction, ``body_read`` and the
    provenance (pack record, documents read) the judgement stores.
    """

    company_ref = str(event.get("company_ref"))
    today = today or datetime.now(timezone.utc).date().isoformat()
    anchor = result_anchor(connection, event, state_dir=state_dir) if is_result_event(event) else None
    dates = event_dates(event, anchor=anchor)
    identity = _identity(state_dir, company_ref) if state_dir is not None else {}
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION, "contract": RESULT_CONTRACT_VERSION,
        "kind": "result" if anchor else "document", "anchor": anchor, "dates": dates,
        "body_read": False, "documents_read": [], "lines": [], "tags": {}, "tag_texts": {},
        "draft_rows": [], "reaction": None, "pack": None, "main_event_ref": event.get("id"),
        "group_event_refs": [item.get("id") for item in group] or [event.get("id")],
        "applicable_rows": [], "citable": [],
    }
    body_doc = body_document(state_dir, event, anchor, identity)
    body_passages = _body_passages(state_dir, body_doc["document_ref"]) if body_doc else []
    result["body_document_ref"] = (body_doc or {}).get("document_ref")
    if anchor is None:
        # A document event that is not a result: its own body, bounded.
        return _document_material(state_dir, event, result, body_doc, body_passages, identity)

    day = anchor["day"]
    # Always the event day + 3, even before that day has come: every section's
    # window is counted back from ``as_of``, so this is what pins them to
    # [day - 3, day + 3] (nothing dated after today exists to be read anyway).
    as_of = _shift(day, EVENT_WINDOW_DAYS)
    baseline_day = _shift(day, -1)
    name = str(identity.get("name") or identity.get("ticker") or "")
    ticker = str(identity.get("ticker") or "")
    topics = [f"{ticker} {name} results guidance".strip(), f"{name} bookings outlook".strip(),
              f"{ticker} preview".strip()]
    if numbers_fn is None:
        def numbers_fn(sd: Any, cr: str, as_of_day: str) -> Mapping[str, Any]:  # noqa: E306
            from .numbers_pack import numbers_pack

            return numbers_pack(sd, cr, as_of_day)
    base_numbers = numbers_fn

    def baseline_numbers(sd: Any, cr: str, _pack_as_of: str) -> Mapping[str, Any]:
        # The pack's numbers are the expectations baseline: the day before.
        return base_numbers(sd, cr, baseline_day)
    pack = None
    if state_dir is not None:
        if pack_builder is None:
            from .material_pack import build as pack_builder  # noqa: N806
        try:
            pack = pack_builder(state_dir, company_ref, "event_judgement", as_of, topics=topics,
                                numbers_fn=baseline_numbers, budget_chars=budget_chars)
        except Exception as exc:  # noqa: BLE001 - no pack is an honest gap, not a crash
            result["pack_error"] = f"{type(exc).__name__}: {exc}"[:300]
    tagger = Tagger(pack)
    read = sorted({item["ref"] for item in (getattr(pack, "inputs", None) or ())
                   if item.get("kind") == "document"})
    lines: list[str] = []
    if pack is not None:
        lines.extend(["", "## 同事件材料包（结果类事件；窗口为事件日前后 3 天，数字包为事件前一日的预期基准）",
                      pack.text.rstrip()])
        result["pack"] = pack.record()
    # The event's own body, pinned: the pack reads the newest exhibit in the
    # window, and this must be *this* event's.
    if body_doc is not None and body_doc["document_ref"] not in read and body_passages:
        lines.extend(["", "## 事件本身的正文（按段落，截取）"])
        used = 0
        for item in body_passages:
            text = str(item.get("text") or "")
            if used + len(text) > BODY_SECTION_CHARS:
                break
            tag = tagger.passage(item["passage_ref"], text)
            lines.append(f"[{tag}] {item.get('kind_label') or item.get('kind')} · {item.get('date')} · "
                         f"《{item.get('title') or ''}》")
            lines.append(text.strip())
            used += len(text)
        tagger.added = []
        read.append(body_doc["document_ref"])
    result["documents_read"] = sorted(set(read))
    result["body_read"] = bool(body_doc is not None and body_doc["document_ref"] in result["documents_read"])

    # -- actuals, baseline, reaction: the deterministic draft ---------------
    period = anchor.get("period")
    release = release_items(connection, state_dir, company_ref=company_ref, accession=anchor.get("accession"),
                            period=period, period_end=anchor.get("period_end"),
                            body_passages=body_passages if event.get("kind") == "earnings_release" or
                            (body_doc or {}).get("kind") == "sec_exhibit" else ())
    if not release["items"] and anchor.get("release_event_ref") and anchor["release_event_ref"] != event.get("id"):
        # A call anchored to a release: the release's exhibit carries the figures.
        release_event = next((r for r in _release_events(connection, company_ref)
                              if r.get("id") == anchor["release_event_ref"]), None)
        if release_event is not None:
            doc = body_document(state_dir, release_event, anchor, identity)
            if doc is not None:
                release = release_items(connection, state_dir, company_ref=company_ref,
                                        accession=anchor.get("accession"), period=period,
                                        period_end=anchor.get("period_end"),
                                        body_passages=_body_passages(state_dir, doc["document_ref"]))
    exhibit_passages = body_passages
    if release.get("source") == "indexed_exhibit_text" and release.get("ref") != (body_doc or {}).get("document_ref"):
        exhibit_passages = _body_passages(state_dir, release["ref"]) if state_dir else []
    prior = prior_guidance_items(connection, state_dir, company_ref=company_ref, before=day, identity=identity)
    consensus = _consensus_rows(connection, company_ref, baseline_day)
    next_q, next_fy = next_periods(period)
    release_payload = (event.get("payload") or {}) if event.get("kind") == "earnings_release" else (
        next((r.get("payload") or {} for r in _release_events(connection, company_ref)
              if r.get("id") == anchor.get("release_event_ref")), {}))
    rows = _draft_rows(tagger, items=release["items"], prior=prior, consensus=consensus, period=period,
                       next_q=next_q, next_fy=next_fy, payload=release_payload or {},
                       passages=exhibit_passages)
    basket = [str(member.get("company_ref")) for member in (mission or {}).get("universe") or ()
              if isinstance(member, Mapping)]
    reaction = market_reaction(connection, company_ref, day, basket=basket)
    reaction_lines = _reaction_lines(tagger, reaction)
    result["draft_rows"] = rows
    result["applicable_rows"] = sorted({row["metric"] for row in rows
                                        if row["metric"] in APPLICABLE_ROWS and row.get("actual")})
    result["reaction"] = reaction
    result["release_source"] = {k: release.get(k) for k in ("source", "ref", "reason")}
    result["prior_guidance_source"] = {k: prior.get(k) for k in ("source", "ref", "report_date", "reason")}
    result["baseline_as_of"] = baseline_day
    result["consensus_version_ref"] = next((row["refs"][0] for row in consensus if row.get("refs")), None)
    lines.extend(tagger.added_lines("## 底稿引用、材料包未收入的原文段落"))
    lines.extend(["", f"## 实际 vs 预期 底稿（确定性；预期基准截至 {baseline_day}，即事件前一日）"])
    lines.extend(_draft_lines(rows))
    if not consensus:
        lines.append("（事件前一日没有可用的一致预期版本：预期一栏只能引用券商预览段落或写「无」）")
    lines.extend(["", f"## 市场反应（{day} 起的首个交易日，收盘对收盘）", *reaction_lines])
    lines.extend(["", "## 日期", dates_line(dates)])
    result["lines"] = lines
    result["tags"] = dict(tagger.tags)
    result["number_json"] = tagger.json
    tag_sources: dict[str, str] = {}
    result["tag_texts"] = _tag_texts(state_dir, tagger, tag_sources)
    result["tag_sources"] = tag_sources
    result["citable"] = _citable(result)
    return result


def _citable(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every printed tag and the ref it binds, findable by either (the verifier's evidence)."""

    out = []
    for tag, ref in sorted((result.get("tags") or {}).items()):
        text = str((result.get("tag_texts") or {}).get(tag, ""))[:1500]
        out.append({"ref": tag, "binds": ref, "text": text})
        out.append({"ref": f"[{tag}]", "binds": ref, "text": text})
        out.append({"ref": ref, "tag": tag, "text": text})
    return out


def _document_material(state_dir: Any, event: Mapping[str, Any], result: dict[str, Any],
                       body_doc: Mapping[str, Any] | None, passages: Sequence[Mapping[str, Any]],
                       identity: Mapping[str, Any]) -> dict[str, Any]:
    tagger = Tagger(None)
    lines: list[str] = []
    if body_doc is not None and passages:
        from .material_sufficiency import mentions

        ranked = sorted(passages, key=lambda item: (0 if identity and mentions(identity, item.get("text")) else 1,
                                                    int(item.get("ordinal") or 0)))
        lines.extend(["", "## 事件文档正文（按段落，优先提到本公司的段落）"])
        used = 0
        for item in ranked:
            text = str(item.get("text") or "")
            if used + len(text) > DOCUMENT_BODY_CHARS:
                continue
            tag = tagger.passage(item["passage_ref"], text)
            tagger.added = []
            lines.append(f"[{tag}] {item.get('kind_label') or item.get('kind')} · {item.get('date')}")
            lines.append(text.strip())
            used += len(text)
        result["documents_read"] = [body_doc["document_ref"]]
        result["body_read"] = True
    else:
        lines.extend(["", "## 事件文档正文", "（正文没有入库：只看到引用，没有读到原文。不得据标题或引用判断内容。）"])
    lines.extend(["", "## 日期", dates_line(result["dates"])])
    result["lines"] = lines
    result["tags"] = dict(tagger.tags)
    result["tag_texts"] = dict(tagger.texts)
    result["citable"] = _citable(result)
    return result


def _tag_texts(state_dir: Any, tagger: Tagger, sources: dict[str, str] | None = None) -> dict[str, str]:
    texts = dict(tagger.texts)
    passages = [tag for tag, ref in tagger.tags.items() if str(ref).startswith("doc-passage:")]
    if passages and state_dir is not None:
        from . import evidence_index

        for tag in passages:
            try:
                found = evidence_index.passage(state_dir, tagger.tags[tag])
            except Exception:  # noqa: BLE001 - an unresolvable tag has no text
                found = None
            if found is None:
                continue
            texts.setdefault(tag, found["text"])
            if sources is not None:
                sources[tag] = "".join(part for part in (
                    found.get("kind_label") or found.get("kind") or "原文",
                    f"（{found['house']}）" if found.get("house") else "") if part)
    for tag, ref in tagger.tags.items():
        if tag not in texts and str(ref).startswith("pack-json:"):
            try:
                from .material_pack import resolve_number

                texts[tag] = str(resolve_number(getattr(tagger.pack, "json", {}) or {}, ref))
            except Exception:  # noqa: BLE001
                continue
    return texts


# -- the draft table ----------------------------------------------------------


def _pick(items: Sequence[Mapping[str, Any]], *, kind: str | Sequence[str], metric: str,
          period: str | None, basis: str | None = None) -> Mapping[str, Any] | None:
    kinds = {kind} if isinstance(kind, str) else set(kind)
    for item in items:
        if item.get("kind") in kinds and item.get("metric") == metric and (period is None or item.get("period") == period) \
                and (basis is None or item.get("basis") == basis):
            return item
    return None


def _cell(tagger: Tagger, path: Sequence[Any], item: Mapping[str, Any] | None, label: str,
          passages: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    if item is None:
        return None
    display = display_item(item)
    if display is None:
        return None
    value = {k: item.get(k) for k in ("value", "low", "high", "unit", "scale", "currency", "period", "metric")
             if item.get(k) is not None}
    tags = [tagger.number(path, value, display)]
    passage = _passage_for(passages, _digits(item))
    if passage is not None:
        tags.append(tagger.passage(passage["passage_ref"], str(passage.get("text") or ""), passage))
    return {"label": label, "display": display, "tags": tags}


def _consensus_cell(tagger: Tagger, path: Sequence[Any], rows: Sequence[Mapping[str, Any]], *,
                    measure: str, period: str | None) -> dict[str, Any] | None:
    if period is None:
        return None
    row = next((r for r in rows if r.get("measure") == measure and r.get("period") == period), None)
    if row is None or row.get("value") is None:
        return None
    if measure == "revenue":
        display = _bn(row["value"])
    else:
        display = _plain(_dec(row["value"]), "0.01")
    if display is None:
        return None
    tag = tagger.number(path, {"value": row["value"], "low": row.get("low"), "high": row.get("high"),
                               "estimates": row.get("estimates"), "as_of": row.get("as_of"),
                               "basis": row.get("basis"), "ref": (row.get("refs") or [None])[0]}, display)
    label = "会前一致预期" + ("（数据商口径，通常为调整后）" if measure == "eps" else "")
    return {"label": label, "display": display, "tags": [tag],
            "note": f"{row.get('estimates') or '?'} 家，截至 {row.get('as_of')}"}


def _draft_rows(tagger: Tagger, *, items: Sequence[Mapping[str, Any]], prior: Mapping[str, Any],
                consensus: Sequence[Mapping[str, Any]], period: str | None, next_q: str | None,
                next_fy: str | None, payload: Mapping[str, Any],
                passages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    prior_items = list(prior.get("items") or ())
    prior_passages = list(prior.get("passages") or ())
    rows: list[dict[str, Any]] = []

    def row(metric: str, row_period: str | None, actual: list, expected: list, guided: list) -> None:
        rows.append({"metric": metric, "label": ROW_LABELS[metric], "period": row_period,
                     "actual": [c for c in actual if c], "expected": [c for c in expected if c],
                     "prior_guidance": [c for c in guided if c]})

    def prior_guide(metric: str, at: str | None, path: Sequence[Any]) -> dict[str, Any] | None:
        found = _pick(prior_items, kind="guidance", metric=metric, period=at)
        return _cell(tagger, path, found, "上次指引", prior_passages)

    # revenue
    revenue = _pick(items, kind="reported", metric="revenue", period=period)
    revenue_cell = _cell(tagger, ("actual", "revenue"), revenue, "实际", passages)
    if revenue_cell is None and _dec(payload.get("revenue")) is not None:
        unit = str(payload.get("revenue_unit") or "")
        scale = {"USD:thousand": 10 ** 6, "USD:million": 10 ** 3, "USD:one": 10 ** 9}.get(unit)
        if scale:
            value = _plain(_dec(payload["revenue"]) / Decimal(scale), "0.01")
            tag = tagger.number(("actual", "revenue_payload"), {"value": payload["revenue"], "unit": unit},
                                f"${value}B")
            revenue_cell = {"label": "实际（业绩稿收入表）", "display": f"${value}B", "tags": [tag]}
    row("revenue", period, [revenue_cell],
        [_consensus_cell(tagger, ("consensus", "revenue", period or "?"), consensus, measure="revenue", period=period)],
        [_cell(tagger, ("prior_guidance", "revenue"),
               _pick(items, kind="guidance_vs_actual", metric="revenue_prior_guidance", period=period),
               "上次指引（业绩稿复述）", passages)
         or prior_guide("revenue", period, ("prior_guidance", "revenue_prior"))])
    # local-currency growth
    row("revenue_growth_lc", period,
        [_cell(tagger, ("actual", "revenue_growth_lc"),
               _pick(items, kind="growth", metric="revenue_growth_lc", period=period), "实际", passages)],
        [],
        [_cell(tagger, ("prior_guidance", "revenue_growth_lc"),
               _pick(items, kind="guidance_vs_actual", metric="revenue_growth_lc_prior_guidance", period=period),
               "上次指引（业绩稿复述）", passages)
         or prior_guide("revenue_growth_lc", period, ("prior_guidance", "revenue_growth_lc_prior"))])
    # bookings and book-to-bill: only where the company reports them
    bookings = _pick(items, kind="bookings", metric="bookings", period=period)
    if bookings is not None:
        row("bookings", period, [_cell(tagger, ("actual", "bookings"), bookings, "实际", passages),
                                 _cell(tagger, ("actual", "bookings_growth_lc"),
                                       _pick(items, kind="growth", metric="bookings_growth_lc", period=period),
                                       "本币同比", passages)], [], [])
    btb = _pick(items, kind="book_to_bill", metric="book_to_bill", period=period)
    if btb is not None:
        row("book_to_bill", period, [_cell(tagger, ("actual", "book_to_bill"), btb, "实际", passages)], [], [])
    # operating margin: the release's income statement, computed
    margin_cell = None
    income, sales = _dec(payload.get("operating_income")), _dec(payload.get("revenue"))
    if income is not None and sales and payload.get("operating_income_unit") == payload.get("revenue_unit"):
        value = _plain(income / sales * 100, "0.1")
        tag = tagger.number(("actual", "operating_margin"),
                            {"value": value, "formula": "operating_income / revenue",
                             "operating_income": payload.get("operating_income"), "revenue": payload.get("revenue")},
                            f"{value}%")
        margin_cell = {"label": "实际（营业利润 ÷ 收入，业绩稿收入表）", "display": f"{value}%", "tags": [tag]}
    fy_now = re.sub(r"Q[1-4]$", "", period or "") or None
    row("operating_margin", period, [margin_cell], [],
        [prior_guide("operating_margin", fy_now, ("prior_guidance", "operating_margin"))])
    # EPS
    eps_cells = [_cell(tagger, ("actual", "eps_gaap"), _pick(items, kind="reported", metric="eps_gaap",
                                                               period=period, basis="reported"),
                       "实际（GAAP 摊薄）", passages),
                 _cell(tagger, ("actual", "eps_adjusted"), _pick(items, kind="reported", metric="eps_adjusted",
                                                                   period=period, basis="reported"),
                       "实际（调整后）", passages)]
    if not any(eps_cells) and _dec(payload.get("eps_diluted")) is not None:
        tag = tagger.number(("actual", "eps_payload"), {"value": payload["eps_diluted"]},
                            f"${payload['eps_diluted']}")
        eps_cells = [{"label": "实际（业绩稿收入表，摊薄）", "display": f"${payload['eps_diluted']}", "tags": [tag]}]
    row("eps", period, eps_cells,
        [_consensus_cell(tagger, ("consensus", "eps", period or "?"), consensus, measure="eps", period=period)], [])
    # next quarter's guidance
    nq = [_cell(tagger, ("guidance", "next_quarter", metric), _pick(items, kind="guidance", metric=metric,
                                                                     period=next_q), label, passages)
          for metric, label in (("revenue", "公司指引：收入"), ("revenue_growth_lc", "公司指引：本币增速"),
                                ("eps", "公司指引：EPS"))] if next_q else []
    row("guidance_next_quarter", next_q, nq,
        [_consensus_cell(tagger, ("consensus", "revenue", next_q or "?"), consensus, measure="revenue", period=next_q),
         _consensus_cell(tagger, ("consensus", "eps", next_q or "?"), consensus, measure="eps", period=next_q)], [])
    # the full year's guidance
    fy = [_cell(tagger, ("guidance", "full_year", metric), _pick(items, kind="guidance", metric=metric,
                                                                  period=next_fy), label, passages)
          for metric, label in (("revenue_growth_lc", "公司指引：本币增速"), ("eps", "公司指引：EPS"),
                                ("operating_margin", "公司指引：营业利润率"))] if next_fy else []
    row("guidance_full_year", next_fy, fy,
        [_consensus_cell(tagger, ("consensus", "eps", next_fy or "?"), consensus, measure="eps", period=next_fy),
         _consensus_cell(tagger, ("consensus", "revenue", next_fy or "?"), consensus, measure="revenue",
                         period=next_fy)],
        [prior_guide(metric, next_fy, ("prior_guidance", "full_year", metric))
         for metric in ("revenue_growth_lc", "eps")])
    return rows


def _cell_text(cells: Sequence[Mapping[str, Any]]) -> str:
    if not cells:
        return "无"
    return "；".join(f"{cell['label']} " + "".join(f"[{tag}]" for tag in cell["tags"]) + f" {cell['display']}"
                    + (f"（{cell['note']}）" if cell.get("note") else "") for cell in cells)


def _draft_lines(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    lines = []
    for row in rows:
        actual = _cell_text(row["actual"]) if row["actual"] else "实际 无"
        expected = _cell_text(row["expected"]) if row["expected"] else "预期 无"
        guided = _cell_text(row["prior_guidance"]) if row["prior_guidance"] else "上次指引 无"
        lines.append(f"- {row['label']}（{row['metric']}，{row.get('period') or '期间未知'}）："
                     f"{actual} | {expected} | {guided}")
    return lines


def _reaction_lines(tagger: Tagger, reaction: Mapping[str, Any]) -> list[str]:
    move = reaction.get("move")
    if not move:
        return [f"（{reaction.get('reason') or '没有行情'}；市场反应一栏写「{NO_BENCHMARK}」并说明）"]
    move_tag = tagger.number(("reaction", "move"), move, f"{move['percent']}%")
    close_tag = tagger.number(("reaction", "close"), move["close"], str(move["close"]))
    prev_tag = tagger.number(("reaction", "previous_close"), move["previous_close"], str(move["previous_close"]))
    lines = [f"公司 {move['date']} 收盘 [{close_tag}] {move['close']}，前收（{move['previous_date']}）"
             f"[{prev_tag}] {move['previous_close']}，单日 [{move_tag}] {move['percent']}%"]
    bench = reaction.get("benchmark") or {}
    if bench.get("status") == "available":
        b_tag = tagger.number(("reaction", "benchmark"), bench, f"{bench['percent']}%")
        r_tag = tagger.number(("reaction", "benchmark_relative"), bench["relative_pp"], f"{bench['relative_pp']}pp")
        lines.append(f"宽基 {bench['ticker']} 同日 [{b_tag}] {bench['percent']}%，相对 [{r_tag}] {bench['relative_pp']}pp")
    else:
        lines.append(f"宽基指数：{NO_BENCHMARK}（{bench.get('reason') or '不可得'}）")
    basket = reaction.get("basket") or {}
    if basket.get("status") == "available":
        k_tag = tagger.number(("reaction", "basket"), basket, f"{basket['percent']}%")
        kr_tag = tagger.number(("reaction", "basket_relative"), basket["relative_pp"], f"{basket['relative_pp']}pp")
        lines.append(f"同组 {basket['members']} 家等权同日 [{k_tag}] {basket['percent']}%，相对 "
                     f"[{kr_tag}] {basket['relative_pp']}pp")
    else:
        lines.append(f"同组篮子：{NO_BENCHMARK}（{basket.get('reason') or '不可得'}）")
    return lines


def benchmark_available(result: Mapping[str, Any]) -> bool:
    reaction = result.get("reaction") or {}
    return any((reaction.get(key) or {}).get("status") == "available" for key in ("benchmark", "basket"))


# ---------------------------------------------------------------------------
# the prompt and the contract
# ---------------------------------------------------------------------------


def prompt_lines(result: Mapping[str, Any]) -> list[str]:
    lines = list(result.get("lines") or ())
    if result.get("kind") != "result":
        if not result.get("body_read"):
            lines.extend(["", "The body of this document was NOT read (only its reference is known). Do not judge",
                          "what it says. If nothing else above decides the matter, answer NO_CHANGE / research",
                          "and ask for the document to be read."])
        else:
            lines.extend(["", "Cite the passages you rely on by their tag ([P1], [P2] ...) in `citations`."])
        return lines
    required = list(REQUIRED_ROWS) + list(result.get("applicable_rows") or ())
    lines.extend([
        "",
        "## How to judge a result-type event (contract; refused whole if broken)",
        "1. Fill the 实际 vs 预期 table FIRST: one row for each of " + ", ".join(required) + ".",
        "   actual = what the company reported or guided; expected = the pre-event consensus or a broker",
        "   preview; prior_guidance = what the company had guided before. Every figure you write must be",
        "   copied exactly as printed above, followed by its tag ([N…] or [P…]); a figure without its tag,",
        "   or not found in the tag it names, refuses the judgement. Write 无 where there is nothing.",
        "   vs_expected: beat | miss | inline | above_range | within_range | below_range | n/a.",
        "   Compare guidance with BOTH the prior guidance and the consensus, and with the previews' figures",
        "   when a preview passage states them.",
        "2. Then explain the market reaction against a benchmark (the broad index and/or the basket above).",
        f"   If no benchmark is available write 「{NO_BENCHMARK}」. Do not attribute the move without one.",
        "3. Then the thesis impact. NO_CHANGE after a beat or a miss must say, in why_unchanged, why that",
        "   beat or miss does not change the thesis. An unexplained NO_CHANGE is refused.",
        "4. If the event's body was not read, or what you need is not in the pack, set evidence_status to",
        "   \"insufficient\", decide NO_CHANGE / research, and say what must be read.",
        f"The event's body was {'READ (its passages are above)' if result.get('body_read') else 'NOT READ'}.",
    ])
    return lines


def output_spec_lines(result: Mapping[str, Any]) -> list[str]:
    if result.get("kind") != "result":
        return []
    return [
        "Because this is a result-type event, ALSO return these keys:",
        '"evidence_status": "read|insufficient",',
        '"actual_vs_expected": [{"metric": "' + "|".join(ROW_KEYS) + '", "period": "<e.g. FY2026Q4>",'
        ' "actual": "<figures with tags>", "expected": "<figures with tags, or 无>",'
        ' "prior_guidance": "<figures with tags, or 无>", "vs_expected": "' + "|".join(COMPARISONS) + '",'
        ' "sources": ["<tags>"]}],',
        '"market_reaction": {"move": "<the move with its tag>", "benchmark": "<benchmark with tag, or '
        + NO_BENCHMARK + '>", "explanation": "<why the market moved, against the benchmark>"},',
        '"thesis_impact": {"explanation": "<what this does to the theses and the forecast>",'
        ' "why_unchanged": "<required when the decision is NO_CHANGE and any row beat or missed>"}',
        "In `citations` you may name tags ([P3] or P3) as well as refs.",
    ]


_TAG_RE = re.compile(r"\[?\b([NPOC]\d{1,4})\b\]?")
_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9_.])[-+]?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?(\s*(?:%|pp|bps|个百分点))?")


def numeric_tokens(text: str) -> list[str]:
    """The figures in a cell that need a source, as the publication check sees them.

    Uses the deliverable authority's own tokeniser (``value_tokens``), so a
    cell this contract admits is a line the authority will publish: the same
    figures, the same small-integer and date rules.  Tags (``[N12]``) are
    removed first; each figure is returned bare (no currency, no percent).
    """

    from .mission_deliverable import value_tokens

    stripped = _TAG_RE.sub(" ", str(text or ""))
    return [bare for bare in (_bare(token) for token in value_tokens(stripped)) if bare is not None]


def _source_values(text: str) -> set[str]:
    values = set()
    for match in re.finditer(r"\d[\d,]*(?:\.\d+)?", str(text or "")):
        bare = _bare(match.group(0))
        if bare is not None:
            values.add(bare)
    return values


def _tags_in(*texts: Any) -> list[str]:
    found: list[str] = []
    for text in texts:
        if isinstance(text, str):
            found.extend(match.group(1) for match in _TAG_RE.finditer(text))
    return found


def _check_cell(text: str, tags: Sequence[str], result: Mapping[str, Any], where: str) -> list[str]:
    problems = []
    known = result.get("tags") or {}
    texts = result.get("tag_texts") or {}
    for tag in tags:
        if tag.startswith(("O", "C")):
            problems.append(f"{where}: {tag} is our own view or a legacy conclusion, not a source")
        elif tag not in known:
            problems.append(f"{where}: tag {tag} was not printed")
    figures = numeric_tokens(text)
    if figures and not [tag for tag in tags if tag in known]:
        problems.append(f"{where}: figures {figures[:3]} carry no tag")
        return problems
    available: set[str] = set()
    for tag in tags:
        available |= _source_values(texts.get(tag, ""))
    missing = [figure for figure in figures if figure not in available]
    if missing:
        problems.append(f"{where}: {missing[:3]} not found in the cited {', '.join(tags)}")
    return problems


def _text_field(value: Any, name: str, problems: list[str], *, required: bool = True,
                maximum: int = MAX_TEXT_CHARS) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            problems.append(f"{name} must be non-empty text")
        return ""
    if not isinstance(value, str):
        problems.append(f"{name} must be text")
        return ""
    if len(value) > maximum:
        problems.append(f"{name} must be at most {maximum} characters")
    return value.strip()


RESULT_KEYS: frozenset[str] = frozenset({"evidence_status", "actual_vs_expected", "market_reaction",
                                         "thesis_impact"})


def result_contract_problems(value: Mapping[str, Any], result: Mapping[str, Any], *,
                             decision: str, action: str) -> tuple[dict[str, Any], list[str]]:
    """The normalised result block and every way it breaks the contract."""

    problems: list[str] = []
    missing = sorted(RESULT_KEYS - set(value))
    if missing:
        return {}, [f"a result-type event's judgement omitted {missing} (the 实际 vs 预期 table comes first)"]
    status = value.get("evidence_status")
    if status not in EVIDENCE_STATUSES:
        problems.append(f"evidence_status must be one of {list(EVIDENCE_STATUSES)}")
    if not result.get("body_read") and status == "read":
        problems.append("the event's body was not read: the only answer is 证据不足，安排读取 "
                        "(evidence_status insufficient, NO_CHANGE / research)")
    insufficient = status == "insufficient"
    if insufficient and (decision != "NO_CHANGE" or action != "research"):
        problems.append("evidence_status insufficient must be NO_CHANGE / research (证据不足，安排读取)")
    rows_in = value.get("actual_vs_expected")
    if not isinstance(rows_in, list):
        problems.append("actual_vs_expected must be a list of rows")
        rows_in = []
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, row in enumerate(rows_in):
        where = f"actual_vs_expected[{index}]"
        if not isinstance(row, Mapping):
            problems.append(f"{where} must be an object")
            continue
        extra = sorted(set(row) - {"metric", "period", "actual", "expected", "prior_guidance", "vs_expected",
                                   "sources"})
        if extra:
            problems.append(f"{where} has keys the contract does not have: {extra}")
        metric = row.get("metric")
        if metric not in ROW_KEYS:
            problems.append(f"{where}.metric must be one of {list(ROW_KEYS)}")
            continue
        if metric in seen:
            problems.append(f"{where}: {metric} appears twice")
        seen.add(metric)
        comparison = row.get("vs_expected")
        if comparison not in COMPARISONS:
            problems.append(f"{where}.vs_expected must be one of {list(COMPARISONS)}")
        sources = row.get("sources") or []
        if not isinstance(sources, list) or not all(isinstance(item, str) for item in sources):
            problems.append(f"{where}.sources must be a list of tags")
            sources = []
        declared = [item.strip("[] ") for item in sources]
        cells = {}
        for key in ("actual", "expected", "prior_guidance"):
            cells[key] = _text_field(row.get(key), f"{where}.{key}", problems, required=(key == "actual"),
                                     maximum=MAX_CELL_CHARS) or "无"
            inline = _tags_in(cells[key])
            problems.extend(_check_cell(cells[key], inline or declared, result, f"{where}.{key}"))
        rows.append({"metric": metric, "label": ROW_LABELS[metric],
                     "period": str(row.get("period") or "")[:40] or None,
                     **cells, "vs_expected": comparison,
                     "sources": list(dict.fromkeys(declared + _tags_in(*cells.values())))})
    if not insufficient:
        wanted = list(REQUIRED_ROWS) + list(result.get("applicable_rows") or ())
        absent = [metric for metric in wanted if metric not in seen]
        if absent:
            problems.append(f"the 实际 vs 预期 table is missing rows: {absent}")
    reaction_in = value.get("market_reaction")
    reaction: dict[str, Any] = {}
    if not isinstance(reaction_in, Mapping) or set(reaction_in) - {"move", "benchmark", "explanation"}:
        problems.append("market_reaction must be exactly move, benchmark, explanation")
    else:
        for key in ("move", "benchmark", "explanation"):
            reaction[key] = _text_field(reaction_in.get(key), f"market_reaction.{key}", problems,
                                        required=not insufficient or key == "explanation")
        for key in ("move", "benchmark"):
            if reaction.get(key):
                problems.extend(_check_cell(reaction[key], _tags_in(reaction[key]), result, f"market_reaction.{key}"))
        if not benchmark_available(result) and NO_BENCHMARK not in (reaction.get("benchmark") or "") + (
                reaction.get("explanation") or ""):
            problems.append(f"no benchmark was available: market_reaction must say 「{NO_BENCHMARK}」")
    impact_in = value.get("thesis_impact")
    impact: dict[str, Any] = {}
    if not isinstance(impact_in, Mapping) or set(impact_in) - {"explanation", "why_unchanged"}:
        problems.append("thesis_impact must be explanation and why_unchanged")
    else:
        impact["explanation"] = _text_field(impact_in.get("explanation"), "thesis_impact.explanation", problems)
        if impact["explanation"] and len(impact["explanation"]) < MIN_EXPLANATION_CHARS:
            problems.append("thesis_impact.explanation is too short to explain anything")
        impact["why_unchanged"] = _text_field(impact_in.get("why_unchanged"), "thesis_impact.why_unchanged",
                                              problems, required=False)
        surprised = [row["metric"] for row in rows if row.get("vs_expected") in SURPRISES]
        if decision == "NO_CHANGE" and not insufficient and surprised and len(
                impact.get("why_unchanged") or "") < MIN_EXPLANATION_CHARS:
            problems.append(f"NO_CHANGE after a beat or miss ({surprised}) must say in why_unchanged why it "
                            "does not change the thesis")
    verdict = VERDICT_EVIDENCE_INSUFFICIENT if insufficient else decision
    return ({"contract": RESULT_CONTRACT_VERSION, "evidence_status": status, "verdict": verdict,
             "verdict_label": VERDICT_LABELS.get(verdict, verdict), "actual_vs_expected": rows,
             "market_reaction": reaction, "thesis_impact": impact}, problems)


def resolve_citation(ref: str, result: Mapping[str, Any] | None) -> str:
    tag = ref.strip("[] ")
    return str(((result or {}).get("tags") or {}).get(tag) or ref)


# ---------------------------------------------------------------------------
# 证据不足，安排读取: the deterministic answer, and the MaterialRequest
# ---------------------------------------------------------------------------


def _doc_kinds(event: Mapping[str, Any]) -> list[str]:
    kind = event.get("kind")
    if kind == "earnings_release":
        return ["sec_exhibit"]
    return {"transcript": ["transcript"], "sales_note": ["sales_note"], "expert_excerpt": ["expert_call"],
            "filing": ["sec_8k", "sec_other"], "news": ["web_page"]}.get(str(kind), ["research_other"])


def gap_need(event: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any]:
    payload = event.get("payload") or {}
    document = payload.get("document_ref") or result.get("body_document_ref")
    kinds = _doc_kinds(event)
    question = (f"读取事件 {event.get('id')} 的正文（{document or '文档'}，{kinds[0]}），并入全文证据索引，"
                "以便按「实际 vs 预期」研判")
    return {"requester": {"lane": "event_judgement", "product_ref": str(event.get("id")), "section": "body"},
            "subject": str(event.get("company_ref")),
            "need": {"kind": "document", "doc_kinds": kinds, "question": question},
            "period": f"event_document:{document or event.get('id')}",
            "acceptance": {"document_ref": document, "indexed": True, "about_company": True},
            "kind": "event_body", "event_ref": event.get("id")}


def research_need(event: Mapping[str, Any], judgement: Mapping[str, Any]) -> dict[str, Any]:
    question = str(judgement.get("research_question") or judgement.get("because") or "")[:1900]
    return {"requester": {"lane": "event_judgement", "product_ref": str(event.get("id")), "section": "research"},
            "subject": str(event.get("company_ref")),
            "need": {"kind": "answer", "question": question or f"核实事件 {event.get('id')}"},
            "period": f"event:{event.get('id')}", "acceptance": {"answers_question": True},
            "kind": "event_research", "event_ref": event.get("id")}


def open_material(state_dir: str | Path, effect: Mapping[str, Any]) -> dict[str, Any]:
    """Open (or join) the MaterialRequest for an effect.

    Batch E's ``material_acquisition.open_for_effect(state_dir, effect)`` by
    name, so the request enters the acquisition service's ladder; this
    ledger's own ``open_request`` when E is not installed.
    """

    try:
        from .material_acquisition import open_for_effect  # type: ignore[attr-defined]
    except ImportError:
        open_for_effect = None
    if open_for_effect is not None:
        found = open_for_effect(state_dir, dict(effect))
        if isinstance(found, Mapping):
            return {"id": found.get("id") or found.get("request_id") or found.get("request_ref"),
                    "status": found.get("status"), "write_status": found.get("write_status"),
                    "via": "material_acquisition"}
    from . import material_requests as mr

    opened = mr.open_request(state_dir, requester=effect["requester"], subject=effect["subject"],
                             need=effect["need"], period=effect.get("period"),
                             acceptance=effect.get("acceptance"))
    return {"id": opened.get("id"), "status": opened.get("status"), "write_status": opened.get("write_status"),
            "via": "material_requests"}


def active_gap_request(state_dir: str | Path | None, event: Mapping[str, Any],
                       result: Mapping[str, Any]) -> dict[str, Any] | None:
    if state_dir is None:
        return None
    from . import material_requests as mr

    need = gap_need(event, result)
    try:
        found = mr.find_request(state_dir, subject=need["subject"], need=need["need"], period=need["period"])
    except Exception:  # noqa: BLE001
        return None
    return found


def gap_judgement(event: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any]:
    """「证据不足，安排读取」 without a model: the body was not read."""

    document = (event.get("payload") or {}).get("document_ref") or result.get("body_document_ref")
    because = (f"事件正文没有读到（{document or '文档'} 未入全文证据索引，只看到引用），不能据标题或引用判断"
               "它对论点的影响；已提交材料请求读取正文，正文入库后重判。在此之前不对这份文档给出维持判断。")
    question = f"读取并研判 {document or event.get('id')} 的正文（{(result.get('dates') or {}).get('occurred_on')}）"
    insufficient = {"contract": RESULT_CONTRACT_VERSION, "evidence_status": "insufficient",
                    "verdict": VERDICT_EVIDENCE_INSUFFICIENT,
                    "verdict_label": VERDICT_LABELS[VERDICT_EVIDENCE_INSUFFICIENT],
                    "actual_vs_expected": [], "market_reaction": {}, "thesis_impact": {
                        "explanation": "正文未读，无法评估对论点和预测的影响；读取后按「实际 vs 预期」重判。",
                        "why_unchanged": ""}}
    return {"status": "judged", "decision": "NO_CHANGE", "action": "research", "driver_refs": [],
            "thesis_refs": [], "because": because, "citations": [event["id"]],
            "research_question": question, "result": insufficient, "model": None,
            "deterministic": "body_not_read"}


# ---------------------------------------------------------------------------
# re-judgement
# ---------------------------------------------------------------------------


def _judgement_rows(connection: Any, event_ref: str) -> list[dict[str, Any]]:
    rows = _query(connection, "SELECT record_json FROM event_judgements WHERE event_ref=? OR event_ref LIKE ? "
                              "ORDER BY created_at, judgement_id", (event_ref, f"{event_ref}#rejudgement-%"))
    return [record for record in (_row_json(row) for row in rows) if record]


def rejudgement_ref(event_ref: str, sequence: int) -> str:
    return f"{event_ref}#rejudgement-{int(sequence)}"


def judgement_history(connection: Any, event_ref: str) -> dict[str, Any]:
    rows = _judgement_rows(connection, event_ref)
    original = next((row for row in rows if row.get("event_ref") == event_ref), None)
    rejudgements = [row for row in rows if row.get("event_ref") != event_ref]
    return {"original": original, "rejudgements": rejudgements,
            "latest": rejudgements[-1] if rejudgements else original}


def lacked_body(record: Mapping[str, Any] | None) -> tuple[bool, bool]:
    """(the judgement lacked the event body, it is a pre-batch-F record)."""

    if not record:
        return False, False
    material = record.get("result_material")
    if not isinstance(material, Mapping):
        return True, True
    result = record.get("result") or {}
    return (not material.get("body_read")) or result.get("evidence_status") == "insufficient", False


def _arrivals(state_dir: str | Path | None, company_ref: str, day: str,
              identity: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Same-event documents in the index: the window's release, call and street notes."""

    if state_dir is None:
        return []
    from .material_sufficiency import BROKER_ABOUTNESS, own_documents

    out = []
    for kinds, threshold in ((["sec_exhibit", "transcript", "sec_8k"], 0.15),
                             (["broker_report", "sales_note"], BROKER_ABOUTNESS)):
        try:
            docs = own_documents(state_dir, company_ref, identity or {"_patterns": []}, kinds,
                                 as_of=_shift(day, EVENT_WINDOW_DAYS), days=2 * EVENT_WINDOW_DAYS,
                                 threshold=threshold)
        except Exception:  # noqa: BLE001
            docs = []
        for doc in docs:
            out.append({"document_ref": doc["document_ref"], "kind": doc.get("kind"),
                        "arrived_at": doc.get("indexed_at") or doc.get("acquired_at")})
    return out


def rejudgement_reason(connection: Any, state_dir: str | Path | None, primary: Mapping[str, Any],
                       members: Sequence[Mapping[str, Any]], *, now: datetime | None = None,
                       identity: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    """Whether the result event ``primary`` owes a re-judgement now, and why.

    ``None`` when it does not: never judged (the unjudged path owns it), the
    cap of :data:`MAX_REJUDGEMENTS` is reached, or nothing new has arrived.
    """

    history = judgement_history(connection, str(primary["id"]))
    if history["original"] is None:
        return None
    done = len(history["rejudgements"])
    if done >= MAX_REJUDGEMENTS:
        return None
    latest = history["latest"]
    material = latest.get("result_material") if isinstance(latest.get("result_material"), Mapping) else {}
    read = set(material.get("documents_read") or ())
    lacked, legacy = lacked_body(latest)
    anchor = result_anchor(connection, primary, state_dir=state_dir)
    if anchor is None or not anchor.get("day"):
        return None
    moment = now or datetime.now(timezone.utc)
    if (date.fromisoformat(moment.date().isoformat()) - date.fromisoformat(anchor["day"])).days > REJUDGE_LOOKBACK_DAYS:
        return None
    body = body_document(state_dir, primary, anchor, identity)
    base = {"sequence": done + 1, "prior_judgement_ref": latest.get("id"), "anchor_day": anchor["day"],
            "event_ref": primary["id"]}
    released = anchor.get("basis") in ("release", "call_matched_to_release")
    if lacked and body is not None and body["document_ref"] not in read and (released or not legacy):
        # A gap judgement (证据不足) is re-judged when its material arrives; a
        # pre-batch-F judgement only for a release-anchored occurrence (the
        # quarter's own results), never for every old call in the ledger.
        reason = "legacy_judgement_without_body" if legacy else "material_arrived"
        return {**base, "reason": reason, "label": REJUDGE_REASONS[reason],
                "trigger_refs": [body["document_ref"]]}
    if not released:
        return None
    available = min((_ts(item.get("created_at")) for item in members if _ts(item.get("created_at"))),
                    default=_ts(primary.get("created_at")))
    if available is None:
        return None
    window_end = available + timedelta(hours=REJUDGE_WINDOW_HOURS)
    judged_at = _ts(latest.get("created_at")) or available
    fresh = []
    primary_payload = primary.get("payload") or {}
    for item in members:
        arrived = _ts(item.get("created_at"))
        payload = item.get("payload") or {}
        document = payload.get("document_ref")
        same_filing = (document and document == primary_payload.get("document_ref")) or (
            payload.get("accession") and payload.get("accession") == primary_payload.get("accession"))
        # The same filing recorded again is the old event re-processed (a
        # repeat), not new material.
        if (arrived and judged_at < arrived <= window_end and item["id"] != primary["id"]
                and not same_filing and document not in read and not _judged(connection, item["id"])):
            fresh.append(str(document or item["id"]))
    for doc in _arrivals(state_dir, str(primary.get("company_ref")), anchor["day"], identity):
        arrived = _ts(doc.get("arrived_at"))
        if arrived and judged_at < arrived <= window_end and doc["document_ref"] not in read:
            fresh.append(doc["document_ref"])
    if fresh:
        reason = "same_event_document_within_72h"
        return {**base, "reason": reason, "label": REJUDGE_REASONS[reason],
                "trigger_refs": sorted(set(fresh)), "window_end": window_end.isoformat()}
    return None


def _judged(connection: Any, event_ref: str) -> bool:
    return bool(_query(connection, "SELECT 1 FROM event_judgements WHERE event_ref=?", (event_ref,)))


def result_events(connection: Any, company_ref: str, *, since: str) -> list[dict[str, Any]]:
    rows = _query(connection, "SELECT record_json FROM research_events WHERE company_ref=? "
                              "AND kind IN ('earnings_release','transcript','filing') AND occurred_at>=? "
                              "ORDER BY occurred_at, event_id", (company_ref, since))
    return [record for record in (_row_json(row) for row in rows) if record and is_result_event(record)]


def primary_first(group: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """A result group led by its release, then the call, then the rest (stable)."""

    order = {"earnings_release": 0, "transcript": 1}
    return sorted((dict(item) for item in group),
                  key=lambda item: (order.get(str(item.get("kind")), 2), str(item.get("occurred_at")),
                                    str(item.get("id"))))


# ---------------------------------------------------------------------------
# summaries and display
# ---------------------------------------------------------------------------


def _figure_entries(text: str, tags: Sequence[str], material: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Number entries for a summary line: each figure bound to the passage or pack number it came from."""

    from .mission_deliverable import value_tokens

    bindings = material.get("tags") or {}
    texts = material.get("tag_texts") or {}
    entries = []
    for token in value_tokens(text):
        bare = _bare(token)
        for tag in tags:
            ref = bindings.get(tag)
            if not ref or bare is None or bare not in _source_values(texts.get(tag, "")):
                continue
            if str(ref).startswith("doc-passage:"):
                entries.append({"text": token, "passage_ref": ref})
            else:
                entries.append({"text": token, "number_ref": ref, "source_text": str(texts.get(tag, ""))[:400]})
            break
    return entries


_STRIP_TAGS = re.compile(r"\s*\[[NPOC]\d{1,4}\]")

#: ``pack-json:`` pointer prefix -> what an analyst calls that source.
NUMBER_SOURCE_LABELS: tuple[tuple[str, str], ...] = (
    ("/event_result/consensus", "会前一致预期（数据商）"), ("/event_result/actual", "公司业绩稿"),
    ("/event_result/guidance", "公司指引（业绩稿）"), ("/event_result/prior_guidance", "公司此前指引"),
    ("/event_result/reaction", "行情（价格库）"), ("/numbers/consensus", "一致预期（数据商）"),
    ("/numbers/street", "券商研报预测"), ("/numbers/market", "行情（价格库）"),
    ("/numbers/valuation", "估值"), ("/numbers/outlook", "公司指引（业绩稿）"),
)


def source_label(tag: str, material: Mapping[str, Any]) -> str | None:
    meta = (material.get("tag_sources") or {}).get(tag)
    if meta:
        return str(meta)
    ref = str((material.get("tags") or {}).get(tag) or "")
    if ref.startswith("pack-json:"):
        pointer = ref[len("pack-json:"):]
        return next((label for prefix, label in NUMBER_SOURCE_LABELS if pointer.startswith(prefix)), "数字包")
    if ref.startswith("doc-passage:"):
        return "原文段落"
    return None


def summary_lines(record: Mapping[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    """A judgement's analyst lines with real figures, and the entries that source them.

    One line per cell (actual / expected / prior guidance), so a figure the
    publisher cannot source drops its own line and not the row.  Tags are
    taken out of the prose and the sources named in words; each figure is bound
    through its number entry (passage or pack number).  No refs in the prose
    (an identifier's digits read as figures to the publication check): the main
    event, the request and the judgement are in the title and display fields.
    """

    result = record.get("result") or {}
    material = record.get("result_material") or {}
    lines: list[str] = []
    entries: list[dict[str, Any]] = []
    used_tags: list[str] = []

    def add(text: str, tags: Sequence[str]) -> None:
        shown = _STRIP_TAGS.sub("", text).strip()
        lines.append(shown)
        entries.extend(_figure_entries(shown, tags, material))
        used_tags.extend(tags)

    dates = material.get("dates") or record.get("event_dates") or {}
    if dates:
        lines.append(dates_line(dates))
    verdict = result.get("verdict_label") or VERDICT_LABELS.get(record.get("decision"), record.get("decision"))
    lines.append(f"结论：{verdict}")
    for row in result.get("actual_vs_expected") or ():
        label = row.get("label") or ROW_LABELS.get(row.get("metric"), row.get("metric"))
        lines.append(f"{label}（{row.get('period') or '—'}）：{COMPARISON_LABELS.get(row.get('vs_expected'), '')}")
        sources = list(row.get("sources") or ())
        for key, name in (("actual", "实际"), ("expected", "预期"), ("prior_guidance", "上次指引")):
            cell = str(row.get(key) or "无")
            if cell == "无" and key != "actual":
                continue
            # A cell naming two sources ("一致预期 … [N50]；高盛预览 … [P9]") is
            # printed part by part, so each part stands on its own source.
            parts = [part.strip() for part in re.split(r"[；;]", cell) if part.strip()] or [cell]
            if len(parts) > 1 and all(_tags_in(part) for part in parts):
                for part in parts:
                    add(f"· {name}：{part}", _tags_in(part))
            else:
                add(f"· {name}：{cell}", list(dict.fromkeys(_tags_in(cell) or sources)))
    reaction = result.get("market_reaction") or {}
    if reaction.get("move"):
        add(f"市场反应：{reaction['move']}", _tags_in(reaction["move"]))
    if reaction.get("benchmark"):
        add(f"基准：{reaction['benchmark']}", _tags_in(reaction["benchmark"]))
    if reaction.get("explanation"):
        lines.append(f"反应解读：{reaction['explanation']}")
    impact = result.get("thesis_impact") or {}
    if impact.get("explanation"):
        lines.append(f"论点影响：{impact['explanation']}")
    if impact.get("why_unchanged"):
        lines.append(f"为何不改论点：{impact['why_unchanged']}")
    effect = record.get("effect") or {}
    request = effect.get("material_request") or {}
    if request.get("id"):
        lines.append(f"已提交材料请求（{request.get('status_label') or STATUS_LABEL_OPEN}），材料到达后重判。")
    rejudge = record.get("rejudgement") or {}
    if rejudge:
        lines.append(f"第 {rejudge.get('sequence')} 次重判：{rejudge.get('label') or rejudge.get('reason')}")
    labels = [label for label in dict.fromkeys(source_label(tag, material) for tag in used_tags) if label]
    if labels:
        lines.append("出处：" + "；".join(labels))
    return lines, entries


STATUS_LABEL_OPEN = "待获取"


def display_fields(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """What the cockpit shows for an event judgement's result contract."""

    result = record.get("result")
    material = record.get("result_material") or {}
    if not isinstance(result, Mapping) and not material:
        return None
    result = result or {}
    reaction = result.get("market_reaction") or {}
    effect = record.get("effect") or {}
    requests = [request for request in (effect.get("material_request"),) if isinstance(request, Mapping)
                and request.get("id")]
    return {
        "verdict": result.get("verdict"),
        "verdict_label": result.get("verdict_label") or VERDICT_LABELS.get(record.get("decision")),
        "evidence_status": result.get("evidence_status"),
        "event_actual_vs_expected": [
            {"metric": row.get("metric"), "label": row.get("label") or ROW_LABELS.get(row.get("metric")),
             "period": row.get("period"), "actual": row.get("actual"), "expected": row.get("expected"),
             "prior_guidance": row.get("prior_guidance"), "vs_expected": row.get("vs_expected"),
             "vs_expected_label": COMPARISON_LABELS.get(row.get("vs_expected")), "sources": row.get("sources")}
            for row in result.get("actual_vs_expected") or ()],
        "benchmark_move": {"move": reaction.get("move"), "benchmark": reaction.get("benchmark") or NO_BENCHMARK,
                           "explanation": reaction.get("explanation")} if reaction else None,
        "thesis_impact": result.get("thesis_impact"),
        "material_requests": [{"id": request.get("id"), "status": request.get("status"),
                               "status_label": request.get("status_label") or "待获取"} for request in requests],
        "event_dates": material.get("dates") or record.get("event_dates"),
        "main_event_ref": material.get("main_event_ref"),
        "catalyst_entry_ref": material.get("catalyst_entry_ref"),
        "rejudgement": record.get("rejudgement"),
        "sources": {tag: source_label(tag, material) for tag in sorted({
            tag for row in result.get("actual_vs_expected") or () for tag in row.get("sources") or ()})},
    }


__all__ = [
    "APPLICABLE_ROWS", "COMPARISONS", "COMPARISON_LABELS", "MAX_REJUDGEMENTS", "NO_BENCHMARK",
    "REJUDGE_REASONS", "REJUDGE_WINDOW_HOURS", "REQUIRED_ROWS", "RESULT_CONTRACT_VERSION", "ROW_KEYS",
    "ROW_LABELS", "VERDICT_EVIDENCE_INSUFFICIENT", "VERDICT_LABELS", "Tagger", "active_gap_request",
    "body_document", "body_required", "build_result_material", "dates_line", "display_fields",
    "display_item", "event_dates", "gap_judgement", "gap_need", "is_document_event", "is_result_event",
    "judgement_history", "lacked_body", "market_reaction", "next_periods", "numeric_tokens", "open_material",
    "output_spec_lines", "primary_first", "prompt_lines", "rejudgement_reason", "rejudgement_ref",
    "release_items", "research_need", "resolve_citation", "result_anchor", "result_contract_problems",
    "result_events", "result_key", "summary_lines",
]
