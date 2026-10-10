"""ADR-0012: propose a company's structured scenarios from its dossier.

One bounded model call per company.  The model reads a deterministic material
table -- the dossier's variant view, demand-driver chains and catalyst
section (each sentence with the refs it already cites), the company's current
theses, its upcoming catalyst dates, and an **observable metrics** table: every
metric a lane can actually read for this company, with its latest value, date
and source ref.  It answers 2-4 scenarios whose triggers and invalidators name
metrics from that table only.

Everything the model says is then checked here, without a second call:

* every ref it cites must be one the material contained;
* every condition must name an observable metric (or be ``owner`` observed);
* the probabilities must sum to one;
* a condition the current value *already* satisfies is dropped and recorded
  (a trigger that has already happened is not a trigger), and a scenario left
  with no trigger refuses the whole proposal.

What survives is written as one ``ScenarioSet`` under the mission's automation
principal.  It is not in force until the owner accepts it.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from datetime import date, datetime, timezone
from typing import Any

from .company_scenario import (
    COMPARATORS,
    DECISION_WORDS,
    MAX_INVALIDATORS,
    MAX_SCENARIOS,
    MAX_TRIGGERS,
    MIN_SCENARIOS,
    CompanyScenarioAuthority,
    CompanyScenarioValidationError,
    Observer,
    PROPOSAL_WRITE_SCOPE,
    check_probabilities,
    decimal_text,
    table_exists,
    validate_scenario,
)
from .store import content_hash

PURPOSE_NAME = "scenario_proposal"
CONTRACT_VERSION = "scenario-proposal-contract:0.3"
MAX_PROMPT_BYTES = 48_000
#: What the prompt is fitted to before the call.  The debate map measured an
#: antigravity provider input bound of 30,000 bytes (2026-09-16); a prompt
#: over it comes back ``model_unavailable`` without reaching a model.
TARGET_PROMPT_BYTES = 26_000
MAX_SENTENCES_PER_SLOT = 3
MAX_CLAIM_METRICS = 15
MAX_DRIVERS = 12
MAX_CATALYSTS = 6
VARIANT_SLOTS = ("our_view", "market_view", "where_market_is_wrong",
                 "convergence_pathway", "observable_signals")
DOSSIER_ASPECTS = ("demand_drivers", "catalyst_calendar", "guidance_style")
#: The driver-model roles worth a trigger; others (tax, interest) rarely are.
DRIVER_ROLES = ("revenue", "operating_income", "net_income", "cost_of_revenue",
                "operating_expense", "gross_profit", "diluted_eps", "free_cash_flow",
                "operating_cash_flow", "capital_expenditure")


class ScenarioProposalError(RuntimeError):
    """The model's reply does not satisfy the contract."""


def _register_purpose() -> str:
    from .cockpit_model import register_purpose

    return register_purpose(PURPOSE_NAME)


PURPOSE = _register_purpose()


# ---------------------------------------------------------------------------
# the material
# ---------------------------------------------------------------------------


def latest_dossier(connection: sqlite3.Connection, company_ref: str) -> dict[str, Any] | None:
    if not table_exists(connection, "company_dossier_versions"):
        return None
    row = connection.execute(
        "SELECT record_json FROM company_dossier_versions WHERE company_ref=? "
        "ORDER BY version_number DESC LIMIT 1", (company_ref,)).fetchone()
    if row is None:
        return None
    try:
        record = json.loads(row["record_json"])
    except (TypeError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _sentences(slots: Any, wanted: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    out = []
    for slot in slots or ():
        if not isinstance(slot, Mapping):
            continue
        if wanted is not None and slot.get("slot_id") not in wanted:
            continue
        for sentence in list(slot.get("sentences") or ())[:MAX_SENTENCES_PER_SLOT]:
            text = str(sentence.get("text") or "").strip()
            if text:
                out.append({"slot": slot.get("slot_id"), "text": text[:400],
                            "refs": [str(ref) for ref in sentence.get("refs") or ()][:8]})
    return out


def dossier_material(dossier: Mapping[str, Any]) -> dict[str, Any]:
    variant = dossier.get("variant_view") or {}
    sections = {item.get("aspect"): item for item in dossier.get("sections") or ()
                if isinstance(item, Mapping)}
    return {
        "version_ref": dossier.get("id"),
        "version_hash": dossier.get("content_hash"),
        "body_hash": dossier.get("body_hash"),
        "variant_view": _sentences(variant.get("slots"), VARIANT_SLOTS),
        "sections": {aspect: _sentences((sections.get(aspect) or {}).get("slots"))
                     for aspect in DOSSIER_ASPECTS if aspect in sections},
        "gaps": [str(item)[:240] for item in variant.get("gaps") or ()][:6],
    }


def _catalysts(connection: sqlite3.Connection, company_ref: str, today: date) -> list[dict[str, Any]]:
    if not table_exists(connection, "catalyst_calendar_versions"):
        return []
    row = connection.execute(
        "SELECT record_json FROM catalyst_calendar_versions WHERE company_ref=? "
        "ORDER BY version_number DESC LIMIT 1", (company_ref,)).fetchone()
    if row is None:
        return []
    entries = json.loads(row["record_json"]).get("entries") or []
    upcoming = sorted((item for item in entries
                       if str(item.get("expected_date") or "") >= today.isoformat()),
                      key=lambda item: item.get("expected_date"))
    return [{"entry_ref": item.get("entry_ref"), "event_kind": item.get("event_kind"),
             "expected_date": item.get("expected_date"), "confidence": item.get("confidence")}
            for item in upcoming[:MAX_CATALYSTS]]


def _row(observer: Observer, company_ref: str, observed_by: str, metric_ref: str,
         label: str, unit: str | None, transforms: tuple[str, ...] = ("level",)) -> dict[str, Any] | None:
    values = {}
    for transform in transforms:
        found = observer.read(company_ref, observed_by, metric_ref, transform,
                              since=datetime.now(timezone.utc).isoformat())
        if found is not None and found.get("value") is not None:
            values[transform] = found
    if not values:
        return None
    return {"observed_by": observed_by, "metric_ref": metric_ref, "label": label[:80],
            "unit": unit, "transforms": values}


def observables(connection: sqlite3.Connection, company_ref: str,
                observer: Observer | None = None) -> list[dict[str, Any]]:
    """Every metric a lane can read for this company today, with its latest value."""

    observer = observer or Observer(connection)
    rows: list[dict[str, Any]] = []
    model = observer.model(company_ref)
    if model:
        picked = 0
        for driver in model.get("drivers") or ():
            if picked >= MAX_DRIVERS:
                break
            if driver.get("role") not in DRIVER_ROLES:
                continue
            row = _row(observer, company_ref, "sec_actual", str(driver.get("ref")),
                       str(driver.get("label") or driver.get("ref")), driver.get("unit"),
                       ("level", "yoy_pct", "qoq_pct"))
            if row is not None:
                rows.append(row)
                picked += 1
    row = _row(observer, company_ref, "market_price", "price:close", "收盘价", None)
    if row is not None:
        rows.append(row)
    valuation = observer.valuation(company_ref)
    for metric in ((valuation or {}).get("record") or {}).get("metrics") or ():
        if metric.get("status") != "available":
            continue
        row = _row(observer, company_ref, "valuation", f"valuation:{metric.get('metric')}",
                   str(metric.get("label") or metric.get("metric")), metric.get("unit"))
        if row is not None:
            rows.append(row)
    if table_exists(connection, "claim_versions"):
        metrics = connection.execute(
            "SELECT json_extract(claim_json,'$.metric_or_aspect') AS metric, COUNT(*) AS n "
            "FROM claim_versions WHERE json_extract(claim_json,'$.subject_ref')=? "
            "AND json_extract(claim_json,'$.claim_kind')='quantitative' "
            "GROUP BY metric ORDER BY n DESC, metric LIMIT ?",
            (company_ref, MAX_CLAIM_METRICS)).fetchall()
        for item in metrics:
            if not item["metric"]:
                continue
            row = _row(observer, company_ref, "claim", str(item["metric"]), str(item["metric"]), None)
            if row is not None:
                rows.append(row)
    row = _row(observer, company_ref, "company_fcf", "fcf:company", "公司口径自由现金流", "usd")
    if row is not None: rows.append(row)
    return rows


def build_material(connection: sqlite3.Connection, *, company: Mapping[str, Any],
                   today: date) -> dict[str, Any] | None:
    """The whole input of one proposal call, or ``None`` with no dossier."""

    if connection.row_factory is None:
        connection.row_factory = sqlite3.Row
    company_ref = company["company_ref"]
    dossier = latest_dossier(connection, company_ref)
    if dossier is None or not (dossier.get("variant_view") or {}).get("slots"):
        return None
    from .event_judgement import company_theses

    observer = Observer(connection)
    model = observer.model(company_ref)
    valuation = observer.valuation(company_ref)
    price = observer.price(company_ref)
    theses = [{"ref": item["ref"], "statement": item["statement"][:400],
               "confidence": item.get("confidence")}
              for item in company_theses(connection, company_ref)]
    return {
        "contract": CONTRACT_VERSION,
        "today": today.isoformat(),
        "company": {"company_ref": company_ref, "ticker": company.get("ticker"),
                    "name": company.get("name")},
        "dossier": dossier_material(dossier),
        "theses": theses,
        "catalysts": _catalysts(connection, company_ref, today),
        "observables": observables(connection, company_ref, observer),
        "price_as_of": None if not price else price.get("as_of"),
        "valuation_snapshot_ref": None if not valuation else valuation.get("version_ref"),
        "model_execution_status": ((model or {}).get("execution_contract") or {}).get("drivers", {}).get("status"),
        "model": None if not model else {"version_ref": model.get("id"),
                                         "version_hash": model.get("content_hash"),
            "driver_assumptions": [{k:a[k] for k in ("driver_ref", "measure", "value", "unit", "because", "refs")}
                for a in model.get("assumptions") or [] if not a.get("superseded_by") and a.get("kind") != "actual"][:64]},
    }


def allowed_refs(material: Mapping[str, Any]) -> set[str]:
    refs: set[str] = set()
    dossier = material["dossier"]
    if dossier.get("version_ref"):
        refs.add(str(dossier["version_ref"]))
    for sentence in dossier["variant_view"]:
        refs.update(sentence["refs"])
    for sentences in dossier["sections"].values():
        for sentence in sentences:
            refs.update(sentence["refs"])
    refs.update(item["ref"] for item in material["theses"])
    refs.update(str(item["entry_ref"]) for item in material["catalysts"] if item.get("entry_ref"))
    for row in material["observables"]:
        for value in row["transforms"].values():
            if value.get("source_ref"):
                refs.add(str(value["source_ref"]))
    if material.get("model"):
        refs.add(str(material["model"]["version_ref"]))
        for a in material["model"].get("driver_assumptions") or []:
            refs.update(str(r["ref"]) for r in a.get("refs") or [] if r.get("ref"))
    return refs


def observable_index(material: Mapping[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {(row["observed_by"], row["metric_ref"]): row for row in material["observables"]}


def fit_material(material: dict[str, Any], target: int = TARGET_PROMPT_BYTES) -> dict[str, Any]:
    """Drop the least useful rows until the prompt fits ``target`` (deterministic).

    Claim metrics go first (the tail of the most-cited list), then the
    guidance and catalyst prose, then the demand-driver chains; the variant
    view, the theses and the filed figures are what a scenario is made of and
    are never dropped.  Returns what was dropped; ``material`` is fitted in place.
    """

    dropped = {"claim_metrics": 0, "sections": []}
    while len(build_prompt(material).encode("utf-8")) > target:
        claims = [i for i, row in enumerate(material["observables"]) if row["observed_by"] == "claim"]
        if len(claims) > 4:
            material["observables"].pop(claims[-1])
            dropped["claim_metrics"] += 1
            continue
        sections = material["dossier"]["sections"]
        name = next((key for key in ("guidance_style", "catalyst_calendar", "demand_drivers")
                     if key in sections), None)
        if name is None:
            break
        sections.pop(name)
        dropped["sections"].append(name)
    return dropped


# ---------------------------------------------------------------------------
# the prompt and the reply
# ---------------------------------------------------------------------------


def build_prompt(material: Mapping[str, Any]) -> str:
    rules = f"""你是覆盖这家公司的研究员。下面的材料来自公司研究档案（每句带出处）、当前论点、催化剂日期，以及「可观察指标表」——系统每天能自动读到的指标、最新值、日期与出处。

任务：把档案里关于未来走向的判断整理成 3 个互斥且穷尽的情景（例如基准 / 乐观 / 悲观），每个情景写清：
- key：小写英文短标识（如 base、bull、bear）；name：不超过 40 字的中文名；narrative：不超过 300 字，说明这个情景成立时公司会怎样、为什么；
- probability：两位小数，全部情景加总等于 1.00；
- linked_thesis_refs：它涉及的论点版本 ref（只能从材料的 theses 里选，可以为空）；linked_driver_refs：它依赖的驱动项（只能从可观察指标表里 observed_by=sec_actual 的 metric_ref 里选，可以为空）；
- evidence_refs：支撑它的出处（只能用材料里出现过的 ref，至少一个）；
- triggers：1-{MAX_TRIGGERS} 个「如果这个情景正在发生，我们会看到」的条件；invalidators：0-{MAX_INVALIDATORS} 个「看到就说明它不成立」的条件。

每个条件是一个对象：condition_id（触发写 t1、t2…，否定写 i1、i2…）、label（中文一句话）、observed_by、metric_ref、transform、comparator、threshold、horizon、refs。
- observed_by 与 metric_ref 必须是可观察指标表里的一行；transform 只有 sec_actual 可以用 yoy_pct（同比%）或 qoq_pct（环比%），其他一律 level；
- comparator 只能是 {', '.join(COMPARATORS)}；threshold 是数字（百分比写成 5 表示 5%），要和表里的最新值放在一起看得懂；阈值共同划分12个月后的经济结果；不得因当前值落入某区间而预先宣称未来情景已发生；
- horizon 是 YYYY-MM-DD，统一价格日到12个月后；每个条件都要写，包括 observed_by=owner 的条件；
- refs 写阈值依据的出处（材料里出现过的 ref，至少一个），通常包括该指标最新值的 source_ref；
- 档案里只能靠人判断的信号（如投资者日的口径），observed_by 写 owner，不写 metric_ref、comparator、threshold。每个情景至少要有一个程序能读的触发条件。
- 禁止 event_judgement、THESIS_*、「研判加强/减弱」等循环条件。价格阈值仅供监测，不定义情景目标价。
- 必须恰好 bear/base/bull 三情景：共同 partition = {{observed_by,metric_ref,transform,cutpoints:[下切点,上切点],source_refs}}；切点相同且有序。经济 triggers 由代码生成：bear <下切点、base >=下切点且<上切点、bull >=上切点，不另写互相重叠的阈值。
- driver_deltas = [{{driver_ref,mode,delta,because,refs}}]：经济 quantity/price 的 economic_driver_level 用 relative_level（0.1表示+10%）；增长/费用率用rate_shift（0.02表示+2个百分点）；只引用材料模型的驱动，不能手填EPS/目标价。base没有偏移时明确[]。
- probability_basis = {{kind, because, source_refs, as_of}}，kind为template_prior/evidence_assessment/owner_assessment；0.5/0.25/0.25只能显式标template_prior，不能冒充统计校准。
- 全部horizon以price_as_of为起点到恰好12个月后。现金条件只能使用fcf:company。owner条件另加owner_rule={{source_ref,as_of,rule}}，定性条件不自动改概率。

只输出一个 JSON 对象，不要其他文字：{{"scenarios": [ ... ]}}。"""
    return rules + "\n\n材料：\n" + json.dumps(material, ensure_ascii=False, sort_keys=True,
                                             separators=(",", ":"))


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def parse_reply(text: Any) -> list[dict[str, Any]]:
    if not isinstance(text, str) or not text.strip():
        raise ScenarioProposalError("the reply is empty")
    body = text.strip()
    fenced = _FENCE_RE.search(body)
    if fenced:
        body = fenced.group(1).strip()
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        raise ScenarioProposalError("the reply holds no JSON object")
    try:
        wire = json.loads(body[start:end + 1])
    except json.JSONDecodeError as exc:
        raise ScenarioProposalError(f"the reply is not JSON: {exc}") from exc
    if not isinstance(wire, Mapping) or set(wire) != {"scenarios"} \
            or not isinstance(wire["scenarios"], list):
        raise ScenarioProposalError('the reply must be {"scenarios": [...]}')
    return [dict(item) if isinstance(item, Mapping) else item for item in wire["scenarios"]]


def _drop_undated_owner_conditions(item: Any, dropped: list[dict[str, Any]], *,
                                   today: date) -> Any:
    """Set aside an owner-judged condition that names no usable horizon.

    ws-7d, 2026-10-07: META's whole paid proposal was refused on
    "scenario base: horizon must be an ISO date" because one 「由你判断」
    condition carried no horizon -- the prompt told the model to leave the
    machine fields off an owner condition and never said horizon was not one
    of them.  An owner condition is a note to the owner, not something a lane
    evaluates; losing one is recorded on the set, it does not cost the set.
    A machine-read condition with a bad horizon still refuses, and a scenario
    left with no trigger still refuses in ``validate_scenario``.
    """

    if not isinstance(item, Mapping):
        return item
    out = dict(item)
    for name in ("triggers", "invalidators"):
        conditions = item.get(name)
        if not isinstance(conditions, list):
            continue
        kept = []
        for condition in conditions:
            if isinstance(condition, Mapping) and condition.get("observed_by") == "owner":
                try:
                    horizon = date.fromisoformat(str(condition.get("horizon")))
                except ValueError:
                    horizon = None
                if horizon is None or horizon < today:
                    dropped.append({"key": item.get("key"),
                                    "condition_id": condition.get("condition_id"),
                                    "reason": "owner_without_horizon",
                                    "label": str(condition.get("label") or "")[:120]})
                    continue
            kept.append(condition)
        out[name] = kept
    return out


def screen(scenarios: list[Any], material: Mapping[str, Any], *,
           today: date) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Validate against the material, then drop conditions already satisfied.

    Returns ``(scenarios, dropped)``.  Raises when the reply cannot stand.
    """

    index = observable_index(material)
    refs = allowed_refs(material)
    theses = {item["ref"] for item in material["theses"]}
    drivers = {row["metric_ref"] for row in material["observables"]
               if row["observed_by"] == "sec_actual"}
    cleaned = []
    dropped: list[dict[str, Any]] = []
    for item in scenarios:
        if isinstance(item, Mapping) and item.get("partition"):
            from .scenario_rules import partition_conditions
            item = dict(item)
            anchor_day = date.fromisoformat(str(material.get("price_as_of") or today.isoformat()))
            item["triggers"] = partition_conditions(item["key"], item["partition"], today=anchor_day, labels={item["key"]:item.get("name")})
        item = _drop_undated_owner_conditions(item, dropped, today=today)
        try:
            cleaned.append(validate_scenario(item, today=today, observables=index,
                                             allowed_refs=refs, thesis_refs=theses,
                                             driver_refs=drivers))
        except CompanyScenarioValidationError as exc:
            key = item.get("key") if isinstance(item, Mapping) else None
            raise ScenarioProposalError(f"scenario {key}: {exc}") from exc
    try:
        check_probabilities(cleaned)
    except CompanyScenarioValidationError as exc:
        raise ScenarioProposalError(str(exc)) from exc
    for scenario in cleaned:
        for name in ("triggers", "invalidators"):
            kept = []
            for condition in scenario[name]:
                baseline = condition.get("baseline") or {}
                if condition["observed_by"] not in {"owner", "event_judgement"} \
                        and baseline.get("value") is not None:
                    value = decimal_text(baseline["value"], "baseline")
                    threshold = decimal_text(condition["threshold"], "threshold")
                    met = {"gt": value > threshold, "gte": value >= threshold,
                           "lt": value < threshold, "lte": value <= threshold}[condition["comparator"]]
                    if met and not scenario.get("partition"):
                        dropped.append({"key": scenario["key"],
                                        "condition_id": condition["condition_id"],
                                        "reason": "already_met",
                                        "baseline": baseline, "threshold": condition["threshold"]})
                        continue
                kept.append(condition)
            scenario[name] = kept
        if not scenario["triggers"]:
            raise ScenarioProposalError(
                f"scenario {scenario['key']} has no trigger left that has not already happened")
    return cleaned, dropped


# ---------------------------------------------------------------------------
# one company
# ---------------------------------------------------------------------------


def anchors_for(material: Mapping[str, Any]) -> dict[str, Any]:
    dossier = material["dossier"]
    return {
        "dossier_version_ref": dossier.get("version_ref"),
        "dossier_version_hash": dossier.get("version_hash"),
        "dossier_body_hash": dossier.get("body_hash"),
        "model_version_ref": (material.get("model") or {}).get("version_ref"),
        "model_version_hash": (material.get("model") or {}).get("version_hash"),
        "valuation_snapshot_ref": material.get("valuation_snapshot_ref"),
        "price_as_of": material.get("price_as_of"),
        "thesis_version_refs": [item["ref"] for item in material["theses"]],
        "material_hash": content_hash(json.loads(json.dumps(material, sort_keys=True))),
    }


def proposal_input_gate(material):
    return bool(material.get("model") and material.get("valuation_snapshot_ref") and material.get("price_as_of") and material.get("model_execution_status") == "executed")


def propose_for_company(
    store: Any,
    *,
    mission: Mapping[str, Any],
    company: Mapping[str, Any],
    model: Any,
    today: date | None = None,
) -> dict[str, Any]:
    """Build the material, make the one call, screen it, write the set.

    Returns a summary with ``status`` in ``published`` / ``no_dossier`` /
    ``pending_exists`` / ``not_authorized`` / ``refused`` / ``too_large``;
    model and scheduler errors are raised to the caller (the child CLI maps
    them to ``model_unavailable`` / ``busy``).
    """

    day = today or datetime.now(timezone.utc).date()
    company_ref = company["company_ref"]
    if PROPOSAL_WRITE_SCOPE not in mission["autonomy"]["may_write"]:
        return {"status": "not_authorized",
                "reason": f"任务没有授予 {PROPOSAL_WRITE_SCOPE} 写入范围，不起草情景"}
    authority = CompanyScenarioAuthority(store)
    if authority.pending_set(company_ref) is not None:
        return {"status": "pending_exists", "reason": "上一组情景提案还在等你裁决"}
    material = build_material(store.connection, company=company, today=day)
    if material is None:
        return {"status": "no_dossier", "reason": "这家公司还没有带观点的研究档案"}
    if not proposal_input_gate(material):
        return {"status": "inputs_unavailable", "reason": "没有当前模型、估值或价格日，不起草情景"}
    fit = fit_material(material)
    prompt = build_prompt(material)
    size = len(prompt.encode("utf-8"))
    if size > MAX_PROMPT_BYTES:
        return {"status": "too_large", "prompt_bytes": size,
                "reason": f"材料 {size} 字节，超过 {MAX_PROMPT_BYTES} 的上限"}
    request_id = content_hash({"purpose": PURPOSE, "company": company_ref,
                               "prompt": content_hash({"text": prompt})})[:32]
    call = model.call(purpose=PURPOSE, request_id=request_id, prompt=prompt, mission=mission)
    proposer = {
        "kind": "model", "purpose": PURPOSE, "contract": CONTRACT_VERSION,
        "request_id": request_id, "prompt_sha256": content_hash({"text": prompt}),
        "prompt_bytes": size, "fit": fit, "cost_micros": int(call.get("cost_micros") or 0),
        **{key: call.get(key) for key in ("work_order_ref", "invocation_ref",
                                           "route_decision_ref", "result_envelope_ref")},
    }
    summary = {"prompt_bytes": size, "cost_micros": proposer["cost_micros"],
               "request_id": request_id}
    try:
        scenarios, dropped = screen(parse_reply(call.get("text")), material, today=day)
    except ScenarioProposalError as exc:
        return {**summary, "status": "refused", "reason": str(exc)[:500]}
    actor = mission["autonomy"]["automation_principal"]
    written = authority.propose_set(
        company_ref=company_ref, scenarios=scenarios, anchors=anchors_for(material),
        proposer=proposer, dropped_conditions=dropped, mission=mission, actor_ref=actor,
        today=day,
    )
    return {**summary, "status": written["status"], "set_ref": written.get("id"),
            "scenarios": len(scenarios), "dropped_conditions": len(dropped)}


def eligible_companies(connection: sqlite3.Connection, mission: Mapping[str, Any], *,
                       now: datetime, min_repropose_days: int) -> list[dict[str, Any]]:
    """Companies a proposal is owed to, in universe order.

    Owed when the company has a dossier with a variant view and either has no
    set at all, or its newest set was decided (accepted or rejected), was
    drafted from a dossier body that has since changed, and is at least
    ``min_repropose_days`` old.  A pending or deferred set waits for the owner.
    """

    from .company_scenario import read_sets

    if connection.row_factory is None:
        connection.row_factory = sqlite3.Row
    out = []
    for member in mission["universe"]:
        company_ref = member["company_ref"]
        dossier = latest_dossier(connection, company_ref)
        if dossier is None or not (dossier.get("variant_view") or {}).get("slots"):
            continue
        material=build_material(connection,company=member,today=now.date())
        if material is None or not proposal_input_gate(material):continue
        current_anchors=anchors_for(material)
        contract_hash=content_hash({k:current_anchors[k] for k in ("dossier_body_hash","model_version_ref","model_version_hash","valuation_snapshot_ref","price_as_of")})
        sets = [item for item in read_sets(connection, company_ref) if item["origin"] == "automation"]
        if sets:
            newest = sets[-1]
            if newest["state"] in {"pending", "deferred"}:
                continue
            previous_anchors=newest.get("anchors") or {}
            if all(previous_anchors.get(k)==current_anchors.get(k) for k in ("dossier_body_hash","model_version_ref","model_version_hash","valuation_snapshot_ref","price_as_of")):
                continue
            created = datetime.fromisoformat(newest["created_at"])
            if (now - created).days < min_repropose_days:
                continue
        out.append({"company_ref": company_ref, "ticker": member.get("ticker"),
                    "name": member.get("name"), "dossier_ref": dossier.get("id"),
                    "dossier_body_hash": dossier.get("body_hash"),"proposal_input_hash":contract_hash})
    return out


__all__ = [
    "CONTRACT_VERSION", "MAX_PROMPT_BYTES", "PURPOSE", "ScenarioProposalError",
    "TARGET_PROMPT_BYTES", "fit_material",
    "allowed_refs", "anchors_for", "build_material", "build_prompt", "eligible_companies",
    "latest_dossier", "observable_index", "observables", "parse_reply", "propose_for_company",
    "screen",
]
