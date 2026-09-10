"""W4: what an insider trade is worth knowing, computed rather than asked.

The owner's instruction (2026-09-10) is that filings are part of daily tracking,
management share sales especially, and that *whether one matters is the brain's
call, not a rule*.  Those two halves are in tension only if you confuse the
context with the decision.  A Form 4 on its own is unreadable: "SOLD 6,692
shares" says nothing until you know it is 0.2% of what this person holds, that
they have sold four times in ninety days, that the box saying it was a
pre-arranged plan is ticked, and that a Form 144 two months ago already told the
market it was coming.  All four of those are arithmetic over filings this Core
already holds.  None of them is a judgement.

So this module computes them, and stops.  It contains no threshold above which
a sale is "large", no rule that a 10b5-1 sale is benign, and no word for what
any of it means.  It renders a table and a set of *considerations* -- the
owner's own reading of these signals, written as things to weigh -- into the
judgement prompt, and the five-word decision vocabulary decides.

Three properties are load-bearing:

**Replayable, with no model call.**  Every number here is a deterministic
function of events already in the ledger.  The same ledger produces the same
context tomorrow, which is what lets a judgement be re-read a month later and
checked rather than taken on trust.

**Every figure points at an accession.**  A percentage of a holding is only as
good as the two filings it came from, so the context carries the refs and the
prompt prints them; a citation the model makes has to be one of those.

**Absent is never zero and never False.**  The Rule 10b5-1 box is ``unknown``
when the form predates it.  ``anticipated`` is ``unknown`` when there is
nothing to have searched.  A trailing aggregate over a ledger that only started
last week says how far back it could actually see.  Every one of these was a
place where a confident-looking default would have been a lie the prompt
printed in bold.

The four editorial flags (``routine_award``, ``exercise_and_sell``,
``tax_withholding``, ``no_consideration``) come from the owner's own
filing-alert practice.  They are flags and not filters: a withholding
disposition is still recorded, still judged, and still counted -- it is simply
labelled as what it is, because "the CFO disposed of 12,000 shares" and "the
CFO's vesting triggered a tax withholding of 12,000 shares" are different
sentences and only one of them is a decision anybody made.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

SCHEMA_VERSION = "0.1"

#: The window the owner named for "recent insider activity", and the window a
#: Form 144 covers: an affiliate who files one may sell within three months.
#: One number for both is deliberate -- it is the same question asked twice.
TRAILING_DAYS = 90
#: "Several insiders sold in the same week" is a different fact from "several
#: insiders sold this quarter", and the cron practice the owner runs reports
#: the first. Seven days rather than five, because a filing made on Monday for
#: a Friday trade is the same week to a reader and is not to a calendar.
CLUSTER_DAYS = 7
#: How many rows of the trailing window the prompt prints. A bound, because a
#: prompt is a table somebody has to read: an issuer whose executives file
#: forty Form 4s a quarter would otherwise push the theses off the end.
MAX_TRAILING_ROWS = 8
MAX_ANTICIPATION_ROWS = 3
MIN_CAPITAL_ALLOCATION_CLAIMS = 5

#: §16 codes that dispose of shares. ``S`` is a sale into the market; ``D`` is
#: a disposition back to the issuer; ``F`` is shares withheld to pay the tax on
#: a vesting, which is not a decision to sell and is flagged as such below.
DISPOSAL_CODES: frozenset[str] = frozenset({"S", "D", "F", "U"})
ACQUISITION_CODES: frozenset[str] = frozenset({"P", "A", "M", "C", "X", "L"})
#: An award, a vesting, a gift: nothing changed hands for money.
NO_CONSIDERATION_CODES: frozenset[str] = frozenset({"A", "G"})
TAX_WITHHOLDING_CODE = "F"
EXERCISE_CODE = "M"
SALE_CODE = "S"

#: What the owner reads each of these signals *for*. Considerations, not rules:
#: they are printed to the brain as things to weigh, and none of them says what
#: to decide. The wording is the owner's own (2026-09-10) and is frozen here
#: because it is what the prompt shows.
CONSIDERATIONS: tuple[str, ...] = (
    "A small insider sale is a weak signal. Do not build a case on one.",
    "A large sale can mean two opposite things and the filing does not say "
    "which. It may be management reading the shares as fully priced. It may "
    "equally be an overhang the market already expected, in which case the "
    "negative has now landed and the stock is freer than it was.",
    "Ask whether anything anticipated this sale -- a Form 144, a disclosed "
    "trading arrangement, a note in our own file. A sale nobody expected and a "
    "sale everybody was waiting for are not the same event.",
    "A sale under a Rule 10b5-1 plan was scheduled before the window it fell "
    "in, so it says less about what management thinks today than an "
    "unscheduled one does. It does not say nothing: somebody chose to adopt "
    "the plan, and when.",
    "Weigh the size against what the person holds, not against the share "
    "count. Selling a fifth of a position is a different act from selling a "
    "fiftieth of it, whatever the dollar figure.",
    "Several insiders selling in the same week is a different fact from one "
    "insider selling. So is one insider selling in four consecutive months.",
    "A withholding disposition (code F) and a routine award (code A at no "
    "consideration) are mechanics, not decisions. Say so rather than counting "
    "them as selling.",
)

#: Words in our own file that would mean somebody already expected this sale.
#: Deliberately narrow: this is a search for an *anticipation*, and a claim
#: that merely mentions the word "sell" is not one.
_ANTICIPATION_RE = re.compile(
    r"(10b5-1|10b5\-1|rule 10b5|trading (?:plan|arrangement)|"
    r"(?:planned|expected|announced|scheduled|proposed|intended) (?:share )?sale|"
    r"(?:plans?|intends?|expects?) to sell|lock-?up (?:expiry|expiration|release)|"
    r"secondary offering|form 144)",
    re.IGNORECASE,
)


class InsiderContextError(ValueError):
    """The event handed in is not an insider transaction this can read."""


# ---------------------------------------------------------------------------
# numbers
# ---------------------------------------------------------------------------


def _decimal(value: Any) -> Decimal | None:
    """A filed figure as a Decimal, or ``None`` when it is not one.

    Filings write "4,321,766" and "1200.0000" and "—". None of those is a
    float and two of them are not numbers at all, so this converts what it can
    and refuses the rest rather than reaching for a default.
    """

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Decimal(value)
    if not isinstance(value, str):
        return None
    text = value.strip().replace(",", "").replace(" ", "")
    if not text:
        return None
    try:
        parsed = Decimal(text)
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() else None


def _plain(value: Decimal | None, places: int) -> str | None:
    """A Decimal as text with a fixed number of places, never in exponent form."""

    if value is None:
        return None
    quantised = value.quantize(Decimal(1).scaleb(-places))
    return format(quantised, "f")


def percent_of_holding(shares: Any, owned_following: Any) -> str | None:
    """The trade as a percentage of what the owner holds after it.

    The owner's own formulation ("as % of the owner's holdings following the
    transaction"), and it is the ratio a reader wants: a director left holding
    almost nothing has sold something different from one left holding almost
    everything, whatever the absolute number was.

    ``None`` when the filing does not carry both figures, or when the holding
    after is zero -- the second is a real case (a director who sold out) and
    the honest answer is "they hold nothing now", which the caller prints from
    ``shares_owned_following`` rather than from a division by zero.
    """

    traded = _decimal(shares)
    held = _decimal(owned_following)
    if traded is None or held is None or held == 0:
        return None
    return _plain(traded / held * 100, 2)


def usd_value(shares: Any, price_per_share: Any) -> str | None:
    """Cash across the table, or ``None`` when the filing names no price.

    A grant reports a price of zero and the value really is zero; that is a
    fact and is returned as ``0.00``. A row with no price element at all is a
    different thing and returns ``None``.
    """

    traded = _decimal(shares)
    price = _decimal(price_per_share)
    if traded is None or price is None:
        return None
    return _plain(traded * price, 2)


# ---------------------------------------------------------------------------
# one row, read
# ---------------------------------------------------------------------------


def transaction_flags(payload: Mapping[str, Any]) -> list[str]:
    """The editorial flags for one row: mechanics told apart from decisions.

    From the owner's filing-alert practice. Each one marks a row that a reader
    scanning a list of "insider sales" would otherwise misread, and none of
    them removes the row.
    """

    code = payload.get("transaction_code")
    price = _decimal(payload.get("price_per_share"))
    shares = _decimal(payload.get("shares"))
    flags: list[str] = []
    if code == TAX_WITHHOLDING_CODE:
        flags.append("tax_withholding")
    if code in NO_CONSIDERATION_CODES and (price is None or price == 0):
        flags.append("no_consideration")
    if code == "A" and (price is None or price == 0):
        flags.append("routine_award")
    if shares is not None and shares == 0:
        flags.append("zero_shares")
    return flags


def _direction(payload: Mapping[str, Any]) -> str:
    """``disposal``, ``acquisition`` or ``holding``, from the filing's codes.

    The acquired/disposed code is asked first because it is the field that
    answers the question; the transaction code is the fallback for a row that
    reports one and not the other. A row with neither is a holding line on a
    Form 3, which is a position and not a trade.
    """

    marker = payload.get("acquired_disposed")
    if marker == "D":
        return "disposal"
    if marker == "A":
        return "acquisition"
    code = payload.get("transaction_code")
    if code in DISPOSAL_CODES:
        return "disposal"
    if code in ACQUISITION_CODES:
        return "acquisition"
    return "holding"


def _day(payload: Mapping[str, Any], event: Mapping[str, Any]) -> str | None:
    value = payload.get("transaction_date")
    if isinstance(value, str) and len(value) == 10:
        return value
    occurred = event.get("occurred_at")
    return occurred[:10] if isinstance(occurred, str) and len(occurred) >= 10 else None


def read_row(event: Mapping[str, Any]) -> dict[str, Any]:
    """One ``insider_transaction`` event as the figures a reader needs.

    Pure, and it does not touch the ledger: everything here is in the payload
    the emitter already bound to an accession and an artifact hash.
    """

    payload = event.get("payload") or {}
    return {
        "event_ref": event.get("id"),
        "accession": payload.get("accession"),
        "form": payload.get("form"),
        "owner_name": payload.get("owner_name"),
        "owner_cik": payload.get("owner_cik"),
        "role": payload.get("role"),
        "transaction_code": payload.get("transaction_code"),
        "transaction_meaning": payload.get("transaction_meaning"),
        "transaction_date": _day(payload, event),
        "security_title": payload.get("security_title"),
        "shares": payload.get("shares"),
        "price_per_share": payload.get("price_per_share"),
        "shares_owned_following": payload.get("shares_owned_following"),
        "direct_or_indirect": payload.get("direct_or_indirect"),
        "direction": _direction(payload),
        "percent_of_holding_following": percent_of_holding(
            payload.get("shares"), payload.get("shares_owned_following")
        ),
        "usd_value": usd_value(payload.get("shares"), payload.get("price_per_share")),
        "plan_10b5_1": plan_word(payload.get("plan_10b5_1")),
        "footnotes_hash": payload.get("footnotes_hash"),
        "flags": transaction_flags(payload),
        "invocation_ref": payload.get("invocation_ref"),
    }


def plan_word(value: Any) -> str:
    """``yes`` / ``no`` / ``unknown`` -- and ``unknown`` is not ``no``.

    The Rule 10b5-1 checkbox arrived with the 2023 amendments to Form 4.
    Everything filed under schema X0508 lacks the element entirely, and a
    reader who takes its absence for an unticked box has converted "the form
    could not say" into "management chose their own moment", which is an
    accusation the filing does not make.
    """

    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "unknown"


# ---------------------------------------------------------------------------
# the trailing window
# ---------------------------------------------------------------------------


def _within(day: str | None, *, end: str, days: int) -> bool:
    if not day:
        return False
    try:
        stamp = date.fromisoformat(day)
        last = date.fromisoformat(end)
    except ValueError:
        return False
    return last - timedelta(days=days) <= stamp <= last


def _aggregate(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Shares and dollars, split by direction, with the mechanics separated.

    Withholding and no-consideration rows are counted in their own totals
    rather than folded into the disposals, because a quarter in which an
    executive "disposed of" 40,000 shares reads very differently once you know
    38,000 of them went to the tax authority.
    """

    totals = {
        "disposal_shares": Decimal(0), "disposal_usd": Decimal(0),
        "acquisition_shares": Decimal(0), "acquisition_usd": Decimal(0),
        "withheld_shares": Decimal(0), "no_consideration_shares": Decimal(0),
    }
    counts = {"disposals": 0, "acquisitions": 0, "rows": 0}
    usd_incomplete = False
    for row in rows:
        counts["rows"] += 1
        shares = _decimal(row["shares"]) or Decimal(0)
        value = _decimal(row["usd_value"])
        flags = set(row["flags"])
        if "tax_withholding" in flags:
            totals["withheld_shares"] += shares
            continue
        if "no_consideration" in flags:
            totals["no_consideration_shares"] += shares
            continue
        if row["direction"] == "disposal":
            counts["disposals"] += 1
            totals["disposal_shares"] += shares
            if value is None:
                usd_incomplete = True
            else:
                totals["disposal_usd"] += value
        elif row["direction"] == "acquisition":
            counts["acquisitions"] += 1
            totals["acquisition_shares"] += shares
            if value is None:
                usd_incomplete = True
            else:
                totals["acquisition_usd"] += value
    return {
        **counts,
        "disposal_shares": _plain(totals["disposal_shares"], 0),
        "disposal_usd": _plain(totals["disposal_usd"], 2),
        "acquisition_shares": _plain(totals["acquisition_shares"], 0),
        "acquisition_usd": _plain(totals["acquisition_usd"], 2),
        "withheld_shares": _plain(totals["withheld_shares"], 0),
        "no_consideration_shares": _plain(totals["no_consideration_shares"], 0),
        # Said out loud rather than left to be inferred from a total that is
        # quietly short: a window in which one row had no price is a window
        # whose dollar figure is a floor.
        "usd_is_a_floor": usd_incomplete,
    }


def _exercise_and_sell(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Days on which the same owner exercised and sold, netted.

    The pair is one act -- an option turned into cash -- and reporting the two
    legs separately double-counts the shares and reports an "acquisition" that
    nobody paid for. The net is what was realised.
    """

    pairs: list[dict[str, Any]] = []
    by_day: dict[tuple[Any, Any], list[Mapping[str, Any]]] = {}
    for row in rows:
        by_day.setdefault((row["owner_cik"] or row["owner_name"],
                           row["transaction_date"]), []).append(row)
    for (owner, day), same in sorted(
        by_day.items(), key=lambda item: (str(item[0][1]), str(item[0][0]))
    ):
        exercises = [row for row in same if row["transaction_code"] == EXERCISE_CODE]
        sales = [row for row in same if row["transaction_code"] == SALE_CODE]
        if not exercises or not sales:
            continue
        sold = sum((_decimal(row["shares"]) or Decimal(0) for row in sales), Decimal(0))
        exercised = sum(
            (_decimal(row["shares"]) or Decimal(0) for row in exercises), Decimal(0)
        )
        realised = sum(
            (_decimal(row["usd_value"]) or Decimal(0) for row in sales), Decimal(0)
        )
        pairs.append({
            "owner": owner, "date": day,
            "exercised_shares": _plain(exercised, 0),
            "sold_shares": _plain(sold, 0),
            "net_shares": _plain(exercised - sold, 0),
            "cash_realised_usd": _plain(realised, 2),
            "refs": [row["event_ref"] for row in same if row["event_ref"]],
        })
    return pairs


# ---------------------------------------------------------------------------
# did anything anticipate this?
# ---------------------------------------------------------------------------


def anticipation_from_events(
    row: Mapping[str, Any], events: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Form 144 notices by this person that cover this sale.

    A Form 144 is an affiliate telling the SEC they may sell within the next
    three months. S5 records one as an ``ownership_change`` whose
    ``person_type`` begins "planned sale", which is the shape this looks for.
    A 144 filed by the same person inside the window before the trade is the
    cleanest anticipation this system can find, and it is entirely derivable:
    no model reads anything.

    Matched on the person's *name* rather than their CIK because a Form 144 is
    filed without one -- ``person_cik`` is ``None`` on every notice S5 writes.
    Names are folded to lowercase words so "Teter Timothy S." and "TETER
    TIMOTHY S" match; anything looser would match two different people who
    share a surname, which is worse than missing the link.
    """

    day = row["transaction_date"]
    if not day:
        return []
    wanted = _fold_name(row["owner_name"])
    found: list[dict[str, Any]] = []
    for event in events:
        if event.get("kind") != "ownership_change":
            continue
        payload = event.get("payload") or {}
        if payload.get("form") != "144":
            continue
        person_type = payload.get("person_type") or ""
        if not str(person_type).startswith("planned sale"):
            continue
        if wanted and _fold_name(payload.get("reporting_person")) != wanted:
            continue
        filed = payload.get("event_date") or (event.get("occurred_at") or "")[:10]
        if not _within(filed, end=day, days=TRAILING_DAYS):
            continue
        found.append({
            "kind": "form_144",
            "ref": event.get("id"),
            "accession": payload.get("accession"),
            "person": payload.get("reporting_person"),
            "shares_to_be_sold": payload.get("aggregate_shares"),
            "approx_sale_date": payload.get("event_date"),
        })
    return found[:MAX_ANTICIPATION_ROWS]


def _fold_name(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(re.findall(r"[a-z]+", value.lower()))


def anticipation_from_claims(
    connection: sqlite3.Connection, company_ref: str, *, limit: int = 200
) -> dict[str, Any]:
    """Whether our own file already expected a sale, and which claim said so.

    Reads the Claim ledger directly rather than the P12b index projection: the
    index is a projection that a Core may not have built, and the statement
    text -- which is what a keyword search needs -- is in the versions table
    either way.

    Three answers. ``true`` with a ref. ``false``, which means we looked at the
    claims this company has and none of them anticipates a sale. ``unknown``,
    which means there was nothing to look at -- no claim table on this Core, or
    no claims for this company -- and is emphatically not ``false``.
    """

    try:
        rows = connection.execute(
            "SELECT claim_version_id AS id, claim_json FROM claim_versions "
            "WHERE json_extract(claim_json,'$.subject_ref')=? "
            "ORDER BY created_at DESC, claim_version_id DESC LIMIT ?",
            (company_ref, int(limit)),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        return {"answer": "unknown", "reason": "this Core holds no Claim ledger",
                "matches": []}
    if not rows:
        return {"answer": "unknown",
                "reason": f"no Claim has ever been recorded for {company_ref}",
                "matches": []}
    import json as _json

    matches: list[dict[str, Any]] = []
    capital_allocation_claims = 0
    for row in rows:
        try:
            claim = _json.loads(row["claim_json"])
        except (TypeError, ValueError):
            continue
        statement = str(
            claim.get("normalized_statement") or claim.get("statement") or ""
        )
        if claim.get("aspect") == "management_and_capital_allocation":
            capital_allocation_claims += 1
        if not _ANTICIPATION_RE.search(statement):
            continue
        matches.append({
            "kind": "claim",
            "ref": row["id"],
            "aspect": claim.get("aspect"),
            "statement": statement[:300],
        })
        if len(matches) >= MAX_ANTICIPATION_ROWS:
            break
    if matches:
        return {"answer": "true", "reason": None, "matches": matches}
    if capital_allocation_claims < MIN_CAPITAL_ALLOCATION_CLAIMS:
        return {
            "answer": "unknown",
            "reason": (
                "coverage_thin: only "
                f"{capital_allocation_claims} management_and_capital_allocation "
                f"Claims are held; at least {MIN_CAPITAL_ALLOCATION_CLAIMS} are "
                "required before absence is evidence that a sale was unanticipated"
            ),
            "matches": [],
        }
    return {
        "answer": "false",
        "reason": (
            f"searched {len(rows)} Claims for this company; none of them "
            "anticipates a sale by anybody"
        ),
        "matches": [],
    }


# ---------------------------------------------------------------------------
# the context
# ---------------------------------------------------------------------------


def build_insider_context(
    event: Mapping[str, Any],
    *,
    company_events: Sequence[Mapping[str, Any]] = (),
    connection: sqlite3.Connection | None = None,
    trailing_days: int = TRAILING_DAYS,
) -> dict[str, Any]:
    """Everything derivable about one insider transaction, and nothing asserted.

    ``company_events`` is this company's other events -- insider transactions
    for the aggregates, ``ownership_change`` rows for the Form 144 link. Handed
    in rather than queried so the whole computation is a pure function of a
    ledger slice and can be replayed from one.
    """

    if event.get("kind") != "insider_transaction":
        raise InsiderContextError(
            f"insider context is for insider_transaction events, not {event.get('kind')!r}"
        )
    subject = read_row(event)
    day = subject["transaction_date"]
    peers = [
        read_row(other) for other in company_events
        if other.get("kind") == "insider_transaction"
        and other.get("id") != event.get("id")
    ]
    in_window = (
        [row for row in peers if _within(row["transaction_date"], end=day,
                                         days=trailing_days)]
        if day else []
    )
    owner_key = subject["owner_cik"] or subject["owner_name"]
    owner_window = [
        row for row in in_window
        if (row["owner_cik"] or row["owner_name"]) == owner_key
    ]
    # The subject is in its own window: a trailing aggregate that excluded the
    # trade being judged would report a smaller number than the ledger holds
    # and would disagree with the same figure computed tomorrow.
    owner_rows = [subject, *owner_window]
    all_rows = [subject, *in_window]
    cluster = [
        row for row in all_rows
        if _within(row["transaction_date"], end=day, days=CLUSTER_DAYS)
    ] if day else [subject]
    cluster_owners = sorted({
        str(row["owner_name"]) for row in cluster if row["owner_name"]
    })
    anticipation_events = anticipation_from_events(subject, company_events)
    claims = (
        anticipation_from_claims(connection, str(event.get("company_ref")))
        if connection is not None
        else {"answer": "unknown", "reason": "no ledger was handed to this reader",
              "matches": []}
    )
    if anticipation_events:
        anticipated = "true"
        anticipation_reason = None
    else:
        anticipated = claims["answer"]
        anticipation_reason = claims["reason"]
    refs = [ref for ref in (
        subject["event_ref"],
        subject["invocation_ref"],
        *(row["event_ref"] for row in owner_window),
        *(row["ref"] for row in anticipation_events),
        *(row["ref"] for row in claims["matches"]),
    ) if isinstance(ref, str) and ref]
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "insider_transaction",
        "subject": subject,
        "trailing_days": int(trailing_days),
        "as_of": day,
        "owner_trailing": _aggregate(owner_rows),
        "issuer_trailing": _aggregate(all_rows),
        "cluster_days": CLUSTER_DAYS,
        "cluster_owner_count": len(cluster_owners),
        "cluster_owners": cluster_owners,
        "exercise_and_sell": _exercise_and_sell(owner_rows),
        "trailing_rows": [
            {key: row[key] for key in (
                "event_ref", "accession", "transaction_date", "transaction_code",
                "direction", "shares", "price_per_share", "usd_value", "flags",
            )}
            for row in sorted(
                owner_window, key=lambda row: str(row["transaction_date"]), reverse=True
            )[:MAX_TRAILING_ROWS]
        ],
        "anticipated": anticipated,
        "anticipated_reason": anticipation_reason,
        "anticipation": [*anticipation_events, *claims["matches"]],
        # How far back the ledger could actually see. A trailing aggregate over
        # a Core that started recording last Tuesday is not a ninety-day
        # aggregate, and reporting it as one would be the most quietly wrong
        # number on the page.
        "ledger_earliest": min(
            (row["transaction_date"] for row in [subject, *peers]
             if row["transaction_date"]),
            default=None,
        ),
        "considerations": list(CONSIDERATIONS),
        "refs": list(dict.fromkeys(refs)),
    }


def prompt_block(context: Mapping[str, Any]) -> list[str]:
    """The insider context as the lines the judgement prompt prints.

    A table, like everything else in that prompt: each line either carries a
    ref the answer may cite or is a figure whose inputs are two lines above it.
    """

    subject = context["subject"]
    owner = context["owner_trailing"]
    issuer = context["issuer_trailing"]
    lines = [
        "",
        "## Derived insider context (computed from filings this Core holds; no model read this)",
        f"owner: {subject['owner_name']} ({subject['role'] or 'role not stated'})",
        f"trade: {subject['direction']} of {subject['shares']} "
        f"{subject['security_title'] or 'shares'} at {subject['price_per_share']} "
        f"on {subject['transaction_date']} (code {subject['transaction_code']}"
        f" -- {subject['transaction_meaning'] or 'meaning not in the table'})",
        f"value: {subject['usd_value'] or 'no price on the filing'} USD",
        f"size vs this owner's holding after the trade: "
        f"{subject['percent_of_holding_following'] or 'not computable'}% "
        f"(they hold {subject['shares_owned_following'] or 'unknown'} after, "
        f"{subject['direct_or_indirect'] or '?'})",
        f"Rule 10b5-1 plan box on the form: {subject['plan_10b5_1']}",
        f"flags: {', '.join(subject['flags']) if subject['flags'] else 'none'}",
        f"accession: {subject['accession']} (invocation {subject['invocation_ref']})",
    ]
    if subject["plan_10b5_1"] == "unknown":
        lines.append(
            "  the box is 'unknown' because this form does not carry the element, "
            "not because it was left unticked; do not read it as an unplanned sale"
        )
    lines.append(
        f"trailing {context['trailing_days']} days, this owner: "
        f"{owner['disposals']} disposals of {owner['disposal_shares']} shares "
        f"({owner['disposal_usd']} USD), {owner['acquisitions']} acquisitions of "
        f"{owner['acquisition_shares']} shares; {owner['withheld_shares']} shares "
        f"withheld for tax and {owner['no_consideration_shares']} received at no "
        f"consideration are excluded from both"
    )
    lines.append(
        f"trailing {context['trailing_days']} days, every insider of this issuer: "
        f"{issuer['disposals']} disposals of {issuer['disposal_shares']} shares "
        f"({issuer['disposal_usd']} USD), {issuer['acquisitions']} acquisitions of "
        f"{issuer['acquisition_shares']} shares"
    )
    if owner["usd_is_a_floor"] or issuer["usd_is_a_floor"]:
        lines.append(
            "  at least one row in these windows had no price on the filing, so the "
            "dollar totals are floors rather than totals"
        )
    lines.append(
        f"insiders trading within {context['cluster_days']} days of this one: "
        f"{context['cluster_owner_count']} "
        f"({', '.join(context['cluster_owners']) if context['cluster_owners'] else 'none'})"
    )
    if context["ledger_earliest"]:
        lines.append(
            f"the earliest insider transaction this ledger holds for the company is "
            f"{context['ledger_earliest']}; the windows above cannot see past it"
        )
    for pair in context["exercise_and_sell"]:
        lines.append(
            f"exercise-and-sell on {pair['date']}: exercised {pair['exercised_shares']}, "
            f"sold {pair['sold_shares']}, net {pair['net_shares']}, cash realised "
            f"{pair['cash_realised_usd']} USD -- one act, not two"
        )
    for row in context["trailing_rows"]:
        lines.append(
            f"  {row['transaction_date']} {row['direction']} {row['shares']} @ "
            f"{row['price_per_share']} = {row['usd_value']} "
            f"[{','.join(row['flags']) or 'no flags'}] {row['event_ref']}"
        )
    lines.append(f"did anything anticipate this sale: {context['anticipated']}")
    if context["anticipated_reason"]:
        lines.append(f"  ({context['anticipated_reason']})")
    for row in context["anticipation"]:
        if row["kind"] == "form_144":
            lines.append(
                f"  Form 144 {row['accession']} by {row['person']}: up to "
                f"{row['shares_to_be_sold']} shares, on or about "
                f"{row['approx_sale_date']} -- {row['ref']}"
            )
        else:
            lines.append(
                f"  our own Claim [{row.get('aspect')}] {row['ref']}: {row['statement']}"
            )
    lines.append("")
    lines.append("How to weigh this (considerations, not rules; you decide):")
    lines.extend(f"- {line}" for line in context["considerations"])
    return lines


__all__ = [
    "ACQUISITION_CODES",
    "CLUSTER_DAYS",
    "CONSIDERATIONS",
    "DISPOSAL_CODES",
    "MAX_ANTICIPATION_ROWS",
    "MIN_CAPITAL_ALLOCATION_CLAIMS",
    "MAX_TRAILING_ROWS",
    "NO_CONSIDERATION_CODES",
    "SCHEMA_VERSION",
    "TRAILING_DAYS",
    "InsiderContextError",
    "anticipation_from_claims",
    "anticipation_from_events",
    "build_insider_context",
    "percent_of_holding",
    "plan_word",
    "prompt_block",
    "read_row",
    "transaction_flags",
    "usd_value",
]
