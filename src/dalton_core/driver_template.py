"""W4: which drivers a company is modelled on, decided by what kind of company it is.

The Chem retrospective's clearest finding (§7.1) is that the original agent got
one thing right that this system does not: it used a *different framework* for
each business model.  Wanhua was read as a commodity cycle -- spread, operating
rate, cost-curve position, capacity coming on -- and Linde as contracted
compounding -- contract structure, network density, project returns.  Same
analyst, same week, two different sets of questions, because the two companies
earn money in two different ways.

This system already decides which of five kinds a company is: the dossier's
``industry_classification`` is the Deep Insight Gate's first question and it is
a closed vocabulary.  What it did not have is any consequence.  The model
specification, the dossier's ``demand_drivers`` section and the DebateMap all
asked for "the drivers" in the same generic shape whatever the answer to that
question was, so classifying a company cost a model call and bought nothing.

**This module is the consequence, and it is a table rather than a prompt.**  A
frozen registry maps each classification to a driver template: named slots,
each with the filed concepts it would rest on if the company files them, and
the kinds of evidence that answer it.  The bytes are content-hashed, published
as ``deploy/phase9/w4-driver-template-v1.json``, and a test asserts the two
have not drifted -- the same discipline as the debate policy, and for the same
reason: a template that lives in an ``if`` cannot be replayed, and a driver
slot that was expected in September has to still be explicable in December.

Three rules shape what is here.

**A missing slot is a gap, never a refusal.**  A commodity company whose file
says nothing about where it sits on the cost curve has a hole in it, and the
reader needs to see the hole.  Refusing the section instead would delete the
only record that the question was asked -- and would make the template a
gatekeeper over judgement, which is exactly the mistake the classification was
supposed to avoid.  So the coverage check returns strings for the ``gaps``
list every one of these outputs already carries.

**Coverage is decided by a frozen cue table, not by a model.**  Asking a second
model whether a section covered "the spread" would put a judgement inside the
check that verifies a judgement.  The cues are lowercase substrings, matched
against folded text, in both the language the specification is written in and
the language the dossier is written in.  They under-report on purpose: a slot
the cues miss shows up as a gap somebody can dismiss, whereas a slot the cues
invent shows up as coverage nobody can find.

**No classification is a classification.**  A company nobody has typed yet, and
one the evidence could not type (``insufficient_evidence``), both get the
generic template -- and it is *labelled* generic in the prompt and in every gap
string it produces, because a generic frame silently substituted for a specific
one is how a template stops being a decision.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from types import MappingProxyType
from typing import Any

from .claim_index_authority import EVIDENCE_KINDS
from .store import content_hash

SCHEMA_VERSION = "0.1"
REGISTRY_REF = "driver-template-registry:w4:v1"
COST_REGISTRY_REF = "cost-driver-template-registry:w5:v1"

#: The word used where a classification is absent, unknown, or was refused for
#: want of evidence.  It is a key in the registry rather than a fallback branch
#: so that "we do not know what kind of company this is" is a template with a
#: name, and reads as one everywhere it is shown.
GENERIC = "generic"

#: How many gap strings a coverage check may return.  The dossier's own cap is
#: six for the whole section and a template that filled it would leave no room
#: for a gap the drafter noticed itself, which is the more informative kind.
MAX_TEMPLATE_GAPS = 4


class DriverTemplateError(ValueError):
    """The classification is not a word this registry knows."""


def _slot(
    slot_id: str,
    label: str,
    question: str,
    basis_concepts: Sequence[str],
    evidence_kinds: Sequence[str],
    cues: Sequence[str],
) -> dict[str, Any]:
    for kind in evidence_kinds:
        if kind not in EVIDENCE_KINDS:
            raise DriverTemplateError(
                f"{slot_id} expects evidence kind {kind!r}, which is not in the "
                "claim index vocabulary")
    return {
        "slot_id": slot_id,
        "label": label,
        "question": question,
        # Local names, without the taxonomy prefix: a company files
        # ``us-gaap:Revenues`` and a template that hard-coded the prefix would
        # stop matching the day a filer used a different one.
        "basis_concepts": list(basis_concepts),
        "evidence_kinds": list(evidence_kinds),
        "cues": list(cues),
    }


_TEMPLATES: tuple[dict[str, Any], ...] = (
    {
        "classification": "commodity_cycle",
        "label": "商品周期",
        "generic": False,
        "because": (
            "earnings follow the price of an undifferentiated output, so the "
            "four questions are what the spread is, how much of the plant is "
            "running, where this producer sits on the industry cost curve, and "
            "what capacity is coming"
        ),
        "slots": [
            _slot(
                "spread", "价差",
                "What is the spread between the product price and the main "
                "input cost, and how far is it from what this company realises?",
                ("Revenues", "CostOfRevenue", "CostOfGoodsAndServicesSold"),
                ("market_proxy", "company_figure"),
                ("spread", "价差", "crack", "netback", "margin over feedstock",
                 "价差走阔", "价差收窄", "product price", "价格差"),
            ),
            _slot(
                "utilisation", "开工率",
                "How much of the company's capacity is running, and against "
                "what industry operating rate?",
                ("PropertyPlantAndEquipmentNet",),
                ("company_figure", "market_proxy"),
                ("utilisation", "utilization", "operating rate", "开工率",
                 "产能利用", "run rate of the plant", "负荷率"),
            ),
            _slot(
                "cost_curve_position", "成本曲线位置",
                "Where on the industry cost curve does this producer sit, and "
                "what puts it there?",
                ("CostOfRevenue", "CostOfGoodsAndServicesSold", "GrossProfit"),
                ("market_proxy", "third_party_estimate"),
                ("cost curve", "cost-curve", "成本曲线", "marginal producer",
                 "marginal cost", "边际成本", "cash cost", "现金成本"),
            ),
            _slot(
                "capacity_additions", "产能投放",
                "What capacity is being added or retired in this industry, by "
                "whom, and when does it start up?",
                ("PaymentsToAcquirePropertyPlantAndEquipment",
                 "PropertyPlantAndEquipmentNet"),
                ("market_proxy", "third_party_estimate", "company_figure"),
                ("capacity addition", "new capacity", "capacity coming",
                 "产能投放", "新增产能", "扩产", "capacity closure", "退出产能",
                 "greenfield", "debottleneck"),
            ),
        ],
    },
    {
        "classification": "capital_cycle",
        "label": "资本周期",
        "generic": False,
        "because": (
            "returns are set by how the industry's capital responds to them, so "
            "the questions are what is being spent, what it earns, and where "
            "the capacity cycle stands"
        ),
        "slots": [
            _slot(
                "capex", "资本开支",
                "What is this company spending on capacity, and how does that "
                "compare with its own depreciation and with the industry?",
                ("PaymentsToAcquirePropertyPlantAndEquipment",
                 "PropertyPlantAndEquipmentNet",
                 "DepreciationDepletionAndAmortization"),
                ("company_figure", "market_proxy"),
                ("capex", "capital expenditure", "资本开支", "资本支出",
                 "capital spending", "在建工程"),
            ),
            _slot(
                "returns_on_capital", "投入资本回报",
                "What does the capital already in the ground earn, and is that "
                "above or below the cost of it?",
                ("OperatingIncomeLoss", "StockholdersEquity", "Assets"),
                ("company_figure", "third_party_estimate"),
                ("return on capital", "roic", "roce", "return on invested",
                 "投入资本回报", "资本回报", "回报率", "cost of capital"),
            ),
            _slot(
                "capacity_cycle", "产能周期",
                "Where is the industry in its capacity cycle -- adding, "
                "digesting, or retiring -- and what marks the turn?",
                ("PropertyPlantAndEquipmentNet",),
                ("market_proxy", "third_party_estimate"),
                ("capacity cycle", "产能周期", "supply response", "供给响应",
                 "industry capacity", "行业产能", "capacity discipline",
                 "over-capacity", "产能过剩"),
            ),
        ],
    },
    {
        "classification": "contract_compounder",
        "label": "合同型 compounder",
        "generic": False,
        "because": (
            "contracted revenue reinvested at a stable return is a question "
            "about price, about how much of the book stays, and about what one "
            "unit of it earns -- not about the cycle"
        ),
        "slots": [
            _slot(
                "pricing", "定价",
                "What can this company charge, what has it charged, and what "
                "lets it raise?",
                ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"),
                ("company_figure", "qualitative"),
                ("pricing", "price increase", "定价", "提价", "调价",
                 "price escalator", "list price", "realised price", "议价"),
            ),
            _slot(
                "retention", "留存",
                "How much of the contract book stays, for how long, and what "
                "happens at renewal?",
                ("RevenueRemainingPerformanceObligation",
                 "ContractWithCustomerLiability"),
                ("company_figure", "qualitative"),
                ("retention", "renewal", "churn", "留存", "续约", "流失",
                 "backlog", "在手订单", "remaining performance obligation"),
            ),
            _slot(
                "unit_economics", "单位经济",
                "What does one unit of this business earn, and what does it "
                "cost to add another?",
                ("GrossProfit", "CostOfRevenue", "OperatingIncomeLoss"),
                ("company_figure", "third_party_estimate"),
                ("unit economics", "单位经济", "per unit", "单位成本",
                 "contribution margin", "incremental margin", "边际利润",
                 "payback"),
            ),
        ],
    },
    {
        "classification": "structural_growth",
        "label": "结构成长",
        "generic": False,
        "because": (
            "a durable shift in end demand is a question about how far the "
            "shift has run, how large it can get, and how fast it is being "
            "taken up"
        ),
        "slots": [
            _slot(
                "penetration", "渗透",
                "How far into its addressable base has this product got, and "
                "what is left?",
                ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"),
                ("company_figure", "market_proxy"),
                ("penetration", "渗透率", "渗透", "attach rate", "installed base",
                 "share of wallet", "覆盖率"),
            ),
            _slot(
                "tam", "可及市场",
                "How large is the market this can reach, on whose number, and "
                "how far is that number from anything the company reports?",
                (),
                ("market_proxy", "third_party_estimate"),
                ("tam", "addressable market", "可及市场", "市场规模",
                 "total addressable", "sam", "市场空间"),
            ),
            _slot(
                "adoption", "采纳节奏",
                "How quickly is it being adopted, by which cohort, and what "
                "would slow it?",
                ("Revenues", "DeferredRevenueCurrent"),
                ("company_figure", "qualitative", "market_proxy"),
                ("adoption", "采纳", "上量", "ramp", "cohort", "同期群",
                 "new customers", "新增客户", "订阅用户"),
            ),
        ],
    },
    {
        "classification": "turnaround",
        "label": "转型公司",
        "generic": False,
        "because": (
            "a self-help thesis is a question about whether the steps are "
            "being taken and whether the cash lasts long enough for them to "
            "matter"
        ),
        "slots": [
            _slot(
                "milestones", "里程碑",
                "What are the named steps of the plan, which have been done, "
                "and by when is the next one due?",
                ("RestructuringCharges", "OperatingIncomeLoss"),
                ("company_figure", "qualitative"),
                ("milestone", "里程碑", "restructuring", "重组", "turnaround plan",
                 "转型计划", "cost programme", "cost program", "降本",
                 "disposal", "剥离"),
            ),
            _slot(
                "cash_runway", "现金跑道",
                "How long does the cash last at the current burn, and what is "
                "due before then?",
                ("CashAndCashEquivalentsAtCarryingValue",
                 "NetCashProvidedByUsedInOperatingActivities",
                 "LongTermDebtNoncurrent"),
                ("company_figure",),
                ("cash runway", "现金跑道", "runway", "liquidity", "流动性",
                 "burn", "现金消耗", "covenant", "债务到期", "maturity wall",
                 "refinanc"),
            ),
        ],
    },
    {
        "classification": GENERIC,
        "label": "通用（未分类）",
        "generic": True,
        "because": (
            "nothing is known about which kind of business this is, so the "
            "frame is the one that assumes least: what is sold, at what price, "
            "and what it costs to sell"
        ),
        "slots": [
            _slot(
                "volume", "量",
                "How much does this company sell, and of what?",
                ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"),
                ("company_figure",),
                ("volume", "销量", "出货", "units sold", "shipments", "件数",
                 "seats", "headcount"),
            ),
            _slot(
                "price", "价",
                "What does it charge, and what moves that?",
                ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"),
                ("company_figure", "market_proxy"),
                ("price", "价格", "单价", "rate", "realisation", "realization",
                 "asp", "定价"),
            ),
            _slot(
                "cost_structure", "成本结构",
                "What are the costs, and which of them move with what is sold?",
                ("CostOfRevenue", "CostOfGoodsAndServicesSold", "OperatingExpenses"),
                ("company_figure",),
                ("cost structure", "成本结构", "cost of revenue", "营业成本",
                 "fixed cost", "固定成本", "variable cost", "变动成本",
                 "operating expense", "费用率"),
            ),
        ],
    },
)

#: The frozen registry.  Published byte-for-byte as
#: ``deploy/phase9/w4-driver-template-v1.json`` with ``content_hash`` appended.
DRIVER_TEMPLATES: Mapping[str, Any] = MappingProxyType({
    "schema_version": SCHEMA_VERSION,
    "registry_ref": REGISTRY_REF,
    "generic_classification": GENERIC,
    "templates": list(_TEMPLATES),
})

REGISTRY_HASH = content_hash({
    "schema_version": SCHEMA_VERSION,
    "registry_ref": REGISTRY_REF,
    "generic_classification": GENERIC,
    "templates": list(_TEMPLATES),
})

_BY_CLASSIFICATION: Mapping[str, Mapping[str, Any]] = MappingProxyType({
    str(item["classification"]): MappingProxyType(item) for item in _TEMPLATES
})

_COST_SLOTS: Mapping[str, tuple[dict[str, Any], ...]] = MappingProxyType({
    "commodity_cycle": (
        _slot("raw_material_spread", "原料价差", "Which product/input spread drives realised gross cost?",
              ("CostOfRevenue", "CostOfGoodsAndServicesSold"), ("company_figure", "market_proxy"),
              ("raw material", "feedstock", "spread", "原料", "价差")),
        _slot("energy_intensity", "能源强度", "How much energy is consumed per unit and at what price?",
              ("CostOfRevenue",), ("company_figure", "market_proxy"),
              ("energy", "power", "gas cost", "能源", "电耗")),
        _slot("utilisation_cost", "开工率成本", "How does utilisation move fixed cost per unit?",
              ("CostOfRevenue", "PropertyPlantAndEquipmentNet"), ("company_figure", "market_proxy"),
              ("utilisation", "utilization", "operating rate", "开工率", "负荷率")),
    ),
    "capital_cycle": (
        _slot("depreciation_curve", "折旧曲线", "How does the installed asset base depreciate through the forecast?",
              ("DepreciationDepletionAndAmortization", "PropertyPlantAndEquipmentNet"), ("company_figure",),
              ("depreciation", "useful life", "折旧", "使用寿命")),
        _slot("maintenance_capex", "维护性资本开支", "What spending merely maintains current capacity?",
              ("PaymentsToAcquirePropertyPlantAndEquipment",), ("company_figure", "own_assumption"),
              ("maintenance capex", "sustaining capex", "维护性资本开支", "维持性资本开支")),
    ),
    "contract_compounder": (
        _slot("delivery_cost", "交付成本", "What does serving and retaining contracted work cost?",
              ("CostOfRevenue", "CostOfGoodsAndServicesSold"), ("company_figure", "own_assumption"),
              ("delivery cost", "cost to serve", "交付成本", "履约成本")),
        _slot("revenue_per_employee", "人均产出", "What revenue and delivery cost does each employee support?",
              ("Revenues", "OperatingExpenses"), ("company_figure", "market_proxy"),
              ("revenue per employee", "utilisation", "人均产出", "人效")),
    ),
    "structural_growth": (
        _slot("unit_cost_curve", "单位成本曲线", "How does unit cost change as adoption scales?",
              ("CostOfRevenue", "GrossProfit"), ("company_figure", "own_assumption"),
              ("unit cost", "cost curve", "单位成本", "规模效应")),
    ),
    "turnaround": (
        _slot("fixed_cost_removal", "固定成本剥离进度", "Which fixed costs leave, by when, and with what cash cost?",
              ("OperatingExpenses", "RestructuringCharges"), ("company_figure", "qualitative"),
              ("fixed cost", "cost removal", "restructuring", "固定成本", "降本", "剥离")),
    ),
    GENERIC: (
        _slot("fixed_cost", "固定成本", "Which costs persist when revenue changes?",
              ("OperatingExpenses",), ("company_figure",), ("fixed cost", "固定成本")),
        _slot("variable_cost", "变动成本", "Which costs move with volume or revenue?",
              ("CostOfRevenue", "CostOfGoodsAndServicesSold"), ("company_figure",),
              ("variable cost", "cost of revenue", "变动成本", "营业成本")),
    ),
})

COST_DRIVER_TEMPLATES: Mapping[str, Any] = MappingProxyType({
    "schema_version": SCHEMA_VERSION, "registry_ref": COST_REGISTRY_REF,
    "generic_classification": GENERIC,
    "templates": [{"classification": key, "cost_slots": list(value)}
                  for key, value in _COST_SLOTS.items()],
})
COST_REGISTRY_HASH = content_hash(dict(COST_DRIVER_TEMPLATES))


def cost_slots_for(classification: Any) -> tuple[Mapping[str, Any], ...]:
    selected = template_for(classification)["classification"]
    return tuple(_COST_SLOTS[str(selected)])


def cost_slot_ids(classification: Any) -> tuple[str, ...]:
    return tuple(str(item["slot_id"]) for item in cost_slots_for(classification))


def cost_prompt_block(classification: Any, filed_concepts: Iterable[str] = ()) -> str:
    filed = {_local_name(item): str(item) for item in filed_concepts}
    selected = template_for(classification)
    lines = [f"COST DRIVER TEMPLATE ({selected['classification']}):",
             "Bind each proposed expense line to one cost slot, or use null and explain why it is company-specific.",
             "<cost slot id>\t<question>\t<filed concepts, or none>\t<evidence kinds>"]
    for slot in cost_slots_for(classification):
        matched = [filed[_local_name(name)] for name in slot["basis_concepts"]
                   if _local_name(name) in filed]
        lines.append(f"{slot['slot_id']}\t{slot['question']}\t"
                     f"{', '.join(matched) or 'none filed'}\t"
                     f"{', '.join(slot['evidence_kinds'])}")
    return "\n".join(lines)


def cost_template_gaps(classification: Any, texts: Iterable[Any], *, subject: str,
                       limit: int = MAX_TEMPLATE_GAPS) -> list[str]:
    folded = [_fold(item) for item in texts]
    gaps = []
    for slot in cost_slots_for(classification):
        if any(cue in text for cue in slot["cues"] for text in folded):
            continue
        gaps.append(f"{subject} 未覆盖成本模板的「{slot['label']}」（{slot['slot_id']}）："
                    f"{slot['question']} 期望证据种类：{', '.join(slot['evidence_kinds'])}。")
        if len(gaps) >= limit:
            break
    return gaps

#: Every classification the dossier may record that has no template of its own.
#: ``insufficient_evidence`` is deliberately in here rather than given an empty
#: template: "the evidence does not say what kind of company this is" is not a
#: fifth kind of company, it is the absence of an answer, and the frame that
#: assumes least is the honest response to it.
GENERIC_FOR: frozenset[str] = frozenset({"insufficient_evidence"})

_WHITESPACE_RE = re.compile(r"\s+")


def _fold(value: Any) -> str:
    """Lowercase, whitespace-collapsed text, for substring cue matching.

    Deliberately local rather than borrowed from ``claim_index_tagging``: that
    module strips punctuation for a different purpose (dedupe keys) and pulls
    in the model layer, and this file is imported by the specification lane,
    which has no business importing a model client to read a table.
    """

    if value is None:
        return ""
    return _WHITESPACE_RE.sub(" ", str(value)).strip().lower()


def known_classifications() -> tuple[str, ...]:
    """Every word this registry has a template for, registry order."""

    return tuple(_BY_CLASSIFICATION)


def template_for(classification: Any) -> Mapping[str, Any]:
    """The template for one classification; the generic one for anything else.

    Never raises.  An unknown word is the same situation as no word at all --
    somebody has not decided, or has decided something this registry has not
    heard of -- and both are answered by the frame that assumes least.  The
    template says of itself that it is generic, so no caller can lose track of
    which happened.
    """

    key = classification
    if isinstance(key, Mapping):
        # The dossier carries the classification as a block; taking the word
        # out of it here saves every caller the same two lines and the same
        # chance of reading ``variant_view`` by mistake.
        key = key.get("classification")
    if not isinstance(key, str) or key not in _BY_CLASSIFICATION or key in GENERIC_FOR:
        return _BY_CLASSIFICATION[GENERIC]
    return _BY_CLASSIFICATION[key]


def is_generic(classification: Any) -> bool:
    return bool(template_for(classification)["generic"])


def slot_ids(classification: Any) -> tuple[str, ...]:
    return tuple(str(slot["slot_id"]) for slot in template_for(classification)["slots"])


def evidence_kinds_for(classification: Any, slot_id: str) -> tuple[str, ...]:
    for slot in template_for(classification)["slots"]:
        if slot["slot_id"] == slot_id:
            return tuple(slot["evidence_kinds"])
    raise DriverTemplateError(
        f"{slot_id!r} is not a slot of this classification's template")


def _local_name(concept: Any) -> str:
    return str(concept or "").split(":")[-1].strip().lower()


def basis_concept_plan(
    classification: Any, filed_concepts: Iterable[str] = ()
) -> list[dict[str, Any]]:
    """Each template slot with the concepts *this company actually filed*.

    This is the "fill" half of "select the template by classification and fill
    basis concepts from it".  The registry names candidate concepts; the
    company's own disclosure decides which of them exist.  A slot for which the
    company filed nothing is not an error -- it is the normal case for a spread
    or a TAM, and it is exactly the case ``market_proxy`` was added for -- so it
    comes back with an empty list and ``needs_proxy`` set, which is what the
    prompt then tells the model.
    """

    filed = {}
    for concept in filed_concepts or ():
        name = _local_name(concept)
        if name and name not in filed:
            filed[name] = str(concept)
    out: list[dict[str, Any]] = []
    for slot in template_for(classification)["slots"]:
        matched = [filed[_local_name(name)] for name in slot["basis_concepts"]
                   if _local_name(name) in filed]
        out.append({
            "slot_id": slot["slot_id"],
            "label": slot["label"],
            "question": slot["question"],
            "candidate_concepts": list(slot["basis_concepts"]),
            "filed_concepts": matched,
            "evidence_kinds": list(slot["evidence_kinds"]),
            "needs_proxy": not matched and "market_proxy" in slot["evidence_kinds"],
        })
    return out


def prompt_block(classification: Any, filed_concepts: Iterable[str] = ()) -> str:
    """The template, as the lines a drafting prompt carries.

    A table, for the reason every prompt in this repository is a table: the
    same content as JSON costs several times the bytes in repeated keys, and
    the router reserves budget against the size of the prompt.
    """

    template = template_for(classification)
    plan = basis_concept_plan(classification, filed_concepts)
    word = str(template["classification"])
    marker = " -- GENERIC, no classification was available" if template["generic"] else ""
    because = str(template["because"])
    # Deliberately not indented, and for the same reason the dossier's
    # classification vocabulary is not: a drafting prompt already prints its
    # slot structure as two spaces, an id and a tab, and a table in that shape
    # is a table a reader -- human or model -- will read as structure. This is
    # a checklist about the subject, not the shape of the answer.
    lines = [
        f"DRIVER TEMPLATE ({word}{marker}):",
        because + ".",
        "",
        "One line per driver slot, tab separated:",
        "<slot id>\t<question>\t<filed concepts, or none>\t<evidence kinds>",
    ]
    for row in plan:
        filed = ", ".join(row["filed_concepts"]) or "none filed"
        lines.append(
            f"{row['slot_id']}\t{row['question']}\t{filed}\t"
            f"{', '.join(row['evidence_kinds'])}"
        )
    lines += [
        "",
        "Cover every slot above or say in as many words which one you cannot "
        "cover and why. A slot with no filed concept is not a slot to drop: it "
        "is the slot that rests on a market_proxy -- a public quantity such as "
        "a spread, a list price or a futures continuation -- and a driver "
        "resting on one must say how far that quantity sits from what this "
        "company realises.",
    ]
    if template["generic"]:
        lines.append(
            "This is the generic template because no industry classification "
            "was available. Do not present it as a considered frame for this "
            "company.")
    return "\n".join(lines)


def covered_slots(classification: Any, texts: Iterable[Any]) -> set[str]:
    """Which template slots the given text covers, by the frozen cue table."""

    folded = [_fold(item) for item in texts]
    folded = [item for item in folded if item]
    covered: set[str] = set()
    for slot in template_for(classification)["slots"]:
        for cue in slot["cues"]:
            if any(cue in item for item in folded):
                covered.add(str(slot["slot_id"]))
                break
    return covered


def template_gaps(
    classification: Any,
    texts: Iterable[Any],
    *,
    subject: str,
    limit: int = MAX_TEMPLATE_GAPS,
) -> list[str]:
    """Template slots nothing in ``texts`` speaks to, as gap strings.

    Never an error and never a refusal.  ``subject`` names what was checked --
    the dossier section, the DebateMap, the specification -- so a reader of a
    gap list knows which output has the hole in it.
    """

    template = template_for(classification)
    covered = covered_slots(classification, texts)
    word = str(template["classification"])
    label = str(template["label"])
    generic = " （通用模板：该公司尚无行业分类）" if template["generic"] else ""
    gaps: list[str] = []
    for slot in template["slots"]:
        if str(slot["slot_id"]) in covered:
            continue
        gaps.append(
            f"{subject} 未覆盖 {label}（{word}）模板的「{slot['label']}」"
            f"（{slot['slot_id']}）：{slot['question']}"
            f" 期望证据种类：{', '.join(slot['evidence_kinds'])}。{generic}".strip()
        )
        if len(gaps) >= max(0, int(limit)):
            break
    return gaps


# ---------------------------------------------------------------------------
# the three readers
# ---------------------------------------------------------------------------

def section_texts(section: Mapping[str, Any] | None) -> list[str]:
    """Every sentence and slot prompt a dossier section carries."""

    if not isinstance(section, Mapping):
        return []
    out: list[str] = []
    for item in section.get("structure") or ():
        out.append(str(item))
    for slot in section.get("slots") or ():
        if not isinstance(slot, Mapping):
            continue
        out.append(str(slot.get("slot_id") or ""))
        if slot.get("unknown"):
            out.append(str(slot["unknown"]))
        for sentence in slot.get("sentences") or ():
            if isinstance(sentence, Mapping):
                out.append(str(sentence.get("text") or ""))
    for gap in section.get("gaps") or ():
        out.append(str(gap))
    return [item for item in out if item]


def dossier_demand_driver_gaps(
    section: Mapping[str, Any] | None,
    classification: Any,
    *,
    limit: int = MAX_TEMPLATE_GAPS,
) -> list[str]:
    """What the dossier's ``demand_drivers`` section does not say, per template."""

    return template_gaps(classification, section_texts(section),
                         subject="档案 demand_drivers", limit=limit)


def debate_texts(debates: Iterable[Mapping[str, Any]]) -> list[str]:
    """Every question, driver ref and stated position on a DebateMap."""

    out: list[str] = []
    for debate in debates or ():
        if not isinstance(debate, Mapping):
            continue
        out.append(str(debate.get("question") or ""))
        for ref in debate.get("driver_refs") or ():
            out.append(str(ref))
        for key in ("bull_position", "bear_position", "market_position",
                    "our_position"):
            position = debate.get(key)
            if isinstance(position, Mapping):
                out.append(str(position.get("statement") or ""))
    return [item for item in out if item]


def debate_map_gaps(
    debates: Iterable[Mapping[str, Any]],
    classification: Any,
    *,
    limit: int = MAX_TEMPLATE_GAPS,
) -> list[str]:
    """Template slots no debate on this map argues about."""

    return template_gaps(classification, debate_texts(debates),
                         subject="DebateMap", limit=limit)


def spec_texts(spec: Mapping[str, Any] | None) -> list[str]:
    """Every label, reason and metric name a model specification carries."""

    if not isinstance(spec, Mapping):
        return []
    out = [str(spec.get("assessment") or "")]
    for key in ("revenue_drivers", "expense_lines", "operating_metrics"):
        for row in spec.get(key) or ():
            if not isinstance(row, Mapping):
                continue
            out.append(str(row.get("label") or ""))
            out.append(str(row.get("because") or ""))
            out.append(str(row.get("basis_concept") or ""))
    return [item for item in out if item]


def spec_gaps(
    spec: Mapping[str, Any] | None,
    classification: Any,
    *,
    limit: int = MAX_TEMPLATE_GAPS,
) -> list[str]:
    """Template slots the model specification does not model."""

    return template_gaps(classification, spec_texts(spec),
                         subject="建模规格", limit=limit)


__all__ = [
    "COST_DRIVER_TEMPLATES",
    "COST_REGISTRY_HASH",
    "COST_REGISTRY_REF",
    "DRIVER_TEMPLATES",
    "DriverTemplateError",
    "GENERIC",
    "GENERIC_FOR",
    "MAX_TEMPLATE_GAPS",
    "REGISTRY_HASH",
    "REGISTRY_REF",
    "SCHEMA_VERSION",
    "basis_concept_plan",
    "covered_slots",
    "cost_prompt_block",
    "cost_slot_ids",
    "cost_slots_for",
    "cost_template_gaps",
    "debate_map_gaps",
    "debate_texts",
    "dossier_demand_driver_gaps",
    "evidence_kinds_for",
    "is_generic",
    "known_classifications",
    "prompt_block",
    "section_texts",
    "slot_ids",
    "spec_gaps",
    "spec_texts",
    "template_for",
    "template_gaps",
]
