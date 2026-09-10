"""Generic, hash-bound governance for connector capabilities.

The governance record intentionally has no ``kind`` field.  Its closed wire
shape predates this module, so the connector kind is resolved from the
record's capability id against the registry below.  This keeps existing
AlphaEngine records byte-compatible while allowing the same owner approval
boundary to govern other connectors.
"""

from __future__ import annotations

import copy
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

from .capability_catalog import CapabilityPermissions, canonical_hash
from .connector_inventory import load_packaged_connector_inventory
from .store import canonical_json, content_hash


GOVERNANCE_SCHEMA_VERSION = "0.1"
GOVERNANCE_FIELDS = frozenset({
    "schema_version", "id", "status", "capability_id", "approved_by",
    "principal_ref", "policy_ref", "approval_ref", "decision_ref",
    "registry_revision_ref", "attestation_ref", "effective_from",
    "effective_until", "max_lease_seconds", "allowed_permissions",
    "expected_source_hash", "expected_schema_hash", "content_hash",
})
GOVERNANCE_STATUSES = frozenset({"proposed", "approved"})

ALPHAENGINE_KIND = "alphaengine-get-document"
ALPHAENGINE_SEARCH_KIND = "alphaengine-search-library"
SEC_COMPANY_FACTS_KIND = "sec-company-facts"
GEMINI_WEB_SEARCH_KIND = "gemini-web-search"
WEB_FETCH_KIND = "web-fetch"
SEC_FILINGS_INDEX_KIND = "sec-filings-index"
ALPHAENGINE_CAPABILITY_ID = (
    "capability:dalton:connector:alphaengine-get-document"
)
ALPHAENGINE_SEARCH_CAPABILITY_ID = (
    "capability:dalton:connector:alphaengine-search-library"
)
SEC_CAPABILITY_ID = "capability:dalton:connector:sec-edgar"
GEMINI_WEB_SEARCH_CAPABILITY_ID = "capability:dalton:connector:gemini-web-search"
WEB_FETCH_CAPABILITY_ID = "capability:dalton:connector:web-fetch"
SEC_FILINGS_INDEX_CAPABILITY_ID = "capability:dalton:connector:sec-filings-index"
# P13ae: Guidepoint is two capabilities for the same reason AlphaEngine is --
# reading an index and reading a document are different permissions, and a
# schema hash binds one operation, so one record cannot cover both.
GUIDEPOINT_SEARCH_KIND = "guidepoint-search-library"
GUIDEPOINT_TRANSCRIPT_KIND = "guidepoint-get-transcript"
GUIDEPOINT_SEARCH_CAPABILITY_ID = (
    "capability:dalton:connector:guidepoint-search-library"
)
GUIDEPOINT_TRANSCRIPT_CAPABILITY_ID = (
    "capability:dalton:connector:guidepoint-get-transcript"
)
# P13ag: SEC financial statements, the same SEC read as statements rather than
# one concept at a time. Its own connector, schema and approval; shared source.
SEC_FINANCIALS_KIND = "sec-financial-statements"
SEC_FINANCIALS_CAPABILITY_ID = (
    "capability:dalton:connector:sec-financial-statements"
)
# P13ah: roic.ai transcripts, a second independent source for the calls the
# Playbook requires. Split in two like every other library here.
ROIC_LIST_KIND = "roic-list-transcripts"
ROIC_GET_KIND = "roic-get-transcript"
ROIC_LIST_CAPABILITY_ID = "capability:dalton:connector:roic-list-transcripts"
ROIC_GET_CAPABILITY_ID = "capability:dalton:connector:roic-get-transcript"
# P11a: Yahoo Finance. Prices are what a market printed; analyst estimates are
# what sell-side analysts said. C1 adds the calendar: when the company will
# next speak. Three kinds because they are three kinds of thing, and because a
# schema hash binds one operation.
YFINANCE_DAILY_PRICES_KIND = "yfinance-daily-prices"
YFINANCE_ANALYST_ESTIMATES_KIND = "yfinance-analyst-estimates"
YFINANCE_CALENDAR_KIND = "yfinance-calendar"
YFINANCE_DAILY_PRICES_CAPABILITY_ID = (
    "capability:dalton:connector:yfinance-daily-prices"
)
YFINANCE_ANALYST_ESTIMATES_CAPABILITY_ID = (
    "capability:dalton:connector:yfinance-analyst-estimates"
)
YFINANCE_CALENDAR_CAPABILITY_ID = "capability:dalton:connector:yfinance-calendar"
# S1: the two human / vendor feeds. Each is split into an index operation and
# a document operation for the same reason every library here is -- reading
# what exists and reading one of them are different permissions.
SALES_NOTES_LIST_KIND = "sales-notes-list-notes"
SALES_NOTES_GET_KIND = "sales-notes-get-note"
SALES_NOTES_LIST_CAPABILITY_ID = "capability:dalton:connector:sales-notes-list-notes"
SALES_NOTES_GET_CAPABILITY_ID = "capability:dalton:connector:sales-notes-get-note"
COMPANY_WIKI_LIST_KIND = "company-wiki-list-documents"
COMPANY_WIKI_GET_KIND = "company-wiki-get-document"
COMPANY_WIKI_LIST_CAPABILITY_ID = (
    "capability:dalton:connector:company-wiki-list-documents"
)
COMPANY_WIKI_GET_CAPABILITY_ID = "capability:dalton:connector:company-wiki-get-document"
# W3: the fund's own prior work on a company. Split the same way and for the
# same reason: listing what we already wrote about a name and opening one of
# those documents are two permissions.
PRIOR_RESEARCH_LIST_KIND = "prior-research-list-documents"
PRIOR_RESEARCH_GET_KIND = "prior-research-get-document"
PRIOR_RESEARCH_LIST_CAPABILITY_ID = (
    "capability:dalton:connector:prior-research-list-documents"
)
PRIOR_RESEARCH_GET_CAPABILITY_ID = (
    "capability:dalton:connector:prior-research-get-document"
)

# S3 crowd sources. Grouped rather than named one by one at the call site,
# because "which of these seven is it" is the only question the dispatcher asks.
XUEQIU_KINDS = frozenset({"xueqiu-search-posts", "xueqiu-get-post", "xueqiu-hot-rank"})
XREACH_KINDS = frozenset(
    {"x-xreach-user-timeline", "x-xreach-search", "x-xreach-thread"}
)
EMPLOYEE_REVIEWS_KIND = "employee-reviews-blind"

# S4: China / Hong Kong fundamentals through akshare. Six operations, six
# kinds, six approvals -- reading one company's income statement and reading
# the whole market's margin balance are not the same permission, and the kind
# names are derived from the operation so a seventh cannot be typed by hand.
CN_HK_FINDATA_KIND_BY_OPERATION: dict[str, str] = {}
CN_HK_FINDATA_CAPABILITY_BY_OPERATION: dict[str, str] = {}


class ConnectorGovernanceError(RuntimeError):
    """A malformed, unknown, or inactive connector governance record."""


class _KindSpec:
    """Lazy kind-specific identity and authority functions.

    The AlphaEngine functions live in the legacy acquisition module, which
    imports this module for its compatibility class.  Lazy callbacks avoid a
    module cycle while still making the registry the single dispatch table.
    """

    def __init__(
        self,
        *,
        capability_id: str,
        template_key: str,
        source_hash: Callable[[], str],
        schema_hash: Callable[[], str],
        permissions: Callable[[], dict[str, Any]],
        fixture_hash: Callable[[], str],
    ) -> None:
        self.capability_id = capability_id
        self.template_key = template_key
        self.source_hash = source_hash
        self.schema_hash = schema_hash
        self.permissions = permissions
        self.fixture_hash = fixture_hash


def _alpha_source_hash() -> str:
    from .alphaengine_core_acquisition import alphaengine_source_hash

    return alphaengine_source_hash()


def _alpha_schema_hash() -> str:
    from .alphaengine_core_acquisition import alphaengine_get_document_schema_hash

    return alphaengine_get_document_schema_hash()


def _alpha_permissions() -> dict[str, Any]:
    from .alphaengine_core_acquisition import live_alphaengine_permissions

    return copy.deepcopy(live_alphaengine_permissions())


def _alpha_fixture_hash() -> str:
    template = load_packaged_connector_inventory()["templates"]["alphaengine"]
    return template["fixture_manifest_hash"]


def _alpha_search_schema_hash() -> str:
    from .alphaengine_core_search import alphaengine_search_schema_hash

    return alphaengine_search_schema_hash()


def _guidepoint_source_hash() -> str:
    from .guidepoint_core import guidepoint_source_hash

    return guidepoint_source_hash()


def _guidepoint_permissions() -> dict[str, Any]:
    from .guidepoint_core import guidepoint_permissions

    return copy.deepcopy(guidepoint_permissions())


def _guidepoint_fixture_hash() -> str:
    from .guidepoint_core import guidepoint_fixture_hash

    return guidepoint_fixture_hash()


def _guidepoint_search_schema_hash() -> str:
    from .guidepoint_core import SEARCH_OPERATION, guidepoint_schema_hash

    return guidepoint_schema_hash(SEARCH_OPERATION)


def _guidepoint_transcript_schema_hash() -> str:
    from .guidepoint_core import TRANSCRIPT_OPERATION, guidepoint_schema_hash

    return guidepoint_schema_hash(TRANSCRIPT_OPERATION)


def _sec_financials_source_hash() -> str:
    from .sec_financials_core import sec_financials_source_hash

    return sec_financials_source_hash()


def _sec_financials_schema_hash() -> str:
    from .sec_financials_core import sec_financials_schema_hash

    return sec_financials_schema_hash()


def _sec_financials_permissions() -> dict[str, Any]:
    from .sec_financials_core import sec_financials_permissions

    return copy.deepcopy(sec_financials_permissions())


def _sec_financials_fixture_hash() -> str:
    from .sec_financials_core import sec_financials_fixture_hash

    return sec_financials_fixture_hash()


def _roic_source_hash() -> str:
    from .roic_transcript_core import roic_source_hash

    return roic_source_hash()


def _roic_permissions() -> dict[str, Any]:
    from .roic_transcript_core import roic_permissions

    return copy.deepcopy(roic_permissions())


def _roic_fixture_hash() -> str:
    from .roic_transcript_core import roic_fixture_hash

    return roic_fixture_hash()


def _roic_list_schema_hash() -> str:
    from .roic_transcript_core import LIST_OPERATION, roic_schema_hash

    return roic_schema_hash(LIST_OPERATION)


def _roic_get_schema_hash() -> str:
    from .roic_transcript_core import GET_OPERATION, roic_schema_hash

    return roic_schema_hash(GET_OPERATION)


def _yfinance_source_hash() -> str:
    from .yfinance_core import yfinance_source_hash

    return yfinance_source_hash()


def _yfinance_permissions() -> dict[str, Any]:
    from .yfinance_core import yfinance_permissions

    return copy.deepcopy(yfinance_permissions())


def _yfinance_fixture_hash() -> str:
    from .yfinance_core import yfinance_fixture_hash

    return yfinance_fixture_hash()


def _yfinance_daily_prices_schema_hash() -> str:
    from .yfinance_core import DAILY_PRICES_OPERATION, yfinance_schema_hash

    return yfinance_schema_hash(DAILY_PRICES_OPERATION)


def _yfinance_analyst_estimates_schema_hash() -> str:
    from .yfinance_core import ANALYST_ESTIMATES_OPERATION, yfinance_schema_hash

    return yfinance_schema_hash(ANALYST_ESTIMATES_OPERATION)


def _yfinance_calendar_schema_hash() -> str:
    from .yfinance_core import CALENDAR_OPERATION, yfinance_schema_hash

    return yfinance_schema_hash(CALENDAR_OPERATION)


def _cn_hk_findata_source_hash() -> str:
    from .cn_hk_findata_core import cn_hk_findata_source_hash

    return cn_hk_findata_source_hash()


def _cn_hk_findata_permissions() -> dict[str, Any]:
    from .cn_hk_findata_core import cn_hk_findata_permissions

    return copy.deepcopy(cn_hk_findata_permissions())


def _cn_hk_findata_fixture_hash() -> str:
    from .cn_hk_findata_core import cn_hk_findata_fixture_hash

    return cn_hk_findata_fixture_hash()


def _cn_hk_findata_schema_hash(operation: str) -> Callable[[], str]:
    """One thunk per operation, so six kinds do not share a schema hash."""

    def thunk() -> str:
        from .cn_hk_findata_core import cn_hk_findata_schema_hash

        return cn_hk_findata_schema_hash(operation)

    return thunk


def _sec_identity() -> dict[str, Any]:
    from .research_plan_executor import sec_connector_identity

    inventory = load_packaged_connector_inventory()
    return sec_connector_identity(inventory["templates"]["sec"], "get_company_facts")


def _sec_source_hash() -> str:
    return _sec_identity()["source_hash"]


def _sec_schema_hash() -> str:
    return _sec_identity()["schema_hash"]


def _sec_filings_index_schema_hash() -> str:
    from .sec_filings_index import filings_index_schema_hash

    return filings_index_schema_hash()


def _sec_permissions() -> dict[str, Any]:
    # Keep this a deep copy: callers must not be able to mutate the harness'
    # frozen public permission declaration through a governance object.
    from .sec_authority_harness import PUBLIC_PERMISSIONS

    return copy.deepcopy(PUBLIC_PERMISSIONS)


def _sec_fixture_hash() -> str:
    return load_packaged_connector_inventory()["templates"]["sec"][
        "fixture_manifest_hash"
    ]


def _web_search_source_hash() -> str:
    from .public_web_core_search import web_search_source_hash

    return web_search_source_hash()


def _web_search_schema_hash() -> str:
    from .public_web_core_search import web_search_schema_hash

    return web_search_schema_hash()


def _web_search_permissions() -> dict[str, Any]:
    from .public_web_core_search import web_search_permissions

    return copy.deepcopy(web_search_permissions())


def _web_search_fixture_hash() -> str:
    from .public_web_core_search import web_search_fixture_hash

    return web_search_fixture_hash()


def _web_fetch_source_hash() -> str:
    from .public_web_core_fetch import web_fetch_source_hash

    return web_fetch_source_hash()


def _web_fetch_schema_hash() -> str:
    from .public_web_core_fetch import web_fetch_schema_hash

    return web_fetch_schema_hash()


def _web_fetch_permissions() -> dict[str, Any]:
    from .public_web_core_fetch import web_fetch_permissions

    return copy.deepcopy(web_fetch_permissions())


def _web_fetch_fixture_hash() -> str:
    from .public_web_core_fetch import web_fetch_fixture_hash

    return web_fetch_fixture_hash()


# S3: the crowd connectors. Seven kinds, because there are seven operations and
# a schema hash binds exactly one of them -- approving the Xueqiu post search is
# not approving the Xueqiu post read, and approving either is certainly not
# approving X. The source hash is shared inside each connector, because the same
# Xueqiu and the same X are the same sources.
def _xueqiu_source_hash() -> str:
    from .xueqiu_core import xueqiu_source_hash

    return xueqiu_source_hash()


def _xueqiu_permissions() -> dict[str, Any]:
    from .xueqiu_core import xueqiu_permissions

    return copy.deepcopy(xueqiu_permissions())


def _xueqiu_fixture_hash() -> str:
    from .xueqiu_core import xueqiu_fixture_hash

    return xueqiu_fixture_hash()


def _xueqiu_schema_hash_for(operation: str) -> Callable[[], str]:
    def hasher() -> str:
        from .xueqiu_core import xueqiu_schema_hash

        return xueqiu_schema_hash(operation)

    return hasher


def _xreach_source_hash() -> str:
    from .xreach_core import xreach_source_hash

    return xreach_source_hash()


def _xreach_permissions() -> dict[str, Any]:
    from .xreach_core import xreach_permissions

    return copy.deepcopy(xreach_permissions())


def _xreach_fixture_hash() -> str:
    from .xreach_core import xreach_fixture_hash

    return xreach_fixture_hash()


def _xreach_schema_hash_for(operation: str) -> Callable[[], str]:
    def hasher() -> str:
        from .xreach_core import xreach_schema_hash

        return xreach_schema_hash(operation)

    return hasher


def _employee_reviews_source_hash() -> str:
    from .employee_reviews_core import employee_reviews_source_hash

    return employee_reviews_source_hash()


def _employee_reviews_schema_hash() -> str:
    from .employee_reviews_core import employee_reviews_schema_hash

    return employee_reviews_schema_hash()


def _employee_reviews_permissions() -> dict[str, Any]:
    from .employee_reviews_core import employee_reviews_permissions

    return copy.deepcopy(employee_reviews_permissions())


def _employee_reviews_fixture_hash() -> str:
    from .employee_reviews_core import employee_reviews_fixture_hash

    return employee_reviews_fixture_hash()


def _sales_notes_source_hash() -> str:
    from .sales_notes_core import sales_notes_source_hash

    return sales_notes_source_hash()


def _sales_notes_permissions() -> dict[str, Any]:
    from .sales_notes_core import sales_notes_permissions

    return sales_notes_permissions()


def _sales_notes_fixture_hash() -> str:
    from .sales_notes_core import sales_notes_fixture_hash

    return sales_notes_fixture_hash()


def _sales_notes_list_schema_hash() -> str:
    from .sales_notes_core import LIST_OPERATION, sales_notes_schema_hash

    return sales_notes_schema_hash(LIST_OPERATION)


def _sales_notes_get_schema_hash() -> str:
    from .sales_notes_core import GET_OPERATION, sales_notes_schema_hash

    return sales_notes_schema_hash(GET_OPERATION)


def _company_wiki_source_hash() -> str:
    from .company_wiki_core import company_wiki_source_hash

    return company_wiki_source_hash()


def _company_wiki_permissions() -> dict[str, Any]:
    from .company_wiki_core import company_wiki_permissions

    return company_wiki_permissions()


def _company_wiki_fixture_hash() -> str:
    from .company_wiki_core import company_wiki_fixture_hash

    return company_wiki_fixture_hash()


def _company_wiki_list_schema_hash() -> str:
    from .company_wiki_core import LIST_OPERATION, company_wiki_schema_hash

    return company_wiki_schema_hash(LIST_OPERATION)


def _company_wiki_get_schema_hash() -> str:
    from .company_wiki_core import GET_OPERATION, company_wiki_schema_hash

    return company_wiki_schema_hash(GET_OPERATION)


def _prior_research_source_hash() -> str:
    from .prior_research_core import prior_research_source_hash

    return prior_research_source_hash()


def _prior_research_permissions() -> dict[str, Any]:
    from .prior_research_core import prior_research_permissions

    return prior_research_permissions()


def _prior_research_fixture_hash() -> str:
    from .prior_research_core import prior_research_fixture_hash

    return prior_research_fixture_hash()


def _prior_research_list_schema_hash() -> str:
    from .prior_research_core import LIST_OPERATION, prior_research_schema_hash

    return prior_research_schema_hash(LIST_OPERATION)


def _prior_research_get_schema_hash() -> str:
    from .prior_research_core import GET_OPERATION, prior_research_schema_hash

    return prior_research_schema_hash(GET_OPERATION)


# Capability id is deliberately the dispatch key at load time because it is
# the only kind identity present in the closed governance record.
GOVERNANCE_KIND_REGISTRY: dict[str, _KindSpec] = {
    ALPHAENGINE_KIND: _KindSpec(
        capability_id=ALPHAENGINE_CAPABILITY_ID,
        template_key="alphaengine",
        source_hash=_alpha_source_hash,
        schema_hash=_alpha_schema_hash,
        permissions=_alpha_permissions,
        fixture_hash=_alpha_fixture_hash,
    ),
    # P9d-1: the same AlphaEngine template's ``search_library`` operation is a
    # separate capability with its own approval; the get_document record
    # cannot be reused because its schema hash binds one operation only.
    ALPHAENGINE_SEARCH_KIND: _KindSpec(
        capability_id=ALPHAENGINE_SEARCH_CAPABILITY_ID,
        template_key="alphaengine",
        source_hash=_alpha_source_hash,
        schema_hash=_alpha_search_schema_hash,
        permissions=_alpha_permissions,
        fixture_hash=_alpha_fixture_hash,
    ),
    # P10e: the same SEC template's ``list_filings`` operation is a separate
    # capability with its own approval, exactly as P9d-1 split AlphaEngine.
    # The source hash is shared because the source is the same SEC; the schema
    # hash binds one operation only, so neither approval widens into the other.
    SEC_FILINGS_INDEX_KIND: _KindSpec(
        capability_id=SEC_FILINGS_INDEX_CAPABILITY_ID,
        template_key="sec",
        source_hash=_sec_source_hash,
        schema_hash=_sec_filings_index_schema_hash,
        permissions=_sec_permissions,
        fixture_hash=_sec_fixture_hash,
    ),
    SEC_COMPANY_FACTS_KIND: _KindSpec(
        capability_id=SEC_CAPABILITY_ID,
        template_key="sec",
        source_hash=_sec_source_hash,
        schema_hash=_sec_schema_hash,
        permissions=_sec_permissions,
        fixture_hash=_sec_fixture_hash,
    ),
    # P9d-4a: OpenClaw's host-owned Gemini web_search is its own governed
    # capability (discovery-only public-web source), approved separately.
    GEMINI_WEB_SEARCH_KIND: _KindSpec(
        capability_id=GEMINI_WEB_SEARCH_CAPABILITY_ID,
        template_key="gemini-web-search",
        source_hash=_web_search_source_hash,
        schema_hash=_web_search_schema_hash,
        permissions=_web_search_permissions,
        fixture_hash=_web_search_fixture_hash,
    ),
    # P9d-4b: credential-free public-web fetch_get of URLs a web search cited.
    WEB_FETCH_KIND: _KindSpec(
        capability_id=WEB_FETCH_CAPABILITY_ID,
        template_key="web-fetch",
        source_hash=_web_fetch_source_hash,
        schema_hash=_web_fetch_schema_hash,
        permissions=_web_fetch_permissions,
        fixture_hash=_web_fetch_fixture_hash,
    ),
    # P13ae: the expert-network library, split the way AlphaEngine is. Reading
    # the index and reading a transcript are different permissions; approving
    # the search is not approving the reading.
    GUIDEPOINT_SEARCH_KIND: _KindSpec(
        capability_id=GUIDEPOINT_SEARCH_CAPABILITY_ID,
        template_key="guidepoint",
        source_hash=_guidepoint_source_hash,
        schema_hash=_guidepoint_search_schema_hash,
        permissions=_guidepoint_permissions,
        fixture_hash=_guidepoint_fixture_hash,
    ),
    GUIDEPOINT_TRANSCRIPT_KIND: _KindSpec(
        capability_id=GUIDEPOINT_TRANSCRIPT_CAPABILITY_ID,
        template_key="guidepoint",
        source_hash=_guidepoint_source_hash,
        schema_hash=_guidepoint_transcript_schema_hash,
        permissions=_guidepoint_permissions,
        fixture_hash=_guidepoint_fixture_hash,
    ),
    SEC_FINANCIALS_KIND: _KindSpec(
        capability_id=SEC_FINANCIALS_CAPABILITY_ID,
        template_key="sec-financials",
        source_hash=_sec_financials_source_hash,
        schema_hash=_sec_financials_schema_hash,
        permissions=_sec_financials_permissions,
        fixture_hash=_sec_financials_fixture_hash,
    ),
    ROIC_LIST_KIND: _KindSpec(
        capability_id=ROIC_LIST_CAPABILITY_ID,
        template_key="roic-transcript",
        source_hash=_roic_source_hash,
        schema_hash=_roic_list_schema_hash,
        permissions=_roic_permissions,
        fixture_hash=_roic_fixture_hash,
    ),
    ROIC_GET_KIND: _KindSpec(
        capability_id=ROIC_GET_CAPABILITY_ID,
        template_key="roic-transcript",
        source_hash=_roic_source_hash,
        schema_hash=_roic_get_schema_hash,
        permissions=_roic_permissions,
        fixture_hash=_roic_fixture_hash,
    ),
    "xueqiu-search-posts": _KindSpec(
        capability_id="capability:dalton:connector:xueqiu-search-posts",
        template_key="xueqiu-posts",
        source_hash=_xueqiu_source_hash,
        schema_hash=_xueqiu_schema_hash_for("search_posts"),
        permissions=_xueqiu_permissions,
        fixture_hash=_xueqiu_fixture_hash,
    ),
    "xueqiu-get-post": _KindSpec(
        capability_id="capability:dalton:connector:xueqiu-get-post",
        template_key="xueqiu-posts",
        source_hash=_xueqiu_source_hash,
        schema_hash=_xueqiu_schema_hash_for("get_post"),
        permissions=_xueqiu_permissions,
        fixture_hash=_xueqiu_fixture_hash,
    ),
    "xueqiu-hot-rank": _KindSpec(
        capability_id="capability:dalton:connector:xueqiu-hot-rank",
        template_key="xueqiu-posts",
        source_hash=_xueqiu_source_hash,
        schema_hash=_xueqiu_schema_hash_for("hot_rank"),
        permissions=_xueqiu_permissions,
        fixture_hash=_xueqiu_fixture_hash,
    ),
    "x-xreach-user-timeline": _KindSpec(
        capability_id="capability:dalton:connector:x-xreach-user-timeline",
        template_key="x-xreach-crowd",
        source_hash=_xreach_source_hash,
        schema_hash=_xreach_schema_hash_for("user_timeline"),
        permissions=_xreach_permissions,
        fixture_hash=_xreach_fixture_hash,
    ),
    "x-xreach-search": _KindSpec(
        capability_id="capability:dalton:connector:x-xreach-search",
        template_key="x-xreach-crowd",
        source_hash=_xreach_source_hash,
        schema_hash=_xreach_schema_hash_for("search"),
        permissions=_xreach_permissions,
        fixture_hash=_xreach_fixture_hash,
    ),
    "x-xreach-thread": _KindSpec(
        capability_id="capability:dalton:connector:x-xreach-thread",
        template_key="x-xreach-crowd",
        source_hash=_xreach_source_hash,
        schema_hash=_xreach_schema_hash_for("thread"),
        permissions=_xreach_permissions,
        fixture_hash=_xreach_fixture_hash,
    ),
    "employee-reviews-blind": _KindSpec(
        capability_id="capability:dalton:connector:employee-reviews-blind",
        template_key="employee-reviews",
        source_hash=_employee_reviews_source_hash,
        schema_hash=_employee_reviews_schema_hash,
        permissions=_employee_reviews_permissions,
        fixture_hash=_employee_reviews_fixture_hash,
    ),
    # P11a: the market layer's source. Unofficial and free; the quota is small
    # and the approval is per operation.
    YFINANCE_DAILY_PRICES_KIND: _KindSpec(
        capability_id=YFINANCE_DAILY_PRICES_CAPABILITY_ID,
        template_key="yfinance",
        source_hash=_yfinance_source_hash,
        schema_hash=_yfinance_daily_prices_schema_hash,
        permissions=_yfinance_permissions,
        fixture_hash=_yfinance_fixture_hash,
    ),
    YFINANCE_ANALYST_ESTIMATES_KIND: _KindSpec(
        capability_id=YFINANCE_ANALYST_ESTIMATES_CAPABILITY_ID,
        template_key="yfinance",
        source_hash=_yfinance_source_hash,
        schema_hash=_yfinance_analyst_estimates_schema_hash,
        permissions=_yfinance_permissions,
        fixture_hash=_yfinance_fixture_hash,
    ),
    # C1: the dated corporate events. Its own approval, so that a Core allowed
    # to read prices is not thereby allowed to read anything else Yahoo serves.
    YFINANCE_CALENDAR_KIND: _KindSpec(
        capability_id=YFINANCE_CALENDAR_CAPABILITY_ID,
        template_key="yfinance",
        source_hash=_yfinance_source_hash,
        schema_hash=_yfinance_calendar_schema_hash,
        permissions=_yfinance_permissions,
        fixture_hash=_yfinance_fixture_hash,
    ),
    # S1: the sell-side notes already on this disk, and the human wiki beside
    # them. Both read files and neither holds a credential, so their
    # permissions name a host directory and no slot at all.
    SALES_NOTES_LIST_KIND: _KindSpec(
        capability_id=SALES_NOTES_LIST_CAPABILITY_ID,
        template_key="sales-notes",
        source_hash=_sales_notes_source_hash,
        schema_hash=_sales_notes_list_schema_hash,
        permissions=_sales_notes_permissions,
        fixture_hash=_sales_notes_fixture_hash,
    ),
    SALES_NOTES_GET_KIND: _KindSpec(
        capability_id=SALES_NOTES_GET_CAPABILITY_ID,
        template_key="sales-notes",
        source_hash=_sales_notes_source_hash,
        schema_hash=_sales_notes_get_schema_hash,
        permissions=_sales_notes_permissions,
        fixture_hash=_sales_notes_fixture_hash,
    ),
    COMPANY_WIKI_LIST_KIND: _KindSpec(
        capability_id=COMPANY_WIKI_LIST_CAPABILITY_ID,
        template_key="company-wiki",
        source_hash=_company_wiki_source_hash,
        schema_hash=_company_wiki_list_schema_hash,
        permissions=_company_wiki_permissions,
        fixture_hash=_company_wiki_fixture_hash,
    ),
    COMPANY_WIKI_GET_KIND: _KindSpec(
        capability_id=COMPANY_WIKI_GET_CAPABILITY_ID,
        template_key="company-wiki",
        source_hash=_company_wiki_source_hash,
        schema_hash=_company_wiki_get_schema_hash,
        permissions=_company_wiki_permissions,
        fixture_hash=_company_wiki_fixture_hash,
    ),
    PRIOR_RESEARCH_LIST_KIND: _KindSpec(
        capability_id=PRIOR_RESEARCH_LIST_CAPABILITY_ID,
        template_key="prior-research",
        source_hash=_prior_research_source_hash,
        schema_hash=_prior_research_list_schema_hash,
        permissions=_prior_research_permissions,
        fixture_hash=_prior_research_fixture_hash,
    ),
    PRIOR_RESEARCH_GET_KIND: _KindSpec(
        capability_id=PRIOR_RESEARCH_GET_CAPABILITY_ID,
        template_key="prior-research",
        source_hash=_prior_research_source_hash,
        schema_hash=_prior_research_get_schema_hash,
        permissions=_prior_research_permissions,
        fixture_hash=_prior_research_fixture_hash,
    ),
}


def _register_cn_hk_findata_kinds() -> None:
    """Register the six China / Hong Kong kinds from the frozen operation list.

    Written as a loop rather than six near-identical literals because the six
    differ in exactly one thing -- the schema hash -- and a copied block that
    forgets to change it produces an approval that silently covers the wrong
    operation. The import is local for the same reason every other identity
    module's is: ``cn_hk_findata_core`` imports this module's error type.
    """

    from .cn_hk_findata_core import (
        CAPABILITY_BY_OPERATION as _CN_HK_CAPABILITIES,
        KIND_BY_OPERATION as _CN_HK_KINDS,
    )

    for operation, kind in _CN_HK_KINDS.items():
        capability_id = _CN_HK_CAPABILITIES[operation]
        CN_HK_FINDATA_KIND_BY_OPERATION[operation] = kind
        CN_HK_FINDATA_CAPABILITY_BY_OPERATION[operation] = capability_id
        GOVERNANCE_KIND_REGISTRY[kind] = _KindSpec(
            capability_id=capability_id,
            template_key="cn-hk-findata",
            source_hash=_cn_hk_findata_source_hash,
            schema_hash=_cn_hk_findata_schema_hash(operation),
            permissions=_cn_hk_findata_permissions,
            fixture_hash=_cn_hk_findata_fixture_hash,
        )


_register_cn_hk_findata_kinds()
# Public aliases make the registry discoverable without exposing mutable
# implementation details of a spec.  The old name is useful to callers that
# treat the set as a connector-kind catalog.
CONNECTOR_GOVERNANCE_KINDS = GOVERNANCE_KIND_REGISTRY
GOVERNANCE_KINDS = GOVERNANCE_KIND_REGISTRY

_CAPABILITY_TO_KIND = {
    spec.capability_id: kind for kind, spec in GOVERNANCE_KIND_REGISTRY.items()
}


def governance_kind_for_capability(capability_id: str) -> str:
    try:
        return _CAPABILITY_TO_KIND[capability_id]
    except (KeyError, TypeError) as exc:
        raise ConnectorGovernanceError(
            "governance capability_id is not a registered connector"
        ) from exc


def _kind_spec(kind: str) -> _KindSpec:
    try:
        return GOVERNANCE_KIND_REGISTRY[kind]
    except (KeyError, TypeError) as exc:
        raise ConnectorGovernanceError(f"unknown connector governance kind: {kind}") from exc


def _wire_time(value: str) -> str:
    from datetime import datetime, timezone

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ConnectorGovernanceError("effective_from must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise ConnectorGovernanceError("effective_from must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _with_hash(value: Mapping[str, Any]) -> dict[str, Any]:
    wire = json.loads(canonical_json(value))
    wire["content_hash"] = content_hash(wire)
    return wire


def build_governance_record(
    kind: str,
    *,
    approved_by: str,
    status: str = "proposed",
    effective_from: str = "2026-08-26T00:00:00+00:00",
    max_lease_seconds: int = 120,
    version: int = 1,
) -> dict[str, Any]:
    """Build one closed governance record for a registered connector kind.

    AlphaEngine delegates to its original builder intentionally: this is the
    compatibility promise that old live records remain byte-for-byte stable.
    """

    _kind_spec(kind)
    if kind == ALPHAENGINE_KIND:
        from .alphaengine_core_acquisition import (
            build_governance_record as build_alphaengine_governance_record,
        )

        return build_alphaengine_governance_record(
            approved_by=approved_by,
            status=status,
            effective_from=effective_from,
            max_lease_seconds=max_lease_seconds,
            version=version,
        )

    if kind == ALPHAENGINE_SEARCH_KIND:
        from .alphaengine_core_search import build_search_governance_record

        return build_search_governance_record(
            approved_by=approved_by,
            status=status,
            effective_from=effective_from,
            max_lease_seconds=max_lease_seconds,
            version=version,
        )

    if kind == GEMINI_WEB_SEARCH_KIND:
        from .public_web_core_search import build_web_search_governance_record

        return build_web_search_governance_record(
            approved_by=approved_by,
            status=status,
            effective_from=effective_from,
            max_lease_seconds=max_lease_seconds,
            version=version,
        )

    if kind == SEC_FILINGS_INDEX_KIND:
        from .sec_filings_index import build_filings_index_governance_record

        return build_filings_index_governance_record(
            approved_by=approved_by,
            status=status,
            effective_from=effective_from,
            max_lease_seconds=max_lease_seconds,
            version=version,
        )

    if kind == WEB_FETCH_KIND:
        from .public_web_core_fetch import build_web_fetch_governance_record

        return build_web_fetch_governance_record(
            approved_by=approved_by,
            status=status,
            effective_from=effective_from,
            max_lease_seconds=max_lease_seconds,
            version=version,
        )

    if kind in (ROIC_LIST_KIND, ROIC_GET_KIND):
        from .roic_transcript_core import (
            KIND_BY_OPERATION as ROIC_KINDS,
            build_roic_governance_record,
        )

        operation = next(op for op, name in ROIC_KINDS.items() if name == kind)
        return build_roic_governance_record(
            operation=operation,
            approved_by=approved_by,
            status=status,
            effective_from=effective_from,
            max_lease_seconds=max_lease_seconds,
            version=version,
        )

    if kind in (YFINANCE_DAILY_PRICES_KIND, YFINANCE_ANALYST_ESTIMATES_KIND,
                YFINANCE_CALENDAR_KIND):
        from .yfinance_core import (
            KIND_BY_OPERATION as YFINANCE_KINDS,
            build_yfinance_governance_record,
        )

        operation = next(op for op, name in YFINANCE_KINDS.items() if name == kind)
        return build_yfinance_governance_record(
            operation=operation,
            approved_by=approved_by,
            status=status,
            effective_from=effective_from,
            max_lease_seconds=max_lease_seconds,
            version=version,
        )

    if kind in CN_HK_FINDATA_KIND_BY_OPERATION.values():
        from .cn_hk_findata_core import (
            OPERATION_BY_KIND as CN_HK_OPERATIONS,
            build_cn_hk_findata_governance_record,
        )

        return build_cn_hk_findata_governance_record(
            operation=CN_HK_OPERATIONS[kind],
            approved_by=approved_by,
            status=status,
            effective_from=effective_from,
            max_lease_seconds=max_lease_seconds,
            version=version,
        )

    if kind == SEC_FINANCIALS_KIND:
        from .sec_financials_core import build_sec_financials_governance_record

        return build_sec_financials_governance_record(
            approved_by=approved_by,
            status=status,
            effective_from=effective_from,
            max_lease_seconds=max_lease_seconds,
            version=version,
        )

    if kind in XUEQIU_KINDS:
        from .xueqiu_core import KIND_BY_OPERATION as XUEQIU_BY_OPERATION
        from .xueqiu_core import build_xueqiu_governance_record

        operation = next(op for op, name in XUEQIU_BY_OPERATION.items() if name == kind)
        return build_xueqiu_governance_record(
            operation=operation,
            approved_by=approved_by,
            status=status,
            effective_from=effective_from,
            max_lease_seconds=max_lease_seconds,
            version=version,
        )

    if kind in XREACH_KINDS:
        from .xreach_core import KIND_BY_OPERATION as XREACH_BY_OPERATION
        from .xreach_core import build_xreach_governance_record

        operation = next(op for op, name in XREACH_BY_OPERATION.items() if name == kind)
        return build_xreach_governance_record(
            operation=operation,
            approved_by=approved_by,
            status=status,
            effective_from=effective_from,
            max_lease_seconds=max_lease_seconds,
            version=version,
        )

    if kind == EMPLOYEE_REVIEWS_KIND:
        from .employee_reviews_core import build_employee_reviews_governance_record

        return build_employee_reviews_governance_record(
            approved_by=approved_by,
            status=status,
            effective_from=effective_from,
            max_lease_seconds=max_lease_seconds,
            version=version,
        )

    if kind in (GUIDEPOINT_SEARCH_KIND, GUIDEPOINT_TRANSCRIPT_KIND):
        from .guidepoint_core import (
            KIND_BY_OPERATION,
            build_guidepoint_governance_record,
        )

        operation = next(op for op, name in KIND_BY_OPERATION.items() if name == kind)
        return build_guidepoint_governance_record(
            operation=operation,
            approved_by=approved_by,
            status=status,
            effective_from=effective_from,
            max_lease_seconds=max_lease_seconds,
            version=version,
        )

    if kind in (SALES_NOTES_LIST_KIND, SALES_NOTES_GET_KIND):
        from .sales_notes_core import (
            KIND_BY_OPERATION as SALES_NOTES_KINDS,
            build_sales_notes_governance_record,
        )

        operation = next(op for op, name in SALES_NOTES_KINDS.items() if name == kind)
        return build_sales_notes_governance_record(
            operation=operation,
            approved_by=approved_by,
            status=status,
            effective_from=effective_from,
            max_lease_seconds=max_lease_seconds,
            version=version,
        )

    if kind in (COMPANY_WIKI_LIST_KIND, COMPANY_WIKI_GET_KIND):
        from .company_wiki_core import (
            KIND_BY_OPERATION as COMPANY_WIKI_KINDS,
            build_company_wiki_governance_record,
        )

        operation = next(op for op, name in COMPANY_WIKI_KINDS.items() if name == kind)
        return build_company_wiki_governance_record(
            operation=operation,
            approved_by=approved_by,
            status=status,
            effective_from=effective_from,
            max_lease_seconds=max_lease_seconds,
            version=version,
        )

    if kind in (PRIOR_RESEARCH_LIST_KIND, PRIOR_RESEARCH_GET_KIND):
        from .prior_research_core import (
            KIND_BY_OPERATION as PRIOR_RESEARCH_KINDS,
            build_prior_research_governance_record,
        )

        operation = next(
            op for op, name in PRIOR_RESEARCH_KINDS.items() if name == kind
        )
        return build_prior_research_governance_record(
            operation=operation,
            approved_by=approved_by,
            status=status,
            effective_from=effective_from,
            max_lease_seconds=max_lease_seconds,
            version=version,
        )

    if kind != SEC_COMPANY_FACTS_KIND:  # registry guard; defensive for future kinds
        raise ConnectorGovernanceError(f"unsupported connector governance kind: {kind}")
    spec = GOVERNANCE_KIND_REGISTRY[kind]
    base = {
        "schema_version": GOVERNANCE_SCHEMA_VERSION,
        "id": f"connector-governance:sec-company-facts:v{version}",
        "status": status,
        "capability_id": spec.capability_id,
        "approved_by": approved_by,
        "principal_ref": "principal:dalton-core-trusted-runner",
        "policy_ref": f"policy:dalton:connector-governance:sec-company-facts:v{version}",
        "approval_ref": f"approval:connector-governance:sec-company-facts:v{version}",
        "decision_ref": f"capability-decision:connector-governance:sec-company-facts:v{version}",
        "registry_revision_ref": f"{spec.capability_id}@v{version}",
        "attestation_ref": f"attestation:connector-governance:sec-company-facts:v{version}",
        "effective_from": _wire_time(effective_from),
        "effective_until": None,
        "max_lease_seconds": max_lease_seconds,
        "allowed_permissions": spec.permissions(),
        "expected_source_hash": spec.source_hash(),
        "expected_schema_hash": spec.schema_hash(),
    }
    return _with_hash(base)


class ConnectorGovernance:
    """Static generic approval and policy authority for a connector record."""

    def __init__(self, value: Mapping[str, Any]) -> None:
        if not isinstance(value, Mapping) or set(value) != set(GOVERNANCE_FIELDS):
            raise ConnectorGovernanceError(
                "connector governance record has an invalid closed shape"
            )
        wire = json.loads(canonical_json(value))
        if wire["schema_version"] != GOVERNANCE_SCHEMA_VERSION:
            raise ConnectorGovernanceError("unsupported governance schema_version")
        if wire["status"] not in GOVERNANCE_STATUSES:
            raise ConnectorGovernanceError("governance status is invalid")
        kind = governance_kind_for_capability(wire["capability_id"])
        spec = GOVERNANCE_KIND_REGISTRY[kind]
        approved_by = wire["approved_by"]
        if (
            not isinstance(approved_by, str)
            or not approved_by.startswith("human:")
            or len(approved_by) <= 6
        ):
            raise ConnectorGovernanceError("governance approved_by must be a human actor")
        for name in (
            "id", "principal_ref", "policy_ref", "approval_ref", "decision_ref",
            "registry_revision_ref", "attestation_ref",
        ):
            if not isinstance(wire[name], str) or ":" not in wire[name]:
                raise ConnectorGovernanceError(f"governance {name} must be a namespaced ref")
        self._parse_time(wire["effective_from"], "effective_from")
        if wire["effective_until"] is not None:
            until = self._parse_time(wire["effective_until"], "effective_until")
            if until <= self._parse_time(wire["effective_from"], "effective_from"):
                raise ConnectorGovernanceError("governance interval is invalid")
        lease = wire["max_lease_seconds"]
        if isinstance(lease, bool) or not isinstance(lease, int) or lease < 1:
            raise ConnectorGovernanceError("max_lease_seconds must be a positive integer")
        try:
            CapabilityPermissions.from_dict(wire["allowed_permissions"])
        except Exception as exc:
            raise ConnectorGovernanceError("governance allowed_permissions are invalid") from exc
        for name in ("expected_source_hash", "expected_schema_hash"):
            if not isinstance(wire[name], str) or len(wire[name]) != 64:
                raise ConnectorGovernanceError(f"governance {name} must be SHA-256 hex")
        declared = wire.pop("content_hash")
        if content_hash(wire) != declared:
            raise ConnectorGovernanceError("governance content_hash mismatch")
        wire["content_hash"] = declared
        self.wire = wire
        self._kind = kind
        self._spec = spec

    @staticmethod
    def _parse_time(value: str, name: str):
        from datetime import datetime, timezone

        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ConnectorGovernanceError(f"{name} must be RFC3339") from exc
        if parsed.tzinfo is None:
            raise ConnectorGovernanceError(f"{name} must include a timezone")
        return parsed.astimezone(timezone.utc)

    @classmethod
    def load(cls, path: str | Path) -> "ConnectorGovernance":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    @property
    def kind(self) -> str:
        return self._kind

    @property
    def capability_id(self) -> str:
        return self.wire["capability_id"]

    @property
    def id(self) -> str:
        return self.wire["id"]

    @property
    def content_hash(self) -> str:
        return self.wire["content_hash"]

    @property
    def status(self) -> str:
        return self.wire["status"]

    @property
    def approved(self) -> bool:
        return self.status == "approved"

    @property
    def principal_ref(self) -> str:
        return self.wire["principal_ref"]

    @property
    def policy_ref(self) -> str:
        return self.wire["policy_ref"]

    @property
    def approved_by(self) -> str:
        return self.wire["approved_by"]

    @property
    def effective_from(self) -> str:
        return self.wire["effective_from"]

    @property
    def allowed_permissions(self) -> dict[str, Any]:
        return copy.deepcopy(self.wire["allowed_permissions"])

    def _require_approved(self) -> None:
        if not self.approved:
            raise ConnectorGovernanceError(
                f"connector governance {self.id} is {self.status}; owner approval is required"
            )

    def approval(self, query: Mapping[str, Any]) -> dict[str, Any] | None:
        """Resolve a hash-bound approval receipt for the registered kind."""

        self._require_approved()
        if (
            query.get("capability_id") != self.capability_id
            or query.get("source_hash") != self.wire["expected_source_hash"]
            or query.get("schema_hash") != self.wire["expected_schema_hash"]
        ):
            return None
        receipt = {
            "schema_version": "0.1",
            "approval_ref": self.wire["approval_ref"],
            "capability_id": self.capability_id,
            "registry_revision_ref": self.wire["registry_revision_ref"],
            "artifact_ref": query["source_ref"],
            "artifact_hash": query["source_hash"],
            "schema_hash": query["schema_hash"],
            "fixture_manifest_hash": self._spec.fixture_hash(),
            "attestation_ref": self.wire["attestation_ref"],
            "attestation_hash": content_hash(
                {"governance_id": self.id, "governance_hash": self.content_hash}
            ),
            "decision_ref": self.wire["decision_ref"],
            "decision": "approve",
            "approved_by": self.approved_by,
            "approved_permissions": self.allowed_permissions,
            "active": True,
            "effective_from": self.wire["effective_from"],
            "effective_until": self.wire["effective_until"],
        }
        receipt["receipt_hash"] = canonical_hash(receipt)
        return receipt

    def policy(self, query: Mapping[str, Any]) -> dict[str, Any] | None:
        """Resolve the hash-bound lease policy for the registered kind."""

        self._require_approved()
        if query.get("policy_ref") != self.policy_ref:
            return None
        wire = {
            "schema_version": "0.1",
            "policy_ref": self.policy_ref,
            "effective_from": self.wire["effective_from"],
            "effective_until": self.wire["effective_until"],
            "allowed_principal_refs": [self.principal_ref],
            "allowed_permissions": self.allowed_permissions,
            "max_lease_seconds": self.wire["max_lease_seconds"],
        }
        wire["content_hash"] = canonical_hash(wire)
        return wire

    def policy_hash(self) -> str:
        policy = self.policy({"policy_ref": self.policy_ref})
        assert policy is not None
        return policy["content_hash"]


def load_connector_governance(path: str | Path) -> ConnectorGovernance:
    """Load a generic governance record and dispatch its registered kind."""

    return ConnectorGovernance.load(path)


def write_governance_proposal(
    path: str | Path,
    *,
    kind: str,
    approved_by: str,
    effective_from: str = "2026-08-26T00:00:00+00:00",
    max_lease_seconds: int = 120,
    version: int = 1,
) -> dict[str, Any]:
    """Create a proposed record without overwriting an existing file."""

    target = Path(path)
    if target.exists():
        raise FileExistsError(str(target))
    record = build_governance_record(
        kind,
        approved_by=approved_by,
        status="proposed",
        effective_from=effective_from,
        max_lease_seconds=max_lease_seconds,
        version=version,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation closes the check/write race and keeps owner files
    # private from the moment they are created.
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(canonical_json(record) + "\n")
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    return record


__all__ = [
    "ALPHAENGINE_CAPABILITY_ID", "ALPHAENGINE_KIND", "CONNECTOR_GOVERNANCE_KINDS",
    "ConnectorGovernance", "ConnectorGovernanceError", "GOVERNANCE_FIELDS",
    "GOVERNANCE_KIND_REGISTRY", "GOVERNANCE_KINDS", "GOVERNANCE_SCHEMA_VERSION",
    "GEMINI_WEB_SEARCH_CAPABILITY_ID", "GEMINI_WEB_SEARCH_KIND",
    "WEB_FETCH_CAPABILITY_ID", "WEB_FETCH_KIND",
    "YFINANCE_ANALYST_ESTIMATES_CAPABILITY_ID", "YFINANCE_ANALYST_ESTIMATES_KIND",
    "YFINANCE_CALENDAR_CAPABILITY_ID", "YFINANCE_CALENDAR_KIND",
    "YFINANCE_DAILY_PRICES_CAPABILITY_ID", "YFINANCE_DAILY_PRICES_KIND",
    "SALES_NOTES_GET_CAPABILITY_ID", "SALES_NOTES_GET_KIND",
    "SALES_NOTES_LIST_CAPABILITY_ID", "SALES_NOTES_LIST_KIND",
    "COMPANY_WIKI_GET_CAPABILITY_ID", "COMPANY_WIKI_GET_KIND",
    "COMPANY_WIKI_LIST_CAPABILITY_ID", "COMPANY_WIKI_LIST_KIND",
    "SEC_CAPABILITY_ID", "SEC_COMPANY_FACTS_KIND", "build_governance_record",
    "governance_kind_for_capability", "load_connector_governance",
    "write_governance_proposal",
]
