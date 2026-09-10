"""Create an owner-only Dalton runtime layout without storing credentials."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sqlite3
import tempfile
from pathlib import Path
from typing import Iterable, Sequence

from .model_input import ModelInputLedger
from .industry_research import IndustryResearchAuthority
from .weekly_brief import WeeklyBriefAuthority
from .model_router import ModelRouter
from .agenda import AgendaStore
from .observability import ObservabilityStore
from .scheduler import Scheduler
from .service import SCHEMA_VERSION, ServiceConfig
from .store import DaltonStore
from .writer_server import (
    CORE_OPERATIONS,
    DASHBOARD_CONTROL_OPERATIONS,
    FEEDBACK_BRIDGE_OPERATIONS,
    RESEARCH_REVIEW_CONTROL_OPERATIONS,
    THESIS_IMPACT_OPERATIONS,
    Principal,
    load_principals,
    replace_token_config,
    write_token_config,
)


#: INT3: which database each packaged ``*_schema.sql`` belongs to.
#:
#: ``dalton-bootstrap`` used to open five authorities.  The other forty-seven
#: schemas were applied the first time the writer or a lane constructed their
#: authority -- which on a live Core is several minutes *after* ``install.sh``
#: has already exited zero.  A deploy that broke a schema therefore did not
#: fail at install time; it failed on the first tick that touched that table,
#: as one lane reporting ``unavailable:OperationalError`` in a heartbeat nobody
#: was watching.  Applying them all here moves that failure to the install.
#:
#: The value is ``None`` for the shared Core database, or the sidecar's file
#: name relative to the state directory.  Order matters twice and both cases
#: are pairs sharing one file: the review authority's tables sit beside the
#: candidate-staging tables, and C2's pool columns beside the day ledger.
SCHEMA_DATABASES: tuple[tuple[str, str | None], ...] = (
    ("schema.sql", None),
    ("observability_schema.sql", None),
    ("agenda_schema.sql", None),
    ("model_input_schema.sql", None),
    ("industry_research_schema.sql", None),
    ("analyst_journal_schema.sql", None),
    ("answer_routing_schema.sql", None),
    ("bounded_planner_loop_schema.sql", None),
    ("capability_schema.sql", None),
    ("catalyst_calendar_schema.sql", None),
    ("claim_index_schema.sql", None),
    ("claim_retirement_schema.sql", None),
    ("company_dossier_schema.sql", None),
    ("connector_schema.sql", None),
    ("consensus_estimate_schema.sql", None),
    ("conviction_call_schema.sql", None),
    ("coverage_mission_schema.sql", None),
    ("credential_authority_schema.sql", None),
    ("debate_map_schema.sql", None),
    ("deep_insight_gate_schema.sql", None),
    ("deliverable_reopen_schema.sql", None),
    ("event_judgement_schema.sql", None),
    ("extraction_backlog_schema.sql", None),
    ("forecast_driver_schema.sql", None),
    ("forecast_reconciliation_schema.sql", None),
    ("market_price_schema.sql", None),
    ("mission_deliverable_schema.sql", None),
    ("model_forecast_schema.sql", None),
    ("prior_model_schema.sql", None),
    ("research_constitution_schema.sql", None),
    ("research_cycle_reflection_schema.sql", None),
    ("research_doctrine_schema.sql", None),
    ("research_event_schema.sql", None),
    ("research_plan_schema.sql", None),
    ("research_playbook_schema.sql", None),
    ("research_quality_schema.sql", None),
    ("research_question_backlog_schema.sql", None),
    ("runner_journal_schema.sql", None),
    ("statement_snapshot_schema.sql", None),
    ("street_estimate_schema.sql", None),
    ("thesis_impact_schema.sql", None),
    ("thesis_revision_schema.sql", None),
    ("tracking_cadence_schema.sql", None),
    ("transcript_correction_schema.sql", None),
    ("transcript_polish_schema.sql", None),
    ("valuation_snapshot_schema.sql", None),
    ("weekly_brief_schema.sql", None),
    ("scheduler_schema.sql", "scheduler.sqlite"),
    ("model_router_schema.sql", "model-router.sqlite"),
    ("dashboard_schema.sql", "dashboard-projection.sqlite"),
    ("capability_catalog_schema.sql", "catalog.sqlite"),
    ("document_index_schema.sql", "document-index.sqlite"),
    ("human_intent_schema.sql", "human-intent.sqlite"),
    ("openclaw_exporter_schema.sql", "openclaw-exporter.sqlite"),
    ("research_coordinator_schema.sql", "research-coordinator.sqlite"),
    ("candidate_staging_schema.sql", "research-review/candidate-staging.sqlite"),
    ("research_review_schema.sql", "research-review/candidate-staging.sqlite"),
    ("tick_ledger_schema.sql", "tick-ledger.sqlite"),
    ("thesis_impact_budget_schema.sql", "thesis-impact-budget.sqlite"),
    ("budget_pools_schema.sql", "thesis-impact-budget.sqlite"),
)


def packaged_schema_files(package: Path | None = None) -> tuple[str, ...]:
    """Every ``*.sql`` shipped in this package, sorted."""

    directory = package or Path(__file__).parent
    return tuple(sorted(path.name for path in directory.glob("*.sql")))


def apply_packaged_schemas(
    state_dir: str | Path,
    *,
    package: Path | None = None,
    schemas: Sequence[tuple[str, str | None]] | None = None,
) -> dict[str, int]:
    """Apply every packaged schema so a broken one fails here, not on a tick.

    The loader is the one every authority uses -- ``executescript`` over the
    packaged ``.sql`` -- rather than fifty imports, so this cannot drift from
    what the authorities actually run.  It is safe to repeat and safe on a live
    Core: every statement in every schema is ``CREATE ... IF NOT EXISTS`` or
    ``INSERT OR IGNORE``, so applying one to a populated database is a no-op.
    The ``ALTER``-and-rebuild migrations still belong to their authority's
    constructor; this is about the schema *existing* and being valid SQL.

    A sidecar the Core does not have yet is applied into a scratch database
    that is thrown away, so this creates no database the deploy would not have
    created and leaves an operator nothing to explain.
    """

    root = Path(state_dir).expanduser().resolve()
    package = package or Path(__file__).parent
    declared = tuple(SCHEMA_DATABASES if schemas is None else schemas)
    known = dict(declared)
    unowned = [name for name in packaged_schema_files(package) if name not in known]
    if unowned:
        raise RuntimeError(
            "these packaged schemas name no database in SCHEMA_DATABASES, so "
            "the install would exit zero without ever applying them: "
            + ", ".join(unowned)
        )
    applied = scratch = 0
    with tempfile.TemporaryDirectory(prefix="dalton-schema-probe-") as probe:
        connections: dict[str, sqlite3.Connection] = {}
        try:
            for name, database in declared:
                if database is None:
                    target = root / "core.sqlite"
                else:
                    target = root / database
                    if not target.exists():
                        target = Path(probe) / Path(database).name
                        scratch += 1
                key = str(target)
                if key not in connections:
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    connections[key] = sqlite3.connect(key)
                connections[key].executescript(
                    (package / name).read_text(encoding="utf-8")
                )
                connections[key].commit()
                applied += 1
        finally:
            for connection in connections.values():
                connection.close()
    return {"schemas_applied": applied, "schemas_applied_to_scratch": scratch}


def _write_config(path: Path, value: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def bootstrap(state_dir: str | Path, config_path: str | Path) -> dict[str, str]:
    root = Path(state_dir).expanduser().resolve()
    config = Path(config_path).expanduser().resolve()
    for directory in (root, root / "run", root / "public", root / "perception", root / "backups"):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
    paths = {
        "core_db": root / "core.sqlite",
        "scheduler_db": root / "scheduler.sqlite",
        "model_router_db": root / "model-router.sqlite",
        "projection_db": root / "dashboard-projection.sqlite",
        "heartbeat_path": root / "run" / "heartbeat.json",
        "writer_socket": root / "run" / "writer.sock",
        "token_config": root / "writer-tokens.json",
        "static_output": root / "public" / "index.html",
    }
    configured_service = ServiceConfig.from_file(config) if config.exists() else None
    discord_feedback_enabled = bool(
        configured_service is not None
        and configured_service.outbox is not None
        and configured_service.outbox.feedback_user_ids
    )
    control_enabled = bool(
        configured_service is not None and configured_service.control is not None
    )
    review_enabled = bool(
        control_enabled
        and configured_service is not None
        and configured_service.control is not None
        and configured_service.control.research_review is not None
    )
    with DaltonStore(paths["core_db"]) as store:
        ObservabilityStore(store)
        AgendaStore(store)
        ModelInputLedger(store)
        industry_research = IndustryResearchAuthority(store)
        WeeklyBriefAuthority(store, industry_research)
    with Scheduler(paths["scheduler_db"]):
        pass
    if not paths["model_router_db"].exists():
        with ModelRouter(paths["model_router_db"]):
            pass
    # INT3: the five authorities above are not the schema set.  Apply the rest
    # now, so a schema that does not parse fails the install rather than one
    # lane, several minutes later, on the first tick that touches its table.
    schemas = apply_packaged_schemas(root)
    if not paths["token_config"].exists():
        initial_principals = [
            Principal(
                principal_id="core",
                token=secrets.token_urlsafe(48),
                operations=CORE_OPERATIONS,
                unrestricted=True,
                actor_ref="core",
            ),
            Principal(
                principal_id="thesis-impact",
                token=secrets.token_urlsafe(48),
                operations=THESIS_IMPACT_OPERATIONS,
                unrestricted=False,
                actor_ref="system:thesis-impact-model-worker",
            ),
        ]
        if control_enabled:
            initial_principals.extend([
                Principal(
                    principal_id="dashboard-control",
                    token=secrets.token_urlsafe(48),
                    operations=DASHBOARD_CONTROL_OPERATIONS,
                    unrestricted=False,
                    actor_ref="bridge:tailscale-dashboard",
                ),
                Principal(
                    principal_id="agenda-timeout",
                    token=secrets.token_urlsafe(48),
                    operations=FEEDBACK_BRIDGE_OPERATIONS,
                    unrestricted=False,
                    actor_ref="automation:agenda-timeout",
                ),
            ])
        if review_enabled:
            initial_principals.append(Principal(
                principal_id="research-review-control",
                token=secrets.token_urlsafe(48),
                operations=RESEARCH_REVIEW_CONTROL_OPERATIONS,
                unrestricted=False,
                actor_ref="bridge:tailscale-review",
            ))
        if discord_feedback_enabled:
            initial_principals.append(Principal(
                principal_id="feedback-bridge",
                token=secrets.token_urlsafe(48),
                operations=FEEDBACK_BRIDGE_OPERATIONS,
                unrestricted=False,
                actor_ref="bridge:openclaw-discord",
            ))
        write_token_config(
            paths["token_config"],
            initial_principals,
        )
    else:
        principals = load_principals(
            paths["token_config"],
            allow_managed_operation_subset=True,
        )
        core = principals.get("core")
        if core is None:
            raise RuntimeError("existing token config is missing the core principal")
        if core.operations != CORE_OPERATIONS:
            principals["core"] = Principal(
                principal_id=core.principal_id,
                token=core.token,
                operations=CORE_OPERATIONS,
                allowed_invocation_refs=core.allowed_invocation_refs,
                work_order_refs=core.work_order_refs,
                unrestricted=True,
                actor_ref=core.actor_ref,
            )
        current_thesis_impact = principals.get("thesis-impact")
        principals["thesis-impact"] = Principal(
            principal_id="thesis-impact",
            token=(
                current_thesis_impact.token
                if current_thesis_impact is not None
                else secrets.token_urlsafe(48)
            ),
            operations=THESIS_IMPACT_OPERATIONS,
            unrestricted=False,
            actor_ref="system:thesis-impact-model-worker",
        )
        if discord_feedback_enabled:
            current_feedback = principals.get("feedback-bridge")
            principals["feedback-bridge"] = Principal(
                principal_id="feedback-bridge",
                token=(
                    current_feedback.token
                    if current_feedback is not None else secrets.token_urlsafe(48)
                ),
                operations=FEEDBACK_BRIDGE_OPERATIONS,
                unrestricted=False,
                actor_ref="bridge:openclaw-discord",
            )
        else:
            principals.pop("feedback-bridge", None)
        if control_enabled:
            for principal_id, actor_ref, operations in (
                (
                    "dashboard-control", "bridge:tailscale-dashboard",
                    DASHBOARD_CONTROL_OPERATIONS,
                ),
                (
                    "agenda-timeout", "automation:agenda-timeout",
                    FEEDBACK_BRIDGE_OPERATIONS,
                ),
            ):
                current = principals.get(principal_id)
                principals[principal_id] = Principal(
                    principal_id=principal_id,
                    token=current.token if current is not None else secrets.token_urlsafe(48),
                    operations=operations,
                    unrestricted=False,
                    actor_ref=actor_ref,
                )
        else:
            principals.pop("dashboard-control", None)
            principals.pop("agenda-timeout", None)
        if review_enabled:
            current_review = principals.get("research-review-control")
            principals["research-review-control"] = Principal(
                principal_id="research-review-control",
                token=(
                    current_review.token
                    if current_review is not None else secrets.token_urlsafe(48)
                ),
                operations=RESEARCH_REVIEW_CONTROL_OPERATIONS,
                unrestricted=False,
                actor_ref="bridge:tailscale-review",
            )
        else:
            principals.pop("research-review-control", None)
        replace_token_config(paths["token_config"], list(principals.values()))
    raw = {
        "schema_version": SCHEMA_VERSION,
        "core_db": str(paths["core_db"]),
        "scheduler_db": str(paths["scheduler_db"]),
        "projection_db": str(paths["projection_db"]),
        "model_router_db": str(paths["model_router_db"]),
        "capability_catalog_db": None,
        "heartbeat_path": str(paths["heartbeat_path"]),
        "writer_socket": str(paths["writer_socket"]),
        "tick_seconds": 5,
        "projection_min_interval_seconds": 2,
        "plugin_retry_seconds": 60,
        "plugins": [
            {
                "type": "static_dashboard",
                "enabled": True,
                "output_path": str(paths["static_output"]),
                "publisher": {
                    "type": "tencent_cos",
                    "bucket": "everflow-1320643462",
                    "region": "ap-hongkong",
                    "key": "dalton/index.html",
                    "public_url": "https://eve.lumos.space/dalton/",
                    "keychain_account": "everflow",
                    "secret_id_service": "com.openclaw.tencent-cos.sentiment-dashboard.secret-id",
                    "secret_key_service": "com.openclaw.tencent-cos.sentiment-dashboard.secret-key",
                    "protected_urls": [
                        "https://eve.lumos.space/",
                        "https://eve.lumos.space/kweb.html",
                    ],
                },
            }
        ],
        "backup": {
            "enabled": True,
            "root": str(root / "backups"),
            "interval_seconds": 86400,
        },
    }
    if not config.exists():
        _write_config(config, raw)
    os.chmod(config, 0o600)
    return {
        "state_dir": str(root),
        "config": str(config),
        "core_db": str(paths["core_db"]),
        "scheduler_db": str(paths["scheduler_db"]),
        "model_router_db": str(paths["model_router_db"]),
        "writer_socket": str(paths["writer_socket"]),
        "token_config": str(paths["token_config"]),
        "schemas_applied": str(schemas["schemas_applied"]),
        "schemas_applied_to_scratch": str(schemas["schemas_applied_to_scratch"]),
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bootstrap an owner-only Dalton runtime")
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(list(argv) if argv is not None else None)
    result = bootstrap(args.state_dir, args.config)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
