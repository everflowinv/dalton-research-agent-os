"""Declared measurable industry mechanisms; never company facts or forecasts."""
from __future__ import annotations
import copy
from typing import Any, Mapping
from .store import content_hash

RULE_REF = "industry-archetypes:1"
# Each industry has demand, supply/cost, pricing and cash mechanisms. Measures
# name the quantity to collect, not a value inferred for an individual company.
ARCHETYPES = {
 "capital_compute": [
  ("demand", "客户算力需求→签约负载→可售容量利用率", "签约负载", "MW"),
  ("supply_and_cost", "可得电力→投运容量→利用率→单位折旧成本", "投运容量", "MW"),
  ("pricing", "可售计算时数×单位计算收入→收入；容量组合需分别核对", "单位计算收入", "USD/hour"),
  ("cash", "利用率→营业现金流；电力/设备资本开支→折旧与自由现金流", "容量资本开支", "USD/MW")],
 "advertising": [
  ("demand", "用户参与时长×广告负载→广告展示量", "广告展示量", "impressions"),
  ("supply_and_cost", "推荐计算量→服务器/电力与折旧→每次展示成本", "每次展示成本", "USD/impression"),
  ("pricing", "展示量×每次展示价格→广告收入；版位与格式组合变化分别桥接", "每次展示价格", "USD/impression"),
  ("cash", "广告应收周转→收现；算力资本开支→自由现金流", "广告应收周转天数", "days")],
 "retail_fulfilment": [
  ("demand", "活跃顾客×购买频次×每单商品额→商品交易额", "每顾客订单数", "orders/customer"),
  ("supply_and_cost", "订单密度→仓储/配送利用率→每单履约成本", "每单履约成本", "USD/order"),
  ("pricing", "商品组合/抽佣率/折扣→每单净收入与毛利", "每单净收入", "USD/order"),
  ("cash", "存货周转与应付账期→营运资本；履约资本开支→自由现金流", "存货周转天数", "days")],
 "it_services": [
  ("demand", "客户预算→新签订单队列→按交付进度转化收入", "订单收入转化率", "fraction"),
  ("supply_and_cost", "员工容量×利用率×交付结构→交付成本与利润率", "员工利用率", "fraction"),
  ("pricing", "可收费交付量×单位价格→收入；AI效率与客户让利分别桥接", "单位交付价格", "USD/hour"),
  ("cash", "项目验收/应收账期→收现→经营现金流与自由现金流", "应收账款天数", "days")],
 "consumption_cloud": [
  ("demand", "客户部署与工作负载→实际消费量→积压确认节奏", "实际消费量", "compute_hours"),
  ("supply_and_cost", "租用/自建容量×利用率→计算单位成本与毛利", "单位计算成本", "USD/hour"),
  ("pricing", "实际消费量×合同单位价格→收入；折扣与组合分别桥接", "消费单位价格", "USD/hour"),
  ("cash", "预付/积压转化→收现；应收与容量投入→自由现金流", "积压收入转化率", "fraction")],
 "semiconductor_equipment": [
  ("demand", "客户晶圆厂资本预算→设备订单→安装验收→收入", "设备订单额", "USD"),
  ("supply_and_cost", "关键组件产能/交期→设备交付容量→每台成本", "设备交付量", "systems"),
  ("pricing", "设备交付量×平均售价；先进制程/服务组合→毛利率", "设备平均售价", "USD/system"),
  ("cash", "客户预付款与验收时点→营运资本；设备产能资本开支→自由现金流", "客户预付款", "USD")],
}
ALIASES = {
 "capital_compute": ("capital-compute", "重资本算力", "data-centers", "ai-infrastructure"),
 "advertising": ("advertising", "广告", "digital-advertising"),
 "retail_fulfilment": ("retail", "retail-fulfilment", "retail-fulfillment", "零售", "零售履约"),
 "it_services": ("it-services", "us-it-services", "IT服务", "it服务"),
 "consumption_cloud": ("consumption-cloud", "cloud-software", "消费型云", "消费云"),
 "semiconductor_equipment": ("semiconductor-equipment", "半导体设备"),
}


def archetypes_for(industry_ref: str) -> list[str]:
    value = industry_ref.removeprefix("industry:").lower().replace("_", "-").replace(" ", "-")
    if value in {"hyperscalers", "hyperscaler", "超大规模云", "美国-hyperscaler-云计算与-ai-基础设施"}:
        return ["capital_compute", "advertising", "retail_fulfilment", "consumption_cloud"]
    return [key for key, aliases in ALIASES.items() if value in [alias.lower() for alias in aliases]]


def specialize(industry_ref: str, driver_pack: Mapping[str, Any], method: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    keys = archetypes_for(industry_ref)
    # A custom, already concrete owner method is not replaced by a library.
    generic = any(str(item.get("driver_ref", "")).startswith("driver:workspace-generic:")
                  for item in driver_pack.get("drivers") or [])
    if not generic:
        return copy.deepcopy(dict(driver_pack)), copy.deepcopy(dict(method)), {"rule_ref": RULE_REF, "status": "owner_specialized", "archetypes": []}
    if not keys:
        return copy.deepcopy(dict(driver_pack)), copy.deepcopy(dict(method)), {"rule_ref": RULE_REF, "status": "unspecialized", "archetypes": [], "reason": "行业未绑定明确原型，通用驱动不能视为专业化研究"}
    drivers, metrics, templates, chains, aspects = [], [], [], [], []
    for key in keys:
        for aspect, chain, measure, unit in ARCHETYPES[key]:
            suffix = f"archetype:{key}:{aspect}"
            driver, metric = "driver:" + suffix, "metric:" + suffix
            drivers.append({"driver_ref": driver, "label": measure, "mechanism": chain, "metric_refs": [metric]})
            metrics.append({"metric_ref": metric, "label": measure, "definition": chain,
                            "unit": unit, "periodicity": "fiscal_quarter",
                            "preferred_source_refs": ["source:sec-edgar", "source:issuer-disclosure"],
                            "verification_kind": "numeric_and_semantic", "caveats": ["企业适用性与精确口径需由申报证据验证"]})
            templates.append({"template_ref": "thesis-template:" + suffix, "statement": chain,
                              "mechanism": chain, "driver_refs": [driver],
                              "implied_expectation": f"预登记{measure}的方向、阈值、期间与反证；未登记不能评分",
                              "falsifier_refs": ["falsifier:" + suffix]})
            chains.append(chain)
            aspects.append(aspect)
    specialized = copy.deepcopy(dict(method))
    specialized["causal_chain"] = chains
    binding = {"rule_ref": RULE_REF, "status": "specialized", "archetypes": keys,
               "aspects": aspects, "library_hash": content_hash({key: ARCHETYPES[key] for key in keys})}
    return {"drivers": drivers, "metric_specs": metrics, "thesis_templates": templates}, specialized, binding


def dossier_chain_sections(chain: list[str]) -> list[str]:
    declared = {text: aspect for rows in ARCHETYPES.values() for aspect, text, _metric, _unit in rows}
    return ["supply_and_cost" if declared.get(text) in {"supply_and_cost", "cash"}
            else "demand_drivers" for text in chain]
