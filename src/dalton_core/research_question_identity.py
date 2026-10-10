"""Conservative, versioned question keys; wording is content, not identity.

Only named metrics are folded across wording. Unclassified questions retain a
text fingerprint so unrelated qualitative questions never merge by guesswork.
"""
from __future__ import annotations
import re
import unicodedata
from typing import Any, Mapping, Sequence
from .store import content_hash

KEY_SCHEMA = "research-question-key-v2"
_METRICS = {
    "revenue": (r"\brevenue\b", r"\bsales\b", "营业收入", "营收"),
    "gross_margin": (r"\bgross margin\b", "毛利率"),
    "operating_margin": (r"\boperating margin\b", "营业利润率"),
    "net_income": (r"\bnet income\b", "净利润"),
    "free_cash_flow": (r"\bfree cash flow\b", "自由现金流"),
    "capex": (r"\bcapex\b", r"\bcapital expenditures?\b", "资本开支"),
}

def normalized(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())

def reporting_scope(value: str) -> str:
    """Filing publication windows are provenance, never reporting periods."""
    return re.sub(r"\bfiled\s+(?:from\s+)?(?:19|20)\d{2}-\d{2}-\d{2}(?:\s*(?:\.\.|to|through)\s*(?:19|20)\d{2}-\d{2}-\d{2})?", "filed window", normalized(value))

def canonical_question_key(company_ref: str, question: str, answer_criteria: str,
                           source_refs: Sequence[str], explicit: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if explicit is not None:
        if set(explicit) != {"issuer", "accession_or_fact", "metric", "period", "doc_kinds"}:
            raise ValueError("canonical_key requires issuer, accession_or_fact, metric, period, doc_kinds")
        if explicit["issuer"] != company_ref or any(not isinstance(explicit[k], str) or not explicit[k].strip()
                for k in ("issuer", "accession_or_fact", "metric", "period")):
            raise ValueError("canonical_key must bind the exact issuer and nonempty fact/metric/period")
        if not isinstance(explicit["doc_kinds"], (list, tuple)) or any(not isinstance(k, str) or not k.strip() for k in explicit["doc_kinds"]):
            raise ValueError("canonical_key.doc_kinds must be an array of document types")
        key = {k: normalized(explicit[k]) for k in ("issuer", "accession_or_fact", "metric", "period")}
        key["doc_kinds"] = sorted({k.strip().upper() for k in explicit["doc_kinds"]})
        # Issuer references are case-sensitive authority references.
        key["issuer"] = company_ref
        return {"schema_version": KEY_SCHEMA, **key}
    text = reporting_scope(question + " " + answer_criteria)
    metrics = sorted(k for k, patterns in _METRICS.items() if any(re.search(p, text) for p in patterns))
    periods = sorted(set(re.findall(r"\b(?:19|20)\d{2}(?:[- /]?(?:q[1-4]|fy))?\b|\bq[1-4][ /-]?(?:19|20)\d{2}\b", text)))
    fiscal_quarters_before = re.findall(r"q([1-4])[ -]*(?:fy|fiscal)[ -]?((?:19|20)\d{2})", text)
    fiscal_quarters_after = re.findall(r"(?:fy|fiscal(?: year)?)[ -]?((?:19|20)\d{2})[ /-]*q([1-4])", text)
    fiscal_quarters = {(year, quarter) for quarter,year in fiscal_quarters_before} | set(fiscal_quarters_after)
    annual_text = re.sub(r"q[1-4][ -]*(?:fy|fiscal)[ -]?(?:19|20)\d{2}|(?:fy|fiscal(?: year)?)[ -]?(?:19|20)\d{2}[ /-]*q[1-4]", " ", text)
    fiscal_years = re.findall(r"(?:fy|fiscal year)[ -]?((?:19|20)\d{2})|((?:19|20)\d{2})[ 年]*(?:财年|年度|全年|年报)", annual_text)
    periods = [period for period in periods if not any(period.replace(' ','').replace('-','') == year+'q'+quarter for year,quarter in fiscal_quarters)]
    periods = sorted(set(periods) | {"fy"+year+"q"+quarter for year,quarter in fiscal_quarters} | {"fy"+(a or b) for a,b in fiscal_years})
    accessions = sorted({a for ref in source_refs for a in re.findall(r"\b\d{10}-\d{2}-\d{6}\b", ref)})
    docs = sorted(set(re.findall(r"(?<![a-z0-9])(?:10-k|10-q|8-k|20-f|6-k)(?![a-z0-9])", text)))
    # A time/filing anchor is required for semantic deduplication. The aspect
    # includes forecast/actual and comparison intent to avoid collapsing them.
    qtext = normalized(question)
    mechanism = bool(re.search(r"\bwhy\b|\bhow\b(?!\s+much)|affect|impact|mechanism|explain|due to|pricing|mix|为何|为什么|如何|影响|机制|原因|解释|归因|结构", qtext + " " + normalized(answer_criteria)))
    numeric_intent = bool(re.search(r"^(?:what (?:was|were|is|are)|how much|report|return|give|list)|多少|数值|金额|具体数据", qtext))
    # A metric-only question fragment is an unambiguous value lookup. Words
    # beyond the named metric, reporting period and generic value instruction
    # make it qualitative unless the caller supplied an explicit exact key.
    fragment = qtext
    for patterns in _METRICS.values():
        for pattern in patterns: fragment = re.sub(pattern, " ", fragment)
    fragment = re.sub(re.escape(normalized(company_ref)), "", fragment)
    fragment = re.sub(r"fy|(?:19|20)\d{2}|q[1-4]|年度|全年|年报|\b(?:in|for|of|the|was|were|is|are|what|how|much|report|return|give|list|amount|value|reported|total|growth|year|over|quarter)\b|[\s?.,:/-]", "", fragment)
    numeric_intent = numeric_intent or not fragment
    semantic = bool(metrics) and bool(periods or accessions) and numeric_intent and not fragment and not mechanism
    qualifiers = sorted(w for w in ("guidance", "forecast", "growth", "segment", "预期", "指引", "增速", "分部") if w in text)
    return {"schema_version": KEY_SCHEMA, "issuer": company_ref,
            "accession_or_fact": ",".join(accessions) or ("reported:" + ",".join(periods) if semantic else "text:" + content_hash(normalized(question))[:32]),
            "metric": ",".join(metrics + qualifiers) if semantic else "unclassified",
            "period": ",".join(periods) or "unspecified", "doc_kinds": [k.upper() for k in docs]}
