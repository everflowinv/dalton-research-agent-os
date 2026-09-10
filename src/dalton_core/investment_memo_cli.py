"""Draft and publish one fully verified Investment Memo candidate."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .call_budget import resolve_call_budget, resolve_run_budget
from .cockpit_model import CockpitModel
from .coverage_mission import CoverageMissionAuthority
from .investment_memo_contract import CHECK_REFS, SCHEMA_VERSION, model_work_order_refs, validate_memo_gate
from .investment_memo_draft import (
    GROUPS, MAX_COST_USD, MAX_INPUT_TOKENS, MAX_OUTPUT_TOKENS, MAX_RUN_COST_USD,
    TIMEOUT_SECONDS, draft_group, verified_material_hash, verify_memo,
)
from .mission_deliverable import MissionDeliverableAuthority, MissionDeliverableError, validate_section
from .research_playbook import ResearchPlaybookAuthority
from .store import DaltonStore, content_hash

SUMMARY_SCHEMA_VERSION = "0.1"


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


def collect_frozen_input(store: DaltonStore, *, company_ref: str | None = None) -> dict[str, Any]:
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
    candidates = [member for member in mission["universe"] if company_ref in (None, member["company_ref"])]
    chosen = next((member for member in candidates
                   if missions.current_stage_state(mission["mission_ref"], member["company_ref"])["next_stage"] == "investment_memo"), None)
    if chosen is None:
        return {"status": "idle", "reason": "no company has passed the folded industry and company model gates"}
    company_ref = chosen["company_ref"]

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
                     "text": claim.get("normalized_statement") or json.dumps(claim, ensure_ascii=False)})
    for table in (
        "company_dossier_versions", "debate_map_versions",
        "forecast_model_versions", "sensitivity_projections", "valuation_snapshot_versions",
        "market_price_series_versions", "catalyst_calendar_versions", "consensus_estimate_versions",
        "prior_model_versions", "event_judgements", "thesis_reflections", "research_events",
    ):
        rows.extend(_latest_rows(store.connection, table, company_ref,
                                subject_column="subject_ref" if table == "debate_map_versions" else "company_ref",
                                all_rows=table == "research_events"))
    for model_row in [row for row in rows if row["kind"] == "forecast_model_versions"]:
        model_record = json.loads(model_row["text"])
        for result in model_record.get("results") or ():
            for cell in result.get("cells") or ():
                if not cell.get("ref"):
                    continue
                rows.append({"ref": cell["ref"], "hash": content_hash(cell),
                             "kind": "forecast_cell",
                             "text": json.dumps(cell, ensure_ascii=False, sort_keys=True)})
    try:
        approved = store.connection.execute(
            "SELECT v.record_json,v.content_hash FROM deep_insight_gate_versions v "
            "JOIN deep_insight_gate_decisions d ON d.gate_version_ref=v.version_id "
            "WHERE v.company_ref=? AND d.decision='approve' ORDER BY v.version_number DESC LIMIT 1",
            (company_ref,),
        ).fetchone()
    except Exception:
        approved = None
    if approved is None:
        return {"status": "held", "reason": "an owner-approved Deep Insight Gate is required"}
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
    kinds = {row["kind"] for row in rows}
    required = {"company_dossier_versions", "deep_insight_gate_versions", "forecast_model_versions",
                "sensitivity_projections", "valuation_snapshot_versions", "industry_framework"}
    missing = sorted(required - kinds)
    if missing:
        return {"status": "held", "reason": "required memo inputs are missing: " + ", ".join(missing)}
    bindings = [{"ref": row["ref"], "hash": row["hash"], "kind": row["kind"]} for row in rows]
    current = MissionDeliverableAuthority(store).latest(
        "mission-deliverable:investment_memo:" + company_ref.rsplit(":", 1)[-1])
    if current is not None and current.get("mission_version_ref") == mission["id"] \
            and (current.get("gate") or {}).get("input_bindings") == bindings:
        return {"status": "idle", "memo_status": "nothing_new", "company_ref": company_ref,
                "reason": "the frozen memo input is unchanged"}
    return {"status": "ready", "mission": mission, "playbook": playbook, "company": chosen,
            "material": rows, "input_bindings": bindings}


def _checks(sections: Sequence[Mapping[str, Any]], questions: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    refs = sorted({str(ref) for q in questions for ref in q["refs"]})
    by_title = {str(section["title"]): section for section in sections}
    values = (
        (len(questions) == 12 and all(not q["unknown"] and q["answer"] and q["refs"] and q["falsifier"] for q in questions), "12 Playbook questions are answered, cited and falsifiable"),
        (bool(questions[7]["answer"] and questions[6]["answer"]), "variant view is stated against consensus"),
        (any("Anti-thesis" in title and section["body"] for title, section in by_title.items()), "Anti-thesis is a written counter-case"),
        (any(title.startswith("S5 ") and section["body"] for title, section in by_title.items()) and bool(questions[11]["answer"]), "risk/reward and loss case are written"),
    )
    return [{"check_ref": ref, "status": "pass" if passed else "fail", "reason": reason,
             "evidence_refs": refs} for ref, (passed, reason) in zip(CHECK_REFS, values)]


def run_memo(*, store: DaltonStore, frozen: Mapping[str, Any], model: Any, verifier_model: Any,
             max_units: int = 4) -> dict[str, Any]:
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
    sections, answers, calls = [], [], []
    for group, section_indexes, question_indexes in GROUPS:
        outcome = draft_group(model, group=group,
            section_titles=[titles[i] for i in section_indexes],
            questions=[questions[i] for i in question_indexes], material=frozen["material"],
            company=company, mission=mission)
        if outcome["status"] != "drafted":
            return {"status": "refused", "memo_status": "draft_refused", "reason": outcome["reason"]}
        sections.extend(outcome["sections"]); answers.extend(outcome["key_questions"])
        calls.append({"group": group, "work_order_ref": outcome["model"]["work_order_ref"],
                      "route_decision_ref": outcome["model"]["route_decision_ref"],
                      "result_envelope_ref": outcome["model"]["result_envelope_ref"],
                      "invocation_ref": outcome["model"]["invocation_ref"]})
    live_claims = {row["ref"] for row in frozen["material"] if row["kind"] == "claim"}
    cell_refs = {row["ref"] for row in frozen["material"] if row["kind"] in (
        "forecast_cell", "statement_accession")}
    try:
        sections = [validate_section(section, live_claim_refs=live_claims,
                                     resolve_cell=lambda cell: cell.get("ref") in cell_refs)
                    for section in sections]
    except MissionDeliverableError as exc:
        return {"status": "refused", "memo_status": "authority_validation_failed",
                "reason": str(exc)}
    checks = _checks(sections, answers)
    if any(check["status"] != "pass" for check in checks):
        return {"status": "refused", "memo_status": "deterministic_gate_failed", "checks": checks}
    summary = "Investment Memo candidate; human approval is still required."
    fields = {"kind": "investment_memo", "subject_ref": company["company_ref"],
              "mission_version_ref": mission["id"], "mission_version_hash": mission["content_hash"],
              "playbook_version_ref": playbook["id"], "playbook_version_hash": playbook["content_hash"],
              "template_ref": "deliverable_templates.investment_memo", "summary": summary, "gaps": []}
    digest = verified_material_hash(sections=sections, questions=answers,
        input_bindings=frozen["input_bindings"], record_fields=fields)
    routes = [call["route_decision_ref"] for call in calls]
    verdict = verify_memo(verifier_model, sections=sections, questions=answers,
        material=frozen["material"], material_hash=digest, mission=mission,
        producer_route_decision_refs=routes)
    if verdict.get("status") != "verified" or verdict.get("verdict") != "pass":
        return {"status": "refused", "memo_status": "verification_failed", "verification": verdict}
    gate = {"schema_version": SCHEMA_VERSION, "passed": True, "checks": checks,
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
            idempotency_key="investment-memo:" + content_hash({"company": company["company_ref"], "digest": digest}), gate=gate)
    except MissionDeliverableError as exc:
        return {"status": "refused", "memo_status": "authority_validation_failed",
                "reason": str(exc)}
    return {"status": "succeeded", "memo_status": published["status"],
            "company_ref": company["company_ref"], "version_ref": published["id"],
            "version_hash": published["content_hash"], "cost_micros": sum(
                int(c.get("cost_micros") or 0) for c in []), "gate": gate}


def run(*, state_dir: Path, model_config_path: Path | None, verifier_model_config_path: Path | None,
        scheduler_db: Path | None = None, company_ref: str | None = None,
        model_factory: Callable[[], Any] | None = None,
        verifier_model_factory: Callable[[], Any] | None = None) -> dict[str, Any]:
    store = DaltonStore(str(state_dir / "core.sqlite"))
    frozen = collect_frozen_input(store, company_ref=company_ref)
    if frozen.get("status") != "ready":
        return {"schema_version": SUMMARY_SCHEMA_VERSION, **frozen}
    if model_config_path is None or verifier_model_config_path is None:
        return {"schema_version": SUMMARY_SCHEMA_VERSION, "status": "held", "memo_status": "no_model_pair",
                "reason": "memo producer and independent verifier configurations are both required"}
    config = json.loads(model_config_path.read_text(encoding="utf-8"))
    verifier_config = json.loads(verifier_model_config_path.read_text(encoding="utf-8"))
    run_budget = resolve_run_budget(config, "investment_memo",
        defaults={"max_cost_usd": MAX_RUN_COST_USD, "max_units": 4})
    producer_budget = resolve_call_budget(config, "investment_memo", defaults={
        "max_input_tokens": MAX_INPUT_TOKENS, "max_output_tokens": MAX_OUTPUT_TOKENS,
        "max_cost_usd": MAX_COST_USD, "timeout_seconds": TIMEOUT_SECONDS})
    verifier_budget = resolve_call_budget(verifier_config, "investment_memo_verifier", defaults=producer_budget)
    if float(run_budget["max_cost_usd"]) < 4 * float(producer_budget["max_cost_usd"]) + float(verifier_budget["max_cost_usd"]):
        return {"schema_version": SUMMARY_SCHEMA_VERSION, "status": "held", "memo_status": "run_budget_too_small",
                "reason": "run budget cannot reserve four producer calls and one verifier call"}
    def build(settings: Mapping[str, Any]) -> CockpitModel:
        return CockpitModel(settings, scheduler_db=str(scheduler_db or state_dir / "scheduler.sqlite"),
                            max_input_tokens=MAX_INPUT_TOKENS, max_output_tokens=MAX_OUTPUT_TOKENS,
                            max_cost_usd=MAX_COST_USD, timeout_seconds=TIMEOUT_SECONDS)
    result = run_memo(store=store, frozen=frozen, model=(model_factory or (lambda: build(config)))(),
                      verifier_model=(verifier_model_factory or (lambda: build(verifier_config)))(),
                      max_units=int(run_budget["max_units"]))
    return {"schema_version": SUMMARY_SCHEMA_VERSION, **result}


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
                 scheduler_db=args.scheduler_db, company_ref=args.company_ref)
    text = json.dumps(result, ensure_ascii=False, sort_keys=True)
    if args.summary_dir:
        args.summary_dir.mkdir(parents=True, exist_ok=True)
        (args.summary_dir / "summary.json").write_text(text + "\n", encoding="utf-8")
    if not args.quiet:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
