"""Draft and publish one fully verified Investment Memo candidate."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .call_budget import resolve_call_budget, resolve_run_budget, freeze_shared_budget_policy
from .cockpit_model import CockpitModel
from .coverage_mission import CoverageMissionAuthority
from .investment_memo_contract import (
    CHECK_REFS_V2, SCHEMA_VERSION_V2, adv_findings, model_work_order_refs, recommendation_findings,
    validate_memo_gate,
)
from .investment_memo_draft import (
    GROUPS, MAX_COST_USD, MAX_INPUT_TOKENS, MAX_OUTPUT_TOKENS, MAX_REPAIRS, MAX_RUN_COST_USD,
    TIMEOUT_SECONDS, citation_index, draft_group, verified_material_hash, verify_memo,
)
from .mission_deliverable import MissionDeliverableAuthority, MissionDeliverableError, validate_section
from .research_playbook import ResearchPlaybookAuthority
from .store import DaltonStore, content_hash

SUMMARY_SCHEMA_VERSION = "0.1"
MEMO_CONTRACT_VERSION = "investment-memo-contract:batch-d:1"


def market_block_row(connection: Any, company_ref: str) -> dict[str, Any] | None:
    """Price, market cap and 30-day ADV as one citable number row.

    The live Key Information title asks for 「30 日 ADV」 and the memo material
    used to carry no ADV at all, so every draft either invented one or was
    refused (ACN, seven times).  The row's text is the figure source a
    ``numbers[].number_ref`` cites; an unavailable ADV is stated with its
    reason so the drafter writes 「30 日 ADV：暂不可得（原因）」.
    """

    from .market_data_block import market_block_from

    try:
        block = market_block_from(connection, company_ref)
    except Exception:  # noqa: BLE001 - one optional block
        return None
    if block.get("status") != "ok":
        return None
    currency = str(block.get("currency") or "")
    price = block.get("price") or {}
    parts = [f"收盘价 {price.get('close')} {currency}（{price.get('date')}）"]
    cap = block.get("market_cap") or {}
    if cap.get("value") is not None:
        parts.append(f"市值 {cap['value']} {currency}（{cap.get('formula')}）")
    else:
        parts.append(f"市值不可得（{cap.get('reason') or '无股本观测'}）")
    adv = block.get("adv_30d") or {}
    if adv.get("status") == "available":
        parts.append(f"30 日 ADV {adv['value']} {currency}，{adv['shares']} 股/日（自 {adv.get('since')}）")
    else:
        parts.append(f"30 日 ADV 不可得（{adv.get('reason') or '没有成交量'}）")
    text = "；".join(parts)
    return {"ref": f"market-block:{block.get('series_version_ref')}:{price.get('date')}",
            "hash": content_hash(text), "kind": "market_block", "text": text,
            "source_kind": "market", "period": str(price.get("date") or "")}


PACK_ROW_KINDS = {"number": "pack_number", "passage": "pack_passage", "ours": "pack_ours"}


def material_pack_rows(store: Any, company_ref: str) -> tuple[list[dict[str, Any]], str | None]:
    """The batch-C material pack's N/P/O tags as citable memo rows.

    ``(rows, reason)``: no rows and the reason when the pack is unavailable
    (sibling module absent, in-memory Core, or a build error).
    """

    rows, reason, _record = material_pack_bound(store, company_ref)
    return rows, reason


def material_pack_bound(store: Any, company_ref: str) -> tuple[list[dict[str, Any]], str | None,
                                                               dict[str, Any] | None]:
    """``material_pack_rows`` plus the pack's ``record()`` the memo version stores."""

    from .product_contract import MaterialPackUnavailable, build_pack, tag_entry

    path = str(getattr(store, "path", ":memory:") or ":memory:")
    if path == ":memory:":
        return [], "in-memory Core: no material pack", None
    try:
        pack = build_pack(Path(path).parent, company_ref, "investment_memo")
    except MaterialPackUnavailable as exc:
        return [], str(exc), None
    except Exception as exc:  # noqa: BLE001 - the pack is an input, not the lane
        return [], f"material pack failed: {type(exc).__name__}: {exc}", None
    record = pack.get("record")
    record = dict(record) if isinstance(record, Mapping) else None
    rows = []
    for tag in sorted(pack.get("tags") or {}):
        entry = tag_entry(pack, str(tag))
        if entry is None or entry["kind"] not in PACK_ROW_KINDS or not entry.get("text"):
            continue
        # Batch C: our model's figures are [N…] tags into /our_model -- those
        # are the 「我们的估计」 rows; an [O…] tag is our earlier *view*, which
        # is background and only an estimate row when it binds our model.
        kind = ("pack_ours" if entry.get("source_kind") == "ours" and entry["kind"] == "number"
                else PACK_ROW_KINDS[entry["kind"]])
        if entry["kind"] == "ours" and not str(entry["ref"]).startswith(("assumption:", "pack-json:/our_model")):
            continue
        text = str(entry["text"])
        rows.append({"ref": entry["ref"], "hash": content_hash(text),
                     "kind": kind, "text": text,
                     "source_kind": str(entry.get("source_kind") or entry["kind"]),
                     "period": str(entry.get("period") or entry.get("as_of") or "")})
    return rows, None, record


def _latest_rows(connection: Any, table: str, company_ref: str, *, subject_column: str = "company_ref",
                 all_rows: bool = False) -> list[dict[str, Any]]:
    """Read only the newest immutable record for each chain in one known table."""
    try:
        columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
        if not {subject_column, "record_json", "content_hash"} <= columns:
            return []
        order = "version_number DESC" if "version_number" in columns else "created_at DESC"
        rows = connection.execute(
            f"SELECT record_json,content_hash FROM {table} WHERE {subject_column}=? ORDER BY {order}",
            (company_ref,),
        ).fetchall()
    except Exception:
        return []
    # Some authorities have several chains per company. Exact duplicate refs are
    # harmless; the input binding hash still covers every retained record.
    seen: set[str] = set()
    result = []
    for row in rows:
        record = json.loads(row["record_json"])
        embedded = record.get("content_hash")
        calculated = content_hash({key: value for key, value in record.items()
                                   if key != "content_hash"})
        if embedded != row["content_hash"] or calculated != row["content_hash"]:
            continue
        ref = next((str(record.get(key)) for key in (
            "id", "version_id", "projection_id", "event_id", "snapshot_ref",
            "calendar_ref", "consensus_ref", "model_ref") if record.get(key)), "")
        if not ref:
            ref = f"{table}:{row['content_hash']}"
        if ref in seen:
            continue
        seen.add(ref)
        result.append({"ref": ref, "hash": str(row["content_hash"]), "kind": table,
                       "text": json.dumps(record, ensure_ascii=False, sort_keys=True)})
    return result if all_rows else result[:1]


def collect_frozen_input(store: DaltonStore, *, company_ref: str | None = None,
                         _excluded: tuple[str, ...] = (),
                         _skipped: tuple[Mapping[str, str], ...] = ()) -> dict[str, Any]:
    missions = CoverageMissionAuthority(store)
    pointers = store.connection.execute(
        "SELECT mission_ref FROM coverage_mission_pointer ORDER BY mission_ref").fetchall()
    if len(pointers) != 1:
        return {"status": "held", "reason": "memo drafting requires exactly one active mission"}
    mission = missions.active_mission(pointers[0]["mission_ref"])
    playbook_binding = mission["bindings"]["playbook_version"]
    playbook = ResearchPlaybookAuthority(store).playbook(playbook_binding["ref"])
    if playbook["content_hash"] != playbook_binding["hash"]:
        return {"status": "held", "reason": "the mission's playbook binding drifted"}
    if "investment_memo" not in mission.get("deliverables", ()) \
            or "deliverable" not in mission["autonomy"]["may_write"] \
            or "investment_memo" not in mission["autonomy"]["human_checkpoints"]:
        return {"status": "held", "reason": "the mission does not authorize memo candidates"}
    from .mission_model_stage_lane import mission_version_refs

    mission_refs = mission_version_refs(store.connection, mission)
    candidates = [member for member in mission["universe"]
                  if company_ref in (None, member["company_ref"])
                  and member["company_ref"] not in _excluded]
    states = {member["company_ref"]: missions.current_stage_state(
        mission["mission_ref"], member["company_ref"]) for member in candidates}
    chosen = next((member for member in candidates
                   if states[member["company_ref"]]["next_stage"] == "investment_memo"), None)
    if chosen is None:
        waiting = [{"company_ref": member["company_ref"],
                    "reason": stage_wait_reason(member, states[member["company_ref"]])}
                   for member in candidates]
        reason = ("；".join(str(item["reason"]) for item in (*_skipped, *waiting))
                  or "研究任务里没有可起草备忘录的公司")
        return {"status": "idle", "reason": reason,
                "skipped": [*_skipped, *waiting]}
    company_ref = chosen["company_ref"]

    def hold_or_continue(reason: str) -> dict[str, Any]:
        reason = f"{chosen.get('ticker') or company_ref.rsplit(':', 1)[-1]}：{reason}"
        skipped = (*_skipped, {"company_ref": company_ref, "reason": reason})
        if len(candidates) > 1 and company_ref not in _excluded:
            return collect_frozen_input(store, _excluded=(*_excluded, company_ref),
                                        _skipped=skipped)
        return {"status": "held", "reason": reason, "skipped": list(skipped)}

    rows: list[dict[str, Any]] = []
    # Canonical live Claims are the only prose refs MissionDeliverable accepts.
    live = MissionDeliverableAuthority(store).live_claim_version_refs()
    for row in store.connection.execute(
        "SELECT claim_version_id,claim_json,content_hash FROM claim_versions ORDER BY created_at"
    ).fetchall():
        if row["claim_version_id"] not in live:
            continue
        claim = json.loads(row["claim_json"])
        if claim.get("subject_ref") != company_ref:
            continue
        rows.append({"ref": row["claim_version_id"], "hash": row["content_hash"], "kind": "claim",
                     "text": json.dumps(claim, ensure_ascii=False, sort_keys=True)})
    from .company_dossier import CompanyDossierAuthority
    from .forecast_sensitivity import SensitivityProjectionAuthority
    from .model_forecast_driver import ForecastModelAuthority
    from .valuation_snapshot import ValuationSnapshotAuthority

    dossier = CompanyDossierAuthority(store).latest(company_ref)
    forecast = ForecastModelAuthority(store).latest(company_ref)
    valuation = ValuationSnapshotAuthority(store).latest_version(company_ref)
    for kind, record in (("company_dossier_versions", dossier),
                         ("forecast_model_versions", forecast),
                         ("valuation_snapshot_versions", valuation)):
        if record is not None:
            rows.append({"ref": record["id"], "hash": record["content_hash"], "kind": kind,
                         "text": json.dumps(record, ensure_ascii=False, sort_keys=True)})
    for table in (
        "debate_map_versions",
        "market_price_series_versions", "catalyst_calendar_versions", "consensus_estimate_versions",
        "prior_model_versions", "event_judgements", "thesis_reflections", "research_events",
    ):
        rows.extend(_latest_rows(store.connection, table, company_ref,
                                subject_column="subject_ref" if table == "debate_map_versions" else "company_ref",
                                all_rows=table == "research_events"))
    projection = SensitivityProjectionAuthority(store).latest(company_ref)
    if projection is not None and forecast is not None \
            and projection.get("model_version_ref") == forecast.get("id") \
            and projection.get("model_version_hash") == forecast.get("content_hash") \
            and projection.get("inputs_hash") == forecast.get("inputs_hash"):
        rows.append({"ref": projection["id"], "hash": projection["content_hash"],
                     "kind": "sensitivity_projections",
                     "text": json.dumps(projection, ensure_ascii=False, sort_keys=True)})
    for model_row in [row for row in rows if row["kind"] == "forecast_model_versions"]:
        model_record = json.loads(model_row["text"])
        for result in model_record.get("results") or ():
            for cell in result.get("cells") or ():
                if not cell.get("ref"):
                    continue
                rows.append({"ref": cell["ref"], "hash": content_hash(cell),
                             "kind": "forecast_cell", "version_ref": str(model_record["id"]),
                             "text": json.dumps(cell, ensure_ascii=False, sort_keys=True)})
    if valuation is not None:
        # The snapshot's computed multiples are the cells a Key Information or
        # valuation figure cites (``valuation-metric:<version>:<metric>``, the
        # shape MissionDeliverable resolves).  Without them the only place a
        # price or multiple lived was the snapshot row itself, which is not
        # citable in a section, and every draft that quoted one was refused.
        for metric in valuation.get("metrics") or ():
            if not isinstance(metric, Mapping) or metric.get("status") != "available" \
                    or metric.get("value") is None or not metric.get("metric"):
                continue
            rows.append({"ref": f"valuation-metric:{valuation['id']}:{metric['metric']}",
                         "hash": content_hash(metric), "kind": "valuation_metric",
                         "version_ref": str(valuation["id"]),
                         "text": json.dumps(metric, ensure_ascii=False, sort_keys=True)})
    try:
        approved_rows = store.connection.execute(
            "SELECT v.record_json,v.content_hash FROM deep_insight_gate_versions v "
            "JOIN deep_insight_gate_decisions d ON d.gate_version_ref=v.version_id "
            "WHERE v.company_ref=? AND d.decision='approve' ORDER BY v.version_number DESC",
            (company_ref,),
        ).fetchall()
        # The draft binds its mission version under ``bindings`` (the Deep
        # Insight Gate record shape); any version of this mission counts,
        # because a version is provenance, not scope (ADR-0008 addendum).
        # Reading only the top-level field matched no real gate record, so
        # every memo held here for ever.
        approved = next((row for row in approved_rows
                         if gate_mission_version(json.loads(row["record_json"])) in mission_refs),
                        None)
    except Exception:
        approved = None
    if approved is None:
        return hold_or_continue("缺少你已批准的深度认知门（当前研究任务下）")
    gate_record = json.loads(approved["record_json"])
    rows.append({"ref": gate_record["id"], "hash": approved["content_hash"],
                 "kind": "deep_insight_gate_versions",
                 "text": json.dumps(gate_record, ensure_ascii=False, sort_keys=True)})
    # Industry framework is an exact current deliverable and may be shared by all companies.
    deliverables = MissionDeliverableAuthority(store).deliverables(mission["id"])
    for record in deliverables:
        if record["kind"] == "investment_memo":
            continue
        if record["subject_ref"] not in (company_ref, mission["industry_ref"]):
            continue
        rows.append({"ref": record["id"], "hash": record["content_hash"],
                     "kind": record["kind"], "text": json.dumps(record, ensure_ascii=False, sort_keys=True)})
    if "industry_framework" not in {row["kind"] for row in rows}:
        # The industry framework is its own authority, not a MissionDeliverable:
        # the live Core has 30 framework versions and no ``industry_framework``
        # deliverable at all, so the memo held on "missing industry_framework"
        # even after every gate passed.
        framework = latest_industry_framework(store.connection, mission, mission_refs)
        if framework is not None:
            rows.append({"ref": framework["id"], "hash": framework["content_hash"],
                         "kind": "industry_framework",
                         "text": json.dumps(framework, ensure_ascii=False, sort_keys=True)})
    kinds = {row["kind"] for row in rows}
    required = {"company_dossier_versions", "deep_insight_gate_versions", "forecast_model_versions",
                "sensitivity_projections", "valuation_snapshot_versions", "industry_framework"}
    missing = sorted(required - kinds)
    if missing:
        return hold_or_continue("备忘录缺少依据：" + ", ".join(missing))
    market = market_block_row(store.connection, company_ref)
    if market is not None:
        rows.append(market)
    pack_rows, pack_reason, pack_record = material_pack_bound(store, company_ref)
    rows.extend(pack_rows)
    from .investment_memo_material import bound_material

    rows = bound_material(rows, company_ref=company_ref)
    bindings = [{"ref": row["ref"], "hash": row["hash"], "kind": row["kind"]} for row in rows]
    current = MissionDeliverableAuthority(store).latest(
        "mission-deliverable:investment_memo:" + company_ref.rsplit(":", 1)[-1])
    if current is not None and current.get("mission_version_ref") in mission_refs \
            and anchor_bindings((current.get("gate") or {}).get("input_bindings") or []) \
            == anchor_bindings(bindings):
        # A memo already stands on this company's approved gate and driver
        # model.  New claims, prices and events arrive every day; redrafting
        # for each of them would put a new four-call memo in front of the
        # owner daily.  The memo is redrafted when what it argues *from*
        # moves: a newly approved gate or a new model version (ADR-0011).
        reason = (f"{chosen.get('ticker') or company_ref.rsplit(':', 1)[-1]}："
                  "已有一份基于当前深度认知门与公司模型的备忘录，依据未变")
        skipped = (*_skipped, {"company_ref": company_ref, "reason": reason})
        if len(candidates) > 1:
            return collect_frozen_input(store, _excluded=(*_excluded, company_ref),
                                        _skipped=skipped)
        return {"status": "idle", "memo_status": "nothing_new", "company_ref": company_ref,
                "reason": reason, "skipped": list(skipped)}
    optional = {"debate_map_versions", "market_price_series_versions",
                "catalyst_calendar_versions", "consensus_estimate_versions", "prior_model_versions",
                "event_judgements", "thesis_reflections", "research_events"}
    return {"status": "ready", "mission": mission, "playbook": playbook, "company": chosen,
            "material": rows, "input_bindings": bindings, "skipped": list(_skipped),
            "material_pack": {"status": "unavailable" if pack_reason else "bound",
                              "reason": pack_reason, "rows": len(pack_rows)},
            "material_pack_record": pack_record,
            "optional_missing": sorted(optional - kinds)}


def cited_refs(sections: Sequence[Mapping[str, Any]],
               questions: Sequence[Mapping[str, Any]]) -> set[str]:
    refs: set[str] = set()
    for section in sections:
        refs.update(str(ref) for ref in section.get("claim_refs") or ())
        refs.update(str(row["ref"]) for row in section.get("source_refs") or ()
                    if isinstance(row, Mapping) and isinstance(row.get("ref"), str))
        for number in section.get("numbers") or ():
            if not isinstance(number, Mapping):
                continue
            if number.get("claim_version_ref"):
                refs.add(str(number["claim_version_ref"]))
            cell = number.get("cell")
            if isinstance(cell, Mapping) and cell.get("ref"):
                refs.add(str(cell["ref"]))
            for key in ("number_ref", "passage_ref"):
                if number.get(key):
                    refs.add(str(number[key]))
            estimate = number.get("estimate")
            if isinstance(estimate, Mapping) and estimate.get("assumption_ref"):
                refs.add(str(estimate["assumption_ref"]))
    for question in questions:
        refs.update(str(ref) for ref in question.get("refs") or ())
    return refs


# The authorities a memo argues from; a memo is redrafted only when one moves.
ANCHOR_KINDS = ("deep_insight_gate_versions", "forecast_model_versions")


def anchor_bindings(bindings: Sequence[Mapping[str, Any]]) -> list[tuple[str, str, str]]:
    return sorted((str(row.get("kind")), str(row.get("ref")), str(row.get("hash")))
                  for row in bindings if row.get("kind") in ANCHOR_KINDS)


def gate_mission_version(record: Mapping[str, Any]) -> str | None:
    bound = (record.get("bindings") or {}).get("mission_version_ref") \
        or record.get("mission_version_ref")
    return None if bound is None else str(bound)


def latest_industry_framework(connection: Any, mission: Mapping[str, Any],
                              mission_refs: Any) -> dict[str, Any] | None:
    """The industry's newest framework version, if it belongs to this mission."""

    try:
        from .industry_framework import IndustryFrameworkAuthority

        reader = IndustryFrameworkAuthority.__new__(IndustryFrameworkAuthority)
        reader.connection = connection
        framework = reader.latest(str(mission["industry_ref"]))
    except Exception:  # noqa: BLE001 - no framework authority yet
        return None
    if framework is None:
        return None
    if (framework.get("bindings") or {}).get("mission_version_ref") not in mission_refs:
        return None
    return framework


def stage_wait_reason(member: Mapping[str, Any], state: Mapping[str, Any]) -> str:
    """One company's answer to "why is there no memo yet", in the owner's words."""

    from .mission_stage import STAGE_LABELS, STAGE_STATUS_LABELS

    label = str(member.get("ticker") or str(member["company_ref"]).rsplit(":", 1)[-1])
    stage = state.get("current_stage")
    nxt = state.get("next_stage")
    if nxt in (None, "active_coverage") and stage in ("investment_memo", "active_coverage"):
        return f"{label} 备忘录已裁决"
    if stage is None:
        return f"{label} 尚未开始研究阶段"
    status = STAGE_STATUS_LABELS.get(str(state.get("current_status")),
                                     str(state.get("current_status")))
    return (f"{label} 停在{STAGE_LABELS.get(stage, stage)}（{status}），"
            f"等{STAGE_LABELS['company_model']}通过审核")


def source_kinds_for(section: Mapping[str, Any], material: Sequence[Mapping[str, Any]]) -> set[str]:
    """What kinds of source a memo section cites, for the source-aware lint rules."""

    rows = {str(row.get("ref")): row for row in material}
    kinds: set[str] = set()
    cited = [str(ref) for ref in section.get("claim_refs") or ()]
    cited.extend(str(row["ref"]) for row in section.get("source_refs") or ()
                 if isinstance(row, Mapping) and isinstance(row.get("ref"), str))
    for number in section.get("numbers") or ():
        for key in ("number_ref", "passage_ref", "claim_version_ref"):
            if number.get(key):
                cited.append(str(number[key]))
    for ref in cited:
        row = rows.get(ref) or {}
        if row.get("source_kind"):
            kinds.add(str(row["source_kind"]))
        if row.get("kind") == "consensus_estimate_versions" or "consensus" in str(row.get("text") or "").lower()[:400]:
            kinds.add("consensus")
    return kinds


def recommendation_check(sections: Sequence[Mapping[str, Any]]) -> tuple[bool, str]:
    key = next((section for section in sections
                if str(section.get("title") or "").startswith("Key Information")), None)
    if key is None:
        return False, "no Key Information section carries the recommendation"
    found = recommendation_findings(str(key.get("body") or "")) + adv_findings(
        str(key.get("title") or ""), str(key.get("body") or ""), key.get("gap_items") or ())
    if found:
        return False, "Key Information: " + "；".join(item["code"] for item in found)
    return True, "Key Information states 建议（研究建议）: direction, position range and horizon"


def lint_check(sections: Sequence[Mapping[str, Any]],
               material: Sequence[Mapping[str, Any]]) -> tuple[bool, str]:
    from .research_reasoning_lint import lint_text

    findings = []
    for section in sections:
        findings += lint_text(str(section.get("body") or ""),
                              source_kinds=source_kinds_for(section, material),
                              section=str(section.get("title") or ""))
    if findings:
        return False, "reasoning lint: " + "；".join(
            f"{item['rule']}@{item.get('section', '')}" for item in findings[:5])
    return True, "no reasoning-lint finding"


def _checks(sections: Sequence[Mapping[str, Any]], questions: Sequence[Mapping[str, Any]],
            material: Sequence[Mapping[str, Any]] = ()) -> list[dict[str, Any]]:
    refs = sorted({str(ref) for q in questions for ref in q["refs"]})
    by_title = {str(section["title"]): section for section in sections}
    values = (
        (len(questions) == 12 and all(not q["unknown"] and q["answer"] and q["refs"] and q["falsifier"] for q in questions), "12 Playbook questions are answered, cited and falsifiable"),
        (bool(questions[7]["answer"] and questions[6]["answer"]), "variant view is stated against consensus"),
        (any("Anti-thesis" in title and section["body"] for title, section in by_title.items()), "Anti-thesis is a written counter-case"),
        (any(title.startswith("S5 ") and section["body"] for title, section in by_title.items()) and bool(questions[11]["answer"]), "risk/reward and loss case are written"),
        recommendation_check(sections),
        lint_check(sections, material),
    )
    return [{"check_ref": ref, "status": "pass" if passed else "fail", "reason": reason,
             "evidence_refs": refs} for ref, (passed, reason) in zip(CHECK_REFS_V2, values)]


def _validated_group(store, outcome, material):
    """Check every section before buying another group; no new repair slot."""
    parse_refused = (outcome.get('status') == 'refused' and outcome.get('failure_class') == 'content'
                     and bool(outcome.get('diagnostic_sections')))
    if outcome.get('status') != 'drafted' and not parse_refused:return outcome
    candidate_sections = outcome['diagnostic_sections'] if parse_refused else outcome['sections']
    from .product_passage_validation import checked_rendered_passages
    index=citation_index(material)
    path=str(getattr(store,'path',':memory:'))
    try:
        passages=checked_rendered_passages(Path(path).parent if path!=':memory:' else ':memory:',
                                           candidate_sections,index['passages'])
    except (ValueError,OSError) as exc:
        return {**outcome,'status':'refused','repairable':False,
                'failure_class':'source_unavailable',
                'reason':'memo_source_proof_unavailable: '+str(exc)}
    from .mission_deliverable import pack_passage_resolver
    checked=[];findings=list(outcome.get('findings') or []) if parse_refused else []
    from .investment_memo_number_diagnostics import findings as number_findings
    for section in candidate_sections:
        numeric_findings=number_findings(section,live_claim_refs=index['claims'],
            resolve_cell=lambda cell:index['cells'].get(str(cell.get('ref')))==dict(cell),
            resolve_passage=pack_passage_resolver(None,passages),resolve_number=index['numbers'].get)
        try:
            checked.append(validate_section(section,live_claim_refs=index['claims'],
                resolve_cell=lambda cell:index['cells'].get(str(cell.get('ref')))==dict(cell),
                resolve_passage=pack_passage_resolver(None,passages),
                resolve_number=index['numbers'].get,strict=True))
        except MissionDeliverableError as exc:
            findings.append({'section':section['title'],'code':'section_authority',
                             'detail':str(exc)[:700]})
        findings.extend(f for f in numeric_findings if f not in findings)
    if findings:
        if len(findings)>128:
            return {**outcome,'status':'refused','repairable':False,'failure_class':'diagnostic_overflow',
                'reason':'memo complete validator diagnostics exceed the fixed correction proof bound',
                'findings':findings[:128],'findings_count':len(findings),
                'omitted_findings_hash':content_hash(findings[128:])}
        return {**outcome,'status':'refused','repairable':True,'failure_class':'content','findings':findings,
                'reason':((outcome['reason']+'; complete diagnostics: ') if parse_refused else
                          'section authority rejected: ')+json.dumps(findings,ensure_ascii=False),
                'refs':list(outcome.get('refs') or []) if parse_refused else []}
    return {**outcome,'sections':checked}


def run_memo(*, store: DaltonStore, frozen: Mapping[str, Any], model: Any, verifier_model: Any,
             max_units: int = 4, max_repairs: int = 0, quiesce: Any = None,
             repair_budget: Any = None) -> dict[str, Any]:
    if frozen.get("status") != "ready":
        return dict(frozen)
    mission, playbook, company = frozen["mission"], frozen["playbook"], frozen["company"]
    titles = playbook["deliverable_templates"]["investment_memo"]
    questions = [{"question_ref": f"memo_q{i:02d}", "question": text}
                 for i, text in enumerate(playbook["key_questions"], 1)]
    if len(titles) != 12 or len(questions) != 12:
        return {"status": "held", "reason": "the bound Playbook does not have 12 memo sections and 12 key questions"}
    if max_units < len(GROUPS):
        return {"status": "held", "reason": "run max_units cannot cover the atomic four-group memo"}
    from .investment_memo_repair_budget import MemoRepairBudget, MemoMacroVerifier
    if isinstance(repair_budget, MemoRepairBudget):
        from .investment_memo_draft import build_group_prompt
        prompts = [build_group_prompt(group=group,
            section_titles=[titles[i] for i in section_indexes],
            questions=[questions[i] for i in question_indexes],
            material=frozen['material'], company=company)
            for group, section_indexes, question_indexes in GROUPS]
        repair_budget.bind_macro(prompts, mission,allow_maintenance=max_repairs>0)
        verifier_model = MemoMacroVerifier(verifier_model, repair_budget)
        # A complete fee census is a free accounting operation, not a
        # refused answer and not a paid retry generation. Stop before drafting
        # while its next bounded page is pending.
        if repair_budget.macro is not None:
            if repair_budget.macro.complete:
                repair_budget._refresh_macro()
            if repair_budget.macro.pending is not None:
                return {"status":"held","memo_status":"macro_accounting_pending",
                        "reason":"完整历史费用索引仍在核对，系统会继续处理。",
                        "macro_progress":repair_budget.macro.pending,"cost_micros":0}
    sections, answers, calls = [], [], []
    cost_micros = 0
    repairs_left = max(0, int(max_repairs))
    repaired: list[dict[str, Any]] = []
    for group_index, (group, section_indexes, question_indexes) in enumerate(GROUPS):
        kwargs = dict(group=group, section_titles=[titles[i] for i in section_indexes],
                      questions=[questions[i] for i in question_indexes],
                      material=frozen["material"], company=company, mission=mission)
        if quiesce is not None:
            kwargs["quiesce"] = quiesce
        if repair_budget is not None:
            repair_budget.begin_stage(max_units=max_units, allow_technical=bool(repairs_left), quiesce=quiesce)
        outcome = _validated_group(store,draft_group(model, **kwargs),frozen['material'])
        cost_micros += int(outcome.get("model", {}).get("cost_micros") or outcome.get("cost_micros") or 0)
        first = {"group": group, "reason": outcome.get("reason"), "refs": outcome.get("refs") or []}
        if isinstance(repair_budget, MemoRepairBudget) and outcome['status'] != 'drafted' and outcome.get('repairable'):
            from .investment_memo_macro_accounting import request_id
            prompt = build_group_prompt(**{key: kwargs[key] for key in ('group','section_titles','questions','material','company')}, repair=first)
            repair_budget.prepare_repair({'purpose': 'investment_memo', 'request_id': request_id(group,prompt),
                                         'prompt': prompt, 'mission': mission})
        if (outcome["status"] != "drafted" and outcome.get("repairable") and repairs_left
                and (repair_budget is None or repair_budget.can_repair(
                    remaining_groups=len(GROUPS) - group_index - 1))):
            # One paid redo, told exactly which refs were refused and why.
            repairs_left -= 1
            if repair_budget is not None:
                repair_budget.begin_stage(max_units=max_units, allow_technical=False, quiesce=quiesce, repair=True)
            outcome = _validated_group(store,draft_group(model, **kwargs, repair=first),frozen['material'])
            cost_micros += int(outcome.get("model", {}).get("cost_micros") or outcome.get("cost_micros") or 0)
            repaired.append(first)
        for _maintenance_round in range(2):
            if not (max_repairs > 0 and isinstance(repair_budget,MemoRepairBudget)
                and outcome.get('status') != 'drafted' and outcome.get('failure_class') == 'content'):
                break
            context = {key:kwargs[key] for key in ('group','section_titles','questions','material','company')}
            maintenance = repair_budget.prepare_maintenance(context=context,outcome=outcome,
                remaining_groups=len(GROUPS)-group_index-1)
            if maintenance is None:
                break
            if maintenance is not None:
                repair_budget.begin_stage(max_units=max_units,allow_technical=False,quiesce=quiesce,
                                          maintenance=maintenance)
                outcome = _validated_group(store,draft_group(model,**kwargs,maintenance=maintenance),
                                           frozen['material'])
                cost_micros += int(outcome.get('model',{}).get('cost_micros') or outcome.get('cost_micros') or 0)
                repaired.append({'group':group,'maintenance_permission_ref':maintenance['permission_ref'],
                                 'parent_work_order_ref':maintenance['parent']['work_order_ref'],
                                 'findings':maintenance['diagnostics']['findings']})
        if outcome["status"] != "drafted":
            refused = {"status": "refused", "memo_status": "draft_refused", "reason": outcome["reason"],
                       "cost_micros": cost_micros}
            if outcome.get("refs"):
                refused["offending_refs"] = list(outcome["refs"])
            if outcome.get('findings'):
                refused['findings']=list(outcome['findings'])
            if isinstance(repair_budget,MemoRepairBudget) and repair_budget.maintenance_hold is not None:
                refused["maintenance_hold"] = repair_budget.maintenance_hold
            if repaired:
                refused["repairs"] = repaired
            return refused
        sections.extend(outcome["sections"]); answers.extend(outcome["key_questions"])
        calls.append({"group": group, "work_order_ref": outcome["model"]["work_order_ref"],
                      "route_decision_ref": outcome["model"]["route_decision_ref"],
                      "result_envelope_ref": outcome["model"]["result_envelope_ref"],
                      "invocation_ref": outcome["model"]["invocation_ref"]})
    live_claims = {row["ref"] for row in frozen["material"] if row["kind"] == "claim"}
    index = citation_index(frozen["material"])
    cells = index["cells"]
    from .mission_deliverable import pack_passage_resolver

    try:
        from .product_passage_validation import checked_rendered_passages
        path = str(getattr(store,'path',':memory:'))
        passages = checked_rendered_passages(Path(path).parent if path != ':memory:'
                                            else ':memory:', sections, index['passages'])
        resolve_passage = pack_passage_resolver(None, passages)
        sections = [validate_section(section, live_claim_refs=live_claims,
                                     resolve_cell=lambda cell: cells.get(str(cell.get("ref"))) == dict(cell),
                                     resolve_passage=resolve_passage,
                                     resolve_number=index["numbers"].get, strict=True)
                    for section in sections]
    except (MissionDeliverableError, ValueError, OSError) as exc:
        return {"status": "refused", "memo_status": "authority_validation_failed",
                "reason": str(exc), "cost_micros": cost_micros}
    checks = _checks(sections, answers, frozen["material"])
    if any(check["status"] != "pass" for check in checks):
        from .investment_memo_question_gaps import CONTRACT_REF as gap_contract, build as question_gaps
        refused = {"status": "refused", "memo_status": "deterministic_gate_failed", "checks": checks,
                   "cost_micros": cost_micros}
        try:
            refused["key_question_gaps"] = question_gaps(frozen, answers, calls)
        except (ValueError, KeyError, TypeError) as exc:
            # Older valid group answers can omit an unknown's research action.
            # Summary enrichment cannot change the original refusal, purchase
            # a correction, or promote incomplete evidence to an available gap.
            refused["key_question_gaps_unavailable"] = {
                "contract_ref": gap_contract, "reason": str(exc)[:300],
                "unknown_question_refs": [str(answer.get("question_ref")) for answer in answers
                    if isinstance(answer, Mapping) and answer.get("unknown") is True][:12]}
        return refused
    summary = "Investment Memo candidate; human approval is still required."
    fields = {"kind": "investment_memo", "subject_ref": company["company_ref"],
              "mission_version_ref": mission["id"], "mission_version_hash": mission["content_hash"],
              "playbook_version_ref": playbook["id"], "playbook_version_hash": playbook["content_hash"],
              "template_ref": "deliverable_templates.investment_memo", "summary": summary, "gaps": []}
    digest = verified_material_hash(sections=sections, questions=answers,
        input_bindings=frozen["input_bindings"], record_fields=fields)
    routes = [call["route_decision_ref"] for call in calls]
    # The verifier reads every row the draft cites -- the rows it has to check
    # -- rather than the whole bounded material: the draft itself is added to
    # its prompt, and both together must stay inside one call (ADR-0011).
    cited = cited_refs(sections, answers)
    verifier_material = [row for row in frozen["material"] if row["ref"] in cited] \
        or list(frozen["material"])
    verdict = verify_memo(verifier_model, sections=sections, questions=answers,
        material=verifier_material, material_hash=digest, mission=mission,
        producer_route_decision_refs=routes, **({"quiesce": quiesce} if quiesce is not None else {}))
    cost_micros += int(verdict.get("model", {}).get("cost_micros") or verdict.get("cost_micros") or 0)
    if verdict.get("status") != "verified" or verdict.get("verdict") != "pass":
        return {"status": "refused", "memo_status": "verification_failed", "verification": verdict,
                "cost_micros": cost_micros}
    gate = {"schema_version": SCHEMA_VERSION_V2, "passed": True, "checks": checks,
            "verified_body_hash": digest, "key_questions": answers,
            "input_bindings": list(frozen["input_bindings"]), "producer_calls": calls,
            "verifier": {"verdict": "pass", "work_order_ref": verdict["model"]["work_order_ref"],
                         "route_decision_ref": verdict["model"]["route_decision_ref"],
                         "result_envelope_ref": verdict["model"]["result_envelope_ref"],
                         "invocation_ref": verdict["model"]["invocation_ref"],
                         "producer_route_decision_refs": routes, "finding_codes": []}}
    validate_memo_gate(gate, material_hash=digest, expected_questions=questions)
    try:
        published = MissionDeliverableAuthority(store).publish(kind="investment_memo",
            subject_ref=company["company_ref"], mission=mission, playbook=playbook,
            template_ref=fields["template_ref"], sections=sections, summary=summary, gaps=[],
            model_invocation_refs=model_work_order_refs(gate), actor_ref=mission["autonomy"]["automation_principal"],
            idempotency_key="investment-memo:" + content_hash({"company": company["company_ref"], "digest": digest}), gate=gate,
            pack_numbers=index["numbers"], pack_passages=index["passages"],
            contract={"version": MEMO_CONTRACT_VERSION,
                      "material_pack": dict(frozen.get("material_pack") or {"status": "unknown"})},
            # Like the initial screen: the pack's provenance block (watermark,
            # inputs, sufficiency) is what redraft_policy reads later.
            material_pack=frozen.get("material_pack_record"))
    except MissionDeliverableError as exc:
        return {"status": "refused", "memo_status": "authority_validation_failed",
                "reason": str(exc), "cost_micros": cost_micros}
    from .research_product_quality import product_quality
    quality = product_quality("investment_memo", published, core=store.connection, playbook=playbook)
    return {"status": "succeeded", "memo_status": published["status"], "quality": quality,
            "company_ref": company["company_ref"], "version_ref": published["id"],
            "version_hash": published["content_hash"], "cost_micros": cost_micros, "gate": gate}


def run(*, state_dir: Path, model_config_path: Path | None, verifier_model_config_path: Path | None,
        scheduler_db: Path | None = None, company_ref: str | None = None,
        model_factory: Callable[[], Any] | None = None,
        verifier_model_factory: Callable[[], Any] | None = None,
        summary_dir: Path | None = None) -> dict[str, Any]:
    store = DaltonStore(str(state_dir / "core.sqlite"))
    frozen = collect_frozen_input(store, company_ref=company_ref)
    if frozen.get("status") != "ready":
        return {"schema_version": SUMMARY_SCHEMA_VERSION, **frozen}
    if model_config_path is None or verifier_model_config_path is None:
        return {"schema_version": SUMMARY_SCHEMA_VERSION, "status": "held", "memo_status": "no_model_pair",
                "reason": "memo producer and independent verifier configurations are both required"}
    producer_bytes, verifier_bytes = model_config_path.read_bytes(), verifier_model_config_path.read_bytes()
    config = freeze_shared_budget_policy(json.loads(producer_bytes))
    verifier_config = freeze_shared_budget_policy(json.loads(verifier_bytes))
    producer_budget = resolve_call_budget(config, "investment_memo", defaults={
        "max_input_tokens": MAX_INPUT_TOKENS, "max_output_tokens": MAX_OUTPUT_TOKENS,
        "max_cost_usd": MAX_COST_USD, "timeout_seconds": TIMEOUT_SECONDS})
    verifier_budget = resolve_call_budget(verifier_config, "investment_memo_verifier", defaults=producer_budget)
    run_budget = resolve_run_budget(config, "investment_memo",
        defaults={"max_cost_usd": MAX_RUN_COST_USD, "max_units": 4},
        call_budgets={"investment_memo": producer_budget, "investment_memo_verifier": verifier_budget})
    if float(run_budget["max_cost_usd"]) < 4 * float(producer_budget["max_cost_usd"]) + float(verifier_budget["max_cost_usd"]):
        return {"schema_version": SUMMARY_SCHEMA_VERSION, "status": "held", "memo_status": "run_budget_too_small",
                "reason": "run budget cannot reserve four producer calls and one verifier call"}
    def build(settings: Mapping[str, Any]) -> CockpitModel:
        return CockpitModel(settings, scheduler_db=str(scheduler_db or state_dir / "scheduler.sqlite"),
                            max_input_tokens=MAX_INPUT_TOKENS, max_output_tokens=MAX_OUTPUT_TOKENS,
                            max_cost_usd=MAX_COST_USD, timeout_seconds=TIMEOUT_SECONDS)
    # One repair may use savings from calls whose exact fees have settled.
    # All remaining groups and the verifier retain their full reservations.
    repairs = MAX_REPAIRS
    from .investment_memo_repair_budget import MemoRepairBudget
    from .investment_memo_failure_cost import MemoFailureCostReporter, with_failure_costs
    failure_cost_watermarks = {}
    producer_report = MemoFailureCostReporter((model_factory or (lambda: build(config)))(), watermarks=failure_cost_watermarks)
    verifier_report = MemoFailureCostReporter((verifier_model_factory or (lambda: build(verifier_config)))(), watermarks=failure_cost_watermarks)
    repair_budget = MemoRepairBudget(
        producer_report,
        run_micros=int(float(run_budget["max_cost_usd"]) * 1_000_000),
        producer_micros=int(float(producer_budget["max_cost_usd"]) * 1_000_000),
        verifier_micros=int(float(verifier_budget["max_cost_usd"]) * 1_000_000))
    from .investment_memo_quiesce import RunQuiesce, MemoQuiesced, MemoQuiesceUnproved
    quiesce = (RunQuiesce(summary_dir, model_config_path, verifier_model_config_path,
                         producer_bytes=producer_bytes, verifier_bytes=verifier_bytes)
               if summary_dir is not None else None)
    try:
        result = run_memo(store=store, frozen=frozen, model=repair_budget,
                          verifier_model=verifier_report,
                          max_units=int(run_budget["max_units"]), max_repairs=repairs, quiesce=quiesce,
                          repair_budget=repair_budget)
    except MemoQuiesced as stopped:
        result = {"status": "quiesced", "memo_status": "quiesced", "resumable": True,
                  "stop_reason": "cooperative_quiesce", "quiesce": stopped.request,
                  "completed_calls": stopped.completed, "frozen_input_hash": content_hash(frozen),
                  "cost_micros": sum(int(call.get("cost_micros") or 0)
                                     for _, call, _, _ in quiesce.calls)}
    except MemoQuiesceUnproved as exc:
        result = {"status": "held", "memo_status": "quiesce_boundary_unproved", "resumable": False,
                  "reason": str(exc), "cost_status": "unproved",
                  "completed_work_refs": [call.get("work_order_ref") for _, call, _, _ in quiesce.calls]}
    result = with_failure_costs(result, producer_report, verifier_report)
    if result.get('memo_status')=='macro_accounting_pending':
        from types import SimpleNamespace
        from .mission_investment_memo_lane import company_signature
        result['memo_input_signature']=company_signature(frozen,SimpleNamespace(
            model_config_path=model_config_path,verifier_model_config_path=verifier_model_config_path))
    return {"schema_version": SUMMARY_SCHEMA_VERSION, **result,
            "budget": {"run": run_budget, "producer": producer_budget,
                       "verifier": verifier_budget, "max_repairs": repairs,
                       "repair_accounted_micros": repair_budget.accounted_micros,
                       **({} if repair_budget.macro is None and repair_budget.macro_error is None else {
                           'macro_accounting': (repair_budget.macro.last if repair_budget.macro else None),
                           'macro_fee_audit_reason': repair_budget.macro_error or
                            ('legacy_fee_audit_required' if repair_budget.macro and not repair_budget.macro.complete else None)})}}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--model-config", type=Path)
    parser.add_argument("--verifier-model-config", type=Path)
    parser.add_argument("--scheduler-db", type=Path)
    parser.add_argument("--company-ref")
    parser.add_argument("--summary-dir", type=Path)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    result = run(state_dir=args.state_dir, model_config_path=args.model_config,
                 verifier_model_config_path=args.verifier_model_config,
                 scheduler_db=args.scheduler_db, company_ref=args.company_ref, summary_dir=args.summary_dir)
    text = json.dumps(result, ensure_ascii=False, sort_keys=True)
    if args.summary_dir:
        # A deployment may close the writer as soon as it sees a paused child.
        # Publish its receipt atomically and durably, not as a partial file.
        import os
        import tempfile
        args.summary_dir.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".memo-summary-", dir=args.summary_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(text + "\n"); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, args.summary_dir / "summary.json")
            fd = os.open(args.summary_dir, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            Path(temporary).unlink(missing_ok=True)
    if not args.quiet:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
