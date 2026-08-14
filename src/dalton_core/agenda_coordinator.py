"""Single-lane Phase 1 coordinator for the Agenda Shadow vertical slice."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .contracts import ResultEnvelope, WorkOrder
from .model_router import ModelRouter
from .openclaw_model_adapter import OpenClawModelAdapter
from .perception import LegacyCoveragePerceptionAdapter, validate_snapshot
from .scheduler import Scheduler
from .store import canonical_json, content_hash
from .writer_client import WriterClient
from .writer_server import load_principals


SCHEMA_VERSION = "0.1"
COORDINATOR_ACTOR = "core"


class CoordinatorError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AgendaCoordinatorConfig:
    scheduler_db: Path
    model_router_db: Path
    writer_socket: Path
    core_token_config: Path
    broker_socket: Path
    broker_auth_key: Path
    perception_source_db: Path
    perception_snapshot_path: Path
    company_ref: str
    routing_policy_ref: str
    credential_slot_refs: tuple[str, ...]
    broker_client_id: str
    expected_agent_id: str
    timeout_seconds: float = 180.0

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "AgendaCoordinatorConfig":
        expected = {
            "scheduler_db", "model_router_db", "writer_socket", "core_token_config",
            "broker_socket", "broker_auth_key", "perception_source_db",
            "perception_snapshot_path", "company_ref", "routing_policy_ref",
            "credential_slot_refs", "broker_client_id", "expected_agent_id",
            "timeout_seconds",
        }
        if set(value) != expected:
            raise CoordinatorError("agenda coordinator config has an invalid closed shape")
        def path(name: str) -> Path:
            raw = value[name]
            if not isinstance(raw, str) or not Path(raw).is_absolute():
                raise CoordinatorError(f"{name} must be an absolute path")
            return Path(raw)
        refs = value["credential_slot_refs"]
        if not isinstance(refs, list) or not refs or not all(isinstance(item, str) and item for item in refs):
            raise CoordinatorError("credential_slot_refs must be a non-empty string array")
        timeout = value["timeout_seconds"]
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(float(timeout)) or timeout <= 0:
            raise CoordinatorError("timeout_seconds must be positive and finite")
        strings = {}
        for name in ("company_ref", "routing_policy_ref", "broker_client_id", "expected_agent_id"):
            raw = value[name]
            if not isinstance(raw, str) or not raw:
                raise CoordinatorError(f"{name} must be a non-empty string")
            strings[name] = raw
        return cls(
            scheduler_db=path("scheduler_db"),
            model_router_db=path("model_router_db"),
            writer_socket=path("writer_socket"),
            core_token_config=path("core_token_config"),
            broker_socket=path("broker_socket"),
            broker_auth_key=path("broker_auth_key"),
            perception_source_db=path("perception_source_db"),
            perception_snapshot_path=path("perception_snapshot_path"),
            company_ref=strings["company_ref"],
            routing_policy_ref=strings["routing_policy_ref"],
            credential_slot_refs=tuple(refs),
            broker_client_id=strings["broker_client_id"],
            expected_agent_id=strings["expected_agent_id"],
            timeout_seconds=float(timeout),
        )


def _utc(value: datetime | None = None) -> datetime:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise CoordinatorError("coordinator clock must include timezone")
    return value.astimezone(timezone.utc)


def _load_core_client(config: AgendaCoordinatorConfig) -> WriterClient:
    principals = load_principals(config.core_token_config)
    principal = principals.get("core")
    if principal is None:
        raise CoordinatorError("core writer principal is unavailable")
    return WriterClient(str(config.writer_socket), principal.token, timeout=30)


def _load_snapshot(path: Path) -> dict[str, Any]:
    try:
        return validate_snapshot(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CoordinatorError("perception snapshot is unavailable or invalid") from exc


def _source_catalog(snapshot: Mapping[str, Any]) -> set[str]:
    result = {snapshot["snapshot_id"], f"company:{snapshot['company']['slug']}"}
    result.update(f"event:{item['event_key']}" for item in snapshot["catalysts"])
    result.update(f"evidence:{item['evidence_key']}" for item in snapshot["evidence"])
    result.update(f"filing:{item['accession_no']}" for item in snapshot["filings"])
    return result


def _prompt(snapshot: Mapping[str, Any], mandate: Mapping[str, Any]) -> str:
    perception = {
        "snapshot_id": snapshot["snapshot_id"],
        "company": snapshot["company"],
        "catalysts": snapshot["catalysts"][:12],
        "evidence": snapshot["evidence"][:24],
        "filings": snapshot["filings"][:12],
    }
    contract = {
        "candidates": [
            {
                "question": "string",
                "answer_criteria": "string",
                "features": {
                    "mandate_relevance": "integer 0..3",
                    "catalyst_urgency": "integer 0..3",
                    "evidence_staleness": "integer 0..3",
                    "decision_impact": "integer 0..3",
                },
                "rationale": "string; display only, never used for ranking",
                "source_refs": ["event:/evidence:/filing: reference from the input"],
            }
        ]
    }
    return (
        "You are the question-proposal edge of Dalton Agenda Shadow. "
        "Return only strict JSON, with no markdown or commentary. Propose 3 to 6 "
        "decision-useful research questions. Do not answer them. Do not invent facts or "
        "source references. Each feature is an integer from 0 to 3; do not output an "
        "overall score. Natural-language rationale is display-only.\n"
        f"MANDATE={canonical_json({'objective': mandate['objective'], 'constraints': mandate['constraints'], 'success_criteria': mandate['success_criteria']})}\n"
        f"PERCEPTION={canonical_json(perception)}\n"
        f"OUTPUT_CONTRACT={canonical_json(contract)}"
    )


def parse_candidates(text: str, snapshot: Mapping[str, Any], cycle_id: str) -> list[dict[str, Any]]:
    try:
        value = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise CoordinatorError("model output is not strict JSON") from exc
    if not isinstance(value, Mapping) or set(value) != {"candidates"}:
        raise CoordinatorError("model output has an invalid closed shape")
    rows = value["candidates"]
    if not isinstance(rows, list) or not 3 <= len(rows) <= 6:
        raise CoordinatorError("model must return 3 to 6 candidates")
    allowed_sources = _source_catalog(snapshot)
    results: list[dict[str, Any]] = []
    seen_questions: set[str] = set()
    for index, raw in enumerate(rows):
        if not isinstance(raw, Mapping):
            raise CoordinatorError("candidate must be an object")
        expected = {"question", "answer_criteria", "features", "rationale", "source_refs"}
        if set(raw) != expected:
            raise CoordinatorError("candidate has an invalid closed shape")
        for name in ("question", "answer_criteria", "rationale"):
            if not isinstance(raw[name], str) or not raw[name].strip():
                raise CoordinatorError(f"candidate {name} is invalid")
        question = raw["question"].strip()
        if question in seen_questions:
            raise CoordinatorError("candidate questions must be unique")
        seen_questions.add(question)
        features = raw["features"]
        expected_features = {"mandate_relevance", "catalyst_urgency", "evidence_staleness", "decision_impact"}
        if not isinstance(features, Mapping) or set(features) != expected_features:
            raise CoordinatorError("candidate feature schema is invalid")
        if any(isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 3 for score in features.values()):
            raise CoordinatorError("candidate feature values must be integer 0..3")
        source_refs = raw["source_refs"]
        if not isinstance(source_refs, list) or not source_refs or not all(isinstance(ref, str) and ref in allowed_sources for ref in source_refs):
            raise CoordinatorError("candidate source references are invalid")
        candidate_id = "agenda-candidate:" + content_hash({"cycle_ref": cycle_id, "question": question})[:32]
        results.append({
            "candidate_id": candidate_id,
            "company_ref": snapshot["company"]["slug"],
            "question": question,
            "answer_criteria": raw["answer_criteria"].strip(),
            "features": {name: int(features[name]) for name in sorted(expected_features)},
            "rationale": raw["rationale"].strip(),
            "source_refs": list(dict.fromkeys(source_refs)),
        })
    return results


class AgendaCoordinator:
    def __init__(self, config: AgendaCoordinatorConfig):
        self.config = config

    def _adapter(self, router: ModelRouter) -> OpenClawModelAdapter:
        return OpenClawModelAdapter(
            self.config.broker_socket,
            route_resolver=lambda decision_ref: router.get_decision(decision_ref),
            auth_client_id=self.config.broker_client_id,
            auth_key_provider=lambda: self.config.broker_auth_key.read_bytes().strip(),
            timeout_seconds=self.config.timeout_seconds,
            expected_agent_id=self.config.expected_agent_id,
        )

    @staticmethod
    def _terminal_control_failure(
        scheduler: Scheduler,
        work: WorkOrder,
        lease: Mapping[str, Any],
        *,
        cycle_id: str,
        code: str,
        invocation_ref: str | None = None,
        usage_refs: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Close a leased WorkOrder when the control plane rejects execution."""

        attempt_number = lease["attempt"]["attempt_number"]
        digest = content_hash(
            {
                "work_order_ref": work.id,
                "attempt_number": attempt_number,
                "cycle_ref": cycle_id,
                "code": code,
            }
        )[:32]
        result = ResultEnvelope(
            schema_version=SCHEMA_VERSION,
            id=f"result:agenda-control-failure-{digest}",
            created_at=_utc().isoformat(timespec="microseconds"),
            work_order_ref=work.id,
            invocation_ref=invocation_ref or f"invocation:not-started:{digest}",
            status="failed",
            outputs={},
            actual_side_effects=(),
            usage_refs=tuple(usage_refs),
            artifact_refs=(),
            error={"code": code},
            metadata={"cycle_ref": cycle_id, "control_plane_failure": True},
        )
        return scheduler.complete(
            work.id,
            attempt_number,
            "worker:agenda-model",
            lease["lease_token"],
            result,
            idempotency_key=f"agenda-control-failure:{cycle_id}:{attempt_number}:{code}",
        )

    @staticmethod
    def _periods(now: datetime) -> tuple[str, str]:
        daily = now.replace(hour=0, minute=0, second=0, microsecond=0)
        monthly = daily.replace(day=1)
        return daily.isoformat(timespec="microseconds"), monthly.isoformat(timespec="microseconds")

    @staticmethod
    def _workflow(client: WriterClient, work: WorkOrder, cycle_id: str, policy_ref: str) -> None:
        digest = content_hash({"work_order_ref": work.id, "cycle_ref": cycle_id})[:32]
        client.create_workflow_version(
            workflow_ref=f"workflow:agenda:{digest}",
            title="Agenda Shadow candidate generation",
            objective="Generate bounded research-question features for deterministic selection",
            scope_refs=[cycle_id],
            root_work_order_refs=[work.id],
            governance_policy_ref=policy_ref,
            actor_ref=COORDINATOR_ACTOR,
            version_id=f"workflow-version:agenda:{digest}",
            idempotency_key=f"workflow:agenda:{digest}",
        )

    @staticmethod
    def _record_usage_and_cost(client: WriterClient, invocation: Any, profile: Mapping[str, Any], cycle_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        usage = dict(invocation.usage)
        workflow_ref = f"workflow:agenda:{content_hash({'work_order_ref': invocation.work_order_ref, 'cycle_ref': cycle_id})[:32]}"
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        total_tokens = usage.get("total_tokens")
        if all(isinstance(value, int) for value in (input_tokens, output_tokens, total_tokens)) and total_tokens != input_tokens + output_tokens:
            total_tokens = None
        usage_entry_id = f"usage-entry:{hashlib.sha256(invocation.id.encode()).hexdigest()[:32]}"
        raw = dict(usage)
        measurement = "partial" if any(value is not None for value in (input_tokens, output_tokens, total_tokens)) else "unavailable"
        usage_entry = client.record_usage(
            invocation.id,
            occurred_at=invocation.completed_at or invocation.created_at,
            metering_source="provider_reported",
            measurement_status=measurement,
            raw_usage=raw,
            workflow_ref=workflow_ref,
            provider_usage_ref=invocation.parent_ref,
            actor_ref=COORDINATOR_ACTOR,
            entry_id=usage_entry_id,
            idempotency_key=f"usage:{invocation.id}",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            reasoning_tokens=None,
            cache_read_tokens=usage.get("cache_read_tokens"),
            cache_write_tokens=usage.get("cache_write_tokens"),
            total_tokens=total_tokens,
            requests=1 if measurement != "unavailable" else None,
            duration_ms=None,
            input_bytes=None,
            output_bytes=None,
        )
        rate_refs: list[str] = []
        amount = Fraction(0, 1)
        for metric, charge in ((input_tokens, "input_tokens"), (output_tokens, "output_tokens")):
            if metric is None:
                continue
            price = profile["cost"]["input_per_million_usd" if charge == "input_tokens" else "output_per_million_usd"]
            unit_price_micros = int(round(float(price) * 1_000_000))
            rate_ref = f"price-rate:{profile['provider']}:{profile['model']}:{charge}"
            rate_version = f"price-rate-version:{content_hash({'profile': profile['profile_version_ref'], 'charge': charge})[:32]}"
            rate = client.create_price_rate_version(
                rate_ref,
                provider=profile["provider"], model=profile["model"], charge_type=charge,
                unit_quantity=1_000_000, unit_price_micros=unit_price_micros,
                currency="USD", effective_from=profile["created_at"], effective_until=None,
                source_ref=profile["profile_version_ref"], actor_ref=COORDINATOR_ACTOR,
                prior_version_ref=None, version_id=rate_version,
                idempotency_key=f"price-rate:{rate_version}",
            )
            rate_refs.append(rate["id"])
            amount += Fraction(metric * unit_price_micros, 1_000_000)
        if rate_refs:
            amount_micros = (2 * amount.numerator + amount.denominator) // (2 * amount.denominator)
            cost_status = "estimated"
        else:
            amount_micros = None
            cost_status = "unpriced"
        cost = client.record_cost(
            usage_entry_id,
            price_rate_refs=rate_refs,
            amount_micros=amount_micros,
            currency="USD",
            cost_status=cost_status,
            calculation_ref="calculator:agenda-profile-rates:0.1",
            actor_ref=COORDINATOR_ACTOR,
            correction_of_ref=None,
            cost_entry_id=f"cost-entry:{hashlib.sha256(usage_entry_id.encode()).hexdigest()[:32]}",
            idempotency_key=f"cost:{usage_entry_id}",
        )
        return usage_entry, cost

    @staticmethod
    def _refresh_selected_profile(router: ModelRouter, profile: Mapping[str, Any], completed_at: str) -> None:
        checked = datetime.fromisoformat(completed_at.replace("Z", "+00:00")).astimezone(timezone.utc)
        refreshed = dict(profile)
        refreshed["version"] = int(profile["version"]) + 1
        refreshed["prior_version_ref"] = profile["profile_version_ref"]
        version_root, separator, prior_version = profile["profile_version_ref"].rpartition(":")
        if not separator or not prior_version.isdigit():
            raise CoordinatorError("selected profile version ref cannot be advanced")
        refreshed["profile_version_ref"] = f"{version_root}:{refreshed['version']}"
        refreshed["created_at"] = checked.isoformat(timespec="microseconds")
        refreshed["availability"] = {
            "state": "available",
            "checked_at": checked.isoformat(timespec="microseconds"),
            "valid_until": (checked + timedelta(days=7)).isoformat(timespec="microseconds"),
        }
        refreshed.pop("content_hash", None)
        router.register_profile(refreshed)

    def run_once(self, *, now: datetime | None = None) -> dict[str, Any]:
        now = _utc(now)
        client = _load_core_client(self.config)
        control = client.agenda_control_state()
        if control["paused"]:
            return {"status": "paused", "control_version_ref": control["id"]}
        policy_wire = client.active_agenda_policy(at=now.isoformat(timespec="microseconds"))
        policy = policy_wire["policy"]
        if not policy["enabled"]:
            return {"status": "disabled", "policy_version_ref": policy_wire["id"]}
        if self.config.company_ref not in policy["trial_company_refs"]:
            return {"status": "out_of_scope", "company_ref": self.config.company_ref}
        policy_key = content_hash({"policy_version_ref": policy_wire["id"]})[:12]
        cycle_key = f"agenda:{now.date().isoformat()}:{self.config.company_ref}:{policy_key}"
        existing = client.agenda_cycle_by_key(cycle_key)
        daily_since, monthly_since = self._periods(now)
        budget = client.agenda_budget_status(daily_since=daily_since, monthly_since=monthly_since)
        if existing is None:
            if budget["unpriced_cost_entries"]:
                raise CoordinatorError("monetary budget cannot be enforced while costs are unpriced")
            if budget["daily_cycle_count"] >= policy["max_daily_cycles"]:
                return {"status": "daily_cycle_budget_exhausted"}
            if budget["daily_cost_micros"] >= round(policy["max_daily_cost_usd"] * 1_000_000):
                return {"status": "daily_cost_budget_exhausted"}
            if budget["monthly_cost_micros"] >= round(policy["max_monthly_cost_usd"] * 1_000_000):
                return {"status": "monthly_cost_budget_exhausted"}
            mandates = client.active_mandates(at=now.isoformat(timespec="microseconds"))
            scoped = [mandate for mandate in mandates if self.config.company_ref in mandate["scope_refs"]]
            if len(scoped) != 1:
                raise CoordinatorError("exactly one active mandate must cover the trial company")
            mandate = scoped[0]
            snapshot = LegacyCoveragePerceptionAdapter(self.config.perception_source_db).write(
                self.config.company_ref, self.config.perception_snapshot_path
            )
            started = client.start_agenda_cycle(
                cycle_key=cycle_key,
                perception_snapshot_ref=snapshot["snapshot_id"],
                perception_snapshot_hash=snapshot["content_hash"],
                mandate_version_ref=mandate["id"],
                policy_version_ref=policy_wire["id"],
                company_ref=self.config.company_ref,
                actor_ref=COORDINATOR_ACTOR,
                cycle_id=f"agenda-cycle:{content_hash({'cycle_key': cycle_key})[:32]}",
                idempotency_key=f"agenda-cycle-start:{cycle_key}",
            )
            cycle_id = started["cycle_id"]
            existing = client.agenda_cycle(cycle_id)
        else:
            mandate_rows = client.active_mandates(at=now.isoformat(timespec="microseconds"))
            mandate = next((item for item in mandate_rows if item["id"] == existing["cycle"]["mandate_version_ref"]), None)
            if mandate is None:
                raise CoordinatorError("cycle mandate is no longer available")
            snapshot = _load_snapshot(self.config.perception_snapshot_path)
            if snapshot["content_hash"] != existing["cycle"]["perception_snapshot_hash"]:
                raise CoordinatorError("cycle perception snapshot changed after start")
            cycle_id = existing["cycle"]["cycle_id"]
        state = existing["state"]
        if state in {"decided", "delivered", "failed"}:
            return {"status": state, "cycle_id": cycle_id, "decision": existing["decision"]}
        if state == "collecting":
            prompt = _prompt(snapshot, mandate)
            estimated_input = max(1, len(prompt.encode("utf-8")) // 4)
            if estimated_input > policy["max_input_tokens"]:
                client.fail_agenda_cycle(cycle_id=cycle_id, reason="prompt_input_budget_exceeded", metadata={"estimated_input_tokens": estimated_input}, actor_ref=COORDINATOR_ACTOR)
                raise CoordinatorError("agenda prompt exceeds policy input-token budget")
            digest = content_hash({"cycle_ref": cycle_id, "prompt": prompt})[:32]
            created_at = existing["cycle"]["created_at"]
            work = WorkOrder(
                schema_version=SCHEMA_VERSION,
                id=f"work:agenda-{digest}",
                created_at=created_at,
                updated_at=created_at,
                question=prompt,
                requested_capabilities=("extract",),
                runtime_profile_ref=self.config.routing_policy_ref,
                budget={
                    "max_input_tokens": policy["max_input_tokens"],
                    "max_output_tokens": policy["max_output_tokens"],
                    "max_total_tokens": policy["max_input_tokens"] + policy["max_output_tokens"],
                    "max_cost_usd": policy["max_daily_cost_usd"],
                    "max_seconds": self.config.timeout_seconds,
                },
                idempotency_key=f"agenda-work:{cycle_id}",
                declared_side_effects=(),
                status="ready",
                input_refs=(snapshot["snapshot_id"], mandate["id"]),
                metadata={"cycle_ref": cycle_id, "mode": "agenda_shadow"},
            )
            self._workflow(client, work, cycle_id, policy_wire["id"])
            with Scheduler(self.config.scheduler_db) as scheduler, ModelRouter(self.config.model_router_db) as router:
                scheduler.enqueue(work)
                formal = scheduler.formal_result(work.id)
                if formal is None:
                    lease = scheduler.claim("worker:agenda-model", work_order_id=work.id)
                    if lease is None:
                        return {"status": "waiting_for_lease", "cycle_id": cycle_id, "work_order_id": work.id}
                    routed = router.route(
                        work,
                        attempt_number=lease["attempt"]["attempt_number"],
                        capability="extract",
                        policy_version_ref=self.config.routing_policy_ref,
                        credential_slot_refs=self.config.credential_slot_refs,
                        required_modalities=("text",),
                        required_context_tokens=max(4096, estimated_input + policy["max_output_tokens"]),
                        estimated_input_tokens=estimated_input,
                        estimated_output_tokens=policy["max_output_tokens"],
                        idempotency_key=f"agenda-route:{cycle_id}:{lease['attempt']['attempt_number']}",
                    )["decision"]
                    if routed["outcome"] != "selected":
                        self._terminal_control_failure(
                            scheduler,
                            work,
                            lease,
                            cycle_id=cycle_id,
                            code="model_route_rejected",
                        )
                        client.fail_agenda_cycle(cycle_id=cycle_id, reason="model_route_rejected", metadata={"rejection_reasons": routed["rejection_reasons"]}, actor_ref=COORDINATOR_ACTOR)
                        raise CoordinatorError("model router rejected the agenda work order")
                    profile = router.get_profile(routed["selected_profile_version_ref"])
                    try:
                        invocation, result = self._adapter(router).execute(work, routed, profile)
                    except Exception as exc:
                        self._terminal_control_failure(
                            scheduler,
                            work,
                            lease,
                            cycle_id=cycle_id,
                            code="model_adapter_rejected_or_failed",
                        )
                        client.fail_agenda_cycle(
                            cycle_id=cycle_id,
                            reason="model_adapter_rejected_or_failed",
                            metadata={"error_type": type(exc).__name__},
                            actor_ref=COORDINATOR_ACTOR,
                        )
                        raise
                    client.register_invocation(invocation.to_dict())
                    self._record_usage_and_cost(client, invocation, profile, cycle_id)
                    if result.status == "succeeded":
                        try:
                            candidates = parse_candidates(
                                result.outputs["text"], snapshot, cycle_id
                            )
                        except Exception as exc:
                            self._terminal_control_failure(
                                scheduler,
                                work,
                                lease,
                                cycle_id=cycle_id,
                                code="model_output_contract_rejected",
                                invocation_ref=invocation.id,
                                usage_refs=result.usage_refs,
                            )
                            client.fail_agenda_cycle(
                                cycle_id=cycle_id,
                                reason="model_output_contract_rejected",
                                metadata={"error_type": type(exc).__name__},
                                actor_ref=COORDINATOR_ACTOR,
                            )
                            raise
                    scheduler.complete(
                        work.id,
                        lease["attempt"]["attempt_number"],
                        "worker:agenda-model",
                        lease["lease_token"],
                        result,
                        idempotency_key=f"agenda-complete:{cycle_id}:{lease['attempt']['attempt_number']}",
                    )
                    if result.status != "succeeded":
                        client.fail_agenda_cycle(cycle_id=cycle_id, reason="model_execution_failed", metadata={"error": result.error or {}}, actor_ref=COORDINATOR_ACTOR)
                        return {"status": "failed", "cycle_id": cycle_id}
                    self._refresh_selected_profile(router, profile, invocation.completed_at or invocation.created_at)
                else:
                    result_wire = formal["result_envelope"]
                    if result_wire["status"] != "succeeded":
                        client.fail_agenda_cycle(
                            cycle_id=cycle_id,
                            reason="model_execution_failed",
                            metadata={"formal_result_replayed": True},
                            actor_ref=COORDINATOR_ACTOR,
                        )
                        return {"status": "failed", "cycle_id": cycle_id}
                    try:
                        candidates = parse_candidates(
                            result_wire["outputs"]["text"], snapshot, cycle_id
                        )
                    except Exception as exc:
                        client.fail_agenda_cycle(
                            cycle_id=cycle_id,
                            reason="model_output_contract_rejected",
                            metadata={"error_type": type(exc).__name__},
                            actor_ref=COORDINATOR_ACTOR,
                        )
                        raise
            client.add_agenda_candidates(
                cycle_id=cycle_id,
                candidates=candidates,
                actor_ref=COORDINATOR_ACTOR,
                idempotency_key=f"agenda-candidates:{cycle_id}",
            )
            state = "candidates_ready"
        if state == "candidates_ready":
            decision = client.decide_agenda_cycle(
                cycle_id=cycle_id,
                actor_ref=COORDINATOR_ACTOR,
                decision_id=f"agenda-decision:{content_hash({'cycle_ref': cycle_id})[:32]}",
                idempotency_key=f"agenda-decision:{cycle_id}",
            )
            return {"status": "decided", "cycle_id": cycle_id, "decision": decision}
        raise CoordinatorError(f"unsupported cycle state: {state}")


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one Dalton Agenda Shadow cycle")
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(list(argv) if argv is not None else None)
    raw = json.loads(args.config.read_text(encoding="utf-8"))
    config = AgendaCoordinatorConfig.from_mapping(raw)
    result = AgendaCoordinator(config).run_once()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
