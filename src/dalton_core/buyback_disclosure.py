"""W4: what a US company did with its own shares, read out of what it filed.

America has no daily buyback return.  Hong Kong issuers file one every trading
day; a US issuer discloses repurchases three ways and all three are late or
partial:

* the **Item 2 issuer-purchases table** in a 10-Q or 10-K -- three monthly rows,
  shares bought, average price paid, how many were under the announced
  programme and what is left to spend.  Complete, filed, and up to a quarter
  stale on the day it appears;
* an **8-K** when the board authorises a programme or increases one.  Prompt,
  and it is a permission rather than an act: companies announce authorisations
  they never use;
* the **earnings call**, where management says what it intends.  That stays a
  ``transcript`` event -- it is management talking, not a filing -- and this
  module's job there is only to make sure the brain sees it beside the other
  two (:func:`transcript_mentions_buyback`).

So this module has two producers and one tagger, and every number any of them
emits is text taken verbatim out of a span that is carried on the event beside
it.  Nothing is computed here except the tie-out that checks the monthly rows
against the filed total; what a buyback *means* -- against the price paid, the
pace, the remaining authorisation, the share count -- is
:mod:`buyback_context`'s arithmetic and the judgement lane's decision.

**Why the table is parsed rather than asked about.**  The document extraction
path exists and works, and it is a model call: it asks a window "is the figure
we still owe in here" and then refuses any digit that is not in the quote it
cited.  That is the right shape for a number buried in prose.  It is the wrong
shape for a four-column table with a fixed layout that every filer in America
renders the same way, where the failure mode is not invention but *column
misalignment* -- a shares column read as a price -- and a model would not
notice while a total that does not tie out will.  So the rows are read
deterministically out of the same block rendering the extraction path uses
(``public_web_extraction_source.render_public_web_text``, renderer
``html-visible-blocks:0.1``), the digits are checked against the span they came
from with the same ``document_numeric_claim.numbers_in`` that checks a model's,
and the row set is checked against the filer's own total.  A section this
cannot resolve is refused with its reason rather than half-read.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from datetime import date
from types import MappingProxyType
from typing import Any

from .document_numeric_claim import numbers_in
from .store import content_hash
from .research_event import validate_payload

SCHEMA_VERSION = "0.1"

#: The two shapes a ``buyback_disclosure`` event can have. Closed: the payload
#: renders differently for each and a third would be a contract change.
DISCLOSURE_KINDS: tuple[str, ...] = ("issuer_purchases_table", "authorisation")

#: The forms that carry an issuer-purchases table. 10-Q Item 2 and 10-K Item 5;
#: an amendment carries it again and is read again, because an amended table is
#: the one that counts.
ISSUER_PURCHASE_FORMS: tuple[str, ...] = ("10-Q", "10-K", "10-Q/A", "10-K/A")
#: The 8-K items a buyback authorisation is announced under. 8.01 "Other
#: Events" is where almost all of them go; 7.01 "Regulation FD Disclosure" is
#: where the rest go when the company treats the press release as FD material.
#: Named as a filter and not as a promise: an 8-K under either item is a
#: *candidate*, and the text has to say "repurchase" before anything is read.
AUTHORISATION_8K_ITEMS: tuple[str, ...] = ("8.01", "7.01")

MAX_EXCERPT_CHARS = 600
MAX_ROWS_PER_TABLE = 24
MAX_AUTHORISATIONS = 4
#: How far the average-price tie-out may miss before it is reported as a
#: mismatch. Filers compute their total average over a cash outlay that
#: includes shares acquired by forfeiture, so the weighted mean of the printed
#: monthly averages is close but is not equal; half a percent catches a column
#: read into the wrong slot and does not cry wolf over rounding.
AVERAGE_PRICE_TOLERANCE = Decimal("0.005")

#: The unit a remaining-authorisation column is stated in. Read from the
#: filing's own header cell -- "(in millions of U.S. dollars)" -- and left
#: ``None`` when the filing does not say. A missing unit is the single most
#: dangerous default in this module: reading $3,244 million as $3,244 would
#: report a company with a quarter of a billion left as one with pocket change.
SCALE_WORDS: Mapping[str, str] = MappingProxyType({
    "thousand": "thousands", "thousands": "thousands",
    "million": "millions", "millions": "millions",
    "billion": "billions", "billions": "billions",
})

_SECTION_RE = re.compile(
    r"issuer purchases of equity securities"
    r"|unregistered sales of equity securities"
    r"|purchases of .{0,90}?(?:ordinary shares|common stock|common shares|equity securities)",
    re.IGNORECASE,
)
_NEXT_ITEM_RE = re.compile(r"^item\s+\d", re.IGNORECASE)
_MONTHS = (
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
)
_MONTH_ALT = "|".join(_MONTHS) + "|" + "|".join(month[:3] for month in _MONTHS)
_DASH = r"[-‐‑‒–—―]|to|through"
_FOOTNOTE_MARK = r"(?:\s*\((?:\d+|[a-z])\))?"
_PERIOD_PATTERNS: tuple[re.Pattern[str], ...] = (
    # March 1, 2026 — March 31, 2026
    re.compile(
        rf"^(?:{_MONTH_ALT})\.?\s+\d{{1,2}},?\s+\d{{4}}\s*(?:{_DASH})\s*"
        rf"(?:{_MONTH_ALT})\.?\s+\d{{1,2}},?\s+\d{{4}}{_FOOTNOTE_MARK}$",
        re.IGNORECASE,
    ),
    # March 1 — March 31, 2026
    re.compile(
        rf"^(?:{_MONTH_ALT})\.?\s+\d{{1,2}}\s*(?:{_DASH})\s*"
        rf"(?:{_MONTH_ALT})\.?\s+\d{{1,2}},?\s+\d{{4}}{_FOOTNOTE_MARK}$",
        re.IGNORECASE,
    ),
    # March 2026
    re.compile(rf"^(?:{_MONTH_ALT})\.?\s+\d{{4}}{_FOOTNOTE_MARK}$", re.IGNORECASE),
    # 03/01/26 - 03/31/26
    re.compile(
        rf"^\d{{1,2}}/\d{{1,2}}/\d{{2,4}}\s*(?:{_DASH})\s*"
        rf"\d{{1,2}}/\d{{1,2}}/\d{{2,4}}{_FOOTNOTE_MARK}$"
    ),
)
_TOTAL_RE = re.compile(rf"^total{_FOOTNOTE_MARK}$", re.IGNORECASE)
_NIL_RE = re.compile(r"^[-‐‑‒–—―−]$|^n/?a$", re.IGNORECASE)
_CURRENCY_MARK_RE = re.compile(r"^[$£€¥]$")
_NUMBER_BLOCK_RE = re.compile(r"^[$£€¥]?\s*\(?\d[\d,]*(?:\.\d+)?\)?$")
_SCALE_RE = re.compile(
    r"\(?\s*(?:amounts?\s+)?in\s+(thousands?|millions?|billions?)", re.IGNORECASE
)
_CURRENCY_RE = re.compile(
    r"\b(?:u\.?s\.?\s*dollars?|usd)\b|\$", re.IGNORECASE
)

#: What has to be in an 8-K before a word of it is read as a buyback. A press
#: release about a dividend, a credit agreement or a director's retirement is
#: also an Item 8.01 and none of them authorises anything.
_BUYBACK_TEXT_RE = re.compile(r"repurchas|buy-?back|share purchase program", re.IGNORECASE)
_AUTHORISATION_RE = re.compile(
    r"(?P<change>authoriz\w*|approv\w*|increas\w*|expand\w*|renew\w*)"
    r"[^.]{0,160}?"
    r"(?P<amount>[$£€]\s?\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<scale>thousand|million|billion)?",
    re.IGNORECASE,
)
_REMAINING_RE = re.compile(
    r"(?P<amount>[$£€]\s?\d[\d,]*(?:\.\d+)?)\s*(?P<scale>thousand|million|billion)?"
    r"[^.]{0,80}?(?:remain\w*|available|yet to be (?:purchas|repurchas)\w*)",
    re.IGNORECASE,
)
_NEW_PROGRAM_RE = re.compile(r"\bnew\b[^.]{0,60}?(?:program|programme|plan)", re.IGNORECASE)
_INCREASE_RE = re.compile(r"increas\w*|additional|expand\w*", re.IGNORECASE)


class BuybackExtractionError(ValueError):
    """A section cannot be read as an issuer-purchases table."""


# ---------------------------------------------------------------------------
# reading the Item 2 table
# ---------------------------------------------------------------------------


def _blocks(text: str) -> list[str]:
    return [block.strip() for block in text.split("\n\n") if block.strip()]


def _is_period(block: str) -> bool:
    return any(pattern.match(block) for pattern in _PERIOD_PATTERNS)


def _number_text(block: str) -> tuple[str, bool] | None:
    """A table cell as ``(digits, was_a_dash)``, or ``None`` when not a cell.

    The currency sign is a cell of its own in the block rendering -- filers put
    it in a separate column so the amounts line up -- so a bare ``$`` is not a
    value and is skipped by the caller. Parentheses are the accounting minus
    and are kept as a leading ``-``, because a negative in this table would be
    a restatement and hiding it would be worse than reporting it oddly.

    An em dash is a filed zero and is returned as ``"0"``, with the flag set so
    the verbatim check knows not to look for the digit in a cell that does not
    contain one. Reading it as missing instead would turn "we bought nothing
    under the programme in May" into "the filing did not say", which is the
    difference between a pause a reader should notice and one they never see.
    """

    if _NIL_RE.match(block):
        return ("0", True)
    if not _NUMBER_BLOCK_RE.match(block):
        return None
    stripped = block.strip().lstrip("$£€¥").strip()
    negative = stripped.startswith("(") and stripped.endswith(")")
    digits = stripped.strip("()").replace(",", "")
    if not digits:
        return None
    return (f"-{digits}" if negative else digits, False)


def issuer_purchase_section(text: str) -> dict[str, Any]:
    """The Item 2 / Item 5 issuer-purchases section of a filing, or a refusal.

    Bounded at both ends: it starts at the block whose wording names the
    section and stops at the next ``Item N`` heading, so the footnotes that
    qualify the table are inside and the next item's prose is not.
    """

    blocks = _blocks(text)
    start = next(
        (index for index, block in enumerate(blocks) if _SECTION_RE.search(block)),
        None,
    )
    if start is None:
        return {
            "status": "absent",
            "reason": "this document has no issuer-purchases section; it is not a "
                      "filing that reports repurchases, or the section is worded in "
                      "a way this reader does not recognise",
            "blocks": [], "scale": None, "currency": None,
        }
    end = len(blocks)
    for index in range(start + 1, len(blocks)):
        if _NEXT_ITEM_RE.match(blocks[index]):
            end = index
            break
    section = blocks[start:end]
    first_row = next(
        (index for index, block in enumerate(section) if _is_period(block)),
        None,
    )
    header = section[: first_row if first_row is not None else len(section)]
    scale = None
    currency = None
    for block in header:
        found = _SCALE_RE.search(block)
        if found and scale is None:
            scale = SCALE_WORDS[found.group(1).lower()]
        if currency is None and _CURRENCY_RE.search(block):
            currency = "USD"
    return {
        "status": "read", "reason": None, "blocks": section,
        "scale": scale, "currency": currency,
        "header": header,
    }


def issuer_purchase_rows(text: str) -> dict[str, Any]:
    """Every row of one issuer-purchases table, verbatim, plus its tie-out.

    Returns monthly rows and the filer's own total separately. The total is not
    an event -- it is a quarter's summary of three rows already recorded -- and
    it is not thrown away either: it is the only independent check this reader
    has that it put each column in the right slot.
    """

    section = issuer_purchase_section(text)
    if section["status"] != "read":
        return {**section, "rows": [], "total": None, "tie_out": None}
    blocks = section["blocks"]
    rows: list[dict[str, Any]] = []
    total: dict[str, Any] | None = None
    index = 0
    while index < len(blocks) and len(rows) < MAX_ROWS_PER_TABLE:
        block = blocks[index]
        is_total = bool(_TOTAL_RE.match(block))
        if not (_is_period(block) or is_total):
            index += 1
            continue
        values: list[tuple[str, bool]] = []
        span = [block]
        cursor = index + 1
        while cursor < len(blocks) and len(values) < 4:
            cell = blocks[cursor]
            if _CURRENCY_MARK_RE.match(cell):
                span.append(cell)
                cursor += 1
                continue
            number = _number_text(cell)
            if number is None:
                break
            values.append(number)
            span.append(cell)
            cursor += 1
        if len(values) < 2:
            index += 1
            continue
        excerpt = " | ".join(span)[:MAX_EXCERPT_CHARS]
        columns = ("shares_purchased", "average_price_paid",
                   "shares_purchased_under_plans", "remaining_authorisation")
        row = {
            "period_label": block,
            **{
                name: (values[position][0] if position < len(values) else None)
                for position, name in enumerate(columns)
            },
            "remaining_authorisation_unit": section["scale"],
            "currency": section["currency"],
            "excerpt": excerpt,
            "excerpt_hash": content_hash({"excerpt": excerpt}),
            "is_total": is_total,
            # Which columns the filer printed as a dash. Carried so the
            # verbatim check can skip them and so a reader can tell a filed
            # zero from a figure this reader put there.
            "filed_as_dash": [
                name for position, name in enumerate(columns)
                if position < len(values) and values[position][1]
            ],
        }
        _verify_verbatim(row)
        if is_total:
            total = row
        else:
            rows.append(row)
        index = cursor
    if not rows:
        return {
            "status": "unreadable",
            "reason": "the issuer-purchases section was located but no monthly row "
                      "could be read out of it; a table read half-way is worse than "
                      "one not read at all",
            "rows": [], "total": total, "scale": section["scale"],
            "currency": section["currency"], "blocks": blocks,
        }
    return {
        "status": "read", "reason": None, "rows": rows, "total": total,
        "scale": section["scale"], "currency": section["currency"],
        "tie_out": tie_out(rows, total),
        "blocks": blocks,
    }


def _verify_verbatim(row: Mapping[str, Any]) -> None:
    """Every figure on a row must be in the span the row was read from.

    An invariant rather than a tamper boundary -- the values were taken out of
    that span a moment ago -- and it is here for the same reason
    ``sec_filings_index.build_filing_url_authorities`` recomputes a record
    hash: the day somebody adds a unit conversion or a rounding step to this
    reader, the check fires instead of the filing quietly acquiring a number it
    never contained.
    """

    present = numbers_in(row["excerpt"])
    dashes = set(row.get("filed_as_dash") or ())
    for field in ("shares_purchased", "average_price_paid",
                  "shares_purchased_under_plans", "remaining_authorisation"):
        value = row.get(field)
        if value is None or field in dashes:
            continue
        try:
            parsed = Decimal(value)
        except InvalidOperation as exc:  # pragma: no cover - _number_text guards this
            raise BuybackExtractionError(f"{field} is not a filed number") from exc
        if parsed not in present:
            raise BuybackExtractionError(
                f"{field}={value} is not in the span it was read from: {row['excerpt']!r}"
            )


def tie_out(
    rows: Sequence[Mapping[str, Any]], total: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Do the monthly rows add up to the total the filer printed?

    The only check in this reader that can catch the failure that matters. A
    column read into the wrong slot produces rows that each verify against
    their own span and a sum that is nowhere near the filer's total, so this is
    what stands between "we parsed the table" and "we parsed the right columns".

    It reports rather than refuses. A filing with no total row is common and is
    not an error; a mismatch is worth putting in front of the brain with the
    two numbers beside each other, because the alternative -- dropping the
    quarter -- loses a real disclosure over an arithmetic disagreement a person
    could resolve in a minute.
    """

    if total is None:
        return {"status": "no_total_row",
                "reason": "the filing printed no total row to check against"}
    summed = sum(
        (Decimal(row["shares_purchased"]) for row in rows), Decimal(0)
    )
    filed = Decimal(total["shares_purchased"])
    shares_match = summed == filed
    result: dict[str, Any] = {
        "status": "matched" if shares_match else "mismatched",
        "summed_shares": format(summed, "f"),
        "filed_total_shares": format(filed, "f"),
    }
    weighted = sum(
        (Decimal(row["shares_purchased"]) * Decimal(row["average_price_paid"])
         for row in rows),
        Decimal(0),
    )
    if summed > 0 and total.get("average_price_paid"):
        computed = weighted / summed
        printed = Decimal(total["average_price_paid"])
        deviation = (
            abs(computed - printed) / printed if printed else Decimal(0)
        )
        result["weighted_average_price"] = format(
            computed.quantize(Decimal("0.0001")), "f"
        )
        result["filed_total_average_price"] = format(printed, "f")
        result["average_price_within_tolerance"] = deviation <= AVERAGE_PRICE_TOLERANCE
        # Stated rather than left implicit: filers compute the total average
        # over a cash outlay that includes shares acquired by forfeiture, so a
        # small miss is the normal case and is not evidence of anything.
        result["average_price_note"] = (
            "the filer's total average is computed over the whole cash outlay, "
            "which need not equal the weighted mean of the printed monthly "
            "averages; a small difference is expected"
        )
    return result


# ---------------------------------------------------------------------------
# reading an authorisation out of an 8-K
# ---------------------------------------------------------------------------


def authorisation_from_text(text: str) -> list[dict[str, Any]]:
    """Board authorisations announced in one 8-K body, or nothing.

    Nothing is read unless the body says "repurchase", "buyback" or "share
    purchase program" somewhere: an Item 8.01 is where a company puts anything
    it wants on the record, and most of them are not about buying shares.

    ``authorisation_change`` is ``new``, ``increase`` or ``unknown``, and the
    third is a real answer. "The Board authorized $5 billion" does not say
    whether that is a fresh programme or a top-up, and the difference matters:
    a $5bn increase on a $54bn programme is a continuation and a $5bn new
    programme after none is a change of policy.
    """

    if not _BUYBACK_TEXT_RE.search(text or ""):
        return []
    found: list[dict[str, Any]] = []
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if not _BUYBACK_TEXT_RE.search(sentence):
            continue
        match = _AUTHORISATION_RE.search(sentence)
        if match is None:
            continue
        amount = _money(match.group("amount"))
        if amount is None:
            continue
        remaining = _REMAINING_RE.search(sentence)
        excerpt = " ".join(sentence.split())[:MAX_EXCERPT_CHARS]
        row = {
            "authorised_amount": amount,
            "authorised_amount_unit": (
                SCALE_WORDS[match.group("scale").lower()] if match.group("scale")
                else None
            ),
            "authorisation_change": _change_word(sentence),
            "remaining_authorisation": (
                _money(remaining.group("amount")) if remaining else None
            ),
            "remaining_authorisation_unit": (
                SCALE_WORDS[remaining.group("scale").lower()]
                if remaining and remaining.group("scale") else None
            ),
            "currency": "USD" if "$" in sentence else None,
            "excerpt": excerpt,
            "excerpt_hash": content_hash({"excerpt": excerpt}),
        }
        _verify_authorisation(row)
        found.append(row)
        if len(found) >= MAX_AUTHORISATIONS:
            break
    return found


def _money(raw: str) -> str | None:
    digits = raw.strip().lstrip("$£€").strip().replace(",", "")
    try:
        Decimal(digits)
    except InvalidOperation:
        return None
    return digits


def _change_word(sentence: str) -> str:
    if _NEW_PROGRAM_RE.search(sentence):
        return "new"
    if _INCREASE_RE.search(sentence):
        return "increase"
    return "unknown"


def _verify_authorisation(row: Mapping[str, Any]) -> None:
    present = numbers_in(row["excerpt"])
    for field in ("authorised_amount", "remaining_authorisation"):
        value = row.get(field)
        if value is None:
            continue
        if Decimal(value) not in present:
            raise BuybackExtractionError(
                f"{field}={value} is not in the sentence it was read from"
            )


# ---------------------------------------------------------------------------
# the transcript tag
# ---------------------------------------------------------------------------

#: The aspect a buyback belongs to in the P12b vocabulary. Not a new word: the
#: dossier section "management and capital allocation" is where buybacks live
#: and ``claim_index_tagging`` already routes "buyback|repurchase" there, so
#: this names the existing tag rather than inventing a parallel one.
BUYBACK_ASPECT = "management_and_capital_allocation"


def transcript_mentions_buyback(
    connection: sqlite3.Connection, *, company_ref: str, document_ref: str | None
) -> dict[str, Any]:
    """Whether this company has buyback Claims, and which ones.

    The owner's instruction is that a transcript mention stays a ``transcript``
    event -- management talking is not a filing and promoting it would flatten
    the evidence tier that the whole ledger is sorted by -- but that the brain
    should see it *beside* the filed disclosures. So the tag is a lookup, not a
    new event kind: the Claims this company has under the capital-allocation
    aspect whose text is about repurchases, with their refs, ready to be
    printed in the buyback context block.
    """

    try:
        rows = connection.execute(
            "SELECT claim_version_id AS id, claim_json FROM claim_versions "
            "WHERE json_extract(claim_json,'$.subject_ref')=? "
            "ORDER BY created_at DESC, claim_version_id DESC LIMIT 200",
            (company_ref,),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        return {"status": "unavailable",
                "reason": "this Core holds no Claim ledger", "claims": []}
    import json as _json

    matches: list[dict[str, Any]] = []
    for row in rows:
        try:
            claim = _json.loads(row["claim_json"])
        except (TypeError, ValueError):
            continue
        statement = str(
            claim.get("normalized_statement") or claim.get("statement") or ""
        )
        if not _BUYBACK_TEXT_RE.search(statement):
            continue
        matches.append({
            "ref": row["id"],
            "aspect": claim.get("aspect") or BUYBACK_ASPECT,
            "source_ref": claim.get("source_ref"),
            "statement": statement[:300],
            "is_this_document": bool(
                document_ref and claim.get("source_ref") == document_ref
            ),
        })
        if len(matches) >= 6:
            break
    return {"status": "read", "reason": None, "claims": matches}


# ---------------------------------------------------------------------------
# producers: event bodies
# ---------------------------------------------------------------------------


def issuer_purchase_events(
    text: str,
    *,
    company_ref: str,
    accession: str,
    form: str,
    filing_date: str | None,
    period_end: str | None,
    document_ref: str | None = None,
    source_ref: str = "source:sec-edgar",
    invocation_ref: str | None = None,
    artifact_hash: str | None = None,
) -> dict[str, Any]:
    """One filing's Item 2 table as ``buyback_disclosure`` event bodies.

    One event per monthly row. Per row rather than per filing because the rows
    are what a reader compares -- March against April, this quarter's pace
    against last -- and a single event carrying three months would have to be
    unpacked by every consumer.

    ``event_key`` names the row inside the filing, so a lane that re-reads the
    same 10-Q marks it emitted without diffing anything, exactly as the
    ownership lane does.
    """

    parsed = issuer_purchase_rows(text)
    if parsed["status"] != "read":
        return {"status": parsed["status"], "reason": parsed["reason"],
                "events": [], "tie_out": None}
    occurred = filing_date or period_end
    if not occurred:
        return {
            "status": "undated",
            "reason": f"{accession} carries neither a filing date nor a period end; "
                      "an event with no date cannot be ordered against anything",
            "events": [], "tie_out": parsed["tie_out"],
        }
    events = []
    for row in parsed["rows"]:
        payload = {
            "disclosure_kind": "issuer_purchases_table",
            "market": "US",
            "cluster_key": f"{date.fromisoformat(occurred).strftime('%G-W%V')}:{accession}",
            "cumulative_shares": None,
            "cumulative_basis": None,
            "accession": accession,
            "form": form,
            "filing_date": filing_date,
            "period_end": period_end,
            "period_label": row["period_label"],
            "shares_purchased": row["shares_purchased"],
            "average_price_paid": row["average_price_paid"],
            "shares_purchased_under_plans": row["shares_purchased_under_plans"],
            "remaining_authorisation": row["remaining_authorisation"],
            "remaining_authorisation_unit": row["remaining_authorisation_unit"],
            "currency": row["currency"],
            "authorised_amount": None,
            "authorised_amount_unit": None,
            "authorisation_change": None,
            "announced_date": None,
            "items": None,
            "exhibit": None,
            "document_ref": document_ref,
            "source_ref": source_ref,
            "excerpt": row["excerpt"],
            "excerpt_hash": row["excerpt_hash"],
            "invocation_ref": invocation_ref,
            "artifact_hash": artifact_hash,
            "event_key": content_hash({
                "accession": accession, "row": row["excerpt_hash"],
                "kind": "issuer_purchases_table",
            }),
        }
        payload = validate_payload("buyback_disclosure", payload)
        events.append({
            "kind": "buyback_disclosure",
            "company_ref": company_ref,
            "evidence_tier": "primary_filing",
            "occurred_at": f"{occurred}T00:00:00+00:00",
            "source_refs": [source_ref, f"sec:filing:{accession}"],
            "payload": payload,
        })
    return {"status": "read", "reason": None, "events": events,
            "tie_out": parsed["tie_out"]}


def authorisation_events(
    text: str,
    *,
    company_ref: str,
    accession: str,
    form: str = "8-K",
    filing_date: str | None,
    items: str | None = None,
    exhibit: str | None = None,
    document_ref: str | None = None,
    source_ref: str = "source:sec-edgar",
    invocation_ref: str | None = None,
    artifact_hash: str | None = None,
) -> dict[str, Any]:
    """One 8-K's buyback authorisations as event bodies, or a reason there are none."""

    if items is not None and not any(
        item.strip() in AUTHORISATION_8K_ITEMS for item in str(items).split(",")
    ):
        return {
            "status": "not_a_candidate",
            "reason": f"{accession} is filed under items {items}; a buyback "
                      f"authorisation is announced under {list(AUTHORISATION_8K_ITEMS)}",
            "events": [],
        }
    found = authorisation_from_text(text)
    if not found:
        return {
            "status": "absent",
            "reason": f"{accession} says nothing about repurchasing shares, or names "
                      "no amount; nothing is read from an announcement that does not "
                      "state one",
            "events": [],
        }
    if not filing_date:
        return {"status": "undated",
                "reason": f"{accession} carries no filing date", "events": []}
    events = []
    for row in found:
        payload = {
            "disclosure_kind": "authorisation",
            "market": "US",
            "cluster_key": f"{date.fromisoformat(filing_date).strftime('%G-W%V')}:{accession}",
            "cumulative_shares": None,
            "cumulative_basis": None,
            "accession": accession,
            "form": form,
            "filing_date": filing_date,
            "period_end": None,
            "period_label": None,
            "shares_purchased": None,
            "average_price_paid": None,
            "shares_purchased_under_plans": None,
            "remaining_authorisation": row["remaining_authorisation"],
            "remaining_authorisation_unit": row["remaining_authorisation_unit"],
            "currency": row["currency"],
            "authorised_amount": row["authorised_amount"],
            "authorised_amount_unit": row["authorised_amount_unit"],
            "authorisation_change": row["authorisation_change"],
            "announced_date": filing_date,
            "items": items,
            "exhibit": exhibit,
            "document_ref": document_ref,
            "source_ref": source_ref,
            "excerpt": row["excerpt"],
            "excerpt_hash": row["excerpt_hash"],
            "invocation_ref": invocation_ref,
            "artifact_hash": artifact_hash,
            "event_key": content_hash({
                "accession": accession, "row": row["excerpt_hash"],
                "kind": "authorisation",
            }),
        }
        payload = validate_payload("buyback_disclosure", payload)
        events.append({
            "kind": "buyback_disclosure",
            "company_ref": company_ref,
            "evidence_tier": "primary_filing",
            "occurred_at": f"{filing_date}T00:00:00+00:00",
            "source_refs": [source_ref, f"sec:filing:{accession}"],
            "payload": payload,
        })
    return {"status": "read", "reason": None, "events": events}


# ---------------------------------------------------------------------------
# where the text comes from
# ---------------------------------------------------------------------------

_ACCESSION_IN_REF_RE = re.compile(r"sec:filing:([0-9]{10}-[0-9]{2}-[0-9]{6})")
_ACCESSION_IN_URL_RE = re.compile(r"/([0-9]{18})/")
_FORM_IN_TITLE_RE = re.compile(r"\b(10-Q|10-K|8-K)(?:/A)?\b", re.IGNORECASE)


def accession_of(*fields: Any) -> str | None:
    """The accession a document belongs to, from refs or an EDGAR path.

    Two routes and no third. A ``sec:filing:`` record ref is the accession
    said outright; an eighteen-digit segment of an EDGAR archive path is the
    accession with its dashes removed, which is how EDGAR writes directories.
    A document that offers neither is skipped by the caller with a reason --
    a buyback figure whose filing nobody can name is not evidence.
    """

    for field in fields:
        if not isinstance(field, str):
            continue
        direct = _ACCESSION_IN_REF_RE.search(field)
        if direct:
            return direct.group(1)
    for field in fields:
        if not isinstance(field, str):
            continue
        path = _ACCESSION_IN_URL_RE.search(field)
        if path:
            plain = path.group(1)
            return f"{plain[:10]}-{plain[10:12]}-{plain[12:]}"
    return None


def buyback_documents(
    connection: sqlite3.Connection, *, company_ref: str, limit: int = 40
) -> list[dict[str, Any]]:
    """The filings held for this company that could carry a repurchase table.

    Reads the document index -- the projection the extraction path already
    builds over every acquired document -- rather than fetching anything. A row
    whose filing cannot be named is returned with ``accession: None`` and the
    caller reports it as skipped: silence about a document we hold and could
    not attribute is worse than a line in a tick summary.
    """

    try:
        rows = connection.execute(
            "SELECT d.artifact_version_ref AS artifact_version_ref, d.title AS title, "
            "d.document_date AS document_date, d.source_metadata AS source_metadata, "
            "d.source_record_refs_json AS source_record_refs_json, "
            "d.extracted_text AS extracted_text, "
            "d.artifact_content_hash AS artifact_content_hash "
            "FROM document_index_documents d "
            "JOIN document_index_companies c ON c.document_rowid = d.rowid "
            "WHERE c.company_ref = ? ORDER BY d.document_date DESC LIMIT ?",
            (company_ref, int(limit)),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        return []
    found: list[dict[str, Any]] = []
    for row in rows:
        title = row["title"] or ""
        form_match = _FORM_IN_TITLE_RE.search(title) or _FORM_IN_TITLE_RE.search(
            row["source_metadata"] or ""
        )
        if form_match is None:
            continue
        accession = accession_of(
            row["source_record_refs_json"], row["source_metadata"], title
        )
        found.append({
            "artifact_version_ref": row["artifact_version_ref"],
            "artifact_hash": row["artifact_content_hash"],
            "title": title,
            "form": form_match.group(1).upper(),
            "document_date": row["document_date"],
            "accession": accession,
            "text": row["extracted_text"],
        })
    return found


def buyback_event_candidates(
    connection: sqlite3.Connection,
    *,
    company_ref: str,
    limit: int = 40,
) -> dict[str, Any]:
    """Every buyback event derivable from the filings this Core already holds.

    Stateless, like every other emitter feeding the event ledger: the ledger is
    idempotent on what an event says, so re-reading the same 10-Q tomorrow
    costs a lookup and writes nothing.
    """

    events: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    tie_outs: list[dict[str, Any]] = []
    for document in buyback_documents(connection, company_ref=company_ref, limit=limit):
        if document["accession"] is None:
            skipped.append({
                "artifact_version_ref": document["artifact_version_ref"],
                "reason": "no accession could be derived from this document's refs "
                          "or URL; a filed figure that cannot name its filing is "
                          "not evidence",
            })
            continue
        if document["form"] in ("10-Q", "10-K"):
            produced = issuer_purchase_events(
                document["text"] or "",
                company_ref=company_ref,
                accession=document["accession"],
                form=document["form"],
                filing_date=document["document_date"],
                period_end=None,
                document_ref=document["artifact_version_ref"],
                artifact_hash=document["artifact_hash"],
            )
            if produced["tie_out"] is not None:
                tie_outs.append({
                    "accession": document["accession"], **produced["tie_out"]
                })
        elif document["form"] == "8-K":
            produced = authorisation_events(
                document["text"] or "",
                company_ref=company_ref,
                accession=document["accession"],
                filing_date=document["document_date"],
                document_ref=document["artifact_version_ref"],
                artifact_hash=document["artifact_hash"],
            )
        else:  # pragma: no cover - _FORM_IN_TITLE_RE admits only the three
            continue
        if produced["status"] != "read":
            skipped.append({
                "accession": document["accession"],
                "form": document["form"],
                "reason": produced["reason"],
            })
            continue
        events.extend(produced["events"])
    return {"events": events, "skipped": skipped, "tie_outs": tie_outs}


__all__ = [
    "AUTHORISATION_8K_ITEMS",
    "AVERAGE_PRICE_TOLERANCE",
    "BUYBACK_ASPECT",
    "DISCLOSURE_KINDS",
    "ISSUER_PURCHASE_FORMS",
    "MAX_EXCERPT_CHARS",
    "MAX_ROWS_PER_TABLE",
    "SCALE_WORDS",
    "SCHEMA_VERSION",
    "BuybackExtractionError",
    "accession_of",
    "authorisation_events",
    "authorisation_from_text",
    "buyback_documents",
    "buyback_event_candidates",
    "issuer_purchase_events",
    "issuer_purchase_rows",
    "issuer_purchase_section",
    "tie_out",
    "transcript_mentions_buyback",
]
