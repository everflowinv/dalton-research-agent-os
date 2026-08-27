#!/usr/bin/env python3
"""Run the Tier 1 bounded planner loop end to end on an isolated live copy.

Copies the live Core and Scheduler read-only via SQLite backup, admits the
standing demand question, the SEC revenue-growth ProbeTemplate and a
five-issuer loop inside the copy, then drives the deterministic planner
through proposal -> Core admission -> Scheduler WorkOrder -> stubbed
source-level result -> ResearchOutcome until the loop reaches its closed
terminal state.  The stub results prove the loop machinery on live authority
data; real probe execution wiring lands with the P8c controller integration.
No writes to live, no network, no paid model calls.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
from pathlib import Path

from dalton_core.bounded_planner_loop import (
    BoundedPlannerAuthority,
    BoundedPlannerControlPlane,
)
from dalton_core.observability import ObservabilityStore
from dalton_core.research_question_backlog import ResearchQuestionBacklog
from dalton_core.scheduler import Scheduler
from dalton_core.store import DaltonStore, content_hash


MANDATE_REF = "mandate-version:us-it-services-constitution-p8a:1"
INDUSTRY = "industry:us-it-services"
QUESTION = "Has US IT services demand bottomed?"
ANSWER_CRITERIA = (
    "Same-filing quarterly revenue comparisons across the five lane issuers."
)
TEMPLATE_REF = "probe-template:sec-company-facts-revenue-growth:v1"
LOOP_REF = "bounded-loop:us-it-services-demand:v1"
ISSUERS = (
    ("acn", "0001467373", "0001467373-26-000031"),
    ("ctsh", "0001058290", "0001058290-26-000031"),
    ("epam", "0001352010", "0001352010-26-000031"),
    ("ibm", "0000051143", "0000051143-26-000078"),
    ("dxc", "001688568", "0001688568-26-000069"),
)
WORKER = "worker:p8c-canary"


def run(source_core: Path, source_scheduler: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="p8c-planner-canary-") as directory:
        core_copy = Path(directory) / "core.sqlite"
        scheduler_copy = Path(directory) / "scheduler.sqlite"
        for source, destination in (
            (source_core, core_copy), (source_scheduler, scheduler_copy),
        ):
            reader = sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)
            writer = sqlite3.connect(destination)
            reader.backup(writer)
            writer.close()
            reader.close()

        store = DaltonStore(str(core_copy))
        scheduler = Scheduler(str(scheduler_copy))
        try:
            backlog = ResearchQuestionBacklog(store)
            planner = BoundedPlannerAuthority(store)
            observability = ObservabilityStore(store)
            control = BoundedPlannerControlPlane(planner, observability, scheduler)

            question = backlog.record_question(
                mandate_version_ref=MANDATE_REF, company_ref=INDUSTRY,
                question=QUESTION, answer_criteria=ANSWER_CRITERIA,
                source_refs=["source:sec-edgar"], actor_ref="human:isolated-canary-owner",
                idempotency_key="p8c-canary:question:1",
            )
            template = planner.publish_probe_template(
                TEMPLATE_REF,
                capability_ref="capability:sec-read-only",
                operation="get_company_facts",
                runtime_profile_ref="runtime:sec-read-only:0.1",
                parameter_contract={
                    "allowed_fields": ["source_ref", "locator", "query_terms"],
                    "required_fields": ["source_ref", "locator", "query_terms"],
                    "constants": {"source_ref": "source:sec-edgar"},
                },
                output_contract_ref="schema:bounded-planner-probe-output:0.1",
                verifier_ref="verifier:source-level-coverage:0.1",
                permission_scope="public_sec_read",
                declared_side_effects=["read:public-http"],
                cost={"cost_units": 1, "max_attempts": 2, "max_seconds": 120},
                actor_ref="human:isolated-canary-owner",
            )
            bindings = []
            coverage_items = []
            for ticker, cik, accession in ISSUERS:
                item = f"coverage:revenue-growth:{ticker}"
                coverage_items.append(item)
                bindings.append({
                    "coverage_item_ref": item,
                    "template_version_ref": template["id"],
                    "parameters": {
                        "source_ref": "source:sec-edgar",
                        "locator": f"company-facts/CIK{cik}",
                        "query_terms": ["Revenues", "10-Q", ticker.upper()],
                    },
                })
            loop = planner.create_loop(
                LOOP_REF,
                question_version_ref=question["question_version_ref"],
                template_bindings=bindings,
                required_coverage_items=coverage_items,
                budget={"max_rounds": 6, "max_cost_units": 6, "max_seconds": 900},
                actor_ref="human:isolated-canary-owner",
            )

            rounds = []
            outcomes = []
            for ordinal in range(1, len(ISSUERS) + 1):
                proposal = planner.propose_next_capital_lease(loop["id"])
                if proposal["status"] != "fresh":
                    raise RuntimeError(
                        f"round {ordinal} proposal status: {proposal['status']}"
                    )
                if proposal["action"]["kind"] != "probe":
                    raise RuntimeError("planner proposed a non-probe action")
                admitted = control.admit_proposal(proposal["id"])
                if admitted["status"] != "fresh":
                    raise RuntimeError(
                        f"round {ordinal} admission status: {admitted['status']}"
                    )
                round_wire = admitted["round"]
                _, _, accession = ISSUERS[ordinal - 1]
                lease = scheduler.claim(
                    WORKER, work_order_id=round_wire["work_order_ref"]
                )
                result_id = (
                    f"result:p8c-canary:{ordinal}:"
                    f"{content_hash(accession)[:8]}"
                )
                scheduler.complete(
                    round_wire["work_order_ref"], 1, WORKER, lease["lease_token"],
                    {
                        "schema_version": "0.1", "id": result_id,
                        "created_at": f"2026-08-27T16:00:{ordinal:02d}.000000+00:00",
                        "work_order_ref": round_wire["work_order_ref"],
                        "invocation_ref": f"invocation:p8c-canary:{ordinal}",
                        "status": "succeeded",
                        "outputs": {"matches": [
                            {"source_location": f"sec:accession:{accession}"}
                        ]},
                        "actual_side_effects": [], "usage_refs": [],
                        "artifact_refs": [], "error": None,
                        "metadata": {"p8c_canary": True},
                    },
                    idempotency_key=f"complete:{result_id}",
                )
                outcome = control.record_outcome(round_wire["id"])
                if outcome["outcome"]["outcome_kind"] != "observed":
                    raise RuntimeError(
                        f"round {ordinal} outcome was not observed"
                    )
                rounds.append(round_wire["id"])
                outcomes.append(outcome["outcome"]["id"])

            terminal_proposal = planner.propose_next_capital_lease(loop["id"])
            if (
                terminal_proposal.get("status") != "fresh"
                or terminal_proposal["action"]["kind"] != "terminate"
            ):
                raise RuntimeError(
                    f"terminal proposal was wrong: {terminal_proposal.get('status')}"
                )
            terminal_admission = control.admit_proposal(terminal_proposal["id"])
            if terminal_admission["status"] != "terminal":
                raise RuntimeError(
                    f"terminal admission status: {terminal_admission['status']}"
                )
            terminal = terminal_admission["terminal_event"]
            if terminal["terminal_state"] != "evidence_observed_for_review":
                raise RuntimeError(f"terminal state was wrong: {terminal}")
            replay = planner.propose_next_capital_lease(loop["id"])
            if replay["status"] != "terminal":
                raise RuntimeError("terminal state did not replay")
            duplicate_outcome = control.record_outcome(rounds[-1])
            if duplicate_outcome["status"] != "duplicate":
                raise RuntimeError("outcome replay was not a duplicate")

            integrity_core = store.connection.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0]
            integrity_scheduler = scheduler.connection.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0]
            return {
                "status": "passed",
                "mode": "isolated-live-copy",
                "paid_model_calls": 0,
                "question_ref": question["question_ref"],
                "probe_template_ref": template["id"],
                "loop_version_ref": loop["id"],
                "round_count": len(rounds),
                "outcome_count": len(outcomes),
                "terminal_reason": terminal["terminal_state"],
                "terminal_decision": terminal_admission["decision"]["decision"],
                "outcomes_observed": len(
                    [item for item in planner.outcomes(loop["id"])
                     if item["outcome_kind"] == "observed"]
                ),
                "integrity_check": {
                    "core": integrity_core, "scheduler": integrity_scheduler,
                },
            }
        finally:
            scheduler.close()
            store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    state = Path(
        "/Users/everflow/Library/Application Support/Dalton/state/dalton-core"
    )
    parser.add_argument("--source-core", type=Path, default=state / "core.sqlite")
    parser.add_argument(
        "--source-scheduler", type=Path, default=state / "scheduler.sqlite"
    )
    args = parser.parse_args()
    result = run(args.source_core.resolve(), args.source_scheduler.resolve())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
