"""P10c child: draft one company's Initial Screen and decide its gate.

Runs out of process, like every other lane that calls a model, so a section
that takes a minute cannot stall the writer or the controller tick.  One run
handles at most one company:

1. pick the highest-priority company whose Initial Screen is entered, not yet
   passed, and either undrafted or older than its newest live Claim;
2. draft each templated section through the mission's own budgeted model
   route (the valuation section is never drafted: no market-data authority);
3. publish the document through the deliverable authority, which refuses any
   figure that no quantitative Claim accounts for;
4. assess the Playbook's four gate questions by structural checks and record
   ``gate_passed`` or ``gate_failed`` in the mission's stage ledger.

Every step is idempotent: a re-run with nothing new to say publishes nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .claim_retirement import ClaimRetirementAuthority
from .cockpit_model import CockpitModel, CockpitModelError
from .coverage_mission import CoverageMissionAuthority, CoverageMissionError
from .initial_screen import (
    KIND,
    SECTION_GUIDANCE,
    TEMPLATE_KEY,
    VALUATION_GAP,
    VALUATION_TITLE_HINT,
    assess_exit_gate,
    build_claim_context,
    build_section_prompt,
    parse_section_output,
    section_titles,
)
from .mission_deliverable import (
    GAP_MARKER,
    MissionDeliverableAuthority,
    MissionDeliverableError,
    WRITE_SCOPE,
    unsourced_numbers,
)
from .mission_stage import evaluate_mission, planned_spec_refs_from_directory
from .store import DaltonStore

SUMMARY_SCHEMA_VERSION = "0.1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _write_owner_only(path: Path, value: Any) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=1) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _live_claims(store: DaltonStore, retired: set[str]) -> dict[str, list[dict[str, Any]]]:
    """subject_ref → its live Claims, oldest first."""

    by_subject: dict[str, list[dict[str, Any]]] = {}
    for row in store.connection.execute(
        "SELECT claim_version_id, claim_json, created_at FROM claim_versions ORDER BY created_at"
    ).fetchall():
        if row["claim_version_id"] in retired:
            continue
        claim = json.loads(row["claim_json"])
        by_subject.setdefault(claim.get("subject_ref") or "", []).append({
            "ref": row["claim_version_id"], "statement": claim.get("normalized_statement") or "",
            "period": claim.get("period"), "aspect": claim.get("metric_or_aspect"),
            "value": claim.get("value"), "created_at": row["created_at"],
        })
    return by_subject


def _playbook(store: DaltonStore, mission: dict[str, Any]) -> dict[str, Any]:
    binding = mission["bindings"]["playbook_version"]
    row = store.connection.execute(
        "SELECT record_json, content_hash FROM research_playbook_versions WHERE playbook_version_id=?",
        (binding["ref"],),
    ).fetchone()
    if row is None:
        raise MissionDeliverableError("the mission's playbook version is missing")
    record = json.loads(row["record_json"])
    if record["content_hash"] != binding["hash"]:
        raise MissionDeliverableError("the mission's playbook binding drifted")
    return record


def _target(
    *, mission: dict[str, Any], stage_rows: list[dict[str, Any]],
    deliverables: dict[str, dict[str, Any]], claims: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """The next company to draft, in mission priority order, and why the rest wait."""

    skipped: list[dict[str, Any]] = []
    for entry in stage_rows:
        company_ref = entry["company_ref"]
        if entry["stage"] != "initial_screen":
            skipped.append({"company_ref": company_ref, "reason": f"stage is {entry['stage']}"})
            continue
        if entry["stage_status"] == "gate_passed":
            skipped.append({"company_ref": company_ref, "reason": "initial screen already passed"})
            continue
        own = claims.get(company_ref) or []
        if not own:
            skipped.append({"company_ref": company_ref, "reason": "no live Claim to write from"})
            continue
        published = deliverables.get(company_ref)
        if published is not None and published["created_at"] >= own[-1]["created_at"]:
            skipped.append({"company_ref": company_ref, "reason": "nothing new since the last version"})
            continue
        return entry, skipped
    return None, skipped


def run(args: argparse.Namespace) -> dict[str, Any]:
    state = Path(args.state_dir).expanduser().resolve()
    summary: dict[str, Any] = {
        "schema_version": SUMMARY_SCHEMA_VERSION, "created_at": _now(), "status": "succeeded",
        "drafted": None, "sections": [], "skipped": [], "gate": None,
        "formal_authority_writes": 0, "failure_reason": None,
    }
    store = DaltonStore(str(state / "core.sqlite"))
    try:
        missions = CoverageMissionAuthority(store)
        deliverable_authority = MissionDeliverableAuthority(store)
        retirements = ClaimRetirementAuthority(store)
        pointer = store.connection.execute(
            "SELECT mission_version_id FROM coverage_mission_pointer ORDER BY mission_ref LIMIT 1"
        ).fetchone()
        if pointer is None:
            summary["status"] = "idle"
            summary["failure_reason"] = "no active mission"
            return summary
        mission = missions.mission(pointer["mission_version_id"])
        # Check the grant before spending: drafting eight sections and then
        # being refused at publish costs the mission real model calls.
        if WRITE_SCOPE not in mission["autonomy"]["may_write"]:
            summary["status"] = "held"
            summary["failure_reason"] = (
                f"任务 {mission['id']} 还没有授予 {WRITE_SCOPE} 写入范围；"
                "在授权前不起草，免得白花模型调用"
            )
            return summary
        playbook = _playbook(store, mission)
        stage_state: dict[str, dict[str, list[str]]] = {}
        for record in missions.stage_records(mission["id"]):
            stage_state.setdefault(record["company_ref"], {}).setdefault(record["stage_ref"], []).append(
                record["status"]
            )
        stage_rows = evaluate_mission(
            store.connection, mission,
            planned_specs=planned_spec_refs_from_directory(state / "discovery-plans"),
            stage_state=stage_state,
        )
        claims = _live_claims(store, retirements.retired_claim_version_refs())
        published = {
            record["subject_ref"]: record
            for record in deliverable_authority.deliverables(mission["id"], kind=KIND)
        }
        entry, skipped = _target(
            mission=mission, stage_rows=stage_rows, deliverables=published, claims=claims,
        )
        summary["skipped"] = skipped
        if entry is None:
            summary["status"] = "idle"
            summary["failure_reason"] = "nothing to draft"
            return summary
        company_ref = entry["company_ref"]
        summary["drafted"] = {"company_ref": company_ref, "ticker": entry["ticker"]}
        titles = section_titles(playbook)
        context = build_claim_context(claims.get(company_ref) or [])
        summary["context"] = {"claims": len(context["claims"]), "numbers": len(context["numbers"])}
        model = None
        if not args.dry_run:
            model = CockpitModel(
                json.loads(Path(args.model_config).expanduser().read_text(encoding="utf-8")),
                scheduler_db=args.scheduler_db or str(state / "scheduler.sqlite"),
                max_output_tokens=1800, timeout_seconds=args.timeout_seconds,
            )
        sections: list[dict[str, Any]] = []
        invocations: list[str] = []
        for index, title in enumerate(titles):
            guidance = SECTION_GUIDANCE[index] if index < len(SECTION_GUIDANCE) else ""
            if VALUATION_TITLE_HINT in title and not guidance:
                sections.append({"title": title, "body": "", "claim_refs": [], "numbers": [],
                                 "gaps": [VALUATION_GAP]})
                summary["sections"].append({"title": title, "status": "not_drafted", "reason": "valuation gate"})
                continue
            prompt = build_section_prompt(
                title=title, guidance=guidance,
                company={"ticker": entry["ticker"], "company_ref": company_ref},
                mission=mission, context=context, checklist=entry["items"],
            )
            if model is None:
                sections.append({"title": title, "body": "", "claim_refs": [], "numbers": [],
                                 "gaps": ["dry run: 没有调用模型"]})
                summary["sections"].append({"title": title, "status": "dry_run"})
                continue
            try:
                call = model.call(
                    purpose="draft",
                    request_id=f"{mission['id']}:{company_ref}:{KIND}:{index}:{len(claims.get(company_ref) or [])}",
                    prompt=prompt, mission=mission,
                )
            except CockpitModelError as exc:
                sections.append({"title": title, "body": "", "claim_refs": [], "numbers": [],
                                 "gaps": [f"这一节没能起草：{exc}"]})
                summary["sections"].append({"title": title, "status": "failed", "reason": str(exc)})
                continue
            section = parse_section_output(call["text"], context=context, title=title)
            if call.get("invocation_ref"):
                invocations.append(call["invocation_ref"])
            # The authority refuses the whole document for one unsourced figure.
            # Give the section one corrective attempt with the figures named,
            # then drop its body to a gap so the rest can still be published.
            stray = unsourced_numbers(section["body"], section["numbers"])
            retried = False
            if stray:
                retried = True
                try:
                    correction = model.call(
                        purpose="draft",
                        request_id=f"{mission['id']}:{company_ref}:{KIND}:{index}:"
                                   f"{len(claims.get(company_ref) or [])}:retry",
                        prompt=prompt + (
                            "\n\nYour previous draft wrote figures no N tag carries: "
                            + "、".join(stray[:8])
                            + f"。Rewrite the section without them: copy a figure verbatim from an N tag "
                              f"or write {GAP_MARKER}. Do not convert units or scales."
                        ),
                        mission=mission,
                    )
                    candidate = parse_section_output(correction["text"], context=context, title=title)
                    if correction.get("invocation_ref"):
                        invocations.append(correction["invocation_ref"])
                    if candidate["body"] and not unsourced_numbers(candidate["body"], candidate["numbers"]):
                        section = candidate
                        stray = []
                    else:
                        stray = unsourced_numbers(candidate["body"], candidate["numbers"]) or stray
                except CockpitModelError as exc:
                    summary["sections"].append({"title": title, "status": "retry_failed", "reason": str(exc)})
            if stray:
                section = {
                    "title": title, "body": "", "claim_refs": [], "numbers": [],
                    "gaps": [f"这一节写了没有来源的数字（{'、'.join(stray[:5])}），已丢弃；"
                             f"需要的数字还没有进入账本"],
                }
            sections.append(section)
            summary["sections"].append({
                "title": title, "status": "drafted" if section["body"] else "dropped_unsourced",
                "retried": retried,
                "chars": len(section["body"]), "claims": len(section["claim_refs"]),
                "numbers": len(section["numbers"]), "replayed": call["replayed"],
                "cost_usd": round(call["cost_micros"] / 1_000_000, 6),
            })
        written = [item for item in sections if item["body"]]
        if not written:
            summary["status"] = "failed"
            summary["failure_reason"] = "没有任何一节写出来，不发布空壳"
            return summary
        summary_text = written[0]["body"][:1200]
        try:
            record = deliverable_authority.publish(
                kind=KIND, subject_ref=company_ref, mission=mission, playbook=playbook,
                template_ref=f"playbook:deliverable_templates.{TEMPLATE_KEY}",
                sections=sections, summary=summary_text,
                gaps=[gap for section in sections for gap in section["gaps"]],
                model_invocation_refs=invocations,
                actor_ref=mission["autonomy"]["automation_principal"],
            )
        except MissionDeliverableError as exc:
            summary["status"] = "failed"
            summary["failure_reason"] = f"{type(exc).__name__}: {exc}"
            return summary
        summary["deliverable"] = {
            "ref": record["deliverable_ref"], "version": record["version"],
            "version_ref": record["id"], "status": record["status"],
        }
        summary["formal_authority_writes"] = 1 if record["status"] == "fresh" else 0
        gate = assess_exit_gate(playbook=playbook, checklist_entry=entry, sections=record["sections"])
        summary["gate"] = gate
        try:
            stage = missions.record_stage(
                mission_version_ref=mission["id"], mission_version_hash=mission["content_hash"],
                company_ref=company_ref, stage_ref="initial_screen",
                status="gate_passed" if gate["passed"] else "gate_failed",
                evidence_refs=[record["id"], mission["id"]],
                rationale=f"P10c 出口门自评：{gate['rationale']}"[:2000],
                actor_ref=mission["autonomy"]["automation_principal"],
                idempotency_key=f"{mission['id']}:{company_ref}:initial_screen:{record['id']}",
            )
            summary["gate"]["stage_record"] = stage.get("status_marker", "recorded")
        except CoverageMissionError as exc:
            summary["gate"]["stage_record"] = f"not_recorded:{type(exc).__name__}: {exc}"
        return summary
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--model-config")
    parser.add_argument("--summary-dir", required=True)
    parser.add_argument("--scheduler-db")
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--dry-run", action="store_true", help="pick a target and build prompts, call nothing")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    if not args.dry_run and not args.model_config:
        parser.error("--model-config is required unless --dry-run")
    summary_dir = Path(args.summary_dir).expanduser().resolve()
    summary_dir.mkdir(parents=True, exist_ok=True)
    try:
        summary = run(args)
    except Exception as exc:  # noqa: BLE001 - the ticket must always carry a reason
        summary = {
            "schema_version": SUMMARY_SCHEMA_VERSION, "created_at": _now(), "status": "failed",
            "failure_reason": f"unexpected {type(exc).__name__}: {exc}",
            "drafted": None, "sections": [], "skipped": [], "gate": None,
            "formal_authority_writes": 0,
        }
    _write_owner_only(summary_dir / "summary.json", summary)
    if not args.quiet:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=1))
    # "held" is a governed refusal, not a failure: the ticket should read as a
    # clean run that changed nothing.
    return 0 if summary["status"] in {"succeeded", "idle", "held"} else 1


if __name__ == "__main__":
    sys.exit(main())
