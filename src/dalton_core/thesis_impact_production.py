"""Short-lived production runner for phase-pinned thesis-impact work.

The process never opens Dalton Core.  It discovers and advances exact closed
ResearchPlan targets through the owner-only writer RPC, while Scheduler,
ModelRouter, broker transport and the separate day-budget authority remain
local execution authorities.  Launchd invokes one bounded pass and lets the
process exit; there is no resident model session.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .model_router import ModelRouter
from .observability import ObservabilityStore
from .openclaw_model_adapter import OpenClawModelAdapter
from .scheduler import Scheduler
from .thesis_impact_budget import ThesisImpactBudgetStore
from .thesis_impact_model_worker import ThesisImpactModelWorker
from .writer_client import WriterClient
from .writer_server import THESIS_IMPACT_OPERATIONS, load_principals


class ThesisImpactProductionError(RuntimeError):
    """Production config, authority or execution contract failed closed."""


def _path(value: Any, name: str) -> Path:
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise ThesisImpactProductionError(f"{name} must be an absolute path")
    return Path(value)


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ThesisImpactProductionError(f"{name} must be non-empty text")
    return value


@dataclass(frozen=True, slots=True)
class ThesisImpactProductionConfig:
    scheduler_db: Path
    model_router_db: Path
    writer_socket: Path
    token_config: Path
    broker_socket: Path
    broker_auth_key: Path
    budget_db: Path
    routing_policy_ref: str
    assessment_routing_policy_ref: str
    verifier_routing_policy_ref: str
    budget_policy_version_id: str
    day_cap_micros: int
    credential_slot_refs: tuple[str, ...]
    broker_client_id: str
    expected_agent_id: str
    company_thesis_refs: Mapping[str, str]
    max_targets: int
    timeout_seconds: float

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ThesisImpactProductionConfig":
        expected = {
            "scheduler_db",
            "model_router_db",
            "writer_socket",
            "token_config",
            "broker_socket",
            "broker_auth_key",
            "budget_db",
            "routing_policy_ref",
            "assessment_routing_policy_ref",
            "verifier_routing_policy_ref",
            "budget_policy_version_id",
            "day_cap_micros",
            "credential_slot_refs",
            "broker_client_id",
            "expected_agent_id",
            "company_thesis_refs",
            "max_targets",
            "timeout_seconds",
        }
        if set(raw) != expected:
            raise ThesisImpactProductionError(
                "thesis-impact production config has an invalid closed shape"
            )
        slots = raw["credential_slot_refs"]
        if (
            not isinstance(slots, list)
            or not slots
            or any(not isinstance(item, str) or not item for item in slots)
            or len(set(slots)) != len(slots)
        ):
            raise ThesisImpactProductionError(
                "credential_slot_refs must be a unique non-empty string array"
            )
        bindings = raw["company_thesis_refs"]
        if not isinstance(bindings, Mapping) or any(
            not isinstance(company, str)
            or not company
            or not isinstance(thesis, str)
            or not thesis
            for company, thesis in bindings.items()
        ):
            raise ThesisImpactProductionError(
                "company_thesis_refs must be a string mapping"
            )
        cap = raw["day_cap_micros"]
        maximum = raw["max_targets"]
        timeout = raw["timeout_seconds"]
        if isinstance(cap, bool) or not isinstance(cap, int) or cap <= 0:
            raise ThesisImpactProductionError("day_cap_micros must be positive")
        if (
            isinstance(maximum, bool)
            or not isinstance(maximum, int)
            or not 1 <= maximum <= 1000
        ):
            raise ThesisImpactProductionError("max_targets must be 1..1000")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or timeout <= 0
        ):
            raise ThesisImpactProductionError(
                "timeout_seconds must be positive and finite"
            )
        return cls(
            scheduler_db=_path(raw["scheduler_db"], "scheduler_db"),
            model_router_db=_path(raw["model_router_db"], "model_router_db"),
            writer_socket=_path(raw["writer_socket"], "writer_socket"),
            token_config=_path(raw["token_config"], "token_config"),
            broker_socket=_path(raw["broker_socket"], "broker_socket"),
            broker_auth_key=_path(raw["broker_auth_key"], "broker_auth_key"),
            budget_db=_path(raw["budget_db"], "budget_db"),
            routing_policy_ref=_text(raw["routing_policy_ref"], "routing_policy_ref"),
            assessment_routing_policy_ref=_text(
                raw["assessment_routing_policy_ref"],
                "assessment_routing_policy_ref",
            ),
            verifier_routing_policy_ref=_text(
                raw["verifier_routing_policy_ref"], "verifier_routing_policy_ref"
            ),
            budget_policy_version_id=_text(
                raw["budget_policy_version_id"], "budget_policy_version_id"
            ),
            day_cap_micros=cap,
            credential_slot_refs=tuple(slots),
            broker_client_id=_text(raw["broker_client_id"], "broker_client_id"),
            expected_agent_id=_text(raw["expected_agent_id"], "expected_agent_id"),
            company_thesis_refs=dict(bindings),
            max_targets=maximum,
            timeout_seconds=float(timeout),
        )


class _WriterBackedStore:
    def __init__(self, client: WriterClient):
        self.client = client

    def get_version(self, version_id: str) -> Any:
        return self.client.get_version(version_id)

    def register_invocation(self, invocation: Mapping[str, Any]) -> Any:
        return self.client.register_invocation(invocation)


class _WriterBackedImpact:
    def __init__(
        self, client: WriterClient, scheduler: Scheduler, store: _WriterBackedStore
    ):
        self.client = client
        self.scheduler = scheduler
        self.store = store

    def assessment(self, assessment_ref: str) -> Any:
        return self.client.thesis_impact_assessment(assessment_ref)

    def invocation(self, invocation_ref: str) -> Any:
        return self.client.thesis_impact_invocation(invocation_ref)

    def find_invocation(self, invocation_ref: str) -> Any:
        return self.client.thesis_impact_find_invocation(invocation_ref)


class _WriterBackedObservability:
    def __init__(self, client: WriterClient, store: _WriterBackedStore):
        self.client = client
        self.store = store

    def record_usage(self, invocation_ref: str, **params: Any) -> Any:
        return self.client.record_usage(invocation_ref, **params)

    def create_price_rate_version(
        self, price_rate_ref: str, **params: Any
    ) -> Any:
        return self.client.create_price_rate_version(price_rate_ref, **params)

    def record_cost(self, usage_entry_ref: str, **params: Any) -> Any:
        return self.client.record_cost(usage_entry_ref, **params)


class ThesisImpactProductionRunner:
    def __init__(self, config: ThesisImpactProductionConfig):
        self.config = config

    def _client(self) -> WriterClient:
        principals = load_principals(self.config.token_config)
        principal = principals.get("thesis-impact")
        if (
            principal is None
            or principal.operations != THESIS_IMPACT_OPERATIONS
            or principal.actor_ref != "system:thesis-impact-model-worker"
        ):
            raise ThesisImpactProductionError(
                "scoped thesis-impact writer principal is unavailable"
            )
        return WriterClient(
            str(self.config.writer_socket),
            principal.token,
            timeout=min(30.0, self.config.timeout_seconds),
        )

    def _worker(
        self,
        client: WriterClient,
        scheduler: Scheduler,
        router: ModelRouter,
        budget: ThesisImpactBudgetStore,
    ) -> ThesisImpactModelWorker:
        store = _WriterBackedStore(client)
        impact = _WriterBackedImpact(client, scheduler, store)
        observability = _WriterBackedObservability(client, store)
        adapter = OpenClawModelAdapter(
            self.config.broker_socket,
            route_resolver=lambda decision_ref: router.get_decision(decision_ref),
            auth_client_id=self.config.broker_client_id,
            auth_key_provider=lambda: self.config.broker_auth_key.read_bytes().strip(),
            timeout_seconds=self.config.timeout_seconds,
            expected_agent_id=self.config.expected_agent_id,
        )
        return ThesisImpactModelWorker(
            scheduler=scheduler,
            router=router,
            adapter=adapter,
            impact=impact,  # type: ignore[arg-type]
            observability=observability,  # type: ignore[arg-type]
            routing_policy_ref=self.config.routing_policy_ref,
            assessment_routing_policy_ref=self.config.assessment_routing_policy_ref,
            verifier_routing_policy_ref=self.config.verifier_routing_policy_ref,
            credential_slot_refs=self.config.credential_slot_refs,
            budget=budget,
            budget_policy_version_id=self.config.budget_policy_version_id,
        )

    @staticmethod
    def _run_target(
        client: WriterClient,
        worker: ThesisImpactModelWorker,
        target: Mapping[str, Any],
    ) -> dict[str, Any]:
        plan_ref = str(target["plan_version_ref"])
        thesis_ref = str(target["thesis_ref"])
        started = client.thesis_impact_start(
            plan_version_ref=plan_ref, thesis_ref=thesis_ref
        )
        if started["status"] == "follow_up_recorded":
            return {"status": "follow_up_recorded", "target": dict(target)}
        assessment_run = worker.run_once(started["assessment_work_order"])
        if assessment_run["status"] != "succeeded":
            return {
                "status": f"assessment_{assessment_run['status']}",
                "target": dict(target),
                "work_order_ref": started["assessment_work_order"]["id"],
            }
        assessed = client.thesis_impact_advance_assessment(
            plan_version_ref=plan_ref, thesis_ref=thesis_ref
        )
        assessment_ref = assessed["assessment"]["assessment"]["id"]
        verifier_run = worker.run_once(assessed["verifier_work_order"])
        if verifier_run["status"] != "succeeded":
            return {
                "status": f"verification_{verifier_run['status']}",
                "target": dict(target),
                "assessment_ref": assessment_ref,
                "work_order_ref": assessed["verifier_work_order"]["id"],
            }
        final = client.thesis_impact_advance_verification(
            plan_version_ref=plan_ref,
            thesis_ref=thesis_ref,
            assessment_ref=assessment_ref,
        )
        return {
            "status": final["status"],
            "target": dict(target),
            "assessment_ref": assessment_ref,
            "verification_ref": final["verification"]["verification"]["id"],
        }

    def run_once(self) -> dict[str, Any]:
        client = self._client()
        with Scheduler(self.config.scheduler_db) as scheduler, ModelRouter(
            self.config.model_router_db
        ) as router, ThesisImpactBudgetStore(self.config.budget_db) as budget:
            budget.register_policy(
                policy_version_id=self.config.budget_policy_version_id,
                day_cap_micros=self.config.day_cap_micros,
            )
            targets = client.thesis_impact_targets(
                self.config.company_thesis_refs, limit=self.config.max_targets
            )
            if not targets:
                return {
                    "status": "idle",
                    "target_count": 0,
                    "provider_call_count": 0,
                }
            worker = self._worker(client, scheduler, router, budget)
            results = [self._run_target(client, worker, target) for target in targets]
            return {
                "status": (
                    "completed"
                    if all(
                        row["status"]
                        in {"eligible", "rejected", "follow_up_recorded"}
                        for row in results
                    )
                    else "incomplete"
                ),
                "target_count": len(targets),
                "results": results,
            }


def load_config(path: str | Path) -> ThesisImpactProductionConfig:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ThesisImpactProductionError(
            "thesis-impact production config is unavailable"
        ) from exc
    if not isinstance(raw, Mapping):
        raise ThesisImpactProductionError(
            "thesis-impact production config must be an object"
        )
    if "thesis_impact" in raw:
        service_section = raw.get("thesis_impact")
        if (
            not isinstance(service_section, Mapping)
            or service_section.get("enabled") is not True
            or not isinstance(service_section.get("config"), Mapping)
        ):
            raise ThesisImpactProductionError(
                "service config does not enable thesis-impact production"
            )
        raw = service_section["config"]
    return ThesisImpactProductionConfig.from_mapping(raw)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run one bounded production thesis-impact pass"
    )
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        result = ThesisImpactProductionRunner(load_config(args.config)).run_once()
    except Exception as exc:
        print(
            json.dumps(
                {"status": "failed", "error_type": type(exc).__name__},
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] in {"idle", "completed"} else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ThesisImpactProductionConfig",
    "ThesisImpactProductionError",
    "ThesisImpactProductionRunner",
    "load_config",
]
