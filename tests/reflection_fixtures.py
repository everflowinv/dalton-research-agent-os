"""Q2: a Core with two weeks of research in it, built from the real schemas.

The reflection reads eight authorities.  Driving all eight through their own
writers would take a thousand lines of setup and would test the writers, not
the reading; hand-writing eight ``CREATE TABLE`` statements would test a copy
of the schema that starts drifting the day it is written.

So this builds the fixture from the **shipped ``*_schema.sql`` files** and
inserts through the triggers with the authorisation functions wired open.  The
tables are therefore exactly the tables a live Core has -- if a column is
renamed, the insert here fails, which is the point -- and the rows are ours.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

SRC = Path(__file__).resolve().parents[1] / "src" / "dalton_core"

# Every authorisation function any of the schemas below installs a trigger on.
# Wired to 1 for the whole fixture: this connection is a stand-in for a Core
# that has already been written to, not a place where writes are being tested.
_AUTH_FUNCTIONS = (
    "dalton_authorized",
    "dalton_analyst_journal_authorized",
    "dalton_claim_retirement_authorized",
    "dalton_coverage_mission_authorized",
    "dalton_industry_research_authorized",
    "dalton_mission_deliverable_authorized",
    "dalton_model_authorized",
    "dalton_research_quality_authorized",
    "dalton_research_cycle_reflection_authorized",
    "dalton_weekly_brief_authorized",
)

SCHEMAS = (
    "schema.sql",
    "observability_schema.sql",
    "coverage_mission_schema.sql",
    "research_question_backlog_schema.sql",
    "claim_retirement_schema.sql",
    "bounded_planner_loop_schema.sql",
    "analyst_journal_schema.sql",
    "research_quality_schema.sql",
)


def open_fixture_core(schemas: Iterable[str] = SCHEMAS) -> sqlite3.Connection:
    core = sqlite3.connect(":memory:")
    core.row_factory = sqlite3.Row
    for name in _AUTH_FUNCTIONS:
        core.create_function(name, 0, lambda: 1)
    for name in schemas:
        core.executescript((SRC / name).read_text(encoding="utf-8"))
    return core


def add_mission_version(core: sqlite3.Connection, mission: dict[str, Any]) -> None:
    """The row the stage records and research plans hang off."""

    core.execute(
        "INSERT OR IGNORE INTO coverage_mission_versions(mission_version_id,mission_ref,"
        "version_number,prior_version_id,industry_ref,playbook_version_ref,"
        "constitution_version_ref,mandate_version_ref,record_json,content_hash,actor_ref,"
        "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (mission["id"], mission["mission_ref"], 1, None, "industry:test",
         "playbook-version:test", "constitution-version:test", "mandate-version:test",
         json.dumps(mission, ensure_ascii=False), mission["content_hash"], "human:owner",
         "2026-01-01T00:00:00.000000+00:00"),
    )


def week_of(anchor: datetime, offset_days: float) -> str:
    return (anchor + timedelta(days=offset_days)).astimezone(timezone.utc).isoformat(
        timespec="microseconds"
    )


def add_spend(
    core: sqlite3.Connection, *, at: str, work_order_ref: str,
    amount_micros: int | None, capability: str = "research", suffix: str = "",
) -> None:
    """One priced model call: invocation -> usage entry -> cost entry."""

    invocation = f"invocation:{work_order_ref}:{at}{suffix}"
    core.execute(
        "INSERT INTO model_invocations(invocation_id,profile_ref,provider,model,capability,"
        "runtime_ref,actor_ref,environment_hash,granularity,work_order_ref,model_family,"
        "invocation_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (invocation, "profile:test", "test", "test-model", capability, "runtime:test",
         "automation:coverage-mission", "envhash", "call", work_order_ref, "test-family",
         "{}", at),
    )
    usage = f"usage:{invocation}"
    core.execute(
        "INSERT INTO observability_usage_entries(usage_entry_id,invocation_ref,work_order_ref,"
        "workflow_ref,revision_number,correction_of_ref,occurred_at,metering_source,"
        "measurement_status,record_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (usage, invocation, work_order_ref, "workflow:test", 1, None, at,
         "provider_reported", "final", "{}", "hash", at),
    )
    core.execute(
        "INSERT INTO observability_cost_entries(cost_entry_id,usage_entry_ref,revision_number,"
        "correction_of_ref,amount_micros,currency,cost_status,record_json,content_hash,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (f"cost:{usage}", usage, 1, None, amount_micros, "USD",
         "actual" if amount_micros is not None else "unpriced", "{}", "hash", at),
    )


def add_backlog_event(
    core: sqlite3.Connection, *, question_ref: str, state: str, at: str, reason: str = "test",
) -> None:
    # The event table has a real foreign key to the question, and foreign keys
    # are on. Creating the parent rather than turning enforcement off keeps the
    # fixture the shape a live Core is.
    core.execute(
        "INSERT OR IGNORE INTO backlog_questions(question_ref,identity_json,identity_hash,"
        "actor_ref,record_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?)",
        (question_ref, "{}", f"identity:{question_ref}", "automation:coverage-mission",
         "{}", "hash", at),
    )
    core.execute(
        "INSERT INTO backlog_question_events(event_id,question_ref,state,reason,metadata_json,"
        "actor_ref,created_at,content_hash) VALUES(?,?,?,?,?,?,?,?)",
        (f"event:{question_ref}:{state}:{at}", question_ref, state, reason, "{}",
         "automation:coverage-mission", at, "hash"),
    )


def add_retirement(
    core: sqlite3.Connection, *, claim_version_ref: str, decision: str, at: str,
) -> None:
    challenge = f"challenge:{claim_version_ref}"
    core.execute(
        "INSERT OR IGNORE INTO claim_versions(claim_version_id,claim_ref,version_number,"
        "claim_json,content_hash,prior_version_id,created_at) VALUES(?,?,?,?,?,?,?)",
        # Its own claim_ref, because the version table is unique on
        # (claim_ref, version_number) and an OR IGNORE that quietly does
        # nothing leaves the challenge below with no parent.
        (claim_version_ref, f"claim:{claim_version_ref}", 1, "{}",
         f"hash:{claim_version_ref}", None, at),
    )
    core.execute(
        "INSERT INTO claim_retirement_challenges(challenge_id,claim_version_ref,"
        "claim_version_hash,claim_ref,subject_ref,reason_code,detector_ref,detector_hash,"
        "rationale,actor_ref,record_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (challenge, claim_version_ref, "hash", f"claim:{claim_version_ref}", "company:test",
         "boilerplate_disclaimer", None, None, "test", "automation:coverage-mission",
         "{}", "hash", at),
    )
    if decision is not None:
        core.execute(
            "INSERT INTO claim_retirement_decisions(decision_id,claim_version_ref,challenge_ref,"
            "challenge_hash,decision,actor_ref,rationale,record_json,content_hash,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (f"decision:{claim_version_ref}", claim_version_ref, challenge, "hash", decision,
             "automation:coverage-mission", "test", "{}", "hash", at),
        )


def add_research_plan(
    core: sqlite3.Connection, *, plan_id: str, at: str, inquiries: list[dict[str, Any]],
    mission_version_ref: str = "coverage-mission-version:test:1",
) -> None:
    core.execute(
        "INSERT INTO coverage_mission_research_plans(plan_id,mission_version_ref,state_hash,"
        "assessment,directives_json,inquiries_json,sufficiency_json,model_profile_ref,"
        "work_order_ref,decided_by,created_at,content_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (plan_id, mission_version_ref, f"state:{plan_id}", "test", "[]",
         json.dumps(inquiries, ensure_ascii=False), "{}", "profile:test",
         f"work:llm-research-planner-{plan_id}", "automation:coverage-mission", at,
         f"hash:{plan_id}"),
    )


def add_stage_record(
    core: sqlite3.Connection, *, company_ref: str, stage_ref: str, status: str, at: str,
    mission_version_ref: str = "coverage-mission-version:test:1",
) -> None:
    core.execute(
        "INSERT INTO coverage_mission_stage_records(record_id,mission_version_ref,company_ref,"
        "stage_ref,status,actor_ref,record_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (f"stage:{company_ref}:{stage_ref}:{status}:{at}", mission_version_ref, company_ref,
         stage_ref, status, "automation:coverage-mission", "{}", f"hash:{at}", at),
    )


def add_quality_score(
    core: sqlite3.Connection, *, target_ref: str, rubric_ref: str, at: str,
    judge_status: str | None = None,
) -> None:
    core.execute(
        "INSERT INTO research_quality_score_versions(version_id,score_ref,version_number,"
        "prior_version_ref,artefact_kind,target_ref,target_hash,subject_ref,rubric_ref,"
        "rubric_hash,scorer_version,model_config_fingerprint,scoring_identity_hash,judge_status,"
        "record_json,content_hash,actor_ref,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"quality-score-version:{target_ref}:{at}", f"quality-score:{target_ref}", 1, None,
         "weekly_brief", target_ref, "hash", None, rubric_ref, "rubrichash", "0.1", "none",
         f"identity:{target_ref}:{at}", judge_status, "{}", "hash",
         "automation:coverage-mission", at),
    )


def add_journal_entry(
    core: sqlite3.Connection, *, target_ref: str, verdict: str, at: str, number: int = 1,
) -> None:
    core.execute(
        "INSERT INTO analyst_journal_entries(entry_id,entry_number,target_ref,target_hash,"
        "target_kind,company_ref,verdict,note,score_override_json,idempotency_key,record_json,"
        "content_hash,actor_ref,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"journal:{target_ref}:{number}", number, target_ref, "hash", "weekly_brief", None,
         verdict, None, None, None, "{}", "hash", "human:owner", at),
    )


def tick_summary(**lanes: str) -> dict[str, Any]:
    """A controller tick summary in ``run_once``'s own shape."""

    return {
        "status": "completed" if any(v not in {"idle", "skipped"} for v in lanes.values()) else "idle",
        "active_loop_count": 0,
        "probes_executed": 0,
        "executed": [],
        "skipped": [],
        "mission_sec_dispatch": {"status": "idle"},
        "forecast_reconciliation": {"status": "idle"},
        **{key: {"status": value} for key, value in lanes.items()},
    }


MISSION = {
    "id": "coverage-mission-version:test:1",
    "mission_ref": "coverage-mission:test",
    "content_hash": "missionhash",
    "budget": {"max_daily_cost_usd": 100.0, "max_daily_paid_calls": 9000,
               "max_alphaengine_calls_24h": 130},
    "autonomy": {"may_write": ["claim", "deliverable"], "human_checkpoints": ["thesis_admission"]},
}
