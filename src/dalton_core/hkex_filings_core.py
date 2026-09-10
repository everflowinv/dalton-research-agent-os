"""W4: what a Hong Kong issuer disclosed today, and who bought or sold it.

Dalton could read what an American company files and what a Chinese vendor
normalises.  It could not read a single Hong Kong disclosure -- and Hong Kong
is the only market on this planet that makes a listed company report *every
day* how many of its own shares it bought back, at what prices, and how much of
itself it has now retired.  That daily tape is the thing the owner asked for:
management selling and companies buying, seen the day after it happens rather
than a quarter later.

Four operations, because a schema hash binds one operation and these are four
different permissions:

``next_day_disclosure_returns``
    The Exchange's own *Share Repurchase Report* for one printed day, filtered
    to one stock code.  Hong Kong's Main Board Rule 10.06(4)(a) makes an issuer
    file a Next Day Disclosure Return by 08:30 on the business day after a
    buy-back; the Exchange aggregates every one of them into a single workbook
    it publishes that morning.  Reading that workbook is reading the returns,
    with the Exchange rather than a vendor as the publisher.

``monthly_returns``
    The Monthly Return of Equity Issuer on Movements in Securities, enumerated
    for one stock code.  What this operation returns is the *index* -- the
    month the return covers, when it was filed, and where the document is --
    and deliberately not the figures inside it, because they are only in a PDF
    and a number read out of a form layout by a text extractor is a number
    nobody can check.  See ``MONTHLY_RETURN_LIMIT_NOTE``.

``disclosure_of_interests``
    The SFC's Part XV filings, from HKEX's Disclosure of Interests database:
    directors and chief executives (Forms 3A/3B) and substantial shareholders
    (Forms 1/2/3).  The list gives the person, the reason code, the size and
    the average price; each notice's own form gives the holding before and
    after the event, and the capacity it is held in.  Both are read.

``announcements_index``
    Every announcement one stock code filed in a window, so that a filings
    index can see a buy-back mandate being granted, a placing or top-up, and
    the date results will be published.

**Verbatim or nothing.**  Every figure these operations emit is the text the
document contained.  The buy-back report writes ``HKD 438.2`` and
``100,431,292.5``; a float would turn the first into a parse and lose the
trailing digit of the second, so nothing here is ever parsed into one.  The
currency is split off the amount because ``HKD 438.2`` is two facts, and a
Hong Kong issuer may report a buy-back in a currency that is not the Hong Kong
dollar.

**This is not a financial statement.**  These are ownership and treasury
disclosures.  They carry :data:`HKEX_GRADE`, which -- exactly like
``sec_ownership_core.OWNERSHIP_GRADE`` -- exists to say what these documents
may *not* be read for: no figure may be graded against one, no statement line
may come from one, and nothing in the forecast model reads this connector.

**The endpoints, and how fragile they are.**  Written down in
:data:`ENDPOINT_FRAGILITY` rather than in a report, because the next person to
find one of them empty needs the reason where the call is, not in a document
they would have to know to look for.
"""

from __future__ import annotations

import copy
import re
from datetime import datetime
from types import MappingProxyType
from typing import Any
from urllib.parse import quote

from .connector_inventory import load_packaged_connector_inventory
from .store import content_hash

TEMPLATE_KEY = "hkex-filings"
CONNECTOR_SLUG = "hkex-filings"
SOURCE_REF = "source:hkex-filings"

NEXT_DAY_DISCLOSURE_OPERATION = "next_day_disclosure_returns"
MONTHLY_RETURNS_OPERATION = "monthly_returns"
DISCLOSURE_OF_INTERESTS_OPERATION = "disclosure_of_interests"
ANNOUNCEMENTS_INDEX_OPERATION = "announcements_index"
OPERATIONS: tuple[str, ...] = (
    NEXT_DAY_DISCLOSURE_OPERATION,
    MONTHLY_RETURNS_OPERATION,
    DISCLOSURE_OF_INTERESTS_OPERATION,
    ANNOUNCEMENTS_INDEX_OPERATION,
)

KIND_BY_OPERATION = MappingProxyType({
    operation: f"hkex-filings-{operation.replace('_', '-')}"
    for operation in OPERATIONS
})
CAPABILITY_BY_OPERATION = MappingProxyType({
    operation: f"capability:dalton:connector:{kind}"
    for operation, kind in KIND_BY_OPERATION.items()
})
OPERATION_BY_KIND = MappingProxyType(
    {kind: operation for operation, kind in KIND_BY_OPERATION.items()}
)

GOVERNANCE_SCHEMA_VERSION = "0.1"
CAPTURE_SCHEMA_VERSION = "0.1"
SIDE_EFFECT = "read:public-http"
ADAPTER_REF = "adapter:dalton-core-hkex-filings:0.1"
# No third-party library reads these documents: the adapter is this repository's
# own reader for a text grid, a JSON body and two HTML tables.  The one place a
# library appears is the *capture* script, which needs ``xlrd`` to turn the
# Exchange's workbook into that grid, and which is not in the child's path.
ADAPTER_LIBRARY = None

USER_AGENT = "Dalton Research Agent OS HKEX filings lane (owner: lumos)"

# -- the grade word ---------------------------------------------------------
HKEX_GRADE = "hkex-disclosure-filing"
HKEX_GRADE_MEANING = (
    "filed with HKEX or the SFC by a Hong Kong issuer or by a person holding "
    "an interest in one; primary and regulatory, and not a statement of what "
    "the business earned -- it may never be read for a financial-statement "
    "figure"
)
HKEX_EVIDENCE_TIER = "primary_filing"
# Two grants, the same pair the SEC ownership lane needs and for the same
# reasons: one authorises learning a dated fact about a covered company, the
# other authorises writing it into the event ledger.
WRITE_SCOPES: tuple[str, ...] = ("observation", "market_event")
FIGURE_GRADE_BY_OPERATION: dict[str, str] = {}

# -- company reference ------------------------------------------------------
#
# ``company:sec-cik:*`` cannot name a Hong Kong issuer: there is no CIK.  S4
# proposed ``company:hk-secucode:00700.HK`` and this is the slice that needs
# it, so this is where it is defined -- as a *ref scheme*, which is a naming
# rule, and emphatically not as a widening of the mission universe.  Admitting
# a Hong Kong name to coverage is an owner decision and nothing here makes it.
COMPANY_REF_PREFIX = "company:hk-secucode:"
_TICKER_RE = re.compile(r"^[0-9]{1,5}$")
_SECUCODE_RE = re.compile(r"^([0-9]{5})\.HK$")


class HkexFilingsError(RuntimeError):
    """The HKEX filings identity, parameters or governance record is invalid."""


def normalise_ticker(value: Any) -> str:
    """A Hong Kong stock code as HKEXnews spells it: five digits, zero-padded.

    Hong Kong writes the same company three ways in three places -- ``700``
    (and ``711``, and ``1``) in the Exchange's buy-back report, ``00700`` in
    HKEXnews and the DI database, ``0700.HK`` on Yahoo -- and they are one
    company.  Everything inside this
    connector uses the five-digit form; the readers that meet the other two
    convert at the edge, so a comparison is never between two spellings.
    """

    text = str(value).strip()
    if not _TICKER_RE.fullmatch(text):
        raise HkexFilingsError(
            f"{value!r} is not a Hong Kong stock code; a Main Board code is "
            "one to five digits and this connector holds them zero-padded to "
            "five (00001, 00700, 09988)"
        )
    return text.zfill(5)


def company_ref(ticker: Any) -> str:
    """``company:hk-secucode:00700.HK`` for one stock code."""

    return f"{COMPANY_REF_PREFIX}{normalise_ticker(ticker)}.HK"


def ticker_for_company_ref(value: Any) -> str | None:
    """The stock code inside a Hong Kong company ref, or None."""

    if not isinstance(value, str) or not value.startswith(COMPANY_REF_PREFIX):
        return None
    match = _SECUCODE_RE.fullmatch(value[len(COMPANY_REF_PREFIX):].strip())
    return match.group(1) if match else None


def yahoo_ticker(ticker: Any) -> str:
    """``0700.HK``: what Yahoo calls the same security.

    Four digits and a suffix, not five: Yahoo drops the leading zero for Main
    Board codes.  Provided because the price comparison this connector's
    derived context wants needs it -- and see ``PRICE_AUTHORITY_NOTE`` for why
    that comparison is unavailable today.
    """

    return f"{normalise_ticker(ticker)[1:]}.HK"


# What the price authority does with that symbol, checked rather than assumed.
PRICE_AUTHORITY_NOTE = (
    "No current HKD price observation was supplied for this company. "
    "Dalton accepts Yahoo's four-digit .HK symbols; the comparison stays "
    "unavailable until a governed price observation is available."
)

MONTHLY_RETURN_LIMIT_NOTE = (
    "HKEXnews serves the Monthly Return only as a PDF. This operation "
    "enumerates the returns -- the month covered, when it was filed, where the "
    "document is -- and claims nothing about the figures inside them. Reading a "
    "form-layout PDF with a text extractor produces numbers whose row and "
    "column nobody can check, and an issued-share count that is wrong is worse "
    "than one that is absent."
)

# -- hosts, and the fragility of each route ---------------------------------

TITLE_SEARCH_HOST = "www1.hkexnews.hk"
BUYBACK_REPORT_HOST = "www3.hkexnews.hk"
DI_HOST = "di.hkex.com.hk"

HOSTS_BY_OPERATION = MappingProxyType({
    NEXT_DAY_DISCLOSURE_OPERATION: (BUYBACK_REPORT_HOST,),
    MONTHLY_RETURNS_OPERATION: (TITLE_SEARCH_HOST,),
    DISCLOSURE_OF_INTERESTS_OPERATION: (DI_HOST,),
    ANNOUNCEMENTS_INDEX_OPERATION: (TITLE_SEARCH_HOST,),
})

# Every one of these was probed on 2026-09-10 from the owner's host and the
# note is what was observed, not what the documentation claims.
ENDPOINT_FRAGILITY: tuple[Any, ...] = (
    MappingProxyType({
        "route": "https://www3.hkexnews.hk/reports/sharerepur/documents/SRRPT{YYYYMMDD}.xls",
        "operation": NEXT_DAY_DISCLOSURE_OPERATION,
        "observed": "200, application/vnd.ms-excel, ~45 KB, 131 rows x 14 columns",
        "fragility": (
            "The same path on www.hkexnews.hk, www1.hkexnews.hk and "
            "www3.hkexnews.hk/reports/sharerepur/sbn.htm all 404 or redirect to "
            "a lowercase path that 404s; only the www3 `/reports/sharerepur/"
            "documents/` form serves the file. There is no .htm or .csv "
            "rendering -- both were probed and both 404 -- so the workbook is "
            "the only form of this report that exists. It is published for "
            "trading days only; a Sunday returns 404 and that is an answer, not "
            "a fault."
        ),
    }),
    MappingProxyType({
        "route": "https://www1.hkexnews.hk/search/titleSearchServlet.do",
        "operation": ANNOUNCEMENTS_INDEX_OPERATION,
        "observed": "200, application/json, `result` is a JSON string inside JSON",
        "fragility": (
            "The single most fragile thing in this connector, and it fails "
            "*silently*: with `t1code=-1` (the obvious 'no category' value) the "
            "servlet returns `recordCnt: 0` and HTTP 200 for an issuer that "
            "filed twenty-four announcements in the window. The value that "
            "means 'every category' is -2, for `t1code`, `t2Gcode` and `t2code` "
            "alike. The defence is structural rather than a check on the "
            "answer: `title_search_url` composes the query and refuses any "
            "`t1code` outside `TIER_ONE_CODES`, which does not contain -1, so "
            "the bad value cannot be sent at all. It is a forbidden route on "
            "the profile as well. The wire carries `record_count` beside "
            "`row_count` so that a short list can still be told from an empty "
            "one. The old advanced search at "
            "www3.hkexnews.hk/listedco/listconews/advancedsearch/ now redirects "
            "to the homepage and is gone."
        ),
    }),
    MappingProxyType({
        "route": "https://www1.hkexnews.hk/search/prefix.do",
        "operation": ANNOUNCEMENTS_INDEX_OPERATION,
        "observed": "200, JSONP: `callback({\"stockInfo\":[{\"stockId\":7609,...}]})`",
        "fragility": (
            "The title search is keyed by an internal `stockId`, not by the "
            "stock code, and this is the only public route from one to the "
            "other. It answers in JSONP, so the body has to be unwrapped before "
            "it is JSON. A code that resolves to more than one security -- a "
            "search prefix rather than an exact code -- is refused rather than "
            "resolved to the first hit."
        ),
    }),
    MappingProxyType({
        "route": "https://di.hkex.com.hk/di/NSSrchCorpList.aspx and NSAllFormList.aspx",
        "operation": DISCLOSURE_OF_INTERESTS_OPERATION,
        "observed": "200, text/html, ASP.NET tables; `sid` for 00700 was 6893",
        "fragility": (
            "Keyed by an internal `sid`, like the title search, and passing "
            "`sc=00700` alone is worse than useless: NSAllFormList answers with "
            "whatever corporation the `sid` names and ignores the stock code "
            "entirely, so a wrong `sid` returns a full, well-formed page of "
            "somebody else's directors. `sid=6` returns Power Assets. The "
            "adapter therefore reads the `sid` out of the corporation list's own "
            "link and refuses any form list whose printed stock code is not the "
            "one that was asked for. Dates are `dd/mm/yyyy` here and "
            "`yyyymmdd` on HKEXnews. There is no JSON and no pagination "
            "parameter; the list pages at 100 rows."
        ),
    }),
    MappingProxyType({
        "route": "https://www1.hkexnews.hk/search/titlesearch.xhtml",
        "operation": None,
        "observed": "200; a JSF form POST that renders 'Total records found: N'",
        "fragility": (
            "The human page. It works with the same parameters and the same "
            "-2 category values, and it is not used: it answers in HTML with a "
            "ViewState and a session cookie, which is three more things that "
            "can change than the servlet has. Recorded here because it is the "
            "route to fall back to if the servlet is withdrawn."
        ),
    }),
)

# What a reader has to be told about one of these numbers even when nothing
# went wrong.  These travel on the rows as ``caliber_note``.
CALIBER_NOTES = MappingProxyType({
    "share_buyback_report": (
        "香港交易所对当日「翌日披露报表」的汇总，非发行人申报原文；同一发行人当日多笔回购"
        "由交易所合并为最后一行，星号列为合并后的数字。"
    ),
    # Measured, not assumed: the 00700 row of SRRPT20260909 carries 44,613,700
    # and 0.48929%, and the issuer's own return of the same day says those are
    # "under the repurchase mandate" granted on 13 May 2026 -- not a calendar
    # year to date.
    "mandate_to_date": (
        "累计数与百分比的基准是「本次回购授权（股东大会决议）以来」，不是自然年迄今；"
        "百分比的分母是授权决议当日的已发行股份（不含库存股）。"
    ),
    "monthly_return_index_only": (
        "本行只是月报表的索引：期间、提交时间、文件位置。表内数字只存在于 PDF，"
        "本操作不声称读到过它们。"
    ),
    "di_list_and_form": (
        "权益披露：列表页给出披露原因代码、涉及股数与均价，事件前后的持股与百分比来自"
        "该通知自己的表格页；两者同属一份申报。"
    ),
    "di_capacity_codes": (
        "capacity 与 reason 是证监会的数字代码（如 2101 实益拥有人、1316 权益性质改变），"
        "本系统按 HKEX 公布的对照表翻译，未列入对照表的代码原样保留。"
    ),
})

# The SFC's own code tables, only as far as this connector needs them.  A code
# that is not here is carried through as the code it is rather than translated
# into a guess -- which is why the adapter emits both ``*_code`` and
# ``*_meaning`` and lets the second be null.
CAPACITY_CODE_MEANINGS = MappingProxyType({
    "2101": "Beneficial owner",
    "2102": "Investment manager",
    "2103": "Trustee",
    "2104": "Person having a security interest in shares",
    "2105": "Person holding shares as custodian",
    "2106": "Approved lending agent",
    "2107": "Interest of corporation controlled by you",
    "2108": "Interest of spouse",
    "2109": "Interest of child under 18",
    "2110": "Held jointly with another person",
    "2111": "Other",
})
REASON_CODE_MEANINGS = MappingProxyType({
    "1001": "Interest in shares of the listed corporation acquired",
    "1003": "Interest in shares of the listed corporation disposed of",
    "1004": "Interest in shares of the listed corporation ceased",
    "1201": "Unlisted derivative interest acquired",
    "1202": "Unlisted derivative interest disposed of",
    "1316": "Change in the nature of the interest",
    "1501": "Long position increases to 5% or more",
    "1502": "Long position falls below 5%",
    "1503": "Long position percentage crosses a whole percentage figure",
})
# Which of the reason codes above describe a *trade* by a person rather than a
# change in how an unchanged holding is described.  The judgement layer reads
# an insider transaction differently from a re-papering, and telling them apart
# is not the model's job.
TRADE_REASON_CODES = frozenset({"1001", "1003", "1004", "1201", "1202"})


def _operation(operation: str) -> str:
    if operation not in KIND_BY_OPERATION:
        raise HkexFilingsError(f"hkex-filings has no frozen {operation!r} operation")
    return operation


def hkex_contract(operation: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """The packaged template and one operation's frozen contract."""

    operation = _operation(operation)
    template = load_packaged_connector_inventory()["templates"][TEMPLATE_KEY]
    matches = [item for item in template["operations"]
               if item["operation"] == operation]
    if len(matches) != 1:
        raise HkexFilingsError(
            f"packaged hkex-filings template lacks the frozen {operation} operation"
        )
    return template, matches[0]


def hkex_source_hash() -> str:
    """Shared by all four: one HKEX is one source."""

    template, _ = hkex_contract(NEXT_DAY_DISCLOSURE_OPERATION)
    return content_hash(dict(template["source_identity"]))


def hkex_schema_hash(operation: str) -> str:
    """Bound to one operation, so approving one cannot widen to another."""

    _, contract = hkex_contract(operation)
    return content_hash({
        "allowed_operations": [operation],
        "input_schema_refs": {operation: contract["input_schema_ref"]},
        "input_schema_hashes": {operation: contract["input_schema_hash"]},
        "output_schema_refs": {operation: contract["output_schema_ref"]},
        "output_schema_hashes": {operation: contract["output_schema_hash"]},
    })


def hkex_adapter_hash(operation: str) -> str:
    """Names this repository's own reader and the hosts it is allowed to reach."""

    template, _ = hkex_contract(operation)
    return content_hash({
        "target_ref": template["transport"]["target_ref"],
        "source": template["source_identity"]["source_ref"],
        "operation": operation,
        "adapter": ADAPTER_REF,
        "hosts": list(HOSTS_BY_OPERATION[operation]),
    })


def hkex_identity(operation: str) -> dict[str, Any]:
    """Source and schema identity of exactly one operation."""

    template, contract = hkex_contract(operation)
    return {
        "capability_id": CAPABILITY_BY_OPERATION[operation],
        "source_identity": dict(template["source_identity"]),
        "source_hash": hkex_source_hash(),
        "schema_hash": hkex_schema_hash(operation),
        "adapter_ref": template["transport"]["target_ref"],
        "adapter_hash": hkex_adapter_hash(operation),
        "operation": operation,
        "allowed_operations": [operation],
        "allowed_hosts": list(HOSTS_BY_OPERATION[operation]),
        "input_schema_ref": contract["input_schema_ref"],
        "input_schema_hash": contract["input_schema_hash"],
        "output_schema_ref": contract["output_schema_ref"],
        "output_schema_hash": contract["output_schema_hash"],
        "input_schema_refs": {operation: contract["input_schema_ref"]},
        "input_schema_hashes": {operation: contract["input_schema_hash"]},
        "output_schema_refs": {operation: contract["output_schema_ref"]},
        "output_schema_hashes": {operation: contract["output_schema_hash"]},
    }


def hkex_permissions() -> dict[str, Any]:
    """Credential-free public HTTPS and the raw sink."""

    from .sec_authority_harness import PUBLIC_PERMISSIONS

    return copy.deepcopy(PUBLIC_PERMISSIONS)


def hkex_fixture_hash() -> str:
    template, _ = hkex_contract(NEXT_DAY_DISCLOSURE_OPERATION)
    return template["fixture_manifest_hash"]


def hkex_output_schema(operation: str) -> dict[str, Any]:
    """The frozen output contract one read must satisfy."""

    template, contract = hkex_contract(operation)
    ref = contract["output_schema_ref"]
    for document in template["schema_documents"]:
        if document["schema_ref"] == ref:
            return document["document"]
    raise HkexFilingsError(
        f"packaged hkex-filings template has no {operation} output contract"
    )


def hkex_allowed_hosts(operation: str) -> tuple[str, ...]:
    return HOSTS_BY_OPERATION[_operation(operation)]


def _wire_time(value: datetime) -> str:
    return value.astimezone(value.tzinfo).isoformat(timespec="microseconds")


def build_hkex_filings_governance_record(
    *,
    operation: str,
    approved_by: str,
    status: str = "proposed",
    effective_from: str = "2026-09-10T00:00:00+00:00",
    max_lease_seconds: int = 300,
    version: int = 1,
) -> dict[str, Any]:
    """Closed, hash-bound governance record for one operation.

    ``max_lease_seconds`` is 300 rather than the SEC lane's 120 because one
    ``disclosure_of_interests`` unit is a corporation lookup, a notice list and
    one page per notice, and a slow ASP.NET page is the normal case rather than
    the bad one.
    """

    operation = _operation(operation)
    if status not in {"proposed", "approved"}:
        raise HkexFilingsError("governance status must be proposed or approved")
    if not isinstance(approved_by, str) or not approved_by.startswith("human:"):
        raise HkexFilingsError("approved_by must be a human: principal")
    kind = KIND_BY_OPERATION[operation]
    capability_id = CAPABILITY_BY_OPERATION[operation]
    base = {
        "schema_version": GOVERNANCE_SCHEMA_VERSION,
        "id": f"connector-governance:{kind}:v{version}",
        "status": status,
        "capability_id": capability_id,
        "approved_by": approved_by,
        "principal_ref": "principal:dalton-core-trusted-runner",
        "policy_ref": f"policy:dalton:connector-governance:{kind}:v{version}",
        "approval_ref": f"approval:connector-governance:{kind}:v{version}",
        "decision_ref": f"capability-decision:connector-governance:{kind}:v{version}",
        "registry_revision_ref": f"{capability_id}@v{version}",
        "attestation_ref": f"attestation:connector-governance:{kind}:v{version}",
        "effective_from": _wire_time(datetime.fromisoformat(effective_from)),
        "effective_until": None,
        "max_lease_seconds": max_lease_seconds,
        "allowed_permissions": copy.deepcopy(hkex_permissions()),
        "expected_source_hash": hkex_source_hash(),
        "expected_schema_hash": hkex_schema_hash(operation),
    }
    base["content_hash"] = content_hash(base)
    return base


def invocation_ref(
    *,
    operation: str,
    governance_ref: str,
    governance_hash: str,
    parameters: dict[str, Any],
    artifact_hash: str,
) -> str:
    """Name one exact read: this approval, these parameters, those bytes.

    The same construction S4 and S5 use, including their hard-won rule: the
    capture's two clock fields are lifted out before the artifact is hashed
    (``hkex_filings_cli.CLOCK_FIELDS``), so two readings of an unchanged day
    are one invocation and a day whose numbers moved is a different one.
    """

    return "connector-invocation:hkex-filings:" + content_hash({
        "operation": _operation(operation),
        "source_ref": SOURCE_REF,
        "adapter_ref": ADAPTER_REF,
        "adapter_hash": hkex_adapter_hash(operation),
        "governance_ref": governance_ref,
        "governance_hash": governance_hash,
        "parameters": parameters,
        "artifact_hash": artifact_hash,
    })[:32]


# -- the URLs, all of them derived and none of them handed in ----------------
#
# ``route:arbitrary-attachment-url`` is a forbidden route on this profile: a
# caller supplies a stock code and a date and these functions supply the path.
# There is no input through which a URL can arrive.

_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
# HKEXnews composes a document path itself and hands it back in the search
# result. It is put in a URL, so it is checked against this shape first -- the
# same discipline ``sec_ownership_core`` applies to a 13F's document name.
_FILE_LINK_RE = re.compile(r"^/listedco/[A-Za-z0-9/_.-]{1,160}$")
_DI_FORM_PATH_RE = re.compile(r"^NSForm[0-9A-Za-z]{1,4}\.aspx\?fn=[A-Z0-9]{1,32}$")

TIER_ONE_ALL = "-2"
TIER_ONE_MONTHLY_RETURNS = "51500"
TIER_ONE_NEXT_DAY_DISCLOSURE = "50000"
TIER_ONE_ANNOUNCEMENTS_AND_NOTICES = "10000"
TIER_ONE_CODES = frozenset({
    TIER_ONE_ALL, TIER_ONE_MONTHLY_RETURNS, TIER_ONE_NEXT_DAY_DISCLOSURE,
    TIER_ONE_ANNOUNCEMENTS_AND_NOTICES,
})
TITLE_SEARCH_ROW_RANGE = 100


def _day(value: Any, name: str) -> str:
    text = str(value).strip()
    if not _DATE_RE.fullmatch(text):
        raise HkexFilingsError(f"{name} must be an ISO date, not {value!r}")
    return text


def _compact_day(value: Any, name: str) -> str:
    return _day(value, name).replace("-", "")


def _di_day(value: Any, name: str) -> str:
    """``dd/mm/yyyy``: the DI database's own date shape, and only its own."""

    year, month, day = _day(value, name).split("-")
    return f"{day}/{month}/{year}"


def share_buyback_report_url(as_of: Any) -> str:
    """The Exchange's Share Repurchase Report printed on one day.

    The report printed on day D carries the buy-backs of the previous trading
    day, because that is what a *next day* disclosure return is.
    """

    return (
        f"https://{BUYBACK_REPORT_HOST}/reports/sharerepur/documents/"
        f"SRRPT{_compact_day(as_of, 'as_of')}.xls"
    )


def stock_prefix_url(ticker: Any) -> str:
    """Stock code to HKEXnews' internal ``stockId``, in JSONP."""

    return (
        f"https://{TITLE_SEARCH_HOST}/search/prefix.do?callback=callback&lang=EN"
        f"&type=A&name={normalise_ticker(ticker)}&market=SEHK"
    )


def title_search_url(
    *, stock_id: int, since: Any, until: Any, tier_one: str = TIER_ONE_ALL,
    row_range: int = TITLE_SEARCH_ROW_RANGE,
) -> str:
    """One issuer's announcements in one window, from the JSON servlet.

    ``tier_one`` is ``-2`` for every category.  It is not ``-1``: that value is
    accepted, returns HTTP 200 and reports zero records for an issuer that
    filed two dozen documents, which is the quietest way this connector could
    be wrong.
    """

    if tier_one not in TIER_ONE_CODES:
        raise HkexFilingsError(
            f"{tier_one!r} is not a headline category this connector reads; "
            f"the frozen set is {sorted(TIER_ONE_CODES)}"
        )
    if not isinstance(stock_id, int) or isinstance(stock_id, bool) or stock_id <= 0:
        raise HkexFilingsError("stock_id must be the positive integer prefix.do gave")
    if not isinstance(row_range, int) or not 1 <= row_range <= 1000:
        raise HkexFilingsError("row_range must be between 1 and 1000")
    return (
        f"https://{TITLE_SEARCH_HOST}/search/titleSearchServlet.do"
        "?sortDir=0&sortByOptions=DateTime&category=0&market=SEHK"
        f"&stockId={stock_id}&documentType=-1"
        f"&t1code={tier_one}&t2Gcode=-2&t2code=-2&title=&searchType=1&lang=EN"
        f"&from={_compact_day(since, 'since')}&to={_compact_day(until, 'until')}"
        f"&MB-Daterange=0&rowRange={row_range}"
    )


def announcement_document_url(file_link: str) -> str:
    """One announcement document, at the path the search result gave.

    The path comes from HKEXnews and is checked against a shape before it is
    put in a URL, for the reason every such check exists here: "the index names
    its own document" stops being safe the moment the name can hold a pair of
    dots.
    """

    if not isinstance(file_link, str) or not _FILE_LINK_RE.fullmatch(file_link):
        raise HkexFilingsError(f"{file_link!r} is not an HKEXnews document path")
    if ".." in file_link:
        raise HkexFilingsError("an HKEXnews document path may not traverse")
    return f"https://{TITLE_SEARCH_HOST}{file_link}"


def di_corp_list_url(*, ticker: Any, since: Any, until: Any) -> str:
    """The Disclosure of Interests corporation list for one stock code."""

    return (
        f"https://{DI_HOST}/di/NSSrchCorpList.aspx?sa1=cl"
        f"&scsd={quote(_di_day(since, 'since'), safe='')}"
        f"&sced={quote(_di_day(until, 'until'), safe='')}"
        f"&sc={normalise_ticker(ticker)}&src=MAIN&lang=EN"
    )


def di_all_form_list_url(
    *, sid: int, corporation_name: str, ticker: Any, since: Any, until: Any
) -> str:
    """Every DI notice one corporation received in one window.

    ``sid`` is the database's own key and is read out of the corporation list's
    link rather than composed: passing the stock code alone returns a
    well-formed page about whichever corporation the default ``sid`` names.
    """

    if not isinstance(sid, int) or isinstance(sid, bool) or sid <= 0:
        raise HkexFilingsError("sid must be the positive integer the corp list gave")
    since_di, until_di = _di_day(since, "since"), _di_day(until, "until")
    return (
        f"https://{DI_HOST}/di/NSAllFormList.aspx?sa2=an&sid={sid}"
        f"&corpn={quote(str(corporation_name), safe='')}"
        f"&sd={quote(since_di, safe='')}&ed={quote(until_di, safe='')}"
        f"&cid=0&sa1=cl&scsd={quote(since_di, safe='')}"
        f"&sced={quote(until_di, safe='')}"
        f"&sc={normalise_ticker(ticker)}&src=MAIN&lang=EN"
    )


def di_form_detail_url(
    *, form_path: str, sid: int, corporation_name: str, ticker: Any,
    since: Any, until: Any,
) -> str:
    """One DI notice's own form, at the path the notice list gave."""

    if not isinstance(form_path, str) or not _DI_FORM_PATH_RE.fullmatch(form_path):
        raise HkexFilingsError(f"{form_path!r} is not a DI form path")
    since_di, until_di = _di_day(since, "since"), _di_day(until, "until")
    return (
        f"https://{DI_HOST}/di/{form_path}&sa2=an&sid={sid}"
        f"&corpn={quote(str(corporation_name), safe='')}"
        f"&sd={quote(since_di, safe='')}&ed={quote(until_di, safe='')}"
        f"&cid=0&sa1=cl&scsd={quote(since_di, safe='')}"
        f"&sced={quote(until_di, safe='')}"
        f"&sc={normalise_ticker(ticker)}&src=MAIN&lang=EN"
    )


__all__ = [
    "ADAPTER_LIBRARY",
    "ADAPTER_REF",
    "ANNOUNCEMENTS_INDEX_OPERATION",
    "BUYBACK_REPORT_HOST",
    "CALIBER_NOTES",
    "CAPABILITY_BY_OPERATION",
    "CAPACITY_CODE_MEANINGS",
    "CAPTURE_SCHEMA_VERSION",
    "COMPANY_REF_PREFIX",
    "CONNECTOR_SLUG",
    "DISCLOSURE_OF_INTERESTS_OPERATION",
    "DI_HOST",
    "ENDPOINT_FRAGILITY",
    "FIGURE_GRADE_BY_OPERATION",
    "GOVERNANCE_SCHEMA_VERSION",
    "HKEX_EVIDENCE_TIER",
    "HKEX_GRADE",
    "HKEX_GRADE_MEANING",
    "HOSTS_BY_OPERATION",
    "HkexFilingsError",
    "KIND_BY_OPERATION",
    "MONTHLY_RETURNS_OPERATION",
    "MONTHLY_RETURN_LIMIT_NOTE",
    "NEXT_DAY_DISCLOSURE_OPERATION",
    "OPERATIONS",
    "OPERATION_BY_KIND",
    "PRICE_AUTHORITY_NOTE",
    "REASON_CODE_MEANINGS",
    "SIDE_EFFECT",
    "SOURCE_REF",
    "TEMPLATE_KEY",
    "TIER_ONE_ALL",
    "TIER_ONE_ANNOUNCEMENTS_AND_NOTICES",
    "TIER_ONE_CODES",
    "TIER_ONE_MONTHLY_RETURNS",
    "TIER_ONE_NEXT_DAY_DISCLOSURE",
    "TITLE_SEARCH_HOST",
    "TITLE_SEARCH_ROW_RANGE",
    "TRADE_REASON_CODES",
    "USER_AGENT",
    "WRITE_SCOPES",
    "announcement_document_url",
    "build_hkex_filings_governance_record",
    "company_ref",
    "di_all_form_list_url",
    "di_corp_list_url",
    "di_form_detail_url",
    "hkex_adapter_hash",
    "hkex_allowed_hosts",
    "hkex_contract",
    "hkex_fixture_hash",
    "hkex_identity",
    "hkex_output_schema",
    "hkex_permissions",
    "hkex_schema_hash",
    "hkex_source_hash",
    "invocation_ref",
    "normalise_ticker",
    "share_buyback_report_url",
    "stock_prefix_url",
    "ticker_for_company_ref",
    "title_search_url",
    "yahoo_ticker",
]
