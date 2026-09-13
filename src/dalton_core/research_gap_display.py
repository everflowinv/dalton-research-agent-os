"""Deterministic display wording for known research-gap contracts."""
from __future__ import annotations
import re
from typing import Any
from .driver_template import COST_DRIVER_TEMPLATES

_QUESTIONS = {
 "Which product/input spread drives realised gross cost?":"哪项产品与投入品价差决定实际毛成本？",
 "How much energy is consumed per unit and at what price?":"每单位产出消耗多少能源，采购价格是多少？",
 "How does utilisation move fixed cost per unit?":"开工率变化如何影响单位固定成本？",
 "How does the installed asset base depreciate through the forecast?":"现有资产在预测期内如何折旧？",
 "What spending merely maintains current capacity?":"哪些资本开支只用于维持现有产能？",
 "What does serving and retaining contracted work cost?":"交付并维持合同业务需要多少成本？",
 "What revenue and delivery cost does each employee support?":"每名员工对应多少收入和交付成本？",
 "How does unit cost change as adoption scales?":"随着采用规模扩大，单位成本如何变化？",
 "Which fixed costs leave, by when, and with what cash cost?":"哪些固定成本会在何时退出，并产生多少现金成本？",
 "Which costs persist when revenue changes?":"收入变化时，哪些成本仍然存在？",
 "Which costs move with volume or revenue?":"哪些成本会随业务量或收入变化？",
}
_EVIDENCE={"company_figure":"公司财务数据","market_proxy":"市场替代指标",
           "own_assumption":"自有假设","qualitative":"定性证据"}
_SLOTS={slot["slot_id"]:slot["label"] for template in COST_DRIVER_TEMPLATES["templates"]
        for slot in template["cost_slots"]}
_FULL=re.compile(r"^(?P<subject>.+?) 未覆盖成本模板的「(?P<label>[^」]+)」（(?P<key>[a-z_]+)）：(?P<question>.+?) 期望证据种类：(?P<kinds>[a-z_, ]+)。$")
_PREFIX=re.compile(r"^(?P<key>[a-z_]+)(?P<sep>\s*[:：/／-]\s*)(?P<rest>.*)$")
_BAD_TAGS=re.compile(r"^模型引用了不存在的标签：(?P<tags>[^。]+)。?$")

def gap_display_text(value: Any) -> str:
    """Return friendly Chinese for a closed known gap; preserve everything else."""
    text=str(value) if value is not None else ""
    match=_FULL.fullmatch(text)
    if match and match["key"] in _SLOTS and match["label"]==_SLOTS[match["key"]] and match["question"] in _QUESTIONS:
        kinds=[item.strip() for item in match["kinds"].split(',')]
        if kinds and all(item in _EVIDENCE for item in kinds):
            subject=("公司档案的“供给与成本”章节" if match["subject"]=="档案 supply_and_cost"
                     else match["subject"].replace(" supply_and_cost","的“供给与成本”章节"))
            return f"{subject}尚未覆盖“{match['label']}”：{_QUESTIONS[match['question']]}需要的证据：{'、'.join(_EVIDENCE[x] for x in kinds)}。"
    if text in _SLOTS:return _SLOTS[text]
    match=_PREFIX.fullmatch(text)
    if match and match["key"] in _SLOTS:
        return _SLOTS[match["key"]]+match["sep"]+match["rest"]
    match=_BAD_TAGS.fullmatch(text)
    if match:return f"模型引用了未纳入依据的内部标签：{match['tags']}。"
    return text

__all__=["gap_display_text"]
