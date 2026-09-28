"""Is a retired Claim an industry-level finding?  A deterministic answer.

The span-level subject-absent detector (``claim_subject``) retires a Claim
whose statement and cited span never name the company it is filed under.
That is right at company level, and it also throws away knowledge: the Wells
Fargo mid-year IT services CIO survey filed 54 times under CTSH and EPAM, or
ws-7d's hyperscaler-capex statements filed under AMZN or META, are facts
about the *industry*.  ``claim_industry_reattribution`` records such a Claim
against the mission's industry; this module decides which ones qualify.

Strict on purpose -- a Claim the rule is unsure about stays retired and
nowhere else.  All of these must hold (``RULE_REF`` v2):

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
   configuration gap, not evidence.  v2 (2026-09-25b): a spending word on its
   own (:data:`CO_OCCURRENCE_TERMS` -- "capex", "capital expenditure") is not
   the industry's; everybody has capex.  It counts only next to one of the
   industry's subject words in the same main clause (a hyperscaler, cloud,
   data center, AI infrastructure ...).  GS on capital markets being "heavily
   supported by AI capex spend" is about banks, not hyperscalers.
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

RULE_REF = "claim-industry-reattribution:industry-level:v2"
#: Reattributions made under these may be withdrawn when today's rule refuses them.
PRIOR_RULE_REFS = frozenset({"claim-industry-reattribution:industry-level:v1"})
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
#: Lexicon terms that are the industry's only next to one of its subject words
#: (v2).  "capex" alone is any company's spending; "hyperscaler capex",
#: "cloud capex", "data center capex" are this industry's.  Every other term
#: of the lexicon -- and the industry's own collectives -- is a subject word.
CO_OCCURRENCE_TERMS: Mapping[str, tuple[str, ...]] = {
    "hyperscaler": (
        "capex", "capital expenditure", "capital expenditures", "capital spending",
        "资本开支", "资本支出",
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
    key = lexicon_key(industry_ref) or ""
    dependent = set(CO_OCCURRENCE_TERMS.get(key, ()))
    if dependent and set(domain) <= dependent:
        # v2: only a spending word.  It needs one of the industry's subject
        # words beside it; an industry collective ("big tech", "data center")
        # is one.
        subjects = _hits(finding, INDUSTRY_COLLECTIVES.get(key, ()))
        verdict["co_occurrence_subjects"] = subjects
        if not subjects:
            return refuse("spending_term_without_industry_subject:" + ",".join(domain))
    if _CJK_RE.search(statement) and not re.search(r"[A-Za-z]{4,}.*[A-Za-z]{4,}", statement):
        head = statement[:CJK_SUBJECT_WINDOW]
        if not any(word in head for word in collective if _CJK_RE.search(word)):
            return refuse("cjk_collective_not_subject")
    verdict["survey_source"] = bool(
        isinstance(document_title, str) and SURVEY_TITLE_RE.search(document_title))
    verdict["industry_level"] = True
    return verdict


# -- the verifier's "about another subject" (2026-09-28) -----------------------
#
# The support check (``claim_support_verification``) retires a Claim on
# ``citation_support_rejected`` when an independent model finds the cited
# sentences support it but that it is about someone else, and names that
# someone (``other_subject``).  When the someone is an industry the finding is
# industry evidence; when it is a company it is that company's, and a mission
# that does not cover it has nowhere to put it.  The name is a model's words,
# so it is read strictly and deterministically, and it only ever *narrows*
# what the rule above keeps -- it never lets a statement through the rule.
#
# 1. **No company.**  Every capitalised or ticker-shaped word of the name must
#    be ordinary vocabulary (:data:`NEUTRAL_WORDS`, :data:`OTHER_SUBJECT_WORDS`);
#    anything else -- "CoreWeave", "Infineon Technologies (IFX)", "OKLO",
#    "S&P", "Anthropic" -- is a company or an unknown name, and so is a covered
#    company's name in any case ("amazon").  A name with Chinese characters
#    outside :data:`OTHER_SUBJECT_CJK_WORDS` cannot be read and is refused.
# 2. **This mission's industry.**  The name is cut into parts ("A / B",
#    "A & B", "A and B", "A, B", "A vs B").  Each part must be the mission's
#    industry (:data:`OTHER_SUBJECT_SCOPE`: "hyperscalers", "cloud service
#    providers", "AI infrastructure" for the hyperscaler mission; "IT services
#    sector", "IT consulting" for US IT services) or an umbrella that contains
#    it (:data:`OTHER_SUBJECT_UMBRELLAS`: "tech sector", "AI trade", "TMT").  A
#    part that is neither -- semiconductors, software, equities, credit,
#    consumer, industrials -- is another industry, and a part naming one of
#    :data:`OTHER_SUBJECT_EXCLUDED` (a supplier industry: "AI infrastructure
#    semiconductors") is too, even beside a scope word.  One such part refuses
#    the whole name: "Hyperscalers / Industrials" is not only hyperscalers.
# 3. **This mission's geography.**  A mission whose industry ref is a US one
#    ("industry:us-…", "industry:美国-…") does not take a part qualified by
#    another region ("European IT Services", "China AI data center sector").
# 4. **An umbrella is not enough on its own.**  When no part names the
#    mission's industry itself, the statement's main clause must: one of the
#    industry's own collectives (:data:`INDUSTRY_COLLECTIVES` -- "hyperscaler",
#    "big tech", "data center" ...).  "The AI trade is de-grossing" is about
#    the trade; "hyperscaler capex drove the AI trade" is about hyperscalers.
#
# No other_subject at all refuses: the verifier did not say who it is about.

ABOUT_OTHER_RULE_REF = "claim-industry-reattribution:about-other-subject:v1"
#: Generic words a verifier's subject name may be made of (lowercase).  Only
#: what makes a word *not a company*; which industry it is comes below.
OTHER_SUBJECT_WORDS = frozenset((
    "hyperscaler", "hyperscalers", "hyperscale", "csp", "csps", "neocloud", "neoclouds",
    "mag7", "mag", "magnificent", "seven", "builders", "builder", "buildout", "build-out",
    "tmt", "info", "sector", "sectors", "industry", "industries", "market", "markets",
    "names", "peers", "peer", "players", "vendors", "providers", "provider", "service",
    "services", "cloud", "data", "center", "centers", "centre", "centres", "datacenter",
    "datacenters", "ai", "infrastructure", "infra", "capex", "compute", "trade", "complex",
    "tech", "technology", "it", "its", "consulting", "consultancies", "digital",
    "engineering", "outsourcing", "outsourcers", "bpo", "integrators", "systems", "us",
    "u.s.", "global", "general", "credit", "bond", "bonds", "index", "indices", "space",
    "group", "theme", "themes", "megacap", "mega-cap", "large-cap", "internet", "software",
    "hardware", "security", "cybersecurity", "consumer", "industrials", "industrial",
    "utilities", "energy", "power", "power-gen", "financials", "banks", "payments",
    "processors", "telecom", "media", "equities", "equity", "macro", "economy", "hedge",
    "funds", "semiconductor", "semiconductors", "semis", "semicaps", "chips", "memory",
    "optical", "supply", "chain", "suppliers", "coverage", "sentiment", "information",
    "business", "enablers", "adoption", "ecosystem", "ai-related", "gpus", "gpu",
    "communication", "communications", "companies", "vs", "and",
))
#: Chinese words a subject name may be made of; any other Chinese is unreadable.
OTHER_SUBJECT_CJK_WORDS: tuple[str, ...] = (
    "超大规模", "云厂商", "云计算", "数据中心", "算力", "人工智能", "科技", "行业", "板块",
    "美国", "资本开支", "外包", "咨询", "服务", "IT服务",
)
#: A subject name that is the mission's industry itself, by lexicon key.
OTHER_SUBJECT_SCOPE: Mapping[str, tuple[str, ...]] = {
    "hyperscaler": (
        "hyperscaler", "hyperscalers", "hyperscale", "cloud service providers",
        "cloud service provider", "cloud providers", "cloud provider", "csp", "csps", "cloud",
        "data center", "data centers", "data-center", "datacenter", "datacenters",
        "data centre", "data centres", "ai infrastructure", "ai infra", "ai capex",
        "ai compute", "compute", "neocloud", "neoclouds", "big tech", "mag7", "mag 7",
        "magnificent 7", "magnificent seven", "megacap tech", "mega-cap tech", "ai builders",
        "ai buildout", "ai build-out", "超大规模", "云厂商", "云计算", "数据中心", "算力",
    ),
    "it-services": (
        "it services", "it service", "it consulting", "consulting", "consultancies",
        "digital engineering", "digital it services", "digital its", "outsourcing",
        "outsourcers", "bpo", "systems integrators", "it服务", "外包", "咨询",
    ),
}
#: A subject name wider than the mission's industry that contains it.
OTHER_SUBJECT_UMBRELLAS: Mapping[str, tuple[str, ...]] = {
    "hyperscaler": ("tech", "technology", "ai trade", "ai sector", "ai market", "ai complex",
                    "tmt", "科技", "人工智能"),
    "it-services": ("info tech", "information technology", "technology services"),
}
#: Industries that sit next to the mission's and are not it: a part naming
#: one is another industry even beside a scope word.
OTHER_SUBJECT_EXCLUDED: Mapping[str, tuple[str, ...]] = {
    "hyperscaler": (
        "semiconductor", "semiconductors", "semis", "semicaps", "chips", "memory", "optical",
        "hardware", "supply chain", "suppliers", "industrials", "power-gen", "utilities",
        "software", "security", "cybersecurity", "consumer", "equities", "equity",
    ),
    "it-services": (
        "software", "semiconductor", "semiconductors", "semis", "payments", "processors",
        "hardware", "security", "cybersecurity", "hedge", "equities", "equity",
    ),
}
#: Words that put a part in another region than a US mission's.
FOREIGN_REGION_WORDS: tuple[str, ...] = (
    "europe", "european", "eu", "uk", "british", "sterling", "china", "chinese", "india",
    "indian", "japan", "japanese", "korea", "korean", "kospi", "asia", "asian", "apac",
    "emea", "taiwan", "taiwanese", "germany", "german", "france", "french", "canada",
    "canadian", "australia", "australian", "brazil", "latam", "中国", "欧洲", "印度",
    "日本", "韩国", "台湾",
)
#: A part made only of these says nothing of its own ("IT Services vendors /
#: industry"): it is skipped, neither in scope nor another industry.  "market"
#: is not one of them -- "hyperscalers / market" is also the broad market.
GENERIC_PART_WORDS = frozenset((
    "industry", "industries", "sector", "sectors", "group", "peers", "peer", "names",
    "vendors", "players", "companies", "space", "行业", "板块",
))
_US_INDUSTRY_RE = re.compile(r"^industry:(?:us-|美国-)")
_PART_SPLIT_RE = re.compile(
    r"\s*(?:/|&|\+|;|,|、|与|和|\bvs\.?(?=\s|$)|\bversus\b|\band\b)\s*", re.IGNORECASE)
_PAREN_RE = re.compile(r"[（(][^()（）]*[)）]")
_NAME_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9&.'’-]*")


def _other_subject_names(text: str, roster: Mapping[str, Sequence[str]]) -> list[str]:
    """The company names (or unknown capitalised words) in a subject name."""

    found = [f"covered:{ref}" for ref in _covered_named(text, roster)]
    for match in _NAME_TOKEN_RE.finditer(text):
        word = re.sub(r"(’s|'s)$", "", match.group(0)).rstrip(".'’-")
        if not word:
            continue
        lowered = word.lower()
        if lowered in OTHER_SUBJECT_WORDS or lowered in NEUTRAL_WORDS:
            continue
        if not (word[:1].isupper() or any(ch.isupper() for ch in word[1:])):
            continue  # an ordinary lowercase word names nobody
        found.append(word)
    rest = text
    for word in sorted(OTHER_SUBJECT_CJK_WORDS, key=len, reverse=True):
        rest = rest.replace(word, " ")
    if _CJK_RE.search(rest):
        found.append("unreadable:" + "".join(_CJK_RE.findall(rest))[:20])
    return found


def other_subject_scope(other_subject: Any, *, industry_ref: Any,
                        roster: Mapping[str, Sequence[str]]) -> dict[str, Any]:
    """Is the verifier's ``other_subject`` the mission's industry?  (``ABOUT_OTHER_RULE_REF``)

    Returns ``{"ok": bool, "refusal": str | None, "scope": "industry" |
    "umbrella" | None, "parts": [...]}``.  ``scope`` is ``umbrella`` when no
    part names the industry itself; the caller then also needs the
    statement to (:func:`statement_names_industry`).
    """

    result: dict[str, Any] = {"rule_ref": ABOUT_OTHER_RULE_REF, "ok": False,
                              "refusal": None, "scope": None, "parts": []}

    def refuse(reason: str) -> dict[str, Any]:
        result["refusal"] = reason
        return result

    if not isinstance(other_subject, str) or not other_subject.strip():
        return refuse("other_subject_missing")
    text = other_subject.strip()
    names = _other_subject_names(text, roster)
    covered = [name for name in names if name.startswith("covered:")]
    if covered:  # a covered company is never an industry, whichever the mission's is
        return refuse("other_subject_names_company:" + ",".join(names[:4]))
    key = lexicon_key(industry_ref)
    if key is None or key not in OTHER_SUBJECT_SCOPE:
        return refuse("no_industry_lexicon")
    if names:  # a name the vocabulary does not know is a company's until shown otherwise
        return refuse("other_subject_names_company:" + ",".join(names[:4]))
    body = _PAREN_RE.sub(" ", text)  # "(CSPs)": an alias, read as a name above
    parts = [part.strip(" .-").lower() for part in _PART_SPLIT_RE.split(body)]
    parts = [part for part in parts if part]
    result["parts"] = parts
    if not parts:
        return refuse("other_subject_missing")
    us_mission = bool(_US_INDUSTRY_RE.match(str(industry_ref)))
    in_scope = umbrella = 0
    for part in parts:
        if all(word in GENERIC_PART_WORDS for word in part.split()):
            continue
        if us_mission and _hits(part, FOREIGN_REGION_WORDS):
            return refuse(f"other_subject_other_region:{part}")
        excluded = _hits(part, OTHER_SUBJECT_EXCLUDED.get(key, ()))
        if excluded:
            return refuse(f"other_subject_other_industry:{part}")
        if _hits(part, OTHER_SUBJECT_SCOPE[key]):
            in_scope += 1
        elif _hits(part, OTHER_SUBJECT_UMBRELLAS.get(key, ())):
            umbrella += 1
        else:
            return refuse(f"other_subject_other_industry:{part}")
    if not in_scope and not umbrella:
        return refuse("other_subject_names_no_industry")
    result["scope"] = "industry" if in_scope else "umbrella"
    result["ok"] = True
    return result


def statement_names_industry(statement: Any, industry_ref: Any) -> list[str]:
    """The industry's own collectives the statement's main clause names."""

    key = lexicon_key(industry_ref)
    if key is None or not isinstance(statement, str):
        return []
    return _hits(main_clause(statement), INDUSTRY_COLLECTIVES.get(key, ()))


__all__ = [
    "ABOUT_OTHER_RULE_REF",
    "CO_OCCURRENCE_TERMS",
    "OTHER_SUBJECT_SCOPE",
    "OTHER_SUBJECT_UMBRELLAS",
    "other_subject_scope",
    "statement_names_industry",
    "COLLECTIVE_TERMS",
    "INDUSTRY_COLLECTIVES",
    "INDUSTRY_LEXICONS",
    "MIN_COVERED_FOR_COMPARISON",
    "MIN_STATEMENT_CHARS",
    "NEUTRAL_WORDS",
    "PRIOR_RULE_REFS",
    "RULE_REF",
    "judge",
    "lexicon_for",
    "lexicon_key",
    "unknown_names",
]
