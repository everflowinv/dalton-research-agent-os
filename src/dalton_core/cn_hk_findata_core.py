"""S4: the China / Hong Kong fundamentals identity, read by a library.

Dalton can read SEC filings and Yahoo prices. It cannot read anything about a
company listed in Shanghai, Shenzhen or Hong Kong, which means the coverage
universe can only ever contain American names. This connector is what has to
exist before a Chinese name can enter one at all.

**Where it comes from.** The owner's OpenClaw workspace already has a
`cn-hk-findata` skill: 87 natural-language intents routed at ``akshare``, with
a per-intent vendor fallback chain, per-(source, capability) health accounting
and a hard-won rule about not probing 东方财富 in batches. That skill is the
research; this is the part of it that can be governed. Six operations, chosen
because they are what a fundamental analyst asks for first: the three
statements, who owns the shares, what the company is buying back, what the
leveraged money is doing, what the northbound money is doing, and what the same
company costs in two currencies.

**What is deliberately not carried over.** The skill's router chooses an
endpoint from 87 candidates at question time, and when nothing matches it picks
by function-name similarity and marks the answer ``degraded=true`` -- which is
how a question about sector flows comes back as a dividend table. The protocol
already has a name for why that cannot be a connector: the Planner makes one
semantic choice per WorkOrder and freezes it, and the Runner never re-runs a
model to decide what to call. So each operation here names one function, on one
host, with one vendor, decided before the call.

**The vendor is not the source.** 东方财富 did not file anything. A Chinese
company's filed number is in the 巨潮 announcement the existing ``cninfo``
connector already reaches; what arrives here is a vendor's normalisation of it,
which is faster, wider, and second-hand. Every row therefore names the vendor
that produced it. Nothing from this connector may be graded as a filing.

**Fallback means labelled, not silent.** The skill falls back to 同花顺, 新浪,
腾讯 or 雪球 for twelve intents, and the survey's rule is that when it does, the
口径 is no longer 东财's and has to be said out loud -- 同花顺's industry fund
flow is an "即时" measure where 东财's is "今日", and the rows look identical.
None of the six operations here has a permitted fallback: the fundamentals path
has exactly one vendor each. So the behaviour reproduced from the skill's
health/demotion logic is the *refusal*, with the reason and the vendor named,
rather than a quiet switch to a number measured differently. The wire can still
carry ``fallback_used`` and a ``caliber_note`` on every row, because the day a
fallback is approved the label has to travel with the number, not with the run.
"""

from __future__ import annotations

import copy
from datetime import datetime
from types import MappingProxyType
from typing import Any

from .connector_inventory import load_packaged_connector_inventory
from .store import content_hash

TEMPLATE_KEY = "cn-hk-findata"
FINANCIAL_STATEMENTS_OPERATION = "financial_statements"
SHAREHOLDERS_OPERATION = "shareholders"
BUYBACKS_OPERATION = "buybacks"
MARGIN_BALANCE_OPERATION = "margin_balance"
NORTHBOUND_FLOW_OPERATION = "northbound_flow"
AH_PREMIUM_OPERATION = "ah_premium"
OPERATIONS = (
    FINANCIAL_STATEMENTS_OPERATION,
    SHAREHOLDERS_OPERATION,
    BUYBACKS_OPERATION,
    MARGIN_BALANCE_OPERATION,
    NORTHBOUND_FLOW_OPERATION,
    AH_PREMIUM_OPERATION,
)

KIND_BY_OPERATION = MappingProxyType(
    {operation: f"cn-hk-findata-{operation.replace('_', '-')}"
     for operation in OPERATIONS}
)
CAPABILITY_BY_OPERATION = MappingProxyType(
    {operation: f"capability:dalton:connector:{kind}"
     for operation, kind in KIND_BY_OPERATION.items()}
)
OPERATION_BY_KIND = MappingProxyType(
    {kind: operation for operation, kind in KIND_BY_OPERATION.items()}
)

GOVERNANCE_SCHEMA_VERSION = "0.1"
SIDE_EFFECT = "read:public-http"
SOURCE_REF = "source:cn-hk-findata"
# The library is the part the owner is asked to trust in place of Dalton
# verifying the bytes, so the adapter hash names it. Swapping it is a different
# capability, not a maintenance detail.
ADAPTER_LIBRARY = "akshare"
# The version the OpenClaw skill runs. akshare renames columns and changes
# units between releases -- ``stock_hsgt_fund_flow_summary_em`` divides the
# vendor's amounts by 10,000 in this one -- so the version is recorded on every
# raw capture and a mismatch is a fact about the numbers, not a warning.
ADAPTER_LIBRARY_VERSION = "1.18.94"

# Which upstream vendor may legitimately produce each operation's rows. These
# are exactly the enums the frozen output contracts carry, so a row whose
# vendor is not on this list cannot be validated into the system at all.
VENDORS_BY_OPERATION = MappingProxyType({
    FINANCIAL_STATEMENTS_OPERATION: ("eastmoney",),
    SHAREHOLDERS_OPERATION: ("eastmoney",),
    BUYBACKS_OPERATION: ("eastmoney",),
    # Two exchanges, not a primary and a fallback: Shanghai publishes Shanghai
    # and Shenzhen publishes Shenzhen, and neither can stand in for the other.
    MARGIN_BALANCE_OPERATION: ("sse", "szse"),
    NORTHBOUND_FLOW_OPERATION: ("eastmoney",),
    AH_PREMIUM_OPERATION: ("eastmoney",),
})

# The akshare functions each operation is frozen to. More than one where the
# question genuinely needs more than one call; never chosen at run time.
FUNCTIONS_BY_OPERATION = MappingProxyType({
    FINANCIAL_STATEMENTS_OPERATION: (
        "stock_profit_sheet_by_report_em",
        "stock_balance_sheet_by_report_em",
        "stock_cash_flow_sheet_by_report_em",
        "stock_financial_hk_report_em",
    ),
    SHAREHOLDERS_OPERATION: (
        "stock_gdfx_free_top_10_em",
        "stock_zh_a_gdhs_detail_em",
    ),
    BUYBACKS_OPERATION: ("stock_repurchase_em",),
    MARGIN_BALANCE_OPERATION: ("stock_margin_sse", "stock_margin_szse"),
    NORTHBOUND_FLOW_OPERATION: ("stock_hsgt_fund_flow_summary_em",),
    AH_PREMIUM_OPERATION: ("stock_zh_ah_spot_em",),
})

# Which declared host each operation actually reaches. The template's allowlist
# is the union; this says which part of it belongs to which approval, so a
# reviewer reading one governance record can see one host rather than six.
HOSTS_BY_OPERATION = MappingProxyType({
    FINANCIAL_STATEMENTS_OPERATION: (
        "datacenter.eastmoney.com", "emweb.securities.eastmoney.com",
    ),
    SHAREHOLDERS_OPERATION: (
        "datacenter-web.eastmoney.com", "emweb.securities.eastmoney.com",
    ),
    BUYBACKS_OPERATION: ("datacenter-web.eastmoney.com",),
    MARGIN_BALANCE_OPERATION: ("query.sse.com.cn", "www.szse.cn"),
    NORTHBOUND_FLOW_OPERATION: ("datacenter-web.eastmoney.com",),
    AH_PREMIUM_OPERATION: ("push2.eastmoney.com",),
})

# 东财's quote cluster refused the owner's egress IP for months in 2026, and
# pressing it took the neighbouring endpoints down with it for minutes at a
# time. One operation needs it; that operation gets one attempt and no retry.
NO_BATCH_PROBE_HOSTS = frozenset({
    "push2.eastmoney.com", "push2his.eastmoney.com", "33.push2.eastmoney.com",
})

# Vendors that exist, were considered, and are refused -- written down so that
# a reviewer can see the alternative was weighed rather than missed, and so
# that a later maintainer does not "fix" an outage by reaching for one.
REFUSED_VENDOR_ROUTES = (
    MappingProxyType({
        "operation": AH_PREMIUM_OPERATION,
        "vendor": "tencent",
        "function": "stock_zh_ah_spot",
        "host": "stock.gtimg.cn",
        "reason": (
            "腾讯的 A+H 列表只有 H 股报价，没有比价与溢价：用它顶替 ah_premium "
            "会对一个没被回答的问题给出一个自信的数字。"
            "（另：akshare 走的是明文 http，不是 https。）"
        ),
    }),
    MappingProxyType({
        "operation": FINANCIAL_STATEMENTS_OPERATION,
        "vendor": "cninfo",
        "function": "stock_financial_report_sina",
        "host": "money.finance.sina.com.cn",
        "reason": (
            "新浪的三表口径与东财不同，字段名也不同；同一份 wire 里混两家的行 "
            "会让 period 之间不可比。要一手数字请走 cninfo 连接器的公告原文。"
        ),
    }),
)

# What a reader has to be told about a number even when nothing fell back.
# These travel on the rows as ``caliber_note``.
CALIBER_NOTES = MappingProxyType({
    "eastmoney_statements_a": (
        "东方财富对 A 股三表的归一化，非申报原文；会计准则为中国企业会计准则。"
    ),
    # Measured, not assumed: 腾讯 FY2025 的营业额在三表接口是 743,689,000,000，
    # 在同一主机的主要指标表是 OPERATE_INCOME 751,766,000,000，CURRENCY=HKD、
    # IS_CNY_CODE=0。差 1.09%，不是汇率。所以主要指标表的币种不能借给三表用。
    "eastmoney_statements_hk": (
        "东方财富对港股三表的归一化，非申报原文；该接口不返回币种与会计准则，"
        "且主要指标表的 CURRENCY 与三表口径不同，不可借用——因此这两格为空。"
    ),
    "eastmoney_hsgt_unit": (
        "akshare 把东财的原始金额除以 10,000 后返回，单位记为 amount_unit。"
    ),
    # 2026-09 实测：上交所的融资融券余额是 1,350,016,680,402，深交所是
    # 12,847.58。同一种钱，两种写法，差一亿倍；两所对融券金额那一列的叫法也不同。
    "szse_short_balance": (
        "深交所把该项称作「融券余额」（上交所称「融券余量金额」）；"
        "深市金额单位为亿元、数量单位为亿股，与沪市的元/股不同，不可直接相加。"
    ),
    "sse_short_balance": (
        "上交所把该项称作「融券余量金额」（深交所称「融券余额」）；"
        "沪市金额单位为元、数量单位为股，与深市的亿元/亿股不同，不可直接相加。"
    ),
    "eastmoney_ah_premium": (
        "比价与溢价由东财按其自用汇率计算；本系统不重算，重算会得到另一个数字。"
    ),
    "eastmoney_top_holders": "前十大流通股东，不是全部股东；变动列为东财的口径。",
    "eastmoney_buyback_market_table": (
        "东财只发布全市场回购表，无按代码检索的接口；本行由全表过滤而来。"
    ),
})


class CnHkFinDataError(RuntimeError):
    """The China / Hong Kong fundamentals identity or record is invalid."""


def _operation(operation: str) -> str:
    if operation not in KIND_BY_OPERATION:
        raise CnHkFinDataError(
            f"cn-hk-findata has no frozen {operation!r} operation"
        )
    return operation


def cn_hk_findata_contract(operation: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """The packaged template and one operation's frozen contract."""

    operation = _operation(operation)
    template = load_packaged_connector_inventory()["templates"][TEMPLATE_KEY]
    matches = [item for item in template["operations"]
               if item["operation"] == operation]
    if len(matches) != 1:
        raise CnHkFinDataError(
            f"packaged cn-hk-findata template lacks the frozen {operation} operation"
        )
    return template, matches[0]


def cn_hk_findata_source_hash() -> str:
    """Shared by all six operations: one skill-shaped source is one source."""

    template, _ = cn_hk_findata_contract(FINANCIAL_STATEMENTS_OPERATION)
    return content_hash(dict(template["source_identity"]))


def cn_hk_findata_schema_hash(operation: str) -> str:
    """Bound to one operation, so approving one cannot widen to another.

    Six operations, six hashes, six approvals. Reading a company's income
    statement and reading the whole market's margin balance are not the same
    permission, and an owner who approves the first has not approved the
    second.
    """

    _, contract = cn_hk_findata_contract(operation)
    return content_hash({
        "allowed_operations": [operation],
        "input_schema_refs": {operation: contract["input_schema_ref"]},
        "input_schema_hashes": {operation: contract["input_schema_hash"]},
        "output_schema_refs": {operation: contract["output_schema_ref"]},
        "output_schema_hashes": {operation: contract["output_schema_hash"]},
    })


def cn_hk_findata_adapter_hash(operation: str) -> str:
    """Names the library, the version and the exact functions called."""

    template, _ = cn_hk_findata_contract(operation)
    return content_hash({
        "target_ref": template["transport"]["target_ref"],
        "source": template["source_identity"]["source_ref"],
        "operation": operation,
        "library": ADAPTER_LIBRARY,
        "library_version": ADAPTER_LIBRARY_VERSION,
        "functions": list(FUNCTIONS_BY_OPERATION[operation]),
        "vendors": list(VENDORS_BY_OPERATION[operation]),
    })


def cn_hk_findata_identity(operation: str) -> dict[str, Any]:
    """Source and schema identity of exactly one operation."""

    template, contract = cn_hk_findata_contract(operation)
    return {
        "capability_id": CAPABILITY_BY_OPERATION[operation],
        "source_identity": dict(template["source_identity"]),
        "source_hash": cn_hk_findata_source_hash(),
        "schema_hash": cn_hk_findata_schema_hash(operation),
        "adapter_ref": template["transport"]["target_ref"],
        "adapter_hash": cn_hk_findata_adapter_hash(operation),
        "operation": operation,
        "allowed_operations": [operation],
        "allowed_hosts": list(HOSTS_BY_OPERATION[operation]),
        "allowed_vendors": list(VENDORS_BY_OPERATION[operation]),
        "input_schema_ref": contract["input_schema_ref"],
        "input_schema_hash": contract["input_schema_hash"],
        "output_schema_ref": contract["output_schema_ref"],
        "output_schema_hash": contract["output_schema_hash"],
        "input_schema_refs": {operation: contract["input_schema_ref"]},
        "input_schema_hashes": {operation: contract["input_schema_hash"]},
        "output_schema_refs": {operation: contract["output_schema_ref"]},
        "output_schema_hashes": {operation: contract["output_schema_hash"]},
    }


def cn_hk_findata_permissions() -> dict[str, Any]:
    """Credential-free public HTTPS, and the raw sink.

    The same declaration every public-web lane carries. Which hosts may be
    reached is the template's allowlist, and ``HOSTS_BY_OPERATION`` says which
    of them belongs to which approval.
    """

    from .sec_authority_harness import PUBLIC_PERMISSIONS

    return copy.deepcopy(PUBLIC_PERMISSIONS)


def cn_hk_findata_fixture_hash() -> str:
    template, _ = cn_hk_findata_contract(FINANCIAL_STATEMENTS_OPERATION)
    return template["fixture_manifest_hash"]


def cn_hk_findata_output_schema(operation: str) -> dict[str, Any]:
    """The frozen output contract one observation must satisfy."""

    template, contract = cn_hk_findata_contract(operation)
    ref = contract["output_schema_ref"]
    for document in template["schema_documents"]:
        if document["schema_ref"] == ref:
            return document["document"]
    raise CnHkFinDataError(
        f"packaged cn-hk-findata template has no {operation} output contract"
    )


def cn_hk_findata_allowed_hosts(operation: str) -> tuple[str, ...]:
    """The hosts one operation is allowed to reach, and only those."""

    return HOSTS_BY_OPERATION[_operation(operation)]


def refused_vendor_route(operation: str, vendor: str) -> dict[str, Any] | None:
    """A considered-and-refused alternative vendor, with its reason.

    Returned rather than raised so the caller can put the reason into a
    summary a person will read. "The source is down" is not actionable; "the
    only alternative returns the H-share quote and no premium at all" is.
    """

    for route in REFUSED_VENDOR_ROUTES:
        if route["operation"] == operation and route["vendor"] == vendor:
            return dict(route)
    return None


def _wire_time(value: datetime) -> str:
    return value.astimezone(value.tzinfo).isoformat(timespec="microseconds")


def build_cn_hk_findata_governance_record(
    *,
    operation: str,
    approved_by: str,
    status: str = "proposed",
    effective_from: str = "2026-09-09T00:00:00+00:00",
    max_lease_seconds: int = 300,
    version: int = 1,
) -> dict[str, Any]:
    """Closed, hash-bound governance record for one operation.

    ``max_lease_seconds`` is longer than the market lane's 120: a three-table
    A-share history is five paged calls behind one function, and the buyback
    table is the whole market read page by page before a single issuer is
    filtered out of it.
    """

    operation = _operation(operation)
    if status not in {"proposed", "approved"}:
        raise CnHkFinDataError("governance status must be proposed or approved")
    if not isinstance(approved_by, str) or not approved_by.startswith("human:"):
        raise CnHkFinDataError("approved_by must be a human: principal")
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
        "allowed_permissions": copy.deepcopy(cn_hk_findata_permissions()),
        "expected_source_hash": cn_hk_findata_source_hash(),
        "expected_schema_hash": cn_hk_findata_schema_hash(operation),
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
    """Name one exact call: this approval, these parameters, that raw output.

    The same construction the market lane uses. Two runs of the same request
    against the same approval that returned the same answer are one
    invocation; a run whose answer differs -- a restated quarter, a corrected
    holder count -- is a different one, which is the distinction a version
    chain has to make.

    That only holds because of what the caller hashes. The capture carries two
    local clock fields, and while they were inside the hashed bytes every run
    minted a new artifact hash and so a new invocation ref -- two readings of
    the same unchanged quarter looked like two different facts. The child
    lifts them out before hashing (``cn_hk_findata_cli.CLOCK_FIELDS``) and
    keeps them on the summary and the wire, where when-it-was-read belongs.
    """

    return "connector-invocation:cn-hk-findata:" + content_hash({
        "operation": _operation(operation),
        "source_ref": SOURCE_REF,
        "adapter_library": ADAPTER_LIBRARY,
        "adapter_hash": cn_hk_findata_adapter_hash(operation),
        "governance_ref": governance_ref,
        "governance_hash": governance_hash,
        "parameters": parameters,
        "artifact_hash": artifact_hash,
    })[:32]


__all__ = [
    "ADAPTER_LIBRARY",
    "ADAPTER_LIBRARY_VERSION",
    "AH_PREMIUM_OPERATION",
    "BUYBACKS_OPERATION",
    "CALIBER_NOTES",
    "CAPABILITY_BY_OPERATION",
    "CnHkFinDataError",
    "FINANCIAL_STATEMENTS_OPERATION",
    "FUNCTIONS_BY_OPERATION",
    "HOSTS_BY_OPERATION",
    "KIND_BY_OPERATION",
    "MARGIN_BALANCE_OPERATION",
    "NORTHBOUND_FLOW_OPERATION",
    "NO_BATCH_PROBE_HOSTS",
    "OPERATIONS",
    "OPERATION_BY_KIND",
    "REFUSED_VENDOR_ROUTES",
    "SHAREHOLDERS_OPERATION",
    "SOURCE_REF",
    "TEMPLATE_KEY",
    "VENDORS_BY_OPERATION",
    "build_cn_hk_findata_governance_record",
    "cn_hk_findata_adapter_hash",
    "cn_hk_findata_allowed_hosts",
    "cn_hk_findata_contract",
    "cn_hk_findata_fixture_hash",
    "cn_hk_findata_identity",
    "cn_hk_findata_output_schema",
    "cn_hk_findata_permissions",
    "cn_hk_findata_schema_hash",
    "cn_hk_findata_source_hash",
    "invocation_ref",
    "refused_vendor_route",
]
