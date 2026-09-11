"""Production assembly for one admitted mission annual-research run.

The admission authority decides what may be read.  This module deliberately
does not expose any of those fields again: callers supply only the immutable
admission ref and the installed authority paths needed to execute it.
"""

from __future__ import annotations

import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from .annual_report_qualitative import (
    RegisteredAnnualReportDraftWorker,
    RegisteredAnnualReportVerifierWorker,
)
from .annual_report_runtime import (
    adapter_for_config,
    annual_attempt_lease_seconds,
    load_annual_report_model_configs,
    plan_model_execution,
    scheduler_policy,
)
from .connector_authority_port import ConnectorCompletionReceiptReader
from .connector import ConnectorStore
from .mission_annual_research import MissionAnnualResearchAuthority
from .mission_annual_research_executor import MissionAnnualResearchExecutor
from .model_router import ModelRouter
from .observability import ObservabilityStore
from .public_web_fetch_launcher import PublicWebFetchLauncher
from .raw_spool import RawSpoolReader
from .registered_annual_report import RegisteredAnnualReportRegistry
from .research_verification import CandidateStagingStore
from .scheduler import Scheduler
from .sec_company_facts_lane import read_active_annual_budget_mission
from .store import DaltonStore
from .thesis_impact_budget import ThesisImpactBudgetStore


ACTOR_REF = "worker:mission-annual-research"


class MissionAnnualResearchRuntimeError(RuntimeError):
    pass


class _RealtimeClock:
    def __call__(self) -> datetime:
        return datetime.now(timezone.utc)


class MissionAnnualResearchRuntime:
    """Open the exact installed authorities needed by the admitted workflow."""

    def __init__(
        self,
        *,
        state_dir: str | Path,
        staging_path: str | Path,
        web_fetch_governance_path: str | Path,
        spool_dir: str | Path,
        draft_config_path: str | Path | None = None,
        verifier_config_path: str | Path | None = None,
        clock: Any | None = None,
    ) -> None:
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.clock = clock or _RealtimeClock()
        self._closed = False
        self.store = DaltonStore(str(self.state_dir / "core.sqlite"))
        self.router = None
        self.budget = None
        self.staging = None
        self.fetch_reader = None
        try:
            draft_config, verifier_config = load_annual_report_model_configs(
                self.state_dir,
                draft_path=draft_config_path,
                verifier_path=verifier_config_path,
            )
            draft_execution = plan_model_execution(
                draft_config, "registered_annual_report_draft"
            )
            verifier_execution = plan_model_execution(
                verifier_config, "registered_annual_report_verifier"
            )
            self.model_executions = {
                "draft": draft_execution,
                "verifier": verifier_execution,
            }
            self.router = ModelRouter(draft_config["model_router_db"])
            self.fetch_reader = PublicWebFetchLauncher(
                state_dir=self.state_dir,
                governance_path=Path(web_fetch_governance_path).expanduser().resolve(),
                spool_dir=Path(spool_dir).expanduser().resolve(),
            )
            observability = ObservabilityStore(self.store)
            connectors = ConnectorStore(self.store, clock=self.clock)
            scheduler = Scheduler(
                connection=self.store.connection,
                **scheduler_policy((draft_execution, verifier_execution)),
            )
            scheduler.clock = self.clock
            spool = RawSpoolReader(str(Path(spool_dir).expanduser().resolve()))
            registry = RegisteredAnnualReportRegistry(
                core=self.store,
                spool=spool,
                manifest_reader=self.fetch_reader.read_completed_manifest,
                receipt_reader=ConnectorCompletionReceiptReader(
                    connectors=connectors,
                    observability=observability,
                ),
            )
            authority = MissionAnnualResearchAuthority(
                self.store,
                state_dir=self.state_dir,
                registry=registry,
                router=self.router,
                clock=self.clock,
                draft_config_path=draft_config_path,
                verifier_config_path=verifier_config_path,
            )
            if (
                draft_execution["budget_db"] != verifier_execution["budget_db"]
                or draft_execution["budget_policy_ref"]
                != verifier_execution["budget_policy_ref"]
            ):
                raise MissionAnnualResearchRuntimeError(
                    "annual draft and verifier must share one mission budget authority"
                )
            self.budget = ThesisImpactBudgetStore(draft_execution["budget_db"])
            budget_policy_ref = draft_execution["budget_policy_ref"]
            self.budget.policy(budget_policy_ref)
            def mission_resolver(mission_ref: str, company_ref: str) -> dict[str, Any]:
                return read_active_annual_budget_mission(
                    self.store.connection,
                    mission_ref,
                    company_ref,
                    now=self.clock(),
                )

            common = {
                "scheduler": scheduler,
                "router": self.router,
                "store": self.store,
                "observability": observability,
                "polish_worker": None,
                "clock": self.clock,
                "budget_store": self.budget,
                "budget_policy_ref": budget_policy_ref,
                "mission_resolver": mission_resolver,
                "mission_annual_research_authority": authority,
            }
            draft_worker = RegisteredAnnualReportDraftWorker(
                **common,
                adapter=adapter_for_config(
                    draft_config,
                    router=self.router,
                    purpose="registered_annual_report_draft",
                    model_execution=draft_execution,
                ),
                routing_policy_ref=draft_execution["routing_policy_ref"],
                credential_slot_refs=draft_execution["credential_slot_refs"],
                provider_retry=draft_execution["provider_retry"],
                transport_retry=draft_execution.get("transport_retry"),
                lease_seconds=annual_attempt_lease_seconds(
                    draft_execution,
                    router=self.router,
                    purpose="registered_annual_report_draft",
                ),
            )
            verifier_worker = RegisteredAnnualReportVerifierWorker(
                **common,
                adapter=adapter_for_config(
                    verifier_config,
                    router=self.router,
                    purpose="registered_annual_report_verifier",
                    model_execution=verifier_execution,
                ),
                routing_policy_ref=verifier_execution["routing_policy_ref"],
                credential_slot_refs=verifier_execution["credential_slot_refs"],
                provider_retry=verifier_execution["provider_retry"],
                transport_retry=verifier_execution.get("transport_retry"),
                lease_seconds=annual_attempt_lease_seconds(
                    verifier_execution,
                    router=self.router,
                    purpose="registered_annual_report_verifier",
                ),
            )
            self.staging = CandidateStagingStore(
                str(Path(staging_path).expanduser().resolve())
            )
            self.scheduler = scheduler
            self.authority = authority
            self.executor = MissionAnnualResearchExecutor(
                authority=authority,
                scheduler=scheduler,
                registry=registry,
                draft_worker=draft_worker,
                verifier_worker=verifier_worker,
                staging=self.staging,
                actor_ref=ACTOR_REF,
                clock=self.clock,
            )
            # The workers resolve the active mission through the closed
            # function above at every physical broker send.
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for item in (self.staging, self.budget, self.fetch_reader, self.router, self.store):
            if item is not None:
                try:
                    item.close()
                except Exception:
                    pass

    def __enter__(self) -> "MissionAnnualResearchRuntime":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def transition_budget(self, admission: Mapping[str, Any]) -> int:
        # Four calls admit the four immutable WorkOrders.  Retrieval gets one
        # local attempt, each model gets its configured Scheduler attempts,
        # and staging may use the executor's closed draft+verifier recovery
        # allowance after a crash.  This is a call-count guard only; every
        # claim remains bounded by the Work's own elapsed/attempt authority.
        steps = self.executor._steps(admission)
        return len(steps) + sum(int(step["max_attempts"]) for step in steps)

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
    "MissionAnnualResearchRuntime",
    "MissionAnnualResearchRuntimeError",
]
