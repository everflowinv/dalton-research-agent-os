"""Is a retired Claim an industry-level finding?  A deterministic answer.

The span-level subject-absent detector (``claim_subject``) retires a Claim
whose statement and cited span never name the company it is filed under.
That is right at company level, and it also throws away knowledge: the Wells
Fargo mid-year IT services CIO survey filed 54 times under CTSH and EPAM, or
ws-7d's hyperscaler-capex statements filed under AMZN or META, are facts
about the *industry*.  ``claim_industry_reattribution`` records such a Claim
against the mission's industry; this module decides which ones qualify.

Strict on purpose -- a Claim the rule is unsure about stays retired and
nowhere else.  All of these must hold (``RULE_REF`` v1):

1. **Nothing leans on a single company.**  The statement has no antecedent
   word ("management", "the company", "he", "我们", "管理层" ...) and no
   single-company research-note word ("price target", "guidance", "rating",
   "the stock", "its shares" ...).
2. **No single company is its actor.**  Either the statement names no
   company at all -- no covered company, no ticker-shaped word, and no
   capitalised word outside :data:`NEUTRAL_WORDS` (sources, places,
   acronyms, ordinary sentence openers) -- or it names at least
   :data:`MIN_COVERED_FOR_COMPARISON` covered companies side by side, which is
   a comparison across the industry.  Exactly one covered company, or an
   unknown capitalised name, is a company-level fact and is refused.
3. **The cited span is not one other company's.**  A span that names exactly
   one covered company (while the statement names none) is a paraphrase of
   that company's fact; a document that is a covered company's own (title,
   head, filing cover or density, as ``claim_subject.own_document_evidence``)
   is that company's.  Both refuse.
4. **It talks about the industry.**  At least one collective term (the
   industry, the market, peers, vendors, enterprises, respondents, or the
   industry's own collectives such as "hyperscalers" / "data centers")
   *and* at least one term from the industry's own lexicon
   (:data:`INDUSTRY_LEXICONS`, chosen by the mission's ``industry_ref``), both
   in the statement's main clause -- before a trailing gloss such as
   "..., relevant to hyperscaler capex" (:func:`main_clause`).  An industry
   with no lexicon here reattributes nothing: a missing vocabulary is a
   configuration gap, not evidence.
5. **It is a statement.**  At least :data:`MIN_STATEMENT_CHARS` characters,
   and a Chinese statement must put its collective term in the first
   :data:`CJK_SUBJECT_WINDOW` characters (the grammatical subject), because
   a Chinese company name cannot be told from a common noun without a table.

The source document being an industry survey ("survey", "CIO", "调研" in its
title) is recorded as supporting evidence but never required and never
enough on its own.  No model is asked: the rule is enough, and a model call
would make an append-only record depend on something that cannot be re-run.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from .claim_subject import (
    _ANAPHOR_RE,
    _CJK_RE,
    _NOT_TICKERS,
    _TICKERLIKE_RE,
    _mentions,
    _usable,
    _word_pattern,
    own_document_evidence,
    text_names_word,
)

RULE_REF = "claim-industry-reattribution:industry-level:v1"
MIN_STATEMENT_CHARS = 40
MIN_COVERED_FOR_COMPARISON = 2
CJK_SUBJECT_WINDOW = 14

#: Words that tie a statement to one company even when no name is written.
_SINGLE_COMPANY_RE = re.compile(
    r"(?i)\b(price target|guidance|guide[sd]?|rating|rated|overweight|underweight|"
    r"outperform|underperform|buy-rated|the stock|its shares|shares of|"
    r"the analyst|the author|this company|the issuer|its own|their own)\b"
    r"|(?-i:\bits\b)"
    r"|公司|(?<![尤极])其(?![中他它余次实])|该(?!行业)|我们|管理层|指引|目标价|评级"
)
#: Collective subjects: a statement about many actors at once.
COLLECTIVE_TERMS: tuple[str, ...] = (
    "industry", "industries", "sector", "sectors", "market", "markets",
    "peers", "peer group", "vendors", "providers", "service providers",
    "suppliers", "players", "incumbents", "competitors", "enterprises",
    "enterprise", "clients", "customers", "buyers", "respondents",
    "decision makers", "cios", "cio", "companies", "firms", "hyperscalers",
    "cohort", "group", "ecosystem", "demand", "spending", "spend",
    "行业", "板块", "市场", "同业", "厂商", "供应商", "企业", "客户",
    "受访者", "云厂商", "超大规模", "巨头", "需求",
)
#: Each industry's own vocabulary, keyed by a word of its ``industry_ref``.
#: Matched as whole words (Latin) or substrings (Chinese).
INDUSTRY_LEXICONS: Mapping[str, tuple[str, ...]] = {
    "it-services": (
        "it services", "it service", "it spending", "it spend", "it budget",
        "it budgets", "tech spending", "technology spending", "outsourcing",
        "insourcing", "offshoring", "consulting", "systems integration",
        "systems integrators", "digital transformation", "managed services",
        "discretionary", "ai services", "genai", "generative ai", "ai adoption",
        "ai initiatives", "proof-of-concept", "proofs-of-concept",
        "time-and-materials", "outcome-based", "cloud migration", "bookings",
        "headcount", "utilization", "deal", "deals", "pricing",
        "it服务", "外包", "咨询", "数字化转型", "it支出",
    ),
    "hyperscaler": (
        "hyperscaler", "hyperscalers", "capex", "capital expenditure",
        "capital expenditures", "capital spending", "data center",
        "data centers", "data-center", "datacenter", "datacenters", "cloud",
        "ai infrastructure", "ai compute", "compute", "gpu", "gpus",
        "accelerator", "accelerators", "ai chips", "power", "electricity",
        "资本开支", "资本支出", "数据中心", "云", "算力", "gpu", "电力",
    ),
}
#: Collective subjects particular to one industry: nouns that name the whole
#: industry or its shared physical base ("hyperscaler economics", "a ban on
#: large data centers", "consultancies").
INDUSTRY_COLLECTIVES: Mapping[str, tuple[str, ...]] = {
    "it-services": (
        "it services", "consultancies", "consultants", "outsourcers",
        "integrators", "it服务",
    ),
    "hyperscaler": (
        "hyperscaler", "hyperscale", "big tech", "mega-cap tech",
        "megacap tech", "cloud providers", "cloud service providers",
        "data center", "data centers", "data-center", "datacenter",
        "datacenters", "data-centre", "buildout", "build-out",
        "ai infrastructure", "数据中心", "云厂商",
    ),
}
#: Capitalised words that name no company: sources a claim is attributed to,
#: places, periods, acronyms and ordinary sentence openers.
NEUTRAL_WORDS = frozenset(word.lower() for word in (
    # sources and institutions a finding is attributed to
    "Wells", "Fargo", "Morgan", "Stanley", "J.P.", "JPMorgan", "JPM", "Goldman",
    "Sachs", "GS", "GIR", "BofA", "Bank", "America", "Citi", "UBS", "Barclays",
    "Jefferies", "Bernstein", "Evercore", "ISI", "Gartner", "IDC", "Forrester",
    "Everest", "ISG", "HFS", "Census", "Bureau", "BLS", "Fed", "Federal",
    "Reserve", "Business", "Trends", "Outlook", "Survey", "Economics",
    "Research", "Group", "Global", "Investment", "Sell-side", "Street",
    # places and periods
    "US", "U.S.", "USA", "United", "States", "North", "South", "Americas",
    "America", "Europe", "European", "EMEA", "APAC", "Asia", "Asian", "Japan",
    "China", "Chinese", "India", "Indian", "UK", "Middle", "East", "Iran",
    "January", "February", "March", "April", "May", "June", "July", "August",
    "September", "October", "November", "December", "Q1", "Q2", "Q3", "Q4",
    "H1", "H2", "FY", "YTD", "Mid-Year", "Mid-year", "Year", "Fiscal",
    # acronyms and trade words
    "AI", "GenAI", "IT", "CIO", "CIOs", "CEO", "CEOs", "CFO", "CFOs", "CTO",
    "IP", "POC", "POCs", "SaaS", "PaaS", "IaaS", "API", "APIs", "GPU", "GPUs",
    "TPU", "CPU", "HBM", "DRAM", "NAND", "LLM", "LLMs", "ROI", "TCO", "HPC",
    "CapEx", "Capex", "OpEx", "Opex", "TAM", "YoY", "BPO", "ADM", "ER&D",
    "IT-services", "GW", "MW", "TMT", "SMB", "SMBs", "GDP", "M&A",
    # ordinary openers and nouns that are capitalised only at sentence start
    "The", "A", "An", "In", "On", "For", "Per", "Of", "At", "As", "By", "To",
    "With", "While", "Whereas", "Although", "Despite", "Overall", "Most",
    "Many", "More", "Some", "Several", "Both", "All", "Few", "Fewer", "Nearly",
    "About", "Roughly", "Majority", "Minority", "Half", "Share", "Growth",
    "Demand", "Spending", "Spend", "Budgets", "Pricing", "Enterprise",
    "Enterprises", "Clients", "Customers", "Buyers", "Respondents",
    "Surveyed", "Survey", "Surveys", "Industry", "Sector", "Market", "Markets",
    "Vendors", "Providers", "Professional", "Services", "Service",
    "Companies", "Firms", "Hyperscaler", "Hyperscalers", "Cloud", "Data",
    "Center", "Centers", "Power", "Electricity", "Capital", "Expenditure",
    "Compute", "Infrastructure", "Digital", "Discretionary", "Outsourcing",
    "Consulting", "Technology", "Tech", "Software", "Hardware", "Supply",
    "Investors", "Investor", "Analysts", "Consensus", "According", "Recent",
    "Across", "Among", "Amid", "After", "Before", "During", "Over", "Since",
    "Through", "Across", "Rising", "Higher", "Lower", "Strong", "Weak",
    "Generative", "Artificial", "Intelligence", "Mid-Year", "IT", "Big",
    "Large", "Larger", "Legacy", "Niche", "Top", "First", "Second", "Third",
    "For", "Regarding", "Following", "Ahead", "Contact", "Expert", "Experts",
    "Commentary", "Checks", "Channel", "This", "These", "That", "Those",
    "There", "It", "Its", "However", "Meanwhile", "Also", "Still",
    "Analyst", "Analysts", "Downside", "Upside", "Four", "Five", "Two",
    "Three", "Key", "Small", "Mid", "Mega", "Cap", "Strategy", "Future",
    "Implementation", "Organizations", "Existing", "Source", "Sources",
    "Memory", "Internet", "Momentum", "Conference", "Macro", "Consumer",
    "Equity", "Equities", "Institutional", "Hedge", "Fund", "Funds", "Flow",
    "Positioning", "Coverage", "Preview", "Earnings", "Growing", "Elevated",
    "Sustained", "Aggregate", "Input", "Options", "Security", "Information",
    "Application", "Applications", "Analytics", "Banking", "Finance",
    "Payments", "Processors", "Competitive", "Strategic", "Attrition",
    "Within", "Index", "Desk", "Utilities", "Industrials", "Industrial",
    "Semis", "Semiconductors", "Texas", "Ohio", "New", "York", "Mexico",
    "Korean", "Australian", "Brazilian", "Brazil", "EU", "IoT", "BFSI",
    "CRM", "ERP", "FX", "Dot-Com", "Act", "Q&A", "Debrief", "Infra",
    "Strategist", "Strategists", "Photonics", "Agentic", "XPU", "XPUs",
    "CPUs", "ASIC", "ASICs", "EUV", "MLCC", "TAMs", "FOMC", "VIX",
    "Summit", "Setups", "Reactors", "Modular", "Nuclear", "Grid", "Energy",
))
#: How a compound's tail may continue a neutral head: "AI-driven",
#: "Asia-focused", "PE-owned".  Only lowercase tails; "SAP-led" is SAP's.
_COMPOUND_TAIL_RE = re.compile(r"^[a-z][a-z-]*$")
_CAPITAL_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z][A-Za-z0-9&.'’/-]*")
SURVEY_TITLE_RE = re.compile(r"(?i)\bsurvey\b|\bcio\b|\bpoll\b|调研|问卷|调查")


#: Where a drafted statement turns from what the source said to why it
#: matters.  Rule 4's terms must appear before the first of these.
_GLOSS_RE = re.compile(
    r"(?i)(?:[,;:—–-]\s*|\s)(?:which is |which |this |a |an )?(?:directionally )?"
    r"(?:relevant to|bears on|framing|frames? (?:it|this|the)|indicating|suggesting|"
    r"signal(?:l)?ing|implying|underscoring|reinforcing|tying|a signal|a read-through|"
    r"a macro|a narrative|a sentiment)\b"
)
#: A gloss this early is the sentence itself, not an afterthought.
MIN_MAIN_CLAUSE_CHARS = 30


def main_clause(statement: str) -> str:
    """The statement up to its first gloss ("…, indicating …")."""

    for match in _GLOSS_RE.finditer(statement):
        if match.start() >= MIN_MAIN_CLAUSE_CHARS:
            return statement[:match.start()]
    return statement


def lexicon_key(industry_ref: Any) -> str | None:
    """The lexicon whose key is a word of this industry ref, or None."""

    if not isinstance(industry_ref, str) or not industry_ref.startswith("industry:"):
        return None
    lowered = industry_ref.lower()
    return next((key for key in INDUSTRY_LEXICONS if key in lowered), None)


def lexicon_for(industry_ref: Any) -> tuple[str, ...]:
    key = lexicon_key(industry_ref)
    return () if key is None else INDUSTRY_LEXICONS[key]


def _hits(text: str, words: Sequence[str]) -> list[str]:
    lowered = text.lower()
    return sorted({word for word in words if _word_pattern(word).search(lowered)})


def _covered_named(text: Any, roster: Mapping[str, Sequence[str]]) -> list[str]:
    return sorted(ref for ref, needles in roster.items()
                  if text_names_word(text, list(needles)))


def _neutral(part: str, core: str, known: set[str]) -> bool:
    lowered = core.lower()
    if lowered in NEUTRAL_WORDS or part.lower() in NEUTRAL_WORDS or lowered in known:
        return True
    if core.isupper() and core in _NOT_TICKERS:
        return True
    if not core[:1].isupper():
        return True  # the lowercase half of "cloud/AI"
    head, _, tail = core.partition("-")
    if tail and head.lower() in NEUTRAL_WORDS and _COMPOUND_TAIL_RE.fullmatch(tail):
        return True
    # "Accenture's", "EPAM-led": a covered name is not an unknown one.
    return head.lower() in known


def unknown_names(statement: str, roster: Mapping[str, Sequence[str]]) -> list[str]:
    """Capitalised or ticker-shaped words that may name a company."""

    known = {needle.lower() for needles in roster.values() for needle in _usable(needles)}
    found: list[str] = []
    for match in _CAPITAL_RE.finditer(statement):
        word = re.sub(r"(’s|'s)$", "", match.group(0).rstrip(",;:'’"))
        for part in word.split("/"):
            core = part.rstrip(".")
            if core and not _neutral(part, core, known):
                found.append(core)
    for match in _TICKERLIKE_RE.finditer(statement):
        word = match.group(0)
        if (word not in _NOT_TICKERS and word.lower() not in NEUTRAL_WORDS
                and word.lower() not in known and word not in found):
            found.append(word)
    return found


def judge(
    *,
    statement: Any,
    cited_span: Any,
    industry_ref: Any,
    roster: Mapping[str, Sequence[str]],
    document_title: Any = None,
    source_text: Any = None,
    issuer_document: bool = False,
) -> dict[str, Any]:
    """The rule's verdict and every fact it read.

    ``roster`` is company_ref -> needles for every covered company of the
    mission (the subject included).  Returns ``{"industry_level": bool,
    "refusal": str | None, ...evidence}``; the evidence is what an append-only
    record stores so the decision can be re-read and re-run.
    """

    verdict: dict[str, Any] = {"rule_ref": RULE_REF, "industry_level": False, "refusal": None}

    def refuse(reason: str) -> dict[str, Any]:
        verdict["refusal"] = reason
        return verdict

    if not isinstance(statement, str) or len(statement.strip()) < MIN_STATEMENT_CHARS:
        return refuse("statement_too_short")
    if not isinstance(cited_span, str) or not cited_span.strip():
        return refuse("span_unreadable")
    lexicon = lexicon_for(industry_ref)
    if not lexicon:
        return refuse("no_industry_lexicon")
    anaphor = _ANAPHOR_RE.search(statement)
    if anaphor is not None:
        return refuse(f"leans_on_antecedent:{anaphor.group(0).lower()}")
    single = _SINGLE_COMPANY_RE.search(statement)
    if single is not None:
        return refuse(f"single_company_word:{single.group(0).lower()}")
    covered = _covered_named(statement, roster)
    verdict["statement_companies"] = covered
    if len(covered) == 1:
        return refuse("names_one_company")
    if covered and len(covered) < MIN_COVERED_FOR_COMPARISON:
        return refuse("too_few_companies_to_compare")
    # A comparison names each company once; one named again is its focus
    # ("frames META's move ... as bullish for META").
    for ref in covered:
        if _mentions(statement.lower(), roster[ref]) > 1:
            return refuse("comparison_dwells_on_one_company")
    unknown = unknown_names(statement, roster)
    verdict["unknown_names"] = unknown
    if unknown:
        return refuse("names_unknown_proper_noun:" + ",".join(unknown[:5]))
    if not covered:
        span_companies = _covered_named(cited_span, roster)
        verdict["span_companies"] = span_companies
        if len(span_companies) == 1:
            return refuse("span_names_one_company")
    verdict["form"] = "company_free" if not covered else "cross_company"
    if issuer_document:
        return refuse("issuer_document")
    # A document that is exactly one covered company's own (its title, cover,
    # filing header or density says so) is that company's.  A digest whose
    # head names several is nobody's own, and is not refused for it.
    owners = {}
    for ref, needles in roster.items():
        own = own_document_evidence(
            title=document_title, text=source_text, needles=list(needles),
            issuer_document=False, subject_ref=ref,
            peer_needles=[n for other, values in roster.items() if other != ref for n in values],
        )
        if own is not None:
            owners[ref] = own
    if len(owners) == 1:
        ref, why = next(iter(owners.items()))
        return refuse(f"document_is_one_company_own:{ref}:{why}")
    verdict["document_owners"] = sorted(owners)
    # The finding, not the gloss: drafted statements often end "..., a
    # macro overhang relevant to how investors value sustained capex", and
    # an industry word that appears only there does not make oil prices an
    # industry finding.
    finding = main_clause(statement)
    verdict["main_clause_chars"] = len(finding)
    collective = _hits(finding, (*COLLECTIVE_TERMS,
                                 *INDUSTRY_COLLECTIVES.get(lexicon_key(industry_ref) or "", ())))
    domain = _hits(finding, lexicon)
    verdict["collective_terms"] = collective
    verdict["industry_terms"] = domain
    if not collective:
        return refuse("no_collective_term")
    if not domain:
        return refuse("no_industry_term")
    if _CJK_RE.search(statement) and not re.search(r"[A-Za-z]{4,}.*[A-Za-z]{4,}", statement):
        head = statement[:CJK_SUBJECT_WINDOW]
        if not any(word in head for word in collective if _CJK_RE.search(word)):
            return refuse("cjk_collective_not_subject")
    verdict["survey_source"] = bool(
        isinstance(document_title, str) and SURVEY_TITLE_RE.search(document_title))
    verdict["industry_level"] = True
    return verdict


__all__ = [
    "COLLECTIVE_TERMS",
    "INDUSTRY_COLLECTIVES",
    "INDUSTRY_LEXICONS",
    "MIN_COVERED_FOR_COMPARISON",
    "MIN_STATEMENT_CHARS",
    "NEUTRAL_WORDS",
    "RULE_REF",
    "judge",
    "lexicon_for",
    "lexicon_key",
    "unknown_names",
]
