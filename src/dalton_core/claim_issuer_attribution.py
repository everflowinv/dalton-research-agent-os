"""Whose financial table is this?  Issuer / period / actual-vs-estimate attribution.

P0 (research artifact quality review, 2026-10-09).  ws-7d held 22 Claims filed
under MSFT whose cited passages are the financial-model pages of a Morgan
Stanley note on *Oracle* (``ORACLE_20260123_0501.pdf``, compiled into the
company wiki with ``related_companies: [ORCL, MSFT]``).  Every quote was exact
and every hash verified; the subject checks passed because the wiki head names
MSFT, so the document counted as "MSFT's own".  The tables themselves
(license/support, IaaS/PaaS, FY25 revenue of about $57.4B) are Oracle's, and
"latest-period free cash flow is negative" was Oracle's *estimate* columns read
as Microsoft's actuals.

The question this module answers deterministically, for a Claim about
financials cited from a document:

* **issuer** -- which issuer the cited table (or passage) belongs to.  Read from
  the passage's own local context: the head of the page it sits on (where a
  table's title and column headers are), and ``LOCAL_CHARS`` either side of the
  span.  When nobody is named there, the document's *primary* issuer answers
  (a wiki front matter's ``分类``, the source file name, the title, or the
  density of mentions) -- a table in an Oracle note that names nobody is
  Oracle's.  A head that lists several "related companies" is not a primary
  issuer, which is the hole the MSFT Claims came through;
* **period / actual vs estimate** -- whether the local context's columns are
  estimates (``FY26E``, ``2/27E``, ``MSe``), and whether the statement reads
  them as reported results.  Recorded as evidence; it never retires on its own.

``attribute`` returns a verdict with its ``attribution_evidence``:

* ``not_applicable`` -- not a statement about financials;
* ``consistent`` -- the subject's own document, or the subject is named in the
  passage's local context;
* ``misattributed`` -- the document's primary issuer is another company and
  the passage's local context never names the subject (the table issuer is
  whoever the passage names, else the document's primary issuer);
* ``ambiguous`` -- no primary issuer (a digest, an industry note) and the
  subject not named locally.  Admission asks the independent support check (a
  cheap model pass) instead of guessing; the audit never retires on it.

Admission holds a ``misattributed`` statement (``document_extraction``); the
claim review patrol retires already-admitted ones under the reason
``issuer_misattribution`` (``claim_retirement``), which re-runs this function
on the exact original before it writes.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

RULE_REF = "claim-attribution:issuer-table-context:v1"
DETECTOR_REF = "claim-detector:issuer-misattribution:v1"
#: How far either side of the cited span counts as the passage's own context.
LOCAL_CHARS = 600
#: How much of the page a span sits on is its table title and column headers.
PAGE_HEAD_CHARS = 700
#: A page marker further back than this is not the span's page any more.
MAX_PAGE_BACK = 40_000
#: How much of a document its front matter / cover occupies.
FRONT_CHARS = 1_500
#: A primary issuer by density: enough mentions and most of every issuer's.
DENSITY_MIN_MENTIONS = 15
DENSITY_MIN_SHARE = 0.7

#: Issuers a sell-side note on a covered company routinely carries tables of,
#: that are not themselves covered.  Only used to *recognise* another issuer;
#: covered companies' names come from the mission and
#: ``document_subject.COMPANY_NAMES``.
EXTRA_ISSUER_NAMES: Mapping[str, tuple[str, ...]] = {
    "ORCL": ("Oracle", "Oracle Corporation", "甲骨文"),
    "NVDA": ("NVIDIA", "Nvidia", "英伟达"),
    "CRWV": ("CoreWeave",),
    "AAPL": ("Apple",),
    "CRM": ("Salesforce",),
    "SAP": ("SAP SE",),
    "ADBE": ("Adobe",),
    "SNOW": ("Snowflake",),
    "AVGO": ("Broadcom",),
    "AMD": ("Advanced Micro Devices",),
    "INTC": ("Intel",),
    "TSLA": ("Tesla",),
    "INFY": ("Infosys",),
    "WIT": ("Wipro",),
    "TCS": ("Tata Consultancy",),
    "NOW": ("ServiceNow",),
    "WDAY": ("Workday",),
    "CSCO": ("Cisco",),
    "DELL": ("Dell Technologies",),
    "SMCI": ("Super Micro",),
}

AMBIGUOUS_TICKERS = frozenset({"NOW", "CRM", "WIT"})

_FINANCIAL_RE = re.compile(
    r"(?i)\b(revenues?|sales|income|earnings|eps|ebitda?|margins?|cash[ -]?flows?|fcf|ocf|"
    r"capex|capital expenditures?|capital spending|depreciation|amorti[sz]ation|debt|leases?|"
    r"liabilit(y|ies)|balance sheet|repurchases?|buybacks?|dividends?|pp&e|"
    r"property and equipment|financing|operating expenses?|opex|cost of revenues?|"
    r"gross profit|special charges|restructuring|tax rate|share count|backlog|rpo|"
    r"bookings|profit|losses?|net cash|cash balance|issuance)\b"
    r"|收入|营收|利润|现金流|资本开支|折旧|负债|回购|毛利|营业|净利|每股|分红|债务|租赁"
)
_PAGE_MARKER_RE = re.compile(r"(?m)^-{2,}\s*Page\s+\d+\s*-{2,}[ \t]*$|\f")
_ESTIMATE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:FY)?(?:20)?\d{2}(?:-?Q[1-4])?\s?E(?![A-Za-z0-9])"   # FY26E, 2027E, 26E
    r"|(?<![0-9])\d{1,2}/\d{2}E(?![A-Za-z0-9])"                               # 2/27E
    r"|(?<![A-Za-z0-9])20\d{2}e(?![A-Za-z0-9])"                               # 2026e
    r"|(?<![A-Za-z])MSe(?![A-Za-z])|(?i:\b(?:estimates?|forecasts?|projected|base case)\b)"
)
_STATEMENT_ESTIMATE_RE = re.compile(
    r"(?i)\b(forecasts?|forecasted|estimates?|estimated|projects?|projected|projections?|"
    r"expects?|expected|models?|modell?ed|will|would|scenario|outlook|guidance|forward|"
    r"future|horizon|target)"
    r"|预测|预计|预期|估计|模型|情景"
)
_FRONT_CATEGORY_RE = re.compile(r"\*{0,2}分类\*{0,2}\s*[:：]\s*([A-Z][A-Z.]{0,5})\s*[（(]公司[)）]")
_FRONT_FILE_RE = re.compile(r"\*{0,2}来源文件\*{0,2}\s*[:：]\s*([A-Za-z][A-Za-z&-]{2,40})_\d{6,8}")
_WORD_CACHE: dict[str, re.Pattern[str]] = {}
_CJK_RE = re.compile(r"[㐀-鿿]")


def _pattern(needle: str) -> re.Pattern[str]:
    found = _WORD_CACHE.get(needle)
    if found is None:
        if _CJK_RE.search(needle):
            found = re.compile(re.escape(needle))
        else:
            found = re.compile(r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])")
        _WORD_CACHE[needle] = found
    return found


def _usable(needles: Sequence[str]) -> list[str]:
    from .claim_subject import WEAK_CONTEXT_NEEDLES

    return [str(n).lower() for n in needles
            if isinstance(n, str) and n.strip()
            and (len(n.strip()) >= 3 or _CJK_RE.search(n))
            and n.strip().lower() not in WEAK_CONTEXT_NEEDLES]


def _named(text: str, needles: Sequence[str]) -> bool:
    lowered = text.lower()
    return any(_pattern(needle).search(lowered) for needle in _usable(needles))


def _count(text: str, needles: Sequence[str]) -> int:
    lowered = text.lower()
    return sum(len(_pattern(needle).findall(lowered)) for needle in _usable(needles))


def is_financial_statement(statement: Any) -> bool:
    """Whether a statement is about financial lines (revenue, cash flow, debt...)."""

    return isinstance(statement, str) and _FINANCIAL_RE.search(statement) is not None


def issuer_table(
    issuers: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, list[str]]:
    """issuer key -> needles: the given (covered) issuers plus the extra table.

    Keys are whatever the caller uses (a ``company:ticker:msft`` ref for a
    covered company); the extra issuers are keyed ``ticker:ORCL``.
    """

    table: dict[str, list[str]] = {str(k): sorted(set(_usable(v))) for k, v in (issuers or {}).items()}
    covered = {needle for values in table.values() for needle in values}
    for ticker, names in EXTRA_ISSUER_NAMES.items():
        # A ticker that is also an ordinary word or acronym ("now", "crm",
        # "wit") is not evidence of the issuer; its name is.
        # Whole names only: splitting "Advanced Micro Devices" into its words
        # would find an issuer in every "advanced".
        words = [*names] if ticker in AMBIGUOUS_TICKERS else [ticker, *names]
        needles = sorted({str(word).strip().lower() for word in words if str(word).strip()}
                         - covered)
        if needles:
            table.setdefault(f"ticker:{ticker}", needles)
    return table


def _key_for_ticker(ticker: str, table: Mapping[str, Sequence[str]]) -> str | None:
    lowered = ticker.strip().lower()
    for key, needles in table.items():
        if key.lower().endswith(":" + lowered) or lowered in needles:
            return key
    return None


def document_primary_issuer(
    text: Any, *, title: Any = None, issuers: Mapping[str, Sequence[str]],
) -> dict[str, Any] | None:
    """The one issuer this document is about, with how that was decided, or None.

    In order: a wiki front matter's ``分类: ORCL (公司)`` and its source file
    name (``ORACLE_20260123``); a title that names exactly one issuer; the
    density of mentions (most of every issuer's, and enough of them).  A head
    that lists several related companies decides nothing.
    """

    table = dict(issuers)
    if isinstance(text, str) and text:
        front = text[:FRONT_CHARS]
        match = _FRONT_CATEGORY_RE.search(front)
        file_match = _FRONT_FILE_RE.search(front)
        if match is not None:
            ticker = match.group(1).strip(".")
            key = _key_for_ticker(ticker, table) or f"ticker:{ticker.upper()}"
            needles = set(table.get(key, ())) | {ticker.lower()}
            if file_match is not None:
                needles.add(file_match.group(1).lower())
            return {"issuer": key, "basis": "front_matter_category", "evidence": match.group(0),
                    "needles": sorted(needles)}
        if file_match is not None:
            # A file name decides only when it is a known issuer's name
            # ("ORACLE_20260123"); "GS_Tech_20260101" decides nothing.
            stem = file_match.group(1).lower()
            key = next((k for k, needles in table.items() if stem in needles), None)
            if key is not None:
                return {"issuer": key, "basis": "source_file_name",
                        "evidence": file_match.group(0), "needles": list(table[key])}
    if isinstance(title, str) and title.strip():
        named = [key for key, needles in table.items() if _named(title, needles)]
        if len(named) == 1:
            return {"issuer": named[0], "basis": "title", "evidence": title[:200],
                    "needles": list(table[named[0]])}
        if len(named) > 1:
            return None
    if isinstance(text, str) and text:
        counts = {key: _count(text, needles) for key, needles in table.items()}
        total = sum(counts.values())
        if total:
            key, best = max(counts.items(), key=lambda item: (item[1], item[0]))
            if best >= DENSITY_MIN_MENTIONS and best >= DENSITY_MIN_SHARE * total:
                return {"issuer": key, "basis": "density",
                        "evidence": f"{best} of {total} issuer mentions",
                        "needles": list(table[key])}
    return None


def local_context(text: str, start: int, end: int) -> dict[str, Any]:
    """The span's page head (table title, column headers) and its neighbourhood."""

    page_start = None
    for match in _PAGE_MARKER_RE.finditer(text, max(0, start - MAX_PAGE_BACK), start):
        page_start = match.end()
    page_head = "" if page_start is None else text[page_start:min(start, page_start + PAGE_HEAD_CHARS)]
    return {
        "page_offset": page_start,
        "page_head": page_head,
        "before": text[max(0 if page_start is None else page_start, start - LOCAL_CHARS):start],
        "span": text[start:end],
        "after": text[end:end + LOCAL_CHARS],
    }


def _snippet(text: str, needles: Sequence[str]) -> str | None:
    lowered = text.lower()
    for needle in _usable(needles):
        match = _pattern(needle).search(lowered)
        if match is not None:
            return text[max(0, match.start() - 60):match.end() + 60].replace("\n", " | ")
    return None


def attribute(
    *,
    text: str,
    start: int,
    end: int,
    statement: str,
    subject_ref: str,
    issuers: Mapping[str, Sequence[str]],
    title: Any = None,
    issuer_document: bool = False,
) -> dict[str, Any]:
    """The attribution verdict for one Claim and its ``attribution_evidence``.

    ``issuers`` maps every issuer key the caller knows (the subject included)
    to its name needles; the extra issuers of :data:`EXTRA_ISSUER_NAMES` are
    added here.  ``issuer_document`` marks a filing fetched by the subject's
    own accession: always the subject's.
    """

    result: dict[str, Any] = {"rule_ref": RULE_REF, "subject_ref": subject_ref}
    if not isinstance(text, str) or not (0 <= start < end <= len(text)):
        return {**result, "verdict": "not_applicable", "reason": "no readable cited span"}
    span = text[start:end]
    if not is_financial_statement(statement):
        return {**result, "verdict": "not_applicable", "reason": "not a statement about financials"}
    if issuer_document:
        return {**result, "verdict": "consistent", "basis": "issuer_document"}
    table = issuer_table(issuers)
    if subject_ref not in table or not table[subject_ref]:
        return {**result, "verdict": "not_applicable", "reason": "the subject has no names to check"}
    primary = document_primary_issuer(text, title=title, issuers=table)
    if primary is not None and primary["needles"]:
        table.setdefault(primary["issuer"], list(primary["needles"]))
    context = local_context(text, start, end)
    estimates = sorted({m.group(0).strip() for m in _ESTIMATE_RE.finditer(
        context["page_head"] + "\n" + span)})[:12]
    period = {
        "estimate_markers": estimates,
        "statement_marks_estimate": _STATEMENT_ESTIMATE_RE.search(statement) is not None,
    }
    period["estimate_presented_as_actual"] = bool(estimates) and not period["statement_marks_estimate"]
    result["period"] = period
    if primary is not None:
        result["primary_issuer"] = {k: primary[k] for k in ("issuer", "basis", "evidence")}
    if primary is not None and primary["issuer"] == subject_ref:
        return {**result, "verdict": "consistent", "basis": "subject_is_primary_issuer"}
    named: dict[str, dict[str, str]] = {}
    for where in ("span", "page_head", "before", "after"):
        part = context[where]
        if not part:
            continue
        for key, needles in table.items():
            if key in named:
                continue
            found = _snippet(part, needles)
            if found is not None:
                named[key] = {"where": where, "snippet": found}
    result["named_locally"] = named
    if subject_ref in named:
        return {**result, "verdict": "consistent", "basis": "subject_named_locally",
                "where": named[subject_ref]["where"]}
    rank = {"span": 0, "page_head": 1, "before": 2, "after": 3}
    others = sorted(named, key=lambda k: (rank[named[k]["where"]], k))
    if primary is not None:
        # Another issuer's document, and the passage never names the subject:
        # whoever the passage names is whose table it is, else the document's.
        if others:
            key = others[0]
            return {**result, "verdict": "misattributed", "basis": "other_issuer_named_locally",
                    "table_issuer": key, "evidence": named[key]}
        return {**result, "verdict": "misattributed", "basis": "document_primary_issuer",
                "table_issuer": primary["issuer"], "evidence": result["primary_issuer"]}
    if others:
        # A digest or an industry note with no one issuer: another company is
        # named next to the passage and the subject is not.  Not decided here.
        return {**result, "verdict": "ambiguous", "basis": "other_issuer_named_without_primary_issuer",
                "table_issuer": others[0], "evidence": named[others[0]]}
    return {**result, "verdict": "ambiguous", "basis": "no_issuer_named_and_no_primary_issuer"}


def misattribution_rationale(verdict: Mapping[str, Any], subject_label: str) -> str:
    """One Chinese sentence for a retirement / hold, naming whose table it is."""

    issuer = str(verdict.get("table_issuer") or "另一家公司")
    issuer = issuer.split(":", 1)[1].upper() if ":" in issuer else issuer
    if verdict.get("basis") == "other_issuer_named_locally":
        where = {"page_head": "所在页的表头/标题", "span": "所引片段", "before": "所引片段之前",
                 "after": "所引片段之后"}.get(str((verdict.get("evidence") or {}).get("where")), "附近")
        text = (f"这条关于财务数据的结论挂在 {subject_label} 名下，但所引表格/段落的{where}"
                f"写的是 {issuer}，附近没有出现 {subject_label}，且整份文档的主体公司不是 "
                f"{subject_label}：这不是 {subject_label} 的数据。")
    else:
        basis = (verdict.get("evidence") or {}).get("basis")
        text = (f"这条关于财务数据的结论挂在 {subject_label} 名下，但所引表格/段落及其附近没有出现 "
                f"{subject_label}，而这份文档的主体公司是 {issuer}（依据 {basis}）：这是 {issuer} 的数据。")
    period = verdict.get("period") or {}
    if period.get("estimate_presented_as_actual"):
        text += "另外，所引列是预测值（" + "、".join(period.get("estimate_markers") or ()) + \
            "），结论却写成了已发生的结果。"
    return text


__all__ = [
    "DETECTOR_REF",
    "EXTRA_ISSUER_NAMES",
    "RULE_REF",
    "attribute",
    "document_primary_issuer",
    "is_financial_statement",
    "issuer_table",
    "local_context",
    "misattribution_rationale",
]
