"""Production assembly for an admitted source-neutral document research run."""

from __future__ import annotations

import math
import json
import time
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from .annual_report_runtime import (
    adapter_for_config,
    annual_attempt_lease_seconds,
    scheduler_policy,
)
from .document_research_qualitative import (
    MissionDocumentDraftWorker,
    MissionDocumentVerifierWorker,
)
from .mission_document_model_authority import (
    DRAFT_PURPOSE,
    VERIFIER_PURPOSE,
    load_mission_document_model_configs,
)
from .mission_document_research_admission import (
    open_mission_document_admission_authority,
)
from .mission_document_research_executor import (
    MissionDocumentResearchExecutor,
    _steps,
)
from .model_router import ModelRouter
from .observability import ObservabilityStore
from .research_verification import CandidateStagingStore
from .scheduler import Scheduler
from .store import DaltonStore
from .store import canonical_json, content_hash
from .thesis_impact_budget import ThesisImpactBudgetStore


ACTOR_REF = "worker:mission-document-research"


class MissionDocumentResearchRuntimeError(RuntimeError):
    pass


class _RealtimeClock:
    def __call__(self) -> datetime:
        return datetime.now(timezone.utc)


class MissionDocumentResearchRuntime:
    """Open exact installed authorities; callers provide only an admission ref."""

    def __init__(
        self,
        *,
        state_dir: str | Path,
        staging_path: str | Path,
        planner_scheduler_db: str | Path,
        planner_model_config_path: str | Path,
        document_config_path: str | Path | None = None,
        admission_ref: str,
        expected_admission_hash: str,
        draft_config_path: str | Path | None = None,
        verifier_config_path: str | Path | None = None,
        clock: Any | None = None,
    ) -> None:
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.clock = clock or _RealtimeClock()
        self._closed = False
        self._resources = ExitStack()
        self.store = DaltonStore(str(self.state_dir / "core.sqlite"))
        self.budget = None
        self.staging = None
        self.router = None
        try:
            from .coverage_mission import CoverageMissionAuthority

            row = self.store.connection.execute(
                "SELECT admission_id,mission_version_ref,mission_version_hash,"
                "record_json,content_hash FROM mission_document_research_admissions "
                "WHERE admission_id=?", (admission_ref,),
            ).fetchone()
            if row is None:
                raise MissionDocumentResearchRuntimeError(
                    "mission document admission is unavailable"
                )
            try:
                persisted = json.loads(row["record_json"])
            except (TypeError, ValueError, RecursionError) as exc:
                raise MissionDocumentResearchRuntimeError(
                    "mission document admission record is invalid"
                ) from exc
            body = dict(persisted) if isinstance(persisted, Mapping) else {}
            asserted = body.pop("content_hash", None)
            if (
                not isinstance(persisted, Mapping)
                or canonical_json(persisted) != row["record_json"]
                or persisted.get("id") != admission_ref
                or persisted.get("mission_version_ref") != row["mission_version_ref"]
                or persisted.get("mission_version_hash") != row["mission_version_hash"]
                or asserted != row["content_hash"]
                or asserted != expected_admission_hash
                or asserted != content_hash(body)
            ):
                raise MissionDocumentResearchRuntimeError(
                    "mission document admission binding drifted"
                )
            mission = CoverageMissionAuthority(self.store).mission(
                row["mission_version_ref"]
            )
            if mission["content_hash"] != row["mission_version_hash"]:
                raise MissionDocumentResearchRuntimeError(
                    "mission document admission names a different mission version"
                )
            authority, _registrations = self._resources.enter_context(
                open_mission_document_admission_authority(
                    store=self.store,
                    state_dir=self.state_dir,
                    mission=mission,
                    planner_scheduler_db=planner_scheduler_db,
                    planner_model_config_path=planner_model_config_path,
                    document_config_path=document_config_path,
                    draft_model_config_path=draft_config_path,
                    verifier_model_config_path=verifier_config_path,
                    clock=self.clock,
                )
            )
            configs = load_mission_document_model_configs(
                self.state_dir,
                draft_path=draft_config_path,
                verifier_path=verifier_config_path,
            )
            executions, _proof = authority.model_execution_resolver()
            draft_execution = executions["draft"]
            verifier_execution = executions["verifier"]
            self.router = ModelRouter(configs[0]["model_router_db"], clock=self.clock)
            router = self.router
            scheduler = Scheduler(
                connection=self.store.connection,
                **scheduler_policy((draft_execution, verifier_execution)),
            )
            scheduler.clock = self.clock
            if (
                draft_execution["budget_db"] != verifier_execution["budget_db"]
                or draft_execution["budget_policy_ref"]
                != verifier_execution["budget_policy_ref"]
            ):
                raise MissionDocumentResearchRuntimeError(
                    "mission document stages must share one budget authority"
                )
            self.budget = ThesisImpactBudgetStore(draft_execution["budget_db"])
            budget_policy_ref = draft_execution["budget_policy_ref"]
            self.budget.policy(budget_policy_ref)
            observability = ObservabilityStore(self.store)
            common = {
                "scheduler": scheduler,
                "router": router,
                "store": self.store,
                "observability": observability,
                "polish_worker": None,
                "clock": self.clock,
                "budget_store": self.budget,
                "budget_policy_ref": budget_policy_ref,
                "mission_document_research_authority": authority,
            }
            draft_worker = MissionDocumentDraftWorker(
                **common,
                adapter=adapter_for_config(
                    configs[0], router=router, purpose=DRAFT_PURPOSE,
                    model_execution=draft_execution,
                ),
                routing_policy_ref=draft_execution["routing_policy_ref"],
                credential_slot_refs=draft_execution["credential_slot_refs"],
                provider_retry=draft_execution["provider_retry"],
                transport_retry=draft_execution.get("transport_retry"),
                lease_seconds=annual_attempt_lease_seconds(
                    draft_execution, router=router, purpose=DRAFT_PURPOSE,
                ),
            )
            verifier_worker = MissionDocumentVerifierWorker(
                **common,
                adapter=adapter_for_config(
                    configs[1], router=router, purpose=VERIFIER_PURPOSE,
                    model_execution=verifier_execution,
                ),
                routing_policy_ref=verifier_execution["routing_policy_ref"],
                credential_slot_refs=verifier_execution["credential_slot_refs"],
                provider_retry=verifier_execution["provider_retry"],
                transport_retry=verifier_execution.get("transport_retry"),
                lease_seconds=annual_attempt_lease_seconds(
                    verifier_execution, router=router, purpose=VERIFIER_PURPOSE,
                ),
            )
            self.staging = CandidateStagingStore(
                str(Path(staging_path).expanduser().resolve())
            )
            self.scheduler = scheduler
            self.authority = authority
            self.admission = authority.resolve_for_execution(admission_ref)
            if self.admission["content_hash"] != expected_admission_hash:
                raise MissionDocumentResearchRuntimeError(
                    "mission document admission hash drifted before execution"
                )
            self.registry = authority.registry
            self.executor = MissionDocumentResearchExecutor(
                authority=authority,
                scheduler=scheduler,
                registry=authority.registry,
                draft_worker=draft_worker,
                verifier_worker=verifier_worker,
                staging=self.staging,
                actor_ref=ACTOR_REF,
                clock=self.clock,
            )
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for item in (self.staging, self.budget, self.router):
            if item is not None:
                try:
                    item.close()
                except Exception:
                    pass
        self._resources.close()
        try:
            self.store.close()
        except Exception:
            pass

    def __enter__(self) -> "MissionDocumentResearchRuntime":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def transition_budget(self, admission: Mapping[str, Any]) -> int:
        steps = _steps(admission)
        transitions = len(steps) + sum(int(step["max_attempts"]) for step in steps)
        for index, stage in ((1, "draft"), (2, "verifier")):
            retry = admission["model_execution"][stage].get("provider_retry") or {}
            recovery = retry.get("unknown_recovery") or {}
            fresh = int(recovery.get("max_fresh_work_orders", 0))
            # Each fresh Work needs an enqueue transition and its own bounded
            # Scheduler attempts. One final transition records waiting/stopped
            # recovery authority even when fresh recovery is disabled.
            transitions += fresh * (1 + int(steps[index]["max_attempts"])) + 1
        return transitions

    def wait_until_claimable(self, work_order_ref: str) -> bool:
        status = self.scheduler.status(work_order_ref)
        claimable_at = status.get("not_before")
        if status.get("state") == "leased":
            row = self.scheduler.connection.execute(
                "SELECT expires_at FROM scheduler_leases WHERE work_order_id=? "
                "ORDER BY lease_version DESC LIMIT 1",
                (work_order_ref,),
            ).fetchone()
            claimable_at = None if row is None else row["expires_at"]
        if claimable_at is None:
            return status.get("state") == "ready"
        target = datetime.fromisoformat(claimable_at).astimezone(timezone.utc)
        work = self.scheduler.work_order_authority(work_order_ref)
        if work is None:
            return False
        maximum = work["work_order"]["budget"].get("max_elapsed_seconds")
        if isinstance(maximum, int) and not isinstance(maximum, bool) and maximum > 0:
            history = self.scheduler.attempt_history(work_order_ref)
            admitted_at = datetime.fromisoformat(history[0]["created_at"]).astimezone(
                timezone.utc
            )
            if target >= admitted_at + timedelta(seconds=maximum):
                return False
        delay = max(0.0, (target - self.clock()).total_seconds())
        if delay:
            time.sleep(math.ceil(delay))
        return True


__all__ = [
    "ACTOR_REF",
    "MissionDocumentResearchRuntime",
    "MissionDocumentResearchRuntimeError",
]
