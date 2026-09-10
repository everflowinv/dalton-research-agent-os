"""P11b: read the first page of a broker note for the two things it always says.

A sell-side note is not a document you extract from; it is a document with a
masthead. The rating and the price target are on page one, in one of about
three layouts, in the same three positions they have been in for thirty years:

    Price: $8.82 (06/10/2026)
    Price Target: $11.00
    HOLD (2)

    Remain EW, PT to $97.

    Maintain our OP rating and $270 PT.

So this pass is deterministic, and that is a decision rather than a shortcut.
A model asked for a price target from a page like the first one will return
$11.00 and be right; asked for one from a 125-page industry note covering
thirty companies it will return *a* price target, correctly read, belonging to
somebody else. The failure that matters here is not misreading digits, it is
misattributing them, and no amount of model quality fixes that -- only a rule
about which documents may be read at all does.

Hence the refusals, which are most of this module:

``multi_company_report``
    The note names more than one company. Live, that is 18 of the 68 sell-side
    documents naming a covered company -- the payments-and-IT-services
    quarterly recaps, thirty issuers to a note, each with its own ``PT:`` line
    a page apart. Reading page one of one of those and filing the answer
    against Accenture is exactly the Haier-transcript failure
    ``document_figure_grade`` records, and it is refused wholesale rather than
    parsed cleverly.

``ambiguous_target``
    Page one yields two different live targets. Morgan Stanley's "What's
    Changed" block prints the old target above the new one, and a label-first
    match takes the old one silently. Two live values means the page is not
    saying one thing, and a refusal is the only answer that cannot be quietly
    wrong.

``label_does_not_name_a_line``
    The note wrote "$270 PT" and nothing longer. ``document_numeric_claim``
    requires the reported label to name a line rather than restate the amount,
    and "PT" is two letters. This is a real yield cost -- it is a quarter of
    the live hits -- and it is accepted rather than worked around: the way to
    get past it would be to hand the verifier a label the document did not
    print, which is the one thing the verifier exists to stop.

What is *not* deterministic is the estimates table. Guggenheim's EPAM note
prints a quarter-by-quarter revenue and EPS grid with an ``E`` suffix on the
estimated cells and a ``Prior`` row underneath holding the superseded numbers;
column alignment is the only thing that says which year a cell belongs to, and
it survives a PDF-to-text conversion badly. That is what the model call below is
for, and it is built here as a request/prompt/verify surface with a frozen task
hash so it can be wired without redesign -- it is deliberately not wired into
the lane in v1.0, because the deterministic pass earns its keep today and a
model call that is 80% right about which column it read would not.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from .document_numeric_claim import (
    NumericCandidateError,
    verify_numeric_candidate,
)
from .store import content_hash
from .street_estimate import (
    BASIS,
    EPS_METRIC,
    RATING_SCALES,
    REVENUE_METRIC,
    SELL_SIDE_SPEC_REFS,
    TARGET_METRIC,
    StreetEstimateValidationError,
    broker_slug,
    normalise_rating,
    rating_scales_for,
)

SCHEMA_VERSION = "0.1"
TASK_REF = "task:street-estimate-extraction:0.1"
MODEL_PURPOSE = "street_estimate"
EXTRACTOR_REF = "extractor:street-estimate-page-one:0.1"
# How much of a note counts as "page one".
#
# Not a page: the acquired text has no pages. Four thousand characters is about
# a page and a half of a research note and comfortably contains every masthead
# in the live spool; the TD layout puts its target 180 characters in and the
# Morgan Stanley one puts it at 1,500. Reading further does not find more
# targets, it finds the *other* numbers a note quotes -- comparable multiples,
# peer targets, the last four quarters -- which is how a page-one rule turns
# into a wrong-number machine.
PAGE_ONE_CHARS = 4000
# How far from the target a rating word may sit and still be the same
# statement.
RATING_WINDOW_CHARS = 400
# How far a two-letter rating abbreviation may sit from a word that makes it a
# rating. "UP" is Underperform at RBC and also the most common two-letter
# string in English; "Maintain our OP rating" is a rating and "shares are up
# 2%" is not.
ABBREVIATION_CUE_CHARS = 40
MAX_ESTIMATE_ROWS = 8

REFUSALS: tuple[str, ...] = (
    "not_sell_side",
    "multi_company_report",
    "subject_not_named",
    "broker_unknown",
    "no_target_price",
    "ambiguous_target",
    "label_does_not_name_a_line",
    "no_currency",
    "digits_not_in_citation",
)

_SYMBOL_CURRENCY = {"$": "USD", "US$": "USD", "€": "EUR", "£": "GBP"}
_SYM = r"(US\$|\$|€|£)"
_NUM = r"([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?)"
_LABEL = r"(Price Target|Target Price|PT)"
# Ordered. The labelled masthead first, because it is the layout that says
# unambiguously "this is the target"; the prose forms after it.
_TARGET_PATTERNS: tuple[tuple[str, Any, tuple[int, int, int]], ...] = (
    # "Price Target: $11.00" at the start of its own line.
    ("labelled", re.compile(
        r"(?:^|\n)[ \t]*" + _LABEL + r"[ \t]*(?:\([^)\n]{0,20}\))?[ \t]*[:：\-–]?[ \t]*"
        + _SYM + r"[ \t]*" + _NUM, re.I), (1, 2, 3)),
    # "price target of $120.00", "PT to $97"
    ("inline_forward", re.compile(
        _LABEL + r"[^.\n$€£]{0,30}?" + _SYM + r"[ \t]?" + _NUM, re.I), (1, 2, 3)),
    # "$270 PT"
    ("inline_reverse", re.compile(
        _SYM + r"[ \t]?" + _NUM + r"[ \t]*" + _LABEL, re.I), (3, 1, 2)),
)
# A target introduced by one of these is the one being replaced.
_SUPERSEDED_RE = re.compile(r"prior|previous|\bfrom\b|\bold\b|\bwas\b", re.I)
# "Price Target (Dec-27):$120.00" -- the horizon the house is underwriting.
_HORIZON_RE = re.compile(
    r"(?:price target|target price|PT)[ \t]*\(([A-Za-z]{3}[-/ ]?\d{2,4})\)", re.I)
_CUE_RE = re.compile(
    r"maintain|reiterat|remain|we rate|our rating|rating|upgrad|downgrad|"
    r"initiat|reaffirm", re.I)
_ABBREVIATIONS = frozenset({"ow", "ew", "uw", "op", "sp", "mp", "pp", "up"})


class StreetEstimateExtractionError(ValueError):
    """The extraction request is malformed."""


def worthy_spec(spec_ref: Any) -> bool:
    """Whether a document of this kind is a broker note at all."""

    return spec_ref in SELL_SIDE_SPEC_REFS


def _page_one(quotes: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The quotes that make up the first page, in order.

    A window rather than a concatenation: a figure is verified against the
    exact quote it was found in, so the quote has to stay a unit. A target
    split across a quote boundary is lost, which costs a little and keeps the
    citation exact.
    """

    page: list[dict[str, Any]] = []
    used = 0
    for quote in quotes:
        if not isinstance(quote, Mapping):
            raise StreetEstimateExtractionError("each quote must be an object")
        raw = quote.get("raw_text")
        quote_id = quote.get("quote_id")
        if not isinstance(raw, str) or not isinstance(quote_id, str) or not quote_id:
            raise StreetEstimateExtractionError("a quote needs a quote_id and raw text")
        if used >= PAGE_ONE_CHARS:
            break
        page.append({"quote_id": quote_id, "raw_text": raw})
        used += len(raw)
    return page


def find_broker(
    sources: Any, page: Sequence[Mapping[str, Any]]
) -> tuple[str | None, str | None, str | None]:
    """Which house published this, and how we know.

    The library's own ``sources`` metadata wins over the body text. It is part
    of the acquired document record, it is what the acquisition was recorded
    under, and live it identifies the house for every note that has a target --
    while the body text of an RBC note may not print "RBC" on page one at all.
    """

    if isinstance(sources, Sequence) and not isinstance(sources, (str, bytes)):
        for name in sources:
            slug = broker_slug(name)
            if slug is not None:
                return slug, str(name), "document_metadata"
    for quote in page:
        slug = broker_slug(quote["raw_text"])
        if slug is not None:
            return slug, slug, "page_text"
    return None, None, None


def find_targets(page: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Every live price target on page one, with the quote each came from.

    Superseded targets -- the ones a note prints under "Prior" or "From" -- are
    collected too, and marked, because whether a page is saying one thing is
    decided by what is left after they are removed.
    """

    found: list[dict[str, Any]] = []
    for quote in page:
        text = quote["raw_text"]
        for kind, pattern, (label_group, symbol_group, number_group) in _TARGET_PATTERNS:
            for match in pattern.finditer(text):
                label = match.group(label_group)
                symbol = match.group(symbol_group)
                number = match.group(number_group).replace(",", "")
                # Only what precedes the match *on its own line*. A window of
                # fixed width reads back over the line before, so a page that
                # prints the prior target above the new one marks both -- and
                # then has no live target at all.
                lead = text[max(0, match.start() - 60):match.start()].rsplit("\n", 1)[-1]
                found.append({
                    "quote_id": quote["quote_id"],
                    "pattern": kind,
                    "label": label,
                    "value": number,
                    "currency": _SYMBOL_CURRENCY.get(
                        "US$" if symbol.upper() == "US$" else symbol
                    ),
                    "superseded": _SUPERSEDED_RE.search(lead) is not None,
                    "start": match.start(),
                })
            if found and kind == "labelled":
                # A masthead beats prose. Looking for prose forms as well would
                # find the same number a second time, and every extra match is
                # another chance to disagree with itself.
                break
    return found


def find_rating(
    page: Sequence[Mapping[str, Any]], *, broker: str
) -> dict[str, Any] | None:
    """The house's own rating word, if page one prints one it is allowed to use.

    Only rungs of a scale this house actually runs are looked for. A note that
    says "Overweight" about a peer does not make an RBC note Overweight, and
    RBC has no such rung; searching for words the house cannot say is how one
    broker's view ends up filed under another's name.
    """

    scales = rating_scales_for(broker)
    if not scales:
        return None
    words: set[str] = set()
    for scale in scales:
        words |= set(RATING_SCALES[scale])
    long_words = sorted((w for w in words if w not in _ABBREVIATIONS),
                        key=len, reverse=True)
    long_re = re.compile(
        r"\b(" + "|".join(re.escape(w).replace(r"\ ", r"[- ]?") for w in long_words)
        + r")\b", re.I)
    abbreviations = sorted(words & _ABBREVIATIONS)
    abbreviation_re = (
        re.compile(r"\b(" + "|".join(w.upper() for w in abbreviations) + r")\b")
        if abbreviations else None
    )
    for quote in page:
        text = quote["raw_text"]
        match = long_re.search(text)
        if match is not None:
            return _rating_wire(match.group(1), broker, quote["quote_id"])
        if abbreviation_re is None:
            continue
        for match in abbreviation_re.finditer(text):
            around = text[
                max(0, match.start() - ABBREVIATION_CUE_CHARS):
                match.end() + ABBREVIATION_CUE_CHARS
            ]
            if _CUE_RE.search(around):
                return _rating_wire(match.group(1), broker, quote["quote_id"])
    return None


def _rating_wire(word: str, broker: str, quote_id: str) -> dict[str, Any] | None:
    try:
        rating = normalise_rating(word, broker=broker)
    except StreetEstimateValidationError:
        return None
    return {**rating, "quote_id": quote_id}


def find_horizon(page: Sequence[Mapping[str, Any]]) -> str | None:
    for quote in page:
        match = _HORIZON_RE.search(quote["raw_text"])
        if match is not None:
            return match.group(1)
    return None


def _names_subject(page: Sequence[Mapping[str, Any]], names: Sequence[str]) -> str | None:
    """The name this document calls the company, if it calls it anything.

    The same rule ``document_figure_grade.figure_recordable`` applies to every
    other document: a note that never names the company is not about it.
    """

    for quote in page:
        lowered = quote["raw_text"].lower()
        for name in names:
            if isinstance(name, str) and name.strip() and name.lower() in lowered:
                return name
    return None


def extract(context: Mapping[str, Any]) -> dict[str, Any]:
    """One broker note in, one street estimate or one named refusal out.

    Never a partial answer with a guess in it: the estimate is returned whole
    or the reason it was not is.
    """

    if not isinstance(context, Mapping):
        raise StreetEstimateExtractionError("context must be an object")
    for field in (
        "company_ref", "document_ref", "spec_ref", "source_manifest_hash",
        "published_on", "quotes",
    ):
        if field not in context:
            raise StreetEstimateExtractionError(f"context is missing {field}")
    document_ref = str(context["document_ref"])

    def refuse(reason: str, **detail: Any) -> dict[str, Any]:
        if reason not in REFUSALS:  # pragma: no cover - constant
            raise StreetEstimateExtractionError(f"unknown refusal {reason!r}")
        return {
            "schema_version": SCHEMA_VERSION, "estimate": None,
            "document_ref": document_ref, "refusal": reason,
            "extractor_ref": EXTRACTOR_REF, "extraction_method": "deterministic",
            **detail,
        }

    if not worthy_spec(context["spec_ref"]):
        return refuse("not_sell_side", detail=str(context["spec_ref"]))
    companies = context.get("document_companies") or []
    if isinstance(companies, Sequence) and not isinstance(companies, (str, bytes)):
        if len(companies) > 1:
            return refuse("multi_company_report", company_count=len(companies))
    page = _page_one(context["quotes"])
    if not page:
        return refuse("no_target_price", detail="the document has no text")
    names = list(context.get("subject_names") or [])
    subject = _names_subject(page, names)
    if subject is None:
        return refuse("subject_not_named")
    broker, broker_as_named, broker_basis = find_broker(context.get("sources"), page)
    if broker is None:
        return refuse("broker_unknown")

    targets = [row for row in find_targets(page) if not row["superseded"]]
    if not targets:
        return refuse("no_target_price")
    values = {row["value"] for row in targets}
    if len(values) > 1:
        return refuse("ambiguous_target", values=sorted(values))
    chosen = targets[0]
    if chosen["currency"] is None:
        return refuse("no_currency")
    horizon = find_horizon(page)
    published_on = str(context["published_on"])
    quotes = {row["quote_id"]: row["raw_text"] for row in page}
    period = (
        f"target horizon {horizon}, set {published_on}" if horizon
        else f"12 months from {published_on}"
    )
    candidate = {
        "quote_id": chosen["quote_id"],
        "metric_ref": TARGET_METRIC,
        "subject_as_named": subject,
        "as_reported_label": chosen["label"],
        "value": chosen["value"],
        "unit": "currency",
        "currency": chosen["currency"],
        "period": period,
        "basis": BASIS,
        "scale": None,
    }
    try:
        figure = verify_numeric_candidate(candidate, quotes)
    except NumericCandidateError as exc:
        message = str(exc)
        if "does not contain" in message:
            return refuse("digits_not_in_citation", detail=message)
        # The note wrote "PT" and nothing longer. See the module docstring.
        return refuse("label_does_not_name_a_line", detail=message,
                      label=chosen["label"])
    rating = find_rating(page, broker=broker)
    estimate = {
        "company_ref": str(context["company_ref"]),
        "document_ref": document_ref,
        "spec_ref": str(context["spec_ref"]),
        "source_manifest_hash": str(context["source_manifest_hash"]),
        "broker": broker,
        "broker_as_named": broker_as_named,
        "broker_basis": broker_basis,
        "analysts": [str(name) for name in (context.get("analysts") or [])],
        "published_on": published_on,
        "subject_as_named": subject,
        "rating": rating,
        "target_price": {
            "value": chosen["value"],
            "currency": chosen["currency"],
            "horizon": horizon,
            "quote_id": chosen["quote_id"],
        },
        "figures": [figure],
        "extraction_method": "deterministic",
    }
    return {
        "schema_version": SCHEMA_VERSION, "estimate": estimate,
        "document_ref": document_ref, "refusal": None,
        "extractor_ref": EXTRACTOR_REF, "extraction_method": "deterministic",
        "pattern": chosen["pattern"], "rating_found": rating is not None,
    }


# -- the model surface, for the estimates table only ------------------------

_ESTIMATE_UNITS = ("currency", "count")

OUTPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "BrokerEstimateTableV0.1",
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "estimates"],
    "properties": {
        "schema_version": {"const": "0.1"},
        "estimates": {
            "type": "array",
            "maxItems": MAX_ESTIMATE_ROWS,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "quote_id", "metric_ref", "as_reported_label", "period",
                    "value", "unit", "currency", "scale", "superseded",
                ],
                "properties": {
                    "quote_id": {"type": "string", "minLength": 1, "maxLength": 100},
                    "metric_ref": {"enum": [EPS_METRIC, REVENUE_METRIC]},
                    "as_reported_label": {"type": "string", "minLength": 1,
                                          "maxLength": 200},
                    "period": {"type": "string", "minLength": 1, "maxLength": 200},
                    "value": {"type": "string", "minLength": 1, "maxLength": 40},
                    "unit": {"enum": list(_ESTIMATE_UNITS)},
                    "currency": {"type": ["string", "null"]},
                    "scale": {"type": ["string", "null"]},
                    # The "Prior" row. Asked for explicitly rather than left to
                    # be inferred, because a superseded estimate looks exactly
                    # like a current one and is the trap this table sets.
                    "superseded": {"type": "boolean"},
                },
            },
        },
    },
}
TASK_HASH = content_hash({
    "task": TASK_REF,
    "output": OUTPUT_SCHEMA,
    "authority": "the_brokers_own_estimate_never_the_companys_figure",
    "page_one_chars": PAGE_ONE_CHARS,
})


def build_request(context: Mapping[str, Any]) -> dict[str, Any]:
    """What the model is shown: this note's quotes and nothing else."""

    quotes = context.get("quotes")
    if not isinstance(quotes, Sequence) or not quotes:
        raise StreetEstimateExtractionError("context must carry at least one quote")
    return {
        "schema_version": SCHEMA_VERSION,
        "task_ref": TASK_REF,
        "task_hash": TASK_HASH,
        "purpose": MODEL_PURPOSE,
        "company_ref": context["company_ref"],
        "document_ref": context["document_ref"],
        "broker": context.get("broker"),
        "quotes": [
            {"quote_id": quote["quote_id"], "raw_text": quote["raw_text"]}
            for quote in quotes
        ],
    }


def build_prompt(request: Mapping[str, Any]) -> str:
    return (
        "You are reading the estimates table of one broker's research note. "
        "Report the broker's own forward revenue and earnings-per-share estimates: "
        "the numbers this broker is forecasting, never the company's reported results "
        "and never a peer's. "
        "Every figure must name the fiscal period it belongs to as the table labels it "
        "(a four-digit year, or a quarter marker such as 3Q26 or FY2027); a figure whose "
        "column you cannot read is one to leave out. "
        "A row labelled Prior, Previous or Old holds a superseded estimate: report it "
        "with superseded=true rather than omitting it, so the revision is visible. "
        "Quote the label the table itself prints for each line. "
        "Report nothing you cannot point at: an empty list is a correct answer. "
        f"Answer as JSON matching this schema exactly: {OUTPUT_SCHEMA}"
    )


def verify_estimate_table(
    response: Mapping[str, Any],
    *,
    quotes: Mapping[str, str],
    subject_as_named: str,
) -> list[dict[str, Any]]:
    """Verify the model's estimates table, or refuse the whole of it.

    Whole, not row by row, and that is the difference between this pass and the
    ordinary numeric one. There, a model that gets three figures right and
    invents a fourth should keep the three. Here the failure mode is reading
    the wrong *column* -- every number real, every one filed against the wrong
    year -- and a response with one row that does not check out is evidence
    about the reading, not about the row.
    """

    if not isinstance(response, Mapping) or response.get("schema_version") != "0.1":
        raise StreetEstimateExtractionError("the response is not this task's schema")
    rows = response.get("estimates")
    if not isinstance(rows, list):
        raise StreetEstimateExtractionError("estimates must be a list")
    if len(rows) > MAX_ESTIMATE_ROWS:
        raise StreetEstimateExtractionError(
            f"a window yields at most {MAX_ESTIMATE_ROWS} estimates"
        )
    verified: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping) or row.get("superseded") not in (True, False):
            raise StreetEstimateExtractionError(
                "each estimate says whether it is superseded"
            )
        if row.get("superseded"):
            continue
        candidate = {
            "quote_id": row.get("quote_id"),
            "metric_ref": row.get("metric_ref"),
            "subject_as_named": subject_as_named,
            "as_reported_label": row.get("as_reported_label"),
            "value": row.get("value"),
            "unit": row.get("unit"),
            "currency": row.get("currency"),
            "period": row.get("period"),
            "basis": BASIS,
            "scale": row.get("scale"),
        }
        try:
            verified.append(verify_numeric_candidate(candidate, quotes))
        except NumericCandidateError as exc:
            raise StreetEstimateExtractionError(
                f"the estimates table is refused whole: {exc}"
            ) from exc
    return verified


__all__ = [
    "ABBREVIATION_CUE_CHARS",
    "EXTRACTOR_REF",
    "MAX_ESTIMATE_ROWS",
    "MODEL_PURPOSE",
    "OUTPUT_SCHEMA",
    "PAGE_ONE_CHARS",
    "RATING_WINDOW_CHARS",
    "REFUSALS",
    "SCHEMA_VERSION",
    "TASK_HASH",
    "TASK_REF",
    "StreetEstimateExtractionError",
    "build_prompt",
    "build_request",
    "extract",
    "find_broker",
    "find_horizon",
    "find_rating",
    "find_targets",
    "verify_estimate_table",
    "worthy_spec",
]
