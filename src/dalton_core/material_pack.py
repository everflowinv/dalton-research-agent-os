"""Batch C: the shared material pack every drafting step reads.

The research capability diagnosis (2026-10-09) traced the Initial Screen's
emptiness to its input: 120 number-free Claims (~0.5 % of ACN's 5.1 M
characters of acquired text).  ``build(state_dir, company_ref, product, as_of)``
assembles what an analyst would actually have open -- 150–250 K characters for
an Initial Screen -- deterministically and with no model call:

* passages from the evidence index (:mod:`evidence_index`: ``documents``,
  ``passages_for_document``, ``evidence_search``), rendered with
  ``render_passage_block`` as ``[P12]`` blocks bound to ``doc-passage:`` refs;
* the numbers pack (:mod:`numbers_pack`) rendered line by line with every
  figure tagged ``[N3]`` and bound to the JSON pointer it came from;
* legacy Claims ``[C7]`` only when the index holds no passage at all;
* our own prior products ``[O2]`` under 「我们的看法（非证据）」 -- context, never a
  source.

Recipes are data (``RECIPES``): sections, char budgets, windows.  Every
section is budgeted (``pack.budget``) and every input recorded
(``pack.inputs``) for provenance; ``pack.watermark`` is what
:mod:`redraft_policy` compares against later.  A blank workspace or a missing
index produces honest empty sections and ``sufficiency.status ==
"insufficient"`` -- never an exception.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

from . import material_sufficiency as ms

SCHEMA_VERSION = "material-pack:0.1"
OUR_VIEW_LABEL = "我们的看法（非证据）"
PRODUCTS = ms.PRODUCTS

#: Q&A topics an analyst listens for on any company's call (company-agnostic).
QA_TOPICS: tuple[str, ...] = (
    "guidance outlook full year growth", "bookings demand pipeline", "pricing discount competition",
    "margin operating expense investment", "AI generative agentic adoption revenue",
    "headcount utilization attrition", "capital allocation buyback acquisitions", "free cash flow",
)
EXPERT_TOPICS: tuple[str, ...] = (
    "pricing competition share wallet", "AI demand budgets spending", "vendor selection consolidation",
    "contract renewal delivery quality",
)
TENK_HEADING_RE = re.compile(r"(?i)item\s*1\b(?!\s*a)|item\s*1\.|business|segment|item\s*7\b|management.s discussion")
TENK_TEXT_RE = re.compile(r"(?i)segment|geographic market|industry group|type of work|competition|competitors|"
                          r"employees|revenues? by|reportable|clients?\b|backlog|bookings")
QA_START_RE = re.compile(
    r"(?i)question[- ]and[- ]answer|q\s*&\s*a session|first question|open (?:it|the call|the line|the lines)s? up"
    r"|our next question|问答环节|提问环节")
VALUATION_RE = re.compile(r"(?i)price target|target price|valuation|\bPT\b|multiple|P/E|EV/EBITDA|目标价|估值|"
                          r"upside|downside risk|risks to our")
ESTIMATE_RE = re.compile(r"(?i)\bEPS\b|estimates?|revenue|forecast|FY\d{2}|20\d\dE|预测|盈利")
NUMBER_RE = re.compile(r"\d[\d,]*\.?\d*")
_RATING_WORDS = (r"(strong buy|conviction buy|buy|overweight|outperform|accumulate|add|hold|neutral|"
                 r"equal[- ]?weight|market perform|sector perform|in[- ]line|peer perform|market[- ]weight|"
                 r"underweight|underperform|sell|reduce|买入|增持|中性|持有|减持|卖出)")
#: "(ACN US): Hold; ..." -- a rating word right after the ticker bracket or a colon in the title.
TITLE_RATING_RE = re.compile(r"(?i)(?:\)|:|;|\|)\s*" + _RATING_WORDS + r"(?![a-z])")
#: "Stock Rating: Equal-weight", "we reiterate our Buy rating", "upgrade to Outperform".
CONTEXT_RATING_RE = re.compile(
    r"(?i)(?:stock rating|rating|rated|reiterat\w*|maintain\w*|initiat\w*|reinstat\w*|upgrad\w*|downgrad\w*|"
    r"评级)\W{0,25}" + _RATING_WORDS + r"(?![a-z])")
_BULL = {"strong buy", "conviction buy", "buy", "overweight", "outperform", "accumulate", "add", "买入", "增持"}
_BEAR = {"underweight", "underperform", "sell", "reduce", "减持", "卖出"}


def _section(key: str, title: str, source: str, budget: int, **params: Any) -> dict[str, Any]:
    return {"key": key, "title": title, "source": source, "budget": budget, **params}


#: product -> recipe.  ``budget`` is characters of rendered text per section.
RECIPES: Mapping[str, Mapping[str, Any]] = {
    "initial_screen": {
        "version": "initial_screen:v1", "target_chars": [150_000, 250_000],
        "sections": [
            _section("numbers", "数字包（一致预期 / 券商 / 我们的模型与差异 / 估值 / 行情）", "numbers", 16_000),
            _section("latest_transcript", "最近一季电话会全文", "transcript_full", 60_000),
            _section("earnings_release", "最近一季业绩新闻稿（8-K 附件 99.1）", "release_full", 12_000),
            _section("prior_transcripts", "前三季电话会：管理层发言", "transcript_prepared", 27_000,
                     quarters=3, per_document=9_000),
            _section("transcript_qa", "前三季电话会：按主题检索的问答", "transcript_topics", 15_000,
                     quarters=3, topics=QA_TOPICS),
            _section("broker_reports", "近 6 个月券商研报（多空两派：摘要 / 估值 / 预测表）", "broker_reports", 42_000,
                     days=183, top_n=8, per_document=5_000, min_each_side=2),
            _section("sales_notes", "近 90 天卖方销售快报中关于本公司的段落", "sales_notes", 15_000, days=90),
            _section("filing_10k", "最新 10-K：业务与分部", "filing_sections", 15_000, kinds=["sec_10k"], days=430),
            _section("expert_calls", "专家访谈（Guidepoint）相关段落", "expert_calls", 12_000, days=365,
                     topics=EXPERT_TOPICS),
            _section("our_views", OUR_VIEW_LABEL, "our_products", 15_000,
                     products=["company_dossier", "debate_map", "previous_version", "event_judgement"]),
            _section("legacy_claims", "旧结论（仅在没有原文段落时）", "claims", 5_000),
        ],
    },
    "investment_memo": {
        "version": "investment_memo:v1", "target_chars": [150_000, 230_000],
        "sections": [
            _section("numbers", "数字包", "numbers", 16_000),
            _section("latest_transcript", "最近一季电话会全文", "transcript_full", 50_000),
            _section("earnings_release", "最近一季业绩新闻稿", "release_full", 12_000),
            _section("prior_transcripts", "前三季电话会：管理层发言", "transcript_prepared", 21_000,
                     quarters=3, per_document=7_000),
            _section("transcript_qa", "前三季电话会：主题问答", "transcript_topics", 12_000, quarters=3, topics=QA_TOPICS),
            _section("broker_reports", "近 6 个月券商研报（多空两派）", "broker_reports", 45_000,
                     days=183, top_n=9, per_document=5_000, min_each_side=2),
            _section("sales_notes", "近 90 天销售快报", "sales_notes", 12_000, days=90),
            _section("filing_10k", "最新 10-K：业务与分部", "filing_sections", 12_000, kinds=["sec_10k"], days=430),
            _section("expert_calls", "专家访谈", "expert_calls", 10_000, days=365, topics=EXPERT_TOPICS),
            _section("our_views", OUR_VIEW_LABEL, "our_products", 20_000,
                     products=["initial_screen", "company_dossier", "debate_map", "company_scenario",
                               "previous_version", "event_judgement"]),
            _section("legacy_claims", "旧结论（仅在没有原文段落时）", "claims", 5_000),
        ],
    },
    "dossier": {
        "version": "dossier:v1", "target_chars": [100_000, 170_000],
        "sections": [
            _section("numbers", "数字包", "numbers", 14_000),
            _section("latest_transcript", "最近一季电话会全文", "transcript_full", 40_000),
            _section("prior_transcripts", "前三季电话会：管理层发言", "transcript_prepared", 24_000,
                     quarters=3, per_document=8_000),
            _section("broker_reports", "近 6 个月券商研报", "broker_reports", 30_000, days=183, top_n=6,
                     per_document=5_000, min_each_side=2),
            _section("filing_10k", "最新 10-K：业务、分部与竞争", "filing_sections", 25_000, kinds=["sec_10k"], days=430),
            _section("expert_calls", "专家访谈", "expert_calls", 10_000, days=365, topics=EXPERT_TOPICS),
            _section("our_views", OUR_VIEW_LABEL, "our_products", 8_000,
                     products=["previous_version", "debate_map"]),
            _section("legacy_claims", "旧结论（仅在没有原文段落时）", "claims", 5_000),
        ],
    },
    "debate_map": {
        "version": "debate_map:v1", "target_chars": [70_000, 110_000],
        "sections": [
            _section("numbers", "数字包", "numbers", 8_000),
            _section("broker_reports", "近 6 个月券商研报（多空两派）", "broker_reports", 40_000, days=183, top_n=10,
                     per_document=4_000, min_each_side=3),
            _section("transcript_qa", "近四季电话会：主题问答", "transcript_topics", 20_000, quarters=4,
                     include_latest=True, topics=QA_TOPICS),
            _section("sales_notes", "近 90 天销售快报", "sales_notes", 12_000, days=90),
            _section("expert_calls", "专家访谈", "expert_calls", 10_000, days=365, topics=EXPERT_TOPICS),
            _section("our_views", OUR_VIEW_LABEL, "our_products", 8_000, products=["previous_version", "company_dossier"]),
            _section("legacy_claims", "旧结论（仅在没有原文段落时）", "claims", 4_000),
        ],
    },
    # Batch F: one result-type event (8-K Item 2.02 / earnings release, its
    # call, a guidance change).  Built with ``as_of`` = event day + 3, so every
    # ``days`` window below reads [event day - 3, event day + 3] -- the same
    # event's documents and nothing else -- except the previews, which reach
    # 14 days before the event.  The numbers section is the pre-event baseline
    # (the caller's ``numbers_fn`` reads it as of the day before the event).
    "event_judgement": {
        "version": "event_judgement:v2", "target_chars": [24_000, 60_000],
        "sections": [
            _section("numbers", "预期基准数字包（事件前一日的一致预期 / 券商 / 行情）", "numbers", 6_000),
            _section("earnings_release", "同事件业绩新闻稿（8-K 附件 99.1，事件日 ±3 天）", "release_full", 14_000,
                     days=6),
            _section("latest_transcript", "同事件电话会（事件日 ±3 天，截取）", "transcript_full", 14_000, days=6),
            _section("sales_notes", "事件日 ±3 天的卖方销售快报", "sales_notes", 6_000, days=6),
            _section("broker_reports", "事件日 ±3 天的券商快评", "broker_reports", 6_000, days=6, top_n=4,
                     per_document=2_000, min_each_side=1),
            _section("previews", "事件前 14 天的券商预览与销售快报（预期基准）", "topic_passages", 6_000,
                     kinds=["broker_report", "sales_note"], days=17, end_days_before=4,
                     topics=("preview expect guide guidance", "consensus expectations quarter")),
            _section("news", "事件日 ±3 天的新闻", "topic_passages", 3_000, kinds=["web_page"], days=6,
                     topics=("results earnings guidance",)),
            _section("our_views", OUR_VIEW_LABEL, "our_products", 3_000, products=["previous_version"]),
        ],
    },
    "earnings_preview": {
        "version": "earnings_preview:v1", "target_chars": [60_000, 90_000],
        "sections": [
            _section("numbers", "数字包（一致预期 / 券商 / 指引）", "numbers", 14_000),
            _section("prior_transcripts", "上一季电话会：管理层发言", "transcript_prepared", 20_000,
                     quarters=1, per_document=20_000, include_latest=True),
            _section("broker_reports", "近 60 天券商预览", "broker_reports", 30_000, days=60, top_n=6,
                     per_document=5_000, min_each_side=2),
            _section("sales_notes", "近 30 天销售快报", "sales_notes", 8_000, days=30),
            _section("our_views", OUR_VIEW_LABEL, "our_products", 8_000, products=["previous_version", "company_dossier"]),
        ],
    },
    "industry_framework": {
        "version": "industry_framework:v1", "target_chars": [60_000, 120_000],
        "sections": [
            _section("industry_reports", "行业研报段落", "topic_passages", 50_000, kinds=["broker_report"], days=183,
                     topics=("industry demand spending budgets", "pricing competition share", "AI adoption impact",
                             "supply cost labor margins", "deal sizes bookings cycle")),
            _section("expert_calls", "专家访谈", "expert_calls", 25_000, days=365, topics=EXPERT_TOPICS),
            _section("sales_notes", "近 90 天销售快报", "topic_passages", 15_000, kinds=["sales_note"], days=90,
                     topics=("sector basket", "IT services demand")),
            _section("our_views", OUR_VIEW_LABEL, "our_products", 10_000, products=["previous_version"]),
        ],
    },
    "ask": {
        "version": "ask:v1", "target_chars": [30_000, 90_000],
        "sections": [
            _section("numbers", "数字包", "numbers", 8_000),
            _section("question_passages", "与问题相关的原文段落（跨资料类型）", "topic_passages", 60_000, days=365,
                     kinds=[], topics=()),
            _section("our_views", OUR_VIEW_LABEL, "our_products", 8_000, products=["previous_version", "event_judgement"]),
            _section("legacy_claims", "旧结论（仅在没有原文段落时）", "claims", 5_000),
        ],
    },
}

#: product -> the deliverable table/kind that holds its previous version.
PREVIOUS_VERSION: Mapping[str, tuple[str, ...]] = {
    "initial_screen": ("mission_deliverable", "initial_screen"),
    "investment_memo": ("mission_deliverable", "investment_memo"),
    "earnings_preview": ("mission_deliverable", "earnings_preview"),
    "dossier": ("company_dossier",), "debate_map": ("debate_map",), "event_judgement": ("event_judgement",),
    "industry_framework": ("industry_framework",), "ask": ("event_judgement",),
}
PRODUCT_SOURCES: Mapping[str, tuple[str, str, str, str]] = {
    # kind -> (table, identity/version column, subject column, order column)
    "company_dossier": ("company_dossier_versions", "version_id", "company_ref", "version_number"),
    "debate_map": ("debate_map_versions", "version_id", "subject_ref", "version_number"),
    "company_scenario": ("company_scenario_versions", "version_id", "company_ref", "version_number"),
    "event_judgement": ("event_judgements", "judgement_id", "company_ref", "created_at"),
    "industry_framework": ("industry_framework_versions", "version_id", "industry_ref", "version_number"),
    "mission_deliverable": ("mission_deliverable_versions", "version_id", "subject_ref", "version_number"),
}
PRODUCT_LABELS = {"company_dossier": "公司档案", "debate_map": "分歧图", "company_scenario": "场景",
                  "event_judgement": "事件研判", "industry_framework": "行业框架", "initial_screen": "初筛",
                  "investment_memo": "投资备忘录", "earnings_preview": "业绩前瞻"}
_TEXT_KEYS = frozenset({"title", "summary", "body", "text", "statement", "question", "because", "verdict",
                        "rationale", "thesis", "headline", "content", "position", "note", "label", "decision",
                        "effect", "our_position", "description", "narrative"})
_SKIP_KEYS = frozenset({"unit_provenance", "input_fingerprints", "change_evidence_refs", "drafted_at", "bindings",
                        "drafted_by", "verified_by", "claim_refs", "citations", "evidence_refs", "driver_refs",
                        "thesis_refs", "verifier", "verifier_model", "model", "content_hash", "body_hash",
                        "id", "actor_ref", "schema_version", "mission_version_hash", "playbook_version_hash"})


class MaterialPackError(ValueError):
    pass


@dataclass
class MaterialPack:
    company_ref: str
    product: str
    as_of: str
    recipe_version: str
    text: str
    tags: dict[str, str]
    json: dict[str, Any]
    sufficiency: dict[str, Any]
    budget: dict[str, Any]
    inputs: list[dict[str, Any]]
    watermark: dict[str, Any]
    sections: list[dict[str, Any]] = field(default_factory=list)
    content_hash: str = ""
    built_at: str = ""

    def record(self) -> dict[str, Any]:
        """The compact provenance block a product version stores (and redraft_policy reads)."""

        return {"schema_version": SCHEMA_VERSION, "company_ref": self.company_ref, "product": self.product,
                "as_of": self.as_of, "recipe_version": self.recipe_version, "content_hash": self.content_hash,
                "built_at": self.built_at, "total_chars": len(self.text),
                "sections": {key: {"used": value["used"], "limit": value["limit"], "items": value["items"]}
                             for key, value in self.budget.items() if not key.startswith("_")},
                "inputs": [{k: item.get(k) for k in ("kind", "ref", "sha256", "date", "doc_kind") if item.get(k) is not None}
                           for item in self.inputs],
                "watermark": self.watermark,
                "sufficiency": {"status": self.sufficiency.get("status"),
                                "missing": [item["key"] for item in self.sufficiency.get("missing") or ()],
                                "gap_request_refs": self.sufficiency.get("gap_request_refs") or []}}

    def resolve(self, tag: str) -> dict[str, Any] | None:
        """``P12`` / ``N3`` / ``C7`` / ``O2`` -> what it is bound to."""

        tag = tag.strip("[]")
        ref = self.tags.get(tag)
        if ref is None:
            return None
        if tag.startswith("N"):
            return {"kind": "number", "ref": ref, "value": resolve_number(self.json, ref),
                    **(self.json.get("number_tags") or {}).get(tag, {})}
        kind = {"P": "passage", "C": "claim", "O": "our_view"}.get(tag[0], "unknown")
        meta = (self.json.get("passage_tags") or {}).get(tag, {}) if kind == "passage" else {}
        return {**meta, "kind": kind, "ref": ref}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _sha(value: Any) -> str:
    data = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _dec(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def pointer(*parts: Any) -> str:
    return "/" + "/".join(_escape(str(part)) for part in parts)


def resolve_number(pack_json: Mapping[str, Any], number_ref: str) -> Any:
    """``pack-json:/numbers/consensus/periods/0/eps/mean`` -> the value in ``pack.json``."""

    if not isinstance(number_ref, str) or not number_ref.startswith("pack-json:"):
        raise MaterialPackError(f"not a number ref: {number_ref!r}")
    node: Any = pack_json
    for raw in number_ref[len("pack-json:"):].lstrip("/").split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, list):
            node = node[int(token)]
        elif isinstance(node, Mapping):
            node = node[token]
        else:
            raise MaterialPackError(f"{number_ref}: path does not exist")
    return node


class _Tags:
    """Global tag counters and the tag -> ref map for one pack."""

    def __init__(self) -> None:
        self.next = {"P": 1, "N": 1, "C": 1, "O": 1}
        self.refs: dict[str, str] = {}
        self.numbers: dict[str, dict[str, Any]] = {}
        #: ``P12`` -> issuer / doc kind / house / date of the passage it binds.
        self.passages: dict[str, dict[str, Any]] = {}
        self._by_pointer: dict[str, str] = {}

    def number(self, root: Mapping[str, Any], parts: Sequence[Any], *, display: str | None = None,
               transform: str | None = None) -> str | None:
        """``[N3] display`` for the value at ``parts`` under the pack json, or None when absent."""

        node: Any = root
        for part in parts:
            if isinstance(node, list) and isinstance(part, int) and 0 <= part < len(node):
                node = node[part]
            elif isinstance(node, Mapping) and part in node:
                node = node[part]
            else:
                return None
        if node is None or isinstance(node, (dict, list)) or node == "":
            return None
        path = pointer(*parts)
        tag = self._by_pointer.get(path)
        if tag is None:
            tag = f"N{self.next['N']}"
            self.next["N"] += 1
            self._by_pointer[path] = tag
            self.refs[tag] = "pack-json:" + path
            self.numbers[tag] = {"pointer": path, "value": node, "display": display or str(node),
                                 **({"transform": transform} if transform else {})}
        return f"[{tag}] {display or node}"

    def other(self, kind: str, ref: str) -> str:
        tag = f"{kind}{self.next[kind]}"
        self.next[kind] += 1
        self.refs[tag] = ref
        return tag


def _scaled(value: Any, divisor: int, suffix: str, quantum: str = "0.01") -> str | None:
    parsed = _dec(value)
    if parsed is None:
        return None
    return f"{format((parsed / Decimal(divisor)).quantize(Decimal(quantum)), 'f')}{suffix}"


# ---------------------------------------------------------------------------
# the build context
# ---------------------------------------------------------------------------


class _Build:
    def __init__(self, state_dir: Path, company_ref: str, product: str, as_of: str, *,
                 topics: Sequence[str] | None, numbers_fn: Callable[..., Mapping[str, Any]] | None,
                 ranked_passages: Sequence[Mapping[str, Any]] | None = None) -> None:
        from . import evidence_index

        self.ei = evidence_index
        self.state_dir = state_dir
        self.company_ref = company_ref
        self.product = product
        self.as_of = as_of
        self.topics = list(topics or ())
        #: Batch G: a caller that has already retrieved and reranked passages
        #: (the ask path: dates, question type, kind weights) hands them in and
        #: the topic section renders exactly those, in that order, instead of
        #: searching again.
        self.ranked_passages = None if ranked_passages is None else [dict(item) for item in ranked_passages]
        self.numbers_fn = numbers_fn
        self.tags = _Tags()
        self.inputs: dict[tuple[str, str], dict[str, Any]] = {}
        self.used_spans: dict[str, list[tuple[int, int]]] = {}
        self.json: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "company_ref": company_ref,
                                     "product": product, "as_of": as_of}
        connection = ms.open_core_ro(state_dir)
        try:
            self.identity = ms.company_identity(connection, company_ref, state_dir)
            self.mission = ms.mission(connection)
        finally:
            if connection is not None:
                connection.close()
        self.has_index = ms._has_index(state_dir)
        self._transcripts: list[dict[str, Any]] | None = None
        self._qa_start: dict[str, int] = {}
        self.passage_count = 0

    # -- inputs / dedupe ------------------------------------------------------
    def note_input(self, kind: str, ref: str | None, **extra: Any) -> None:
        if not ref:
            return
        self.inputs.setdefault((kind, ref), {"kind": kind, "ref": ref, **{k: v for k, v in extra.items()
                                                                         if v is not None}})

    def overlaps(self, document_ref: str, start: int, end: int) -> bool:
        for a, b in self.used_spans.get(document_ref, ()):
            if min(b, end) - max(a, start) > 0.5 * (end - start):
                return True
        return False

    def mark(self, document_ref: str, start: int, end: int) -> None:
        self.used_spans.setdefault(document_ref, []).append((start, end))

    def trimmed(self, passages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """Consecutive passages of one document with the overlap cut, each re-reffed to its exact span."""

        out = []
        cursor = -1
        for item in passages:
            start, end = int(item["start"]), int(item["end"])
            if end <= cursor:
                continue
            if start < cursor:
                cut = cursor - start
                text = str(item["text"])[cut:]
                start = cursor
                view = dict(item)
                view.update({"start": start, "text": text,
                             "passage_ref": self.ei.make_passage_ref(item["document_ref"], start, end, text)})
            else:
                view = dict(item)
            if not str(view["text"]).strip():
                cursor = end
                continue
            out.append(view)
            cursor = end
        return out

    def render(self, passages: Sequence[Mapping[str, Any]], budget: int, *, header: str | None = None,
               stop_at_first_overflow: bool = False) -> tuple[str, int, int]:
        """Render passages as [P…] blocks within ``budget``; returns (text, items, dropped)."""

        chunks: list[str] = []
        used = len(header) + 1 if header else 0
        items = dropped = 0
        labels = {self.company_ref: str(self.identity.get("ticker") or self.identity.get("name"))}
        for item in passages:
            if self.overlaps(item["document_ref"], int(item["start"]), int(item["end"])):
                continue
            block = self.ei.render_passage_block([item], start=self.tags.next["P"], company_labels=labels)
            if used + len(block["text"]) > budget:
                dropped += 1
                if stop_at_first_overflow:
                    break
                continue
            self.tags.next["P"] = block["next_number"]
            self.tags.refs.update(block["tags"])
            for tag in block["tags"]:
                self.tags.passages[tag] = self.passage_meta(item)
            chunks.append(block["text"])
            used += len(block["text"])
            items += 1
            self.passage_count += 1
            self.mark(item["document_ref"], int(item["start"]), int(item["end"]))
            self.note_input("document", item["document_ref"], sha256=item.get("document_sha256"),
                            date=item.get("date"), doc_kind=item.get("kind"))
        body = "\n".join(chunk.rstrip("\n") for chunk in chunks)
        if header and body:
            body = header + "\n" + body
        return body, items, dropped

    def passage_meta(self, item: Mapping[str, Any]) -> dict[str, Any]:
        """Who a ``[P…]`` passage belongs to: the issuer, document kind, house and date.

        The issuer is the document's filer (the subject when it is one of
        several filers); a document nobody filed belongs to the one company it
        lists or names, else to nobody (``company_ref`` absent).
        """

        filed = [str(ref) for ref in item.get("filed_company_refs") or () if ref]
        named = [str(ref) for ref in item.get("company_refs") or () if ref]
        if filed:
            owner: str | None = self.company_ref if self.company_ref in filed else filed[0]
        elif len(named) == 1:
            owner = named[0]
        else:
            owner = None
        meta = {"document_ref": item.get("document_ref"), "company_ref": owner,
                "company_refs": named[:8] or None, "doc_kind": item.get("kind"),
                "house": item.get("house"), "date": item.get("date"), "period": item.get("period")}
        return {key: value for key, value in meta.items() if value not in (None, "", [])}

    def passages(self, document_ref: str) -> list[dict[str, Any]]:
        try:
            return self.ei.passages_for_document(self.state_dir, document_ref)
        except Exception:  # noqa: BLE001 - an unreadable document is an absent one
            return []

    def search(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        try:
            found = self.ei.evidence_search(self.state_dir, query[:self.ei.MAX_QUERY_CHARS], **kwargs)
        except Exception:  # noqa: BLE001
            return []
        return list(found.get("results") or []) if found.get("status") == "ok" else []

    # -- transcripts ------------------------------------------------------------
    def transcripts(self) -> list[dict[str, Any]]:
        """The company's own call transcripts, newest first, one per quarter."""

        if self._transcripts is None:
            docs = ms.own_documents(self.state_dir, self.company_ref, self.identity, ["transcript"],
                                    as_of=self.as_of, days=760)
            out, seen = [], set()
            for doc in docs:
                key = doc.get("period") or str(doc.get("date") or "")[:7]
                if key in seen:
                    continue
                seen.add(key)
                out.append(doc)
            self._transcripts = out
        return self._transcripts

    def qa_start(self, document_ref: str, passages: Sequence[Mapping[str, Any]]) -> int:
        if document_ref not in self._qa_start:
            start = len(passages)
            for index, item in enumerate(passages):
                if index > 0 and QA_START_RE.search(str(item.get("text") or "")):
                    start = index
                    break
            self._qa_start[document_ref] = start
        return self._qa_start[document_ref]


# ---------------------------------------------------------------------------
# section builders: each returns (text, items, dropped, empty_reason)
# ---------------------------------------------------------------------------


def _numbers_section(build: _Build, spec: Mapping[str, Any], budget: int) -> tuple[str, int, int, str | None]:
    try:
        if build.numbers_fn is not None:
            pack = build.numbers_fn(build.state_dir, build.company_ref, build.as_of)
        else:
            from .numbers_pack import numbers_pack

            pack = numbers_pack(build.state_dir, build.company_ref, build.as_of)
        numbers = dict(pack.get("json") or {})
    except Exception as exc:  # noqa: BLE001 - a broken block is an empty section, not a crash
        build.json["numbers"] = None
        return "", 0, 0, f"数字包读取失败：{type(exc).__name__}: {str(exc)[:200]}"
    build.json["numbers"] = numbers
    build.json["numbers_hash"] = _sha(numbers)
    consensus_version = ((numbers.get("consensus") or {}).get("version") or {})
    build.note_input("consensus_version", consensus_version.get("ref"), date=consensus_version.get("as_of"))
    our = _our_model(build, numbers)
    build.json["our_model"] = our
    lines = _number_lines(build, numbers, our)
    kept, used = [], 0
    for line in lines:
        if used + len(line) + 1 > budget:
            continue
        kept.append(line)
        used += len(line) + 1
    empty = None
    if not build.tags.numbers:
        empty = "数字包里没有可用的一致预期、券商预测、估值或行情"
    return "\n".join(kept), len(kept), len(lines) - len(kept), empty


def _our_model(build: _Build, numbers: Mapping[str, Any]) -> dict[str, Any]:
    from .model_eps_basis import read_model_eps_basis

    connection = ms.open_core_ro(build.state_dir)
    try:
        version = ms.model_version(connection, build.company_ref)
        eps_definition = read_model_eps_basis(connection, (version or {}).get("version_ref"),
                                              company_ref=build.company_ref)
    finally:
        if connection is not None:
            connection.close()
    rows = {row.get("source"): row for row in (numbers.get("valuation") or {}).get("forward_pe") or ()}
    ours, vendor, street = rows.get("ours") or {}, rows.get("consensus_vendor") or {}, rows.get("street_median") or {}
    out: dict[str, Any] = {"model": version, "ntm_eps": ours.get("value") if ours.get("status") == "available" else None,
                           "units": {"eps": ours.get("unit")},
                           "status": ours.get("status") or "unavailable", "reason": ours.get("reason"),
                           "method": ours.get("method"), "quarters": ours.get("quarters"),
                           "eps_basis": eps_definition["basis"],
                           "eps_basis_proof": eps_definition["proof"],
                           "eps_basis_reason": eps_definition["reason"],
                           "ours_vs_consensus": None, "ours_vs_street": None}
    if version:
        build.note_input("model_version", version["version_ref"], date=str(version.get("created_at"))[:10])
    out["years"] = our_model_years(build.state_dir, build.company_ref, numbers,
                                   version_ref=(version or {}).get("version_ref"))
    mine = _dec(out["ntm_eps"])
    for key, other in (("ours_vs_consensus", vendor), ("ours_vs_street", street)):
        theirs = _dec(other.get("value")) if other.get("status") == "available" else None
        if mine is not None and theirs:
            out[key] = {"ours": out["ntm_eps"], "theirs": other.get("value"),
                        "diff_pct": format(((mine - theirs) / abs(theirs) * 100).quantize(Decimal("0.1")), "f")}
    return out


_FY_RE = re.compile(r"^FY(\d{4})")
_PROJECTION_LABEL_RE = re.compile(r"^FY(\d{4})(?:E|A/E)$")
#: Our model's fiscal-year rows: metric -> (model result line, consensus field).
OUR_YEAR_METRICS: tuple[tuple[str, str], ...] = (("revenue", "result:revenue"), ("eps", "result:diluted_eps"))
SUM_OF_QUARTERS_FLAG = "按四季合计"


def _model_record(connection: Any, company_ref: str, version_ref: str | None) -> tuple[dict | None, dict | None]:
    """The newest model version's record and its stored annual projection (either may be None)."""

    if connection is None or not ms.table_exists(connection, "forecast_model_versions"):
        return None, None
    if version_ref:
        row = connection.execute("SELECT version_id, record_json FROM forecast_model_versions WHERE version_id=?",
                                 (version_ref,)).fetchone()
    else:
        row = connection.execute("SELECT version_id, record_json FROM forecast_model_versions WHERE company_ref=? "
                                 "ORDER BY version_number DESC LIMIT 1", (company_ref,)).fetchone()
    if row is None:
        return None, None
    record = json.loads(row["record_json"])
    projection = None
    if ms.table_exists(connection, "forecast_model_annual_projections"):
        held = connection.execute("SELECT record_json FROM forecast_model_annual_projections WHERE model_version_id=?",
                                  (row["version_id"],)).fetchone()
        if held is not None:
            projection = json.loads(held["record_json"])
    return record, projection


def _fye_month(numbers: Mapping[str, Any], projection: Mapping[str, Any] | None) -> int | None:
    fye = str(((numbers.get("consensus") or {}).get("calendar") or {}).get("fiscal_year_end") or "")
    if re.fullmatch(r"\d{2}-\d{2}", fye):
        return int(fye[:2])
    month = ((projection or {}).get("calendar_binding") or {}).get("fiscal_year_end_month")
    return int(month) if month else None


def our_model_years(state_dir: str | Path, company_ref: str, numbers: Mapping[str, Any], *,
                    version_ref: str | None = None) -> list[dict[str, Any]]:
    """Our model's FY+1 / FY+2 revenue and diluted EPS beside the consensus for the same years.

    The years are the consensus's FY1/FY2 labels (else the projection's next
    two forecast years).  A year's figure is the stored annual projection's
    when it computed one; otherwise the sum of the model's four forecast
    quarters of that fiscal year (``result:revenue`` / ``result:diluted_eps``),
    flagged 「按四季合计」 for revenue. Annual EPS requires the bound annual
    profit/share method; quarterly EPS is never added into annual EPS.
    Each row carries ``ours_vs_consensus`` with the % gap pre-computed.
    """

    from .financial_period import month_end

    connection = ms.open_core_ro(Path(state_dir))
    try:
        record, projection = _model_record(connection, company_ref, version_ref)
        from .model_eps_basis import read_model_eps_basis
        eps_definition = read_model_eps_basis(connection, (record or {}).get("id") or version_ref,
                                              company_ref=company_ref)
    finally:
        if connection is not None:
            connection.close()
    consensus = numbers.get("consensus") or {}
    periods = [p for p in consensus.get("periods") or () if _FY_RE.match(str(p.get("label") or ""))]
    labels = [str(p["label"]) for p in periods[:2]]
    projected = {}
    for item in (projection or {}).get("periods") or ():
        match = _PROJECTION_LABEL_RE.match(str(item.get("label") or ""))
        if match:
            projected[f"FY{match.group(1)}"] = item
    if not labels:
        labels = [label for label, item in projected.items() if str(item.get("kind")) == "forecast"][:2]
    fye_month = _fye_month(numbers, projection)
    cells: dict[str, dict[str, Decimal]] = {}
    for line in (record or {}).get("results") or ():
        ref = str(line.get("ref") or "")
        if ref not in {line_ref for _, line_ref in OUR_YEAR_METRICS}:
            continue
        for cell in line.get("cells") or ():
            value = _dec(cell.get("value"))
            if cell.get("status") == "computed" and value is not None and cell.get("kind", "estimate") == "estimate":
                cells.setdefault(ref, {})[str((cell.get("period") or {}).get("end"))] = value
    units = {str(line.get("ref")): str(line.get("unit") or "").casefold() for line in (record or {}).get("results") or ()}
    out = []
    for index, label in enumerate(labels):
        year = int(label[2:6])
        row: dict[str, Any] = {"slot": f"FY{index + 1}", "label": label}
        consensus_period = next((p for p in periods if p.get("label") == label), {})
        for metric, line_ref in OUR_YEAR_METRICS:
            row[metric] = _our_year_value(record, projected.get(label), cells.get(line_ref) or {}, metric, line_ref,
                                          year, fye_month, units.get(line_ref, ""), month_end)
            row[metric]["unit"] = units.get(line_ref) or None
            if metric == "eps" and eps_definition["status"] == "available":
                row[metric]["eps_basis"] = eps_definition["basis"]
                row[metric]["eps_basis_proof"] = eps_definition["proof"]
            mine = _dec(row[metric].get("value"))
            theirs_raw = ((consensus_period.get(metric) or {}).get("mean")
                          if consensus_period.get("status", "available") == "available" else None)
            theirs = _dec(theirs_raw)
            key = f"{metric}_vs_consensus"
            if mine is not None and theirs:
                row[key] = {"ours": row[metric]["value"], "theirs": theirs_raw,
                            "diff_pct": format(((mine - theirs) / abs(theirs) * 100).quantize(Decimal("0.1")), "f")}
            else:
                row[key] = {"status": "unavailable",
                            "reason": ("我们的模型这一年没有数" if mine is None else
                                       (consensus_period.get("reason") or "一致预期这一年没有数"))}
        out.append(row)
    return out


def _our_year_value(record: Mapping[str, Any] | None, projected: Mapping[str, Any] | None,
                    cells: Mapping[str, Decimal], metric: str, line_ref: str, year: int, fye_month: int | None,
                    unit: str, month_end: Callable[[int, int], date]) -> dict[str, Any]:
    if record is None:
        return {"status": "unavailable", "value": None, "reason": "没有我们的预测模型"}
    quantum = "0.01" if metric == "eps" else "1"
    if projected is not None:
        outcome = (projected.get("forecast_eps") if metric == "eps"
                   else (projected.get("line_outcomes") or {}).get(line_ref)) or {}
        value = _dec(outcome.get("value"))
        if outcome.get("status") == "computed" and value is not None:
            return {"status": "available", "value": format(value.quantize(Decimal(quantum)), "f"),
                    "method": "annual_projection", "projection_label": projected.get("label")}
    if metric == "eps":
        return {"status": "unavailable", "value": None,
                "reason": "年度EPS没有绑定可计算的年度利润/股份方法；季度EPS不能相加为年度EPS"}
    if metric == "revenue" and unit not in ("", "usd"):
        return {"status": "unavailable", "value": None, "reason": f"模型收入单位 {unit} 不是美元"}
    if fye_month is None:
        return {"status": "unavailable", "value": None, "reason": "不知道财年截止月，无法把季度归入财年"}
    end = month_end(year, fye_month)
    start = month_end(year - 1, fye_month)
    quarters = sorted(day for day in cells if start.isoformat() < day <= end.isoformat())
    if len(quarters) != 4:
        return {"status": "unavailable", "value": None,
                "reason": f"模型只覆盖 FY{year} 的 {len(quarters)} 个预测季度，凑不满四季"
                          + ("（年度 EPS 未启用）" if metric == "eps" else "")}
    total = sum(cells[day] for day in quarters)
    return {"status": "available", "value": format(total.quantize(Decimal(quantum)), "f"),
            "method": "sum_of_four_quarters", "flag": SUM_OF_QUARTERS_FLAG, "quarters": quarters}


def _number_lines(build: _Build, numbers: Mapping[str, Any], our: Mapping[str, Any]) -> list[str]:
    root = build.json
    t = build.tags
    N = lambda *parts, **kw: t.number(root, ("numbers", *parts), **kw)  # noqa: E731
    lines = [f"（数字后的 [N…] 标签绑定数字包 JSON 字段；引用数字请带标签。截至 {numbers.get('as_of')}）"]
    market = numbers.get("market") or {}
    if market.get("status") == "ok":
        price = market.get("price") or {}
        parts = [f"收盘 {N('market', 'price', 'close')}（{price.get('date')}）"]
        cap = N("market", "market_cap", "value", display=_scaled((market.get("market_cap") or {}).get("value"),
                                                                 10 ** 9, "bn"), transform="÷1e9")
        if cap:
            parts.append(f"市值 {cap}")
        adv = market.get("adv_30d") or {}
        if adv.get("status") == "available":
            parts.append("30日ADV " + str(N("market", "adv_30d", "value",
                                            display=_scaled(adv.get("value"), 10 ** 6, "m美元", "0.1"),
                                            transform="÷1e6")))
        rng = market.get("range_52w") or {}
        if rng:
            parts.append(f"52周 {N('market', 'range_52w', 'low')}–{N('market', 'range_52w', 'high')}"
                         f"（距高点 {N('market', 'range_52w', 'from_high_percent')}%）")
        lines.append("行情：" + "；".join(str(p) for p in parts))
        rets = []
        for key, name in (("ytd", "YTD"), ("1m", "1月"), ("3m", "3月"), ("6m", "6月")):
            value = N("market", "returns", key, "percent")
            if value:
                rel = N("market", "relative_to_basket", "windows", key, "relative_pp")
                rets.append(f"{name} {value}%" + (f"（相对篮子 {rel}pp）" if rel else ""))
        if rets:
            lines.append("涨跌：" + "；".join(rets))
    else:
        lines.append(f"行情：无（{market.get('reason') or '未入库'}）")
    consensus = numbers.get("consensus") or {}
    if consensus.get("status") == "ok":
        stale = consensus.get("staleness") or {}
        cal = consensus.get("calendar") or {}
        lines.append(f"一致预期（数据商 {stale.get('vendor_as_of')}，{stale.get('vendor_days_old')} 天前；财年止 "
                     f"{cal.get('fiscal_year_end')}，已报告至 {cal.get('last_reported_period_end')}"
                     + ("，财期已按 8-K 业绩稿更正" if cal.get("advanced_by_release") else "") + "；EPS 为调整后口径）：")
        for index, period in enumerate(consensus.get("periods") or ()):
            if period.get("status") != "available":
                lines.append(f"  {period.get('slot')} {period.get('label')}：无（{period.get('reason')}）")
                continue
            eps = N("consensus", "periods", index, "eps", "mean")
            rev = N("consensus", "periods", index, "revenue", "mean",
                    display=_scaled((period.get("revenue") or {}).get("mean"), 10 ** 9, "bn"), transform="÷1e9")
            lines.append(f"  {period.get('slot')} {period.get('label')}：EPS {eps}（低 {N('consensus', 'periods', index, 'eps', 'low')}"
                         f"/高 {N('consensus', 'periods', index, 'eps', 'high')}，{N('consensus', 'periods', index, 'eps', 'count')} 家）"
                         + (f"；收入 {rev}" if rev else ""))
        for index, quarter in enumerate(consensus.get("quarters") or ()):
            eps = N("consensus", "quarters", index, "eps", "mean")
            rev = N("consensus", "quarters", index, "revenue", "mean",
                    display=_scaled((quarter.get("revenue") or {}).get("mean"), 10 ** 9, "bn"), transform="÷1e9")
            if eps or rev:
                lines.append(f"  {quarter.get('label')}：EPS {eps or '—'}；收入 {rev or '—'}")
        ntm = (consensus.get("ntm") or {}).get("eps") or {}
        if ntm.get("value") is not None:
            lines.append(f"  NTM EPS（未来四季）{N('consensus', 'ntm', 'eps', 'value')}")
        target = consensus.get("target") or {}
        if target.get("mean") is not None:
            lines.append(f"  目标价 均值 {N('consensus', 'target', 'mean')}/中位 {N('consensus', 'target', 'median') or '—'}"
                         f"/高 {N('consensus', 'target', 'high') or '—'}/低 {N('consensus', 'target', 'low') or '—'}"
                         f"（{N('consensus', 'target', 'count') or '—'} 家）")
        ratings = consensus.get("ratings") or {}
        if ratings:
            lines.append("  评级分布：" + "/".join(
                f"{name} {N('consensus', 'ratings', key)}" for key, name in
                (("strong_buy", "强买"), ("buy", "买"), ("hold", "持有"), ("sell", "卖"), ("strong_sell", "强卖"))
                if ratings.get(key) is not None))
        for index, item in enumerate(consensus.get("pre_report") or ()):
            shown = (N("consensus", "pre_report", index, "mean") if item.get("metric") == "eps" else
                     N("consensus", "pre_report", index, "mean", display=_scaled(item.get("mean"), 10 ** 9, "bn"),
                       transform="÷1e9"))
            lines.append(f"  会前一致预期（已公布期间）{item.get('label')} {item.get('metric')} {shown}")
    else:
        lines.append(f"一致预期：无（{consensus.get('reason') or '未入库'}）")
    street = numbers.get("street") or {}
    if street.get("periods") or street.get("targets"):
        lines.append(f"券商研报预测（{('、'.join(street.get('houses') or [])) or '—'}）：")
        for index, item in enumerate(street.get("periods") or ()):
            if item.get("period_kind") != "fiscal_year" or not item.get("count"):
                continue
            scale = {} if item.get("metric") == "eps" else {"divisor": 10 ** 9, "suffix": "bn"}

            def S(*parts: Any, value: Any = None) -> str | None:
                if not scale:
                    return N(*parts)
                return N(*parts, display=_scaled(value, scale["divisor"], scale["suffix"]), transform="÷1e9")

            houses = "，".join(f"{h.get('house')} {S('street', 'periods', index, 'houses', j, 'value', value=h.get('value'))}"
                              for j, h in enumerate(item.get("houses") or ()))
            lines.append(f"  {item.get('period')} {item.get('metric')}：中位 "
                         f"{S('street', 'periods', index, 'median', value=item.get('median'))}（{houses}）")
        for index, row in enumerate(street.get("targets") or ()):
            lines.append(f"  {row.get('broker')} {row.get('published_on')}：评级 {row.get('rating') or '—'}，"
                         f"目标价 {N('street', 'targets', index, 'target_value') or '—'}")
    outlook = numbers.get("outlook") or {}
    if outlook.get("items"):
        picks = []
        for index, item in enumerate(outlook["items"]):
            if item.get("kind") not in {"bookings", "book_to_bill", "growth", "guidance", "guidance_vs_actual"}:
                continue
            if item.get("low") is not None:
                value = f"{N('outlook', 'items', index, 'low')}–{N('outlook', 'items', index, 'high')}"
            else:
                value = N("outlook", "items", index, "value")
            if value:
                unit = "%" if item.get("unit") == "percent" else ({"billion": "bn", "million": "m"}.get(
                    item.get("scale") or "", ""))
                picks.append(f"{item.get('period') or ''} {item.get('metric')} {value}{unit}")
        if picks:
            lines.append(f"业绩稿要点（{outlook.get('release_period')}，{outlook.get('report_date')}）：" + "；".join(picks))
    valuation = numbers.get("valuation") or {}
    for index, row in enumerate(valuation.get("forward_pe") or ()):
        if row.get("status") == "available":
            lines.append(f"估值 {row.get('label')}：NTM EPS {N('valuation', 'forward_pe', index, 'value')} → 前瞻 P/E "
                         f"{N('valuation', 'forward_pe', index, 'forward_pe') or '—'}x")
        else:
            lines.append(f"估值 {row.get('label')}：不可得（{row.get('reason')}）")
    ev = N("valuation", "enterprise_value", "value",
           display=_scaled((valuation.get("enterprise_value") or {}).get("value"), 10 ** 9, "bn"), transform="÷1e9")
    if ev:
        lines.append(f"  EV {ev}；EV/EBITDA {N('valuation', 'ev_to_ebitda') or '—'}x；EV/Sales "
                     f"{N('valuation', 'ev_to_sales') or '—'}x")
    long_pe = valuation.get("long_pe") or {}
    if long_pe.get("status") == "available":
        lines.append(f"  长样本 P/E：当前 {N('valuation', 'long_pe', 'current')}x，第 "
                     f"{N('valuation', 'long_pe', 'percentile')} 百分位，中位 {N('valuation', 'long_pe', 'median')}x")
    for index, metric in enumerate(((valuation.get("snapshot") or {}).get("metrics")) or ()):
        if metric.get("status") == "available":
            lines.append(f"  {metric.get('metric')}：{N('valuation', 'snapshot', 'metrics', index, 'value')}"
                         f"（第 {N('valuation', 'snapshot', 'metrics', index, 'percentile') or '—'} 百分位）")
    # ours vs consensus
    model = our.get("model") or {}
    if our.get("ntm_eps") is not None:
        line = f"我们的模型（v{model.get('version_number')}）NTM EPS {t.number(root, ('our_model', 'ntm_eps'))}"
        for key, name in (("ours_vs_consensus", "一致预期"), ("ours_vs_street", "券商中位")):
            diff = our.get(key)
            if diff:
                word = "低" if str(diff.get("diff_pct") or "").startswith("-") else "高"
                line += (f"；{name} {t.number(root, ('our_model', key, 'theirs'))}，我们{word} "
                         f"{t.number(root, ('our_model', key, 'diff_pct'))}%")
        lines.append(line)
    else:
        lines.append(f"我们的模型：EPS 不可得（{our.get('reason') or '没有模型'}）"
                     + (f"；模型 v{model.get('version_number')}" if model else ""))
    for index, year in enumerate(our.get("years") or ()):
        for metric, name in (("revenue", "收入"), ("eps", "稀释 EPS")):
            cell = year.get(metric) or {}
            head = f"我们的模型 {year.get('slot')} {year.get('label')} {name}"
            if cell.get("status") != "available":
                lines.append(f"{head}：不可得（{cell.get('reason')}）")
                continue
            if metric == "revenue":
                ours = t.number(root, ("our_model", "years", index, metric, "value"),
                                display=_scaled(cell.get("value"), 10 ** 9, "bn"), transform="÷1e9")
            else:
                ours = t.number(root, ("our_model", "years", index, metric, "value"))
            line = f"{head} {ours}" + (f"（{cell['flag']}）" if cell.get("flag") else "（年度预测）")
            diff = year.get(f"{metric}_vs_consensus") or {}
            if diff.get("diff_pct") is not None:
                key = f"{metric}_vs_consensus"
                if metric == "revenue":
                    theirs = t.number(root, ("our_model", "years", index, key, "theirs"),
                                      display=_scaled(diff.get("theirs"), 10 ** 9, "bn"), transform="÷1e9")
                else:
                    theirs = t.number(root, ("our_model", "years", index, key, "theirs"))
                word = "低" if str(diff["diff_pct"]).startswith("-") else "高"
                line += (f"；一致预期 {theirs}；我们 vs 一致预期 {word} "
                         f"{t.number(root, ('our_model', 'years', index, key, 'diff_pct'))}%")
            else:
                line += f"；一致预期：无（{diff.get('reason')}）"
            lines.append(line)
    for flag in (numbers.get("cross_checks") or [])[:6]:
        lines.append(f"⚠ {flag.get('detail')}")
    return lines


def _transcript_full(build: _Build, spec: Mapping[str, Any], budget: int) -> tuple[str, int, int, str | None]:
    docs = build.transcripts()
    if spec.get("days"):
        floor = ms.days_before(build.as_of, int(spec["days"]))
        docs = [doc for doc in docs if str(doc.get("date") or "") >= floor]
    if not docs:
        return "", 0, 0, ("没有建立全文证据索引" if not build.has_index else "索引里没有这家公司的电话会纪要")
    doc = docs[0]
    passages = build.trimmed(build.passages(doc["document_ref"]))
    header = f"《{doc.get('title')}》{doc.get('date')} {doc.get('period') or ''}（全文 {doc.get('text_chars')} 字，{len(passages)} 段）"
    text, items, dropped = build.render(passages, budget, header=header, stop_at_first_overflow=True)
    if dropped:
        text += f"\n（预算到此为止：后 {len(passages) - items} 段未收入）"
    return text, items, dropped, None


def _release_full(build: _Build, spec: Mapping[str, Any], budget: int) -> tuple[str, int, int, str | None]:
    docs = ms.own_documents(build.state_dir, build.company_ref, build.identity, ["sec_exhibit"], as_of=build.as_of,
                            days=int(spec.get("days") or 200))
    if not docs:
        return "", 0, 0, ("没有建立全文证据索引" if not build.has_index else "索引里没有近期的业绩新闻稿")
    doc = docs[0]
    passages = build.trimmed(build.passages(doc["document_ref"]))
    text, items, dropped = build.render(passages, budget, header=f"《{doc.get('title')}》{doc.get('date')}",
                                        stop_at_first_overflow=True)
    return text, items, dropped, None


def _prior(build: _Build, spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    docs = build.transcripts()
    start = 0 if spec.get("include_latest") else 1
    return docs[start:start + int(spec.get("quarters") or 3)]


def _transcript_prepared(build: _Build, spec: Mapping[str, Any], budget: int) -> tuple[str, int, int, str | None]:
    docs = _prior(build, spec)
    if not docs:
        return "", 0, 0, ("没有建立全文证据索引" if not build.has_index else "索引里没有更早季度的电话会纪要")
    per = int(spec.get("per_document") or budget)
    chunks, items, dropped = [], 0, 0
    remaining = budget
    for doc in docs:
        passages = build.passages(doc["document_ref"])
        prepared = build.trimmed(passages[:build.qa_start(doc["document_ref"], passages)])
        text, n, d = build.render(prepared, min(per, remaining),
                                  header=f"《{doc.get('title')}》{doc.get('date')}：管理层发言",
                                  stop_at_first_overflow=True)
        if text:
            chunks.append(text)
            remaining -= len(text) + 1
        items += n
        dropped += d
    return "\n".join(chunks), items, dropped, None if chunks else "前几季纪要没有可收入的发言段"


def _transcript_topics(build: _Build, spec: Mapping[str, Any], budget: int) -> tuple[str, int, int, str | None]:
    docs = _prior(build, spec)
    if not docs:
        return "", 0, 0, ("没有建立全文证据索引" if not build.has_index else "索引里没有更早季度的电话会纪要")
    allowed = {}
    for doc in docs:
        passages = build.passages(doc["document_ref"])
        allowed[doc["document_ref"]] = build.qa_start(doc["document_ref"], passages)
    date_from = min(str(doc.get("date") or build.as_of) for doc in docs)
    candidates: dict[str, tuple[float, dict[str, Any]]] = {}
    topics = list(build.topics) + list(spec.get("topics") or ())
    for topic in topics:
        for item in build.search(topic, company_ref=build.company_ref, kinds=["transcript"], date_from=date_from,
                                 date_to=build.as_of, limit=40, per_document=4):
            start = allowed.get(item["document_ref"])
            if start is None or int(item.get("ordinal") or 0) < start:
                continue
            ref = item["passage_ref"]
            if ref not in candidates or candidates[ref][0] < float(item.get("score") or 0):
                candidates[ref] = (float(item.get("score") or 0), item)
    ranked = [item for _, item in sorted(candidates.values(), key=lambda pair: (-pair[0], pair[1]["passage_ref"]))]
    text, items, dropped = build.render(ranked, budget, header="（按主题检索的问答段落，按相关度排序）")
    return text, items, dropped, None if items else "前几季纪要的问答里没有检索到相关段落"


def _rating_stance(word: str | None) -> str | None:
    if not word:
        return None
    word = re.sub(r"\s+", " ", word.strip().lower()).replace("equalweight", "equal-weight")
    if word in _BULL:
        return "bull"
    if word in _BEAR:
        return "bear"
    return "neutral"


def _stance(title: str, rating: str | None, head: str) -> str | None:
    """bull / bear / neutral from the title's rating, the street table's rating, or the report's own rating line."""

    found = TITLE_RATING_RE.search(title or "")
    if found:
        return _rating_stance(found.group(1))
    if rating:
        match = re.search(_RATING_WORDS, rating, re.I)
        if match:
            return _rating_stance(match.group(1))
    found = CONTEXT_RATING_RE.search(head or "")
    return _rating_stance(found.group(1)) if found else None


def _house_key(name: Any) -> str:
    words = re.findall(r"[a-z0-9]+", str(name or "").lower())
    return words[0] if words else ""


def _broker_reports(build: _Build, spec: Mapping[str, Any], budget: int) -> tuple[str, int, int, str | None]:
    days = int(spec.get("days") or 183)
    docs = ms.own_documents(build.state_dir, build.company_ref, build.identity, ["broker_report"], as_of=build.as_of,
                            days=days, threshold=ms.BROKER_ABOUTNESS)
    if not docs:
        return "", 0, 0, ("没有建立全文证据索引" if not build.has_index else f"索引里没有近 {days} 天关于本公司的券商研报")
    numbers = build.json.get("numbers") or {}
    street = numbers.get("street") or {}
    targets = {_house_key(row.get("broker")): (index, row) for index, row in enumerate(street.get("targets") or ())}
    reference = date.fromisoformat(build.as_of)
    scored = []
    for doc in docs:
        try:
            age = max(0, (reference - date.fromisoformat(str(doc.get("date"))[:10])).days)
        except ValueError:
            age = days
        recency = 0.5 ** (age / 90.0)
        scored.append((recency * (0.4 + 0.6 * float(doc["aboutness"])), doc))
    scored.sort(key=lambda pair: (-pair[0], pair[1]["document_ref"]))
    by_house: dict[str, tuple[float, dict[str, Any]]] = {}
    for score, doc in scored:
        key = _house_key(doc.get("house")) or doc["document_ref"]
        by_house.setdefault(key, (score, doc))
    candidates = sorted(by_house.values(), key=lambda pair: (-pair[0], pair[1]["document_ref"]))
    for _, doc in candidates:
        target = targets.get(_house_key(doc.get("house")))
        rating = str(target[1].get("rating") or "") if target else None
        head = " ".join(str(p.get("text") or "")[:1500] for p in build.passages(doc["document_ref"])[:3])
        doc["stance"] = _stance(str(doc.get("title") or ""), rating, head) or "unknown"
    top_n = int(spec.get("top_n") or 8)
    each = int(spec.get("min_each_side") or 0)
    chosen: list[dict[str, Any]] = []
    bulls = [doc for _, doc in candidates if doc["stance"] == "bull"]
    others = [doc for _, doc in candidates if doc["stance"] in ("bear", "neutral")]
    for group in (bulls[:each], others[:each]):
        for doc in group:
            if doc not in chosen and len(chosen) < top_n:
                chosen.append(doc)
    for _, doc in candidates:
        if len(chosen) >= top_n:
            break
        if doc not in chosen:
            chosen.append(doc)
    chosen.sort(key=lambda doc: (str(doc.get("date") or ""), doc["document_ref"]), reverse=True)
    per = int(spec.get("per_document") or 5000)
    chunks, items, dropped = [], 0, 0
    remaining = budget
    stance_label = {"bull": "偏多", "bear": "偏空", "neutral": "中性", "unknown": "立场未识别"}
    for doc in chosen:
        if remaining < 600:
            dropped += 1
            continue
        passages = build.passages(doc["document_ref"])
        picks: list[int] = [0] if passages else []
        if len(passages) > 1 and len(str(passages[0].get("text") or "")) < 900:
            picks.append(1)
        valuation = [i for i, p in enumerate(passages) if i not in picks and VALUATION_RE.search(str(p.get("text") or ""))]
        estimates = [i for i, p in enumerate(passages)
                     if i not in picks and ESTIMATE_RE.search(str(p.get("text") or ""))
                     and len(NUMBER_RE.findall(str(p.get("text") or ""))) >= 15]
        picks += valuation[:2] + [i for i in estimates if i not in valuation[:2]][:2]
        selected = build.trimmed([passages[i] for i in sorted(set(picks))])
        meta = [f"券商：{doc.get('house') or '—'}（{stance_label[doc['stance']]}）", str(doc.get("date"))]
        target = targets.get(_house_key(doc.get("house")))
        if target:
            index, row = target
            meta.append(f"评级 {row.get('rating') or '—'}")
            value = build.tags.number(build.json, ("numbers", "street", "targets", index, "target_value"))
            if value:
                meta.append(f"目标价 {value}（{row.get('published_on')}）")
        for p_index, period in enumerate(street.get("periods") or ()):
            if period.get("metric") != "eps" or period.get("period_kind") != "fiscal_year":
                continue
            for h_index, house in enumerate(period.get("houses") or ()):
                if _house_key(house.get("house")) == _house_key(doc.get("house")):
                    value = build.tags.number(build.json, ("numbers", "street", "periods", p_index, "houses", h_index,
                                                           "value"))
                    meta.append(f"{period.get('period')} EPS {value}")
        header = "· ".join(meta) + f"\n《{doc.get('title')}》"
        text, n, d = build.render(selected, min(per, remaining), header=header)
        if text:
            chunks.append(text)
            remaining -= len(text) + 1
        items += n
        dropped += d
    build.json.setdefault("selection", {})["broker_reports"] = [
        {"document_ref": doc["document_ref"], "house": doc.get("house"), "date": doc.get("date"),
         "stance": doc["stance"], "aboutness": doc["aboutness"]} for doc in chosen]
    return "\n".join(chunks), items, dropped, None if chunks else "选中的研报没有可收入的段落"


def _sales_notes(build: _Build, spec: Mapping[str, Any], budget: int) -> tuple[str, int, int, str | None]:
    days = int(spec.get("days") or 90)
    query = " ".join(dict.fromkeys(build.identity.get("aliases") or [build.company_ref]))
    results = build.search(query or "company", company_ref=build.company_ref, kinds=["sales_note"],
                           date_from=ms.days_before(build.as_of, days), date_to=build.as_of, limit=80, per_document=2)
    relevant = [item for item in results if ms.mentions(build.identity, item.get("text"))]
    if not relevant:
        return "", 0, 0, ("没有建立全文证据索引" if not build.has_index else f"近 {days} 天的销售快报里没有提到本公司的段落")
    relevant.sort(key=lambda item: (str(item.get("date") or ""), float(item.get("score") or 0)), reverse=True)
    text, items, dropped = build.render(relevant, budget, header="（按日期由近到远）")
    return text, items, dropped, None


def _filing_sections(build: _Build, spec: Mapping[str, Any], budget: int) -> tuple[str, int, int, str | None]:
    kinds = list(spec.get("kinds") or ["sec_10k"])
    docs = ms.own_documents(build.state_dir, build.company_ref, build.identity, kinds, as_of=build.as_of,
                            days=int(spec.get("days") or 430))
    if not docs:
        return "", 0, 0, ("没有建立全文证据索引" if not build.has_index else "索引里没有近期的 10-K")
    doc = docs[0]
    passages = build.passages(doc["document_ref"])
    scored = []
    for index, item in enumerate(passages):
        heading = str(item.get("heading") or "")
        text = str(item.get("text") or "")
        score = (2 if TENK_HEADING_RE.search(heading) else 0) + min(3, len(TENK_TEXT_RE.findall(text)) // 2)
        if score:
            scored.append((score, index))
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    picked, used = [], 0
    for score, index in scored:
        length = len(str(passages[index].get("text") or "")) + 200
        if used + length > budget:
            continue
        picked.append(index)
        used += length
    selected = build.trimmed([passages[i] for i in sorted(picked)])
    text, items, dropped = build.render(selected, budget, header=f"《{doc.get('title')}》{doc.get('date')}")
    return text, items, dropped, None if items else "10-K 里没有找到业务/分部段落"


def _expert_calls(build: _Build, spec: Mapping[str, Any], budget: int) -> tuple[str, int, int, str | None]:
    days = int(spec.get("days") or 365)
    name = str(build.identity.get("name") or "")
    candidates: dict[str, tuple[float, dict[str, Any]]] = {}
    for topic in list(build.topics) + list(spec.get("topics") or ()):
        for item in build.search(f"{name} {topic}".strip(), company_ref=build.company_ref, kinds=["expert_call"],
                                 date_from=ms.days_before(build.as_of, days), date_to=build.as_of, limit=30,
                                 per_document=2):
            if not ms.mentions(build.identity, item.get("text")):
                continue
            ref = item["passage_ref"]
            if ref not in candidates or candidates[ref][0] < float(item.get("score") or 0):
                candidates[ref] = (float(item.get("score") or 0), item)
    ranked = [item for _, item in sorted(candidates.values(), key=lambda pair: (-pair[0], pair[1]["passage_ref"]))]
    if not ranked:
        return "", 0, 0, ("没有建立全文证据索引" if not build.has_index else "专家访谈里没有提到本公司的段落")
    text, items, dropped = build.render(ranked, budget)
    return text, items, dropped, None


def _topic_passages(build: _Build, spec: Mapping[str, Any], budget: int) -> tuple[str, int, int, str | None]:
    if build.ranked_passages is not None and not spec.get("kinds"):
        if not build.ranked_passages:
            return "", 0, 0, ("没有建立全文证据索引" if not build.has_index else "没有检索到与问题相关的段落")
        text, items, dropped = build.render(build.ranked_passages, budget)
        return text, items, dropped, None
    days = int(spec.get("days") or 365)
    kinds = list(spec.get("kinds") or ())
    topics = list(build.topics) + list(spec.get("topics") or ())
    if not topics:
        return "", 0, 0, "没有给出检索主题（问题）"
    company = build.company_ref if not str(build.company_ref).startswith("industry:") else None
    candidates: dict[str, tuple[float, dict[str, Any]]] = {}
    for topic in topics:
        # ``end_days_before``: a window that closes before ``as_of`` (batch F's
        # previews: what the street wrote before the event, not after it).
        date_to = ms.days_before(build.as_of, int(spec.get("end_days_before") or 0))
        for item in build.search(topic, company_ref=company, kinds=kinds or None,
                                 date_from=ms.days_before(build.as_of, days), date_to=date_to, limit=40,
                                 per_document=2):
            ref = item["passage_ref"]
            if ref not in candidates or candidates[ref][0] < float(item.get("score") or 0):
                candidates[ref] = (float(item.get("score") or 0), item)
    ranked = [item for _, item in sorted(candidates.values(), key=lambda pair: (-pair[0], pair[1]["passage_ref"]))]
    if not ranked:
        return "", 0, 0, ("没有建立全文证据索引" if not build.has_index else "没有检索到相关段落")
    text, items, dropped = build.render(ranked, budget)
    return text, items, dropped, None


def _product_text(record: Any, *, limit: int) -> str:
    """The readable prose of a product record (titles, bodies, positions), bounded."""

    lines: list[str] = []
    total = 0

    def walk(value: Any, key: str | None, depth: int) -> None:
        nonlocal total
        if total >= limit or depth > 6:
            return
        if isinstance(value, Mapping):
            title = value.get("title") or value.get("heading") or value.get("question")
            if isinstance(title, str) and title.strip() and depth > 0:
                normalized_title = re.sub(r"\s+", " ", title.strip())
                lines.append(f"§{normalized_title}")
                total += len(lines[-1])
            for child_key, child in value.items():
                if child_key in _SKIP_KEYS:
                    continue
                if child_key in ("title", "heading", "question") and isinstance(child, str):
                    continue
                walk(child, child_key, depth + 1)
        elif isinstance(value, list):
            for child in value:
                walk(child, key, depth + 1)
        elif isinstance(value, str) and key in _TEXT_KEYS and len(value.strip()) >= 12:
            text = re.sub(r"\s+", " ", value.strip())
            lines.append(f"{key}: {text}" if key not in ("body", "text", "summary", "because") else text)
            total += len(lines[-1])

    if isinstance(record, Mapping):
        for section in record.get("sections") or () if isinstance(record.get("sections"), list) else ():
            if isinstance(section, Mapping) and section.get("title"):
                lines.append(f"§{section['title']}")
                walk({k: v for k, v in section.items() if k != "title"}, None, 1)
            else:
                walk(section, "body", 1)
        rest = {k: v for k, v in record.items() if k != "sections"}
        walk(rest, None, 0)
        if isinstance(record.get("sections"), Mapping):
            walk(record["sections"], None, 0)
    out = "\n".join(lines)
    return out[:limit].rstrip() + ("…" if len(out) > limit else "")


def _our_products(build: _Build, spec: Mapping[str, Any], budget: int) -> tuple[str, int, int, str | None]:
    connection = ms.open_core_ro(build.state_dir)
    if connection is None:
        return "", 0, 0, "这个研究环境还没有数据库，没有我们的历史产品"
    rows: list[tuple[str, str, str, Mapping[str, Any]]] = []
    try:
        for product in spec.get("products") or ():
            if product == "previous_version":
                kinds = PREVIOUS_VERSION.get(build.product, ())
                if not kinds:
                    continue
                source, deliverable_kind = kinds[0], (kinds[1] if len(kinds) > 1 else None)
            elif product in ("initial_screen", "investment_memo", "earnings_preview"):
                source, deliverable_kind = "mission_deliverable", product
            else:
                source, deliverable_kind = product, None
            table, version_column, subject_column, order = PRODUCT_SOURCES[source]
            if not ms.table_exists(connection, table):
                continue
            where = f"{subject_column}=?"
            params: list[Any] = [build.company_ref]
            if deliverable_kind:
                where += " AND kind=?"
                params.append(deliverable_kind)
            if order == "created_at":
                where += " AND created_at <= ?"
                params.append(build.as_of + "T23:59:59")
            limit = 4 if source == "event_judgement" else 1
            found = connection.execute(
                f"SELECT {version_column} AS version_ref, {order} AS ordinal, created_at, record_json FROM {table} "
                f"WHERE {where} ORDER BY {order} DESC LIMIT ?", (*params, limit)).fetchall()
            label = PRODUCT_LABELS.get(deliverable_kind or source, source)
            if product == "previous_version":
                label = f"上一版{PRODUCT_LABELS.get(build.product, build.product)}"
            for row in found:
                try:
                    record = json.loads(row["record_json"])
                except (TypeError, ValueError):
                    continue
                rows.append((label, row["version_ref"], str(row["created_at"])[:10], record))
    finally:
        connection.close()
    if not rows:
        return "", 0, 0, "还没有我们自己的历史产品"
    seen: set[str] = set()
    chunks, items, dropped, used = [], 0, 0, 0
    share = max(1500, budget // max(1, len(rows)))
    for label, ref, day, record in rows:
        if ref in seen:
            continue
        seen.add(ref)
        body = _product_text(record, limit=min(share, budget - used - 200))
        if not body.strip():
            continue
        tag = build.tags.other("O", ref)
        block = f"[{tag}] {OUR_VIEW_LABEL} · {label} · {day}\n{body}"
        if used + len(block) + 1 > budget:
            build.tags.refs.pop(tag, None)
            dropped += 1
            continue
        chunks.append(block)
        used += len(block) + 1
        items += 1
        build.note_input("product_version", ref, date=day)
    header = f"（以下是我们自己以前的判断，只作背景；不是证据，不能作为出处引用 —— {OUR_VIEW_LABEL}）"
    return (header + "\n" + "\n".join(chunks)) if chunks else "", items, dropped, None if chunks else "历史产品没有可读正文"


def _claims(build: _Build, spec: Mapping[str, Any], budget: int) -> tuple[str, int, int, str | None]:
    if build.passage_count:
        return "", 0, 0, "已有原文段落，不收入旧结论"
    connection = ms.open_core_ro(build.state_dir)
    if connection is None or not ms.table_exists(connection, "claim_versions"):
        if connection is not None:
            connection.close()
        return "", 0, 0, "没有数据库或没有结论表"
    try:
        retired: set[str] = set()
        try:
            from .claim_retirement import retired_claim_version_refs

            retired = set(retired_claim_version_refs(connection))
        except Exception:  # noqa: BLE001 - no retirement tables: nothing retired
            retired = set()
        rows = connection.execute(
            "SELECT claim_version_id, claim_json, created_at FROM claim_versions "
            "WHERE json_extract(claim_json, '$.subject_ref')=? AND created_at <= ? ORDER BY created_at DESC LIMIT 400",
            (build.company_ref, build.as_of + "T23:59:59")).fetchall()
    finally:
        connection.close()
    chunks, items, dropped, used = [], 0, 0, 0
    for row in rows:
        if row["claim_version_id"] in retired:
            continue
        claim = json.loads(row["claim_json"])
        statement = str(claim.get("normalized_statement") or "").strip()
        if not statement:
            continue
        line_body = f"{statement}（{claim.get('period') or '期间未注明'}，{str(row['created_at'])[:10]}）"
        if used + len(line_body) + 8 > budget:
            dropped += 1
            continue
        tag = build.tags.other("C", row["claim_version_id"])
        chunks.append(f"[{tag}] {line_body}")
        used += len(chunks[-1]) + 1
        items += 1
        build.note_input("claim", row["claim_version_id"], date=str(row["created_at"])[:10])
    return "\n".join(chunks), items, dropped, None if chunks else "没有这家公司的有效旧结论"


BUILDERS: Mapping[str, Callable[[_Build, Mapping[str, Any], int], tuple[str, int, int, str | None]]] = {
    "numbers": _numbers_section, "transcript_full": _transcript_full, "release_full": _release_full,
    "transcript_prepared": _transcript_prepared, "transcript_topics": _transcript_topics,
    "broker_reports": _broker_reports, "sales_notes": _sales_notes, "filing_sections": _filing_sections,
    "expert_calls": _expert_calls, "topic_passages": _topic_passages, "our_products": _our_products,
    "claims": _claims,
}


# ---------------------------------------------------------------------------
# the watermark redraft_policy compares against
# ---------------------------------------------------------------------------


def _compact_consensus(consensus: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not consensus or consensus.get("status") != "ok":
        return None
    keep = ("mean", "high", "low", "count")
    return {
        "as_of": (consensus.get("version") or {}).get("as_of"),
        "version_ref": (consensus.get("version") or {}).get("ref"),
        "periods": [{"label": p.get("label"), **{m: {k: (p.get(m) or {}).get(k) for k in keep}
                                                for m in ("eps", "revenue") if p.get(m)}}
                    for p in consensus.get("periods") or () if p.get("label")],
        "quarters": [{"label": q.get("label"), **{m: {k: (q.get(m) or {}).get(k) for k in keep}
                                                 for m in ("eps", "revenue") if q.get(m)}}
                     for q in consensus.get("quarters") or () if q.get("label")],
        "target": dict(consensus.get("target") or {}),
        "ratings": dict(consensus.get("ratings") or {}),
    }


def watermark(state_dir: str | Path, company_ref: str, as_of: str, *, numbers: Mapping[str, Any] | None = None,
              our_model: Mapping[str, Any] | None = None, identity: Mapping[str, Any] | None = None,
              built_at: str | None = None) -> dict[str, Any]:
    """Everything a later redraft decision is measured from."""

    from . import evidence_index

    try:
        summary = evidence_index.material_arrival_summary(state_dir, company_ref, include_backfill=True)
        arrival_id = int(summary.get("latest_arrival_id") or 0)
    except Exception:  # noqa: BLE001
        arrival_id = 0
    if identity is None:
        connection = ms.open_core_ro(state_dir)
        try:
            identity = ms.company_identity(connection, company_ref, state_dir)
        finally:
            if connection is not None:
                connection.close()
    latest: dict[str, Any] = {}
    for kind in ("transcript", "sec_exhibit", "sec_10k"):
        docs = ms.own_documents(state_dir, company_ref, identity, [kind], as_of=as_of, days=500)
        if docs:
            latest[kind] = {"document_ref": docs[0]["document_ref"], "date": docs[0].get("date"),
                            "period": docs[0].get("period")}
    model = dict((our_model or {}).get("model") or {}) if our_model else None
    return {"arrival_id": arrival_id, "built_at": built_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "as_of": as_of, "consensus": _compact_consensus((numbers or {}).get("consensus")),
            "model": None if not model and not our_model else {
                "version_ref": (model or {}).get("version_ref"), "version_number": (model or {}).get("version_number"),
                "ntm_eps": (our_model or {}).get("ntm_eps")},
            "latest": latest}


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------


def recipe(product: str) -> Mapping[str, Any]:
    if product not in RECIPES:
        raise MaterialPackError(f"unknown product {product!r}; known: {list(RECIPES)}")
    return RECIPES[product]


def build(state_dir: str | Path, company_ref: str, product: str, as_of: str | None = None, *,
          budget_chars: int | None = None, topics: Sequence[str] | None = None,
          numbers_fn: Callable[..., Mapping[str, Any]] | None = None,
          record_requests: bool = False,
          ranked_passages: Sequence[Mapping[str, Any]] | None = None) -> MaterialPack:
    """The material pack for ``product`` about ``company_ref`` as of ``as_of``.

    Deterministic for a given index and Core (no model, no network).
    ``budget_chars`` scales every section budget proportionally to that
    total; ``topics`` adds retrieval topics (the question, for ``ask``).
    ``record_requests`` lets the embedded sufficiency check open
    MaterialRequests; by default the build only reads.  ``ranked_passages``
    (batch G, the ask path) are evidence-index passage views already retrieved
    and reranked for the question; the recipe's question section renders them
    in order instead of searching again.
    """

    spec = recipe(product)
    if not isinstance(company_ref, str) or not company_ref.strip():
        raise MaterialPackError("company_ref is required")
    day = ms.as_of_day(as_of)
    built_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    state = Path(state_dir)
    ctx = _Build(state, company_ref, product, day, topics=topics, numbers_fn=numbers_fn,
                 ranked_passages=ranked_passages)
    total = sum(int(section["budget"]) for section in spec["sections"])
    scale = (float(budget_chars) / total) if budget_chars else 1.0
    ordered = sorted(spec["sections"], key=lambda s: 0 if s["source"] == "numbers" else (2 if s["source"] == "claims" else 1))
    rendered: dict[str, dict[str, Any]] = {}
    for section in ordered:
        limit = max(0, int(int(section["budget"]) * scale))
        try:
            text, items, dropped, empty = BUILDERS[section["source"]](ctx, section, limit)
        except Exception as exc:  # noqa: BLE001 - one section's failure is an honest empty section
            text, items, dropped, empty = "", 0, 0, f"本节构建失败：{type(exc).__name__}: {str(exc)[:200]}"
        if len(text) > limit:  # belt and braces: never exceed the section budget
            text = text[:limit].rsplit("\n", 1)[0]
        rendered[section["key"]] = {"text": text, "items": items, "dropped": dropped, "empty_reason": empty,
                                    "limit": limit}
    parts = [f"# 材料包 · {ctx.identity.get('ticker') or ctx.identity.get('name')} · {product} · 截至 {day} · "
             f"{spec['version']}",
             "标签：[P…] 原文段落（可引用出处）；[N…] 数字包字段（数字须带标签）；[C…] 旧结论（仅在无原文时）；"
             f"[O…] {OUR_VIEW_LABEL}（只作背景，不能作为出处）。"]
    sections_out = []
    budget: dict[str, Any] = {}
    for number, section in enumerate(spec["sections"], start=1):
        result = rendered[section["key"]]
        body = result["text"] if result["text"] else f"（本节为空：{result['empty_reason'] or '没有材料'}）"
        block = f"\n## {number}. {section['title']}\n{body}"
        parts.append(block)
        budget[section["key"]] = {"limit": result["limit"], "used": len(result["text"]), "items": result["items"],
                                  "dropped": result["dropped"], "empty_reason": result["empty_reason"]}
        sections_out.append({"key": section["key"], "title": section["title"], "chars": len(result["text"]),
                             "items": result["items"], "empty_reason": result["empty_reason"]})
    text = "\n".join(parts).rstrip() + "\n"
    tags = {tag: ref for tag, ref in ctx.tags.refs.items() if f"[{tag}]" in text}
    ctx.json["number_tags"] = {tag: value for tag, value in ctx.tags.numbers.items() if tag in tags}
    ctx.json["passage_tags"] = {tag: value for tag, value in ctx.tags.passages.items() if tag in tags}
    numbers = ctx.json.get("numbers") or {}
    ctx.json.update({
        "consensus": numbers.get("consensus"), "street": numbers.get("street"), "outlook": numbers.get("outlook"),
        "market": numbers.get("market"), "valuation": numbers.get("valuation"),
        "identity": {k: v for k, v in ctx.identity.items() if not k.startswith("_")},
        "prior_products": [item for item in ctx.inputs.values() if item["kind"] == "product_version"],
        "scenarios": _scenarios(state, company_ref),
    })
    budget["_total"] = {"limit": sum(item["limit"] for item in budget.values()), "used": len(text),
                        "target_chars": list(spec.get("target_chars") or [])}
    try:
        suff = ms.sufficiency(state, company_ref, product, day, record=record_requests,
                              numbers=numbers or None)
    except Exception as exc:  # noqa: BLE001 - never an exception out of build
        suff = {"status": "insufficient", "reason": f"充分性检查失败：{type(exc).__name__}: {exc}"[:300],
                "missing": [], "items": [], "drafting_allowed": False, "gap_request_refs": []}
    mark = watermark(state, company_ref, day, numbers=numbers, our_model=ctx.json.get("our_model"),
                     identity=ctx.identity, built_at=built_at)
    inputs = sorted(ctx.inputs.values(), key=lambda item: (item["kind"], item["ref"]))
    pack = MaterialPack(company_ref=company_ref, product=product, as_of=day, recipe_version=spec["version"],
                        text=text, tags=tags, json=ctx.json, sufficiency=suff, budget=budget, inputs=inputs,
                        watermark=mark, sections=sections_out, built_at=built_at)
    pack.content_hash = _sha({"recipe": spec["version"], "text": text, "tags": tags})
    return pack


def _scenarios(state_dir: Path, company_ref: str) -> dict[str, Any] | None:
    connection = ms.open_core_ro(state_dir)
    if connection is None or not ms.table_exists(connection, "company_scenario_versions"):
        if connection is not None:
            connection.close()
        return None
    try:
        row = connection.execute(
            "SELECT version_id, created_at, record_json FROM company_scenario_versions WHERE company_ref=? "
            "ORDER BY version_number DESC LIMIT 1", (company_ref,)).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    record = json.loads(row["record_json"])
    return {"version_ref": row["version_id"], "created_at": row["created_at"],
            "scenarios": [{k: item.get(k) for k in ("name", "label", "probability", "kind") if k in item}
                          for item in record.get("scenarios") or () if isinstance(item, Mapping)][:6]}


def sufficiency(state_dir: str | Path, company_ref: str, product: str, as_of: str | None = None,
                **kwargs: Any) -> dict[str, Any]:
    """See :func:`material_sufficiency.sufficiency` (re-exported here)."""

    return ms.sufficiency(state_dir, company_ref, product, as_of, **kwargs)


__all__ = ["BUILDERS", "MaterialPack", "MaterialPackError", "OUR_VIEW_LABEL", "PRODUCTS", "QA_TOPICS", "RECIPES",
           "SCHEMA_VERSION", "build", "pointer", "recipe", "resolve_number", "sufficiency", "watermark"]
