"""Deterministic admission-quality checks for mission-drafted statements.

The 2026-09-25 post-deploy sample of freshly admitted Claims found four kinds
of statement the document qualitative rule let through because it looks only
at the statement's own words and at whether the span names the company:

* **SEO statistics compilations.**  amraandelma.com's "TOP 20 AMAZON ADS
  STATISTICS 2026" put twelve AMZN Claims into ws-7d, among them "68,000 new
  sellers in July 2026 per Amazon's Q3 2026 ... Report" -- a report for a
  quarter that had not ended when the page was fetched.  A page whose title
  says it is a list of statistics and whose body is one figure per paragraph
  is a compilation of other people's numbers, with no way to tell a real one
  from an invented one (:func:`statistics_compilation_evidence`).
* **Temporally impossible statements.**  A statement that cites a report for
  a period not yet over at the document's date, or states as a fact the
  outcome of a period not yet over (:func:`temporal_impossibility`).  A
  forecast stated as a forecast -- in the statement *and* in the span -- is
  admitted as one.
* **Relative years.**  "declined to forecast its spending for the following
  year" filed with period 2026 from a page published in August 2026.  A
  relative year is anchored to the document's *published* date and written
  into the statement and the period; when it cannot be anchored the statement
  is held (:func:`anchor_relative_years`).

Every function here is pure: the same text and dates give the same answer,
so an admission can be re-run and a replay over the Ledger says exactly what
the admission path would have done.  All answers except the system-meta one
are holds -- the candidate waits for a person -- never drops.
"""

from __future__ import annotations

import calendar
import re
from datetime import date
from typing import Any

RULE_REF = "claim-admission-quality:v1"

# -- SEO statistics compilations ------------------------------------------

#: A title that says the page is a list of statistics.  Deliberately narrow:
#: "Top 10 IBM Competitors", "Market Share Report" and "Stock Forecast" are
#: not matched; "Amazon Advertising Statistics (2026 Update)" and "TOP 20
#: AMAZON ADS STATISTICS 2026" are.
STATISTICS_TITLE_RE = re.compile(
    r"(?i)\bstatistics\b|\bstats\b|\bfacts\s*(?:&|and)\s*figures\b"
    r"|\bby the numbers\b|统计数据|数据统计"
)
#: A body paragraph: a rendered line at least this long (menus, headings and
#: captions are shorter).
BODY_PARAGRAPH_CHARS = 80
#: How many body paragraphs must carry a figure.  The four compilations in
#: ws-7d carry 23-70; a news article on the same subject 5-15.  Paired with the
#: title, never used alone: an 8-K is dense with figures too.
MIN_NUMERIC_PARAGRAPHS = 8
_QUANTITY_RE = re.compile(
    r"\d+(?:[.,]\d+)?\s?(?:%|percent\b|billion\b|million\b|bn\b|mn\b|trillion\b|x\b)"
    r"|[$€£¥]\s?\d|\b\d{1,3}(?:,\d{3})+\b|\b\d+\.\d+\b"
)


def document_title(text: Any) -> str:
    """The first non-empty line of a rendered page: its ``<title>``."""

    if not isinstance(text, str):
        return ""
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:300]
    return ""


def statistics_compilation_evidence(text: Any, *, title: Any = None) -> dict[str, Any] | None:
    """Why this page is a statistics compilation, or None.

    Both must hold: the title (``title``, else the rendering's first line)
    names a statistics list, and at least ``MIN_NUMERIC_PARAGRAPHS`` body
    paragraphs carry a quantity.  The answer carries the facts it read so a
    hold can say why.
    """

    if not isinstance(text, str) or not text:
        return None
    heading = title if isinstance(title, str) and title.strip() else document_title(text)
    match = STATISTICS_TITLE_RE.search(heading)
    if match is None:
        return None
    paragraphs = [line for line in (raw.strip() for raw in text.splitlines())
                  if len(line) >= BODY_PARAGRAPH_CHARS]
    numeric = sum(1 for line in paragraphs if _QUANTITY_RE.search(line))
    if numeric < MIN_NUMERIC_PARAGRAPHS:
        return None
    return {"title": heading[:160], "title_marker": match.group(0),
            "body_paragraphs": len(paragraphs), "numeric_paragraphs": numeric}


def statistics_compilation_hold(evidence: dict[str, Any]) -> str:
    return ("held for human review: the page is a statistics compilation (title "
            f"'{evidence['title'][:100]}' names a statistics list and "
            f"{evidence['numeric_paragraphs']} of {evidence['body_paragraphs']} body "
            "paragraphs carry a figure); its figures and claims have no original source "
            "the Ledger can check, so neither qualitative nor quantitative statements from "
            "it are admitted automatically")


# -- periods -----------------------------------------------------------------

_MONTHS = {name.lower(): index for index, name in enumerate(calendar.month_name) if name}
_MONTHS.update({name.lower(): index for index, name in enumerate(calendar.month_abbr) if name})
_MONTH_RE = "|".join(sorted((re.escape(name) for name in _MONTHS), key=len, reverse=True))
_YEAR = r"(?:19|20)\d{2}"
#: A calendar period named in text.  Fiscal periods ("Q2 FY2026", "FY26")
#: are left out on purpose: a fiscal quarter's calendar dates depend on the
#: company, and a wrong guess would hold a true statement.
_PERIOD_TOKEN_RE = re.compile(
    rf"(?<![A-Za-z0-9-])(?:"
    rf"(?P<iso>{_YEAR})-(?P<isom>[01]\d)-(?P<isod>[0-3]\d)"
    rf"|Q(?P<q>[1-4])\s?(?:'|’)?\s?(?P<qy>{_YEAR}|\d{{2}})(?![0-9])"
    rf"|(?P<q2>[1-4])Q\s?(?:'|’)?(?P<q2y>{_YEAR}|\d{{2}})(?![0-9])"
    rf"|(?:H(?P<h>[12])|(?P<h2>[12])H)\s?(?:'|’)?(?P<hy>{_YEAR}|\d{{2}})(?![0-9])"
    rf"|(?P<wq>first|second|third|fourth)\s+quarter\s+(?:of\s+)?(?P<wqy>{_YEAR})"
    rf"|(?P<wh>first|second)\s+half\s+(?:of\s+)?(?P<why>{_YEAR})"
    rf"|(?P<mdm>{_MONTH_RE})\.?\s+(?P<mdd>[0-3]?\d)(?:st|nd|rd|th)?,?\s+(?P<mdy>{_YEAR})"
    rf"|(?P<month>{_MONTH_RE})\.?\s+(?P<my>{_YEAR})"
    rf"|(?P<year>{_YEAR})(?![0-9-])"
    rf")",
    re.IGNORECASE,
)
_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4}
_FISCAL_BEFORE_RE = re.compile(r"(?i)\bF(?:Y|iscal(?:\s+year)?)\s*'?$")


def _full_year(value: str) -> int:
    year = int(value)
    return year + 2000 if year < 100 else year


def _quarter(year: int, quarter: int) -> tuple[date, date]:
    first = 3 * quarter - 2
    last = 3 * quarter
    return date(year, first, 1), date(year, last, calendar.monthrange(year, last)[1])


def period_intervals(text: Any) -> list[dict[str, Any]]:
    """Every calendar period ``text`` names: token, start, end, granularity."""

    if not isinstance(text, str):
        return []
    found: list[dict[str, Any]] = []
    for match in _PERIOD_TOKEN_RE.finditer(text):
        if _FISCAL_BEFORE_RE.search(text[max(0, match.start() - 12):match.start()]):
            continue
        groups = match.groupdict()
        try:
            if groups["iso"]:
                day = date(int(groups["iso"]), int(groups["isom"]), int(groups["isod"]))
                start, end, grain = day, day, "day"
            elif groups["q"] or groups["q2"] or groups["wq"]:
                quarter = (int(groups["q"] or groups["q2"]) if not groups["wq"]
                           else _ORDINALS[groups["wq"].lower()])
                year = _full_year(groups["qy"] or groups["q2y"] or groups["wqy"])
                (start, end), grain = _quarter(year, quarter), "quarter"
            elif groups["mdm"]:
                day = date(int(groups["mdy"]), _MONTHS[groups["mdm"].lower().rstrip(".")],
                           int(groups["mdd"]))
                start, end, grain = day, day, "day"
            elif groups["h"] or groups["h2"] or groups["wh"]:
                half = (int(groups["h"] or groups["h2"]) if not groups["wh"]
                        else _ORDINALS[groups["wh"].lower()])
                year = _full_year(groups["hy"] or groups["why"])
                start = date(year, 1 if half == 1 else 7, 1)
                end = date(year, 6, 30) if half == 1 else date(year, 12, 31)
                grain = "half"
            elif groups["month"]:
                month = _MONTHS[groups["month"].lower().rstrip(".")]
                year = int(groups["my"])
                start = date(year, month, 1)
                end = date(year, month, calendar.monthrange(year, month)[1])
                grain = "month"
            else:
                year = int(groups["year"])
                start, end, grain = date(year, 1, 1), date(year, 12, 31), "year"
        except (ValueError, KeyError):
            continue
        found.append({"token": match.group(0), "start": start, "end": end, "grain": grain,
                      "at": match.start(), "stop": match.end()})
    return found


def _day(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


# -- temporal possibility ----------------------------------------------------

#: A statement that reports a forecast rather than an outcome: a modal, a
#: "... is expected to" construction, a forecaster's verb with its object, or
#: the forecast named as the source.  Not the bare nouns: "Target" is a
#: retailer, "May" a month and "its earlier projection" what an outcome beat.
FORECAST_RE = re.compile(
    r"(?i)\b(?:is|are|was|were|be|been|being|remains?|seen)\s+(?:\w+\s+)?"
    r"(?:forecast(?:ed)?|projected|expected|estimated|anticipated|predicted|set|poised"
    r"|on track|likely|slated|scheduled|guided)\s+to\b"
    r"|\b(?:will|would|could|should|might)\b|(?-i:\bmay\b)(?!\s*\d)"
    r"|\b(?:forecasts?|projects?|expects?|estimates?|anticipates?|predicts?|guides?|plans?"
    r"|intends?|aims?)\s+(?:that\s+)?(?:[\w'’-]+\s+){0,4}?(?:to|will|would)\b"
    r"|\b(?:according to|per|in)\s+(?:[\w'’&-]+\s+){0,4}?(?:forecasts?|projections?|estimates?"
    r"|outlook)\b"
    r"|\bforecast\s+to\b|预计|预期|预测|有望|将会|计划"
)
#: A published account of a quarter's or half's results.
_REPORT_AFTER_RE = re.compile(
    r"(?i)^[^.;!?\n]{0,60}?\b(?:report|results|earnings|disclosure|filing|10-q"
    r"|shareholder letter|press release)\b"
)
#: Words just before a period that make it a reference to something ahead.
_AHEAD_RE = re.compile(
    r"(?i)\b(?:ahead of|upcoming|preview(?:ing)?|before|into|await(?:s|ing)?|scheduled|due"
    r"|forthcoming|next|expected|when|until|going into)\b[^.;!?\n]{0,40}$"
)
_FISCAL_NEAR_RE = re.compile(r"(?i)\bfiscal\b|\bFY\s?'?\d{2}|\bF[1-4]Q|\b[1-4]QF")
#: A period qualified as "so far": its outcome to date is a fact.
_TO_DATE_BEFORE_RE = re.compile(
    r"(?i)\b(?:through|thru|so far in|to date in|year[- ]to[- ]date|ytd|as of|since|until|by)"
    r"\s+(?:the\s+)?(?:end\s+of\s+)?$"
)
_OUTCOME_WORDS = (
    r"(?:rose|grew|increased|surpassed|exceeded|reached|hit|totall?ed|accelerated|declined"
    r"|fell|dropped|doubled|tripled|climbed|jumped|soared|outperformed|topped|expanded"
    r"|contracted|shrank)"
)
#: "... share rose in 2026": the outcome verb, then the period it happened in.
_OUTCOME_IN_BEFORE_RE = re.compile(
    rf"(?i)\b{_OUTCOME_WORDS}\b(?:\s+[\w'’-]+){{0,6}}?\s+(?:in|for|during|over)\s+"
    r"(?:the\s+)?(?:full[- ]year\s+)?$"
)
#: "Amazon's 2026 Prime Video ad revenue surpassed ...": the period's metric,
#: then the outcome verb.
_METRIC_OUTCOME_AFTER_RE = re.compile(
    r"(?i)^(?:'s|’s)?\s+(?:[\w-]+\s+){0,4}?(?:revenue|revenues|sales|share|growth|spend"
    r"|spending|earnings|income|margin|margins|bookings|profit|profits)\s+"
    rf"(?:[\w-]+\s+){{0,2}}?{_OUTCOME_WORDS}\b"
)


def _report_references(text: str, as_of: date) -> list[str]:
    """Quarters or halves ``text`` cites a report for that were not over by ``as_of``.

    Only a period the text itself pins to the calendar counts -- it names a
    month inside it ("68,000 new sellers in July 2026 per Amazon's Q3 2026
    ... Report") -- because a fiscal Q3 2026 may have ended months before
    the calendar one.  A text that says "fiscal" or "FY" anywhere is left
    alone for the same reason; so is a period the text looks ahead to
    ("ahead of Q3 2026 results").  A year is never one: a report called
    "2026" is published during 2026.
    """

    if _FISCAL_NEAR_RE.search(text):
        return []
    periods = period_intervals(text)
    months = [item for item in periods if item["grain"] in ("month", "day")]
    cited: list[str] = []
    for item in periods:
        if item["grain"] not in ("quarter", "half") or item["end"] < as_of:
            continue
        if not any(item["start"] <= month["start"] <= item["end"] for month in months):
            continue
        after = text[item["stop"]:item["stop"] + 80]
        before = text[max(0, item["at"] - 60):item["at"]]
        if _REPORT_AFTER_RE.search(after) is None or _AHEAD_RE.search(before) is not None:
            continue
        if FORECAST_RE.search(after[:40]) is not None:
            continue
        cited.append(item["token"])
    return cited


def _unfinished_outcomes(statement: str, as_of: date) -> list[str]:
    """Periods not over by ``as_of`` whose outcome the statement states as fact."""

    if _FISCAL_NEAR_RE.search(statement):
        return []
    found: list[str] = []
    for item in period_intervals(statement):
        if item["grain"] not in ("year", "half", "quarter") or item["end"] < as_of:
            continue
        before = statement[:item["at"]]
        if _TO_DATE_BEFORE_RE.search(before[-30:]):
            continue
        if (_OUTCOME_IN_BEFORE_RE.search(before[-120:]) is not None
                or _METRIC_OUTCOME_AFTER_RE.search(statement[item["stop"]:]) is not None):
            found.append(item["token"])
    return found


def temporal_impossibility(
    *, statement: Any, period: Any, document_date: Any, cited_span: Any = None,
) -> str | None:
    """Why the statement cannot be true as of the document's date, or None.

    ``document_date`` is when the document was published or, failing that,
    retrieved -- either way the latest day its author can have known
    anything.  Two tests:

    1. ``report_not_yet_possible`` -- the statement or its cited span cites
       a report or results for a calendar quarter or half that was not over
       on that day (see :func:`_report_references` for what counts);
    2. ``unfinished_period_outcome_as_fact`` -- the statement says, as a
       fact, how a year, half or quarter turned out ("ad spend share rose in
       2026", "Amazon's 2026 Prime Video ad revenue surpassed ...") while
       that period was not over on that day.  A statement written as a
       forecast is the document's forecast and is admitted as one; so is an
       outcome "so far" ("through 2026", "year to date").

    ``period`` is not read on its own: statements about 2027 are normally
    forward-looking and are not held for naming it.
    """

    as_of = _day(document_date)
    if as_of is None or not isinstance(statement, str):
        return None
    span = cited_span if isinstance(cited_span, str) else ""
    for label, text in (("statement", statement), ("cited span", span)):
        cited = _report_references(text, as_of)
        if cited:
            return ("held for human review: temporally impossible -- the "
                    f"{label} cites a report for {', '.join(cited[:3])}, a period not "
                    f"over by the document's date {as_of.isoformat()}, so that report "
                    "cannot exist yet (report_not_yet_possible)")
    if FORECAST_RE.search(statement) is not None:
        return None
    unfinished = _unfinished_outcomes(statement, as_of)
    if unfinished:
        return ("held for human review: temporally impossible -- the statement says "
                f"how {unfinished[0]} turned out, but that period was not over by the "
                f"document's date {as_of.isoformat()} (unfinished_period_outcome_as_fact); "
                "a forecast is admitted only as a forecast")
    return None


# -- relative years ----------------------------------------------------------

#: The relative years that point away from the document's date.  "This
#: year" and "the prior year" are left out: the first is almost always the
#: period the Claim already names, the second almost always a comparison base
#: ("up versus the prior year") relative to that period, not to the document.
_EN_RELATIVE_YEAR_RE = re.compile(
    r"(?i)\b(?:the\s+|its\s+|their\s+)?(?P<word>following|next|coming|last)"
    r"\s+(?P<fiscal>fiscal\s+|calendar\s+)?year(?:'s|’s)?\b(?![-‑](?:over|on|ago|end))"
)
#: A relative year used as a comparison base: skipped.
_COMPARISON_BEFORE_RE = re.compile(
    r"(?i)\b(?:versus|vs\.?|compared (?:with|to)|from|over|than|relative to|against|above"
    r"|below|in line with|similar to|lapping|like)\s+(?:both\s+)?(?:the\s+|its\s+)?$"
)
#: "次年" is "the year after" some other year as often as not, and "来年"
#: sits inside "未来年份"; both are left out.
_ZH_RELATIVE_YEAR_RE = re.compile(r"明年|下一年度?|去年|上一年度?")
_OFFSETS = {
    "following": 1, "next": 1, "coming": 1, "last": -1,
    "明年": 1, "下一年": 1, "下一年度": 1,
    "去年": -1, "上一年": -1, "上一年度": -1,
}
_FISCAL_RE = re.compile(r"(?i)\bfiscal\b|\bFY\s?'?\d{0,4}\b|财年|财政年度")
_EXPLICIT_YEAR_RE = re.compile(r"(?<![0-9])((?:19|20)\d{2})(?![0-9])")


def relative_year_mentions(statement: Any) -> list[dict[str, Any]]:
    """The relative years a statement uses, each with its offset.

    A comparison base ("up versus last year") is left out: there the year is
    relative to the period being compared, not to the document.
    """

    if not isinstance(statement, str):
        return []
    found: list[dict[str, Any]] = []
    for match in _EN_RELATIVE_YEAR_RE.finditer(statement):
        if _COMPARISON_BEFORE_RE.search(statement[max(0, match.start() - 30):match.start()]):
            continue
        found.append({"phrase": match.group(0).strip(), "at": match.end(),
                      "offset": _OFFSETS[match.group("word").lower()],
                      "fiscal": bool(match.group("fiscal"))
                      and match.group("fiscal").strip().lower() == "fiscal"})
    for match in _ZH_RELATIVE_YEAR_RE.finditer(statement):
        if statement[max(0, match.start() - 3):match.start()].endswith(("较", "与", "比", "同比")):
            continue
        found.append({"phrase": match.group(0), "at": match.end(),
                      "offset": _OFFSETS[match.group(0)], "fiscal": False})
    return found


def anchor_relative_years(
    *, statement: Any, period: Any, document_date: Any, date_basis: Any,
) -> dict[str, Any]:
    """Write a relative year as the absolute year it means, or say why not.

    Returns ``{"statement", "period", "hold", "anchored"}``.  With no
    relative year the inputs come back unchanged.  Otherwise the year is
    anchored to the document's *published* date -- a retrieval date is not
    when the author wrote "next year" -- and:

    * every relative phrase gets its year in brackets ("the following year
      (2027)"), and the period becomes that year when it names none or
      another;
    * it is held when the date is only a retrieval date or unknown, when the
      phrases mean different years, or when the statement also names another
      explicit year (then which fact the period belongs to is a judgment).
    """

    result = {"statement": statement, "period": period, "hold": None, "anchored": None}
    mentions = relative_year_mentions(statement)
    if not mentions:
        return result
    phrases = ", ".join(sorted({item["phrase"] for item in mentions}))
    as_of = _day(document_date)
    if as_of is None or date_basis != "published":
        result["hold"] = ("held for human review: the statement uses a relative year "
                          f"({phrases}) and the document has no published date to anchor "
                          "it to" + ("" if as_of is None else
                                     f" (only its retrieval date {as_of.isoformat()})"))
        return result
    if any(item["fiscal"] for item in mentions) or _FISCAL_RE.search(statement) or (
            isinstance(period, str) and _FISCAL_RE.search(period)):
        result["hold"] = ("held for human review: the statement uses a relative year "
                          f"({phrases}) in a fiscal context; a fiscal year cannot be "
                          "anchored to a calendar year without the company's fiscal calendar")
        return result
    years = sorted({as_of.year + item["offset"] for item in mentions})
    if len(years) != 1:
        result["hold"] = ("held for human review: the statement's relative years "
                          f"({phrases}) mean different years")
        return result
    year = years[0]
    explicit = {int(value) for value in _EXPLICIT_YEAR_RE.findall(statement)}
    if explicit - {year}:
        result["hold"] = ("held for human review: the statement uses a relative year "
                          f"({phrases}, {year} from the document date {as_of.isoformat()}) "
                          f"and also names {sorted(explicit - {year})}; which the period "
                          "belongs to is a judgment")
        return result
    text = statement
    for item in sorted(mentions, key=lambda value: value["at"], reverse=True):
        if text[item["at"]:item["at"] + 8].lstrip().startswith(f"({year})"):
            continue
        text = f"{text[:item['at']]} ({year}){text[item['at']:]}"
    result["statement"] = text
    period_text = period if isinstance(period, str) else ""
    period_years = {int(value) for value in _EXPLICIT_YEAR_RE.findall(period_text)}
    if year not in period_years:
        result["period"] = str(year)
    result["anchored"] = {"year": year, "phrases": phrases,
                          "document_date": as_of.isoformat()}
    return result


__all__ = [
    "FORECAST_RE",
    "MIN_NUMERIC_PARAGRAPHS",
    "RULE_REF",
    "STATISTICS_TITLE_RE",
    "anchor_relative_years",
    "document_title",
    "period_intervals",
    "relative_year_mentions",
    "statistics_compilation_evidence",
    "statistics_compilation_hold",
    "temporal_impossibility",
]
