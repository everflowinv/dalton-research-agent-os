"""Human-only local diagnostics. No broker, credentials, scheduler or staging writes."""
from contextlib import ExitStack
from datetime import datetime, timezone
from decimal import Decimal
import sqlite3

from .document_extraction import build_work, _record
from .coverage_mission import CoverageMissionError
from .alphaengine_acquisition_launcher import AcquisitionLaunchError, AcquisitionLaunchConflict, AcquisitionTicketNotFound
from .raw_spool import RawSpoolError
from .model_router import ModelRouter, ModelRouterError, _profile_wire
from .openclaw_model_adapter import ModelAdmissionError, _validate_route, _budget
from .research_verification import ResearchVerificationError
from .store import content_hash
from .thesis_impact_budget import (
    ThesisImpactBudgetStore, ThesisImpactBudgetError, ThesisImpactDayBudgetExceeded,
)


def preflight(service, *, review_id, expected_review_hash, offset, actor_ref,
              expected_context_hash=None):
    """Check current exact authority, then simulate a NEW call in private memory.

    Database snapshots are individually consistent, not a cross-store transaction.
    A successful diagnostic is neither admission nor source/provider permission.
    Existing work is deliberately not treated as a new call or a recovery permit.
    """
    # Authentication also happens at the writer boundary; do not turn an invalid
    # human actor into a diagnostics response disclosing authority to automation.
    import re
    if not isinstance(actor_ref, str) or re.fullmatch(r"human:[A-Za-z0-9][A-Za-z0-9._/@:-]*", actor_ref) is None:
        raise ResearchVerificationError("document extraction requires authenticated human request")

    now = datetime.now(timezone.utc)
    report = {
        "schema_version": "0.1", "preview_only": True, "status": "blocked",
        "checked_at": now.isoformat(), "local_checks_passed": False,
        "execution_authorized": False, "reservation_created": False,
        "persistent_authority_writes": 0, "broker_connections": 0, "credential_contents_read": False,
        "checks": [], "blockers": [],
        "unverified": ["broker_identity", "source_to_model_permission", "credential_validity",
                       "real_provider_billing", "real_model_quality"],
        "limitations": ["Not a reservation or an enablement/approval operation.",
                        "Independent read-only snapshots can expire or change during this check.",
                        "Actual execution must revalidate current authority and atomically reserve budget.",
                        "SQLite read-only WAL readers use volatile SHM read-mark/locking slots; DB/WAL authority is not written.",
                        "A local pass does not verify broker identity or source-to-model permission."],
    }
    errors = (ResearchVerificationError, ModelRouterError, ModelAdmissionError, ThesisImpactBudgetError, CoverageMissionError,
              AcquisitionLaunchError, AcquisitionLaunchConflict, AcquisitionTicketNotFound, RawSpoolError,
              sqlite3.Error, OSError, ValueError, KeyError, TypeError)

    def blocked(check, code, *, error=None):
        item = {"check": check, "code": code}
        if error is not None:
            item["error_type"] = type(error).__name__
            # Domain errors describe the local guard, never credential contents.
            if isinstance(error, (ResearchVerificationError, ThesisImpactBudgetError, CoverageMissionError)):
                item["reason"] = str(error)
            elif isinstance(error, OSError):
                item["reason"] = "Existing authority file is missing or unreadable; nothing was provisioned."
            elif isinstance(error, sqlite3.Error):
                item["reason"] = "Existing SQLite authority is unreadable, locked, lacks WAL sidecars or has an incompatible schema; no migration was attempted."
        report["blockers"].append(item)
        report["checks"].append({"check": check, "status": "blocked"})

    def checked(check, function):
        try:
            value = function()
        except errors as exc:
            blocked(check, check + "_unavailable_or_invalid", error=exc)
            return None
        report["checks"].append({"check": check, "status": "passed"})
        return value

    base = checked("review_source_mission", lambda: service._source_context(
        review_id, expected_review_hash, offset, actor_ref))
    # Check outer bindings even if original content is unavailable, where the
    # exact review still resolves. This gives useful independent blockers.
    def outer_check():
        review = service.writer.coverage_mission.document_review(review_id)
        if content_hash(review) != expected_review_hash:
            raise ResearchVerificationError("review hash changed")
        return service.outer_budget(review["mission_version_ref"])
    outer = checked("outer_authority", outer_check)
    if outer is not None:
        report["outer_budget"] = outer
    config = getattr(service.writer, "_document_extraction_model_config", None)
    if config is None:
        blocked("configuration", "document_extraction_model_config_not_installed")
        return report
    # Writer installation uses this same pure closed-object validator. Never
    # stat/open the auth-key path or resolve/connect the broker socket here.
    from .document_extraction import validate_model_config
    config = checked("configuration", lambda: validate_model_config(config))
    if config is None:
        return report
    report["config_hash"] = content_hash(config)

    with ExitStack() as stack:
        def snapshot(cls, key):
            reader = stack.enter_context(cls(config[key], read_only=True, clock=lambda: now))
            return stack.enter_context(reader.memory_snapshot())
        router = checked("router_snapshot", lambda: snapshot(ModelRouter, "model_router_db"))
        budget = checked("budget_snapshot", lambda: snapshot(ThesisImpactBudgetStore, "budget_db"))
        policy = None if router is None else checked("routing_policy", lambda:
            service.model_policy(router, config["routing_policy_ref"]))
        cap = None if budget is None else checked("shared_budget_policy", lambda:
            budget.policy(config["budget_policy_ref"]))
        if cap is not None:
            summary = checked("shared_budget_balance", lambda: budget.day_summary(
                policy_version_id=config["budget_policy_ref"], day=now.date().isoformat()))
            report["shared_budget"] = summary
        if any(value is None for value in (base, outer, router, budget, policy, cap)):
            report["checks"].append({"check": "route_and_admission", "status": "not_run_dependencies_blocked"})
            return report
        base["model_binding"] = {
            "config_hash": content_hash(config), "routing_policy_ref": config["routing_policy_ref"],
            "routing_policy_hash": content_hash(policy), "budget_policy_ref": config["budget_policy_ref"],
            "budget_policy_hash": cap["content_hash"], "outer_budget": outer,
        }
        context = _record({"id": "document-extraction-context:" + content_hash(base)[:32], **base})
        report["context_binding"] = {key: context[key] for key in (
            "id", "content_hash", "review_id", "review_hash", "mission_version_ref", "mission_version_hash",
            "source_manifest_ref", "source_manifest_hash", "source_content_hash", "offset", "end")}
        if expected_context_hash is not None and expected_context_hash != context["content_hash"]:
            blocked("context", "source_context_changed_reload")
            return report
        work = build_work(context)
        report["work_budget"] = dict(work.budget)
        # Do not replay a persisted admission and mistake it for fresh headroom.
        def new_work_check():
            scheduler = service.writer._scheduler
            if scheduler is None:
                raise ResearchVerificationError("existing Scheduler is required for execution")
            if (scheduler.work_order_authority(work.id) is not None or router.list_decisions(work_order_id=work.id)
                or budget.connection.execute("SELECT 1 FROM thesis_impact_day_admissions WHERE work_order_ref=?", (work.id,)).fetchone()
                or budget.connection.execute("SELECT 1 FROM thesis_impact_day_rejections WHERE work_order_ref=?", (work.id,)).fetchone()):
                raise ResearchVerificationError("existing extraction work requires status/recovery review, not a new-call preview")
            return True
        if checked("new_work", new_work_check) is None:
            return report
        estimated_input = max(1, len(work.question.encode("utf-8")))
        estimated_output = int(work.budget["max_output_tokens"])
        # These are exactly the initial canonical worker.route parameters.
        routed = checked("route_simulation", lambda: router.route(
            work, attempt_number=1, capability="research", policy_version_ref=config["routing_policy_ref"],
            credential_slot_refs=config["credential_slot_refs"], required_modalities=("text",),
            required_context_tokens=estimated_input + estimated_output,
            estimated_input_tokens=estimated_input, estimated_output_tokens=estimated_output,
            decision_kind="initial", previous_decision_ref=None, producer_family=None,
            idempotency_key=f"document-extraction-route:{work.id}:1"))
        if routed is None:
            return report
        route = routed.get("decision")
        if route is None:
            blocked("route", "route_simulation_conflict")
            return report
        # Return only refs/hashes of EXISTING profiles/policy. Never expose the
        # simulated decision/admission/work IDs as persisted authority.
        report["route_preview"] = {"preview_only": True, "outcome": route["outcome"],
            "selected_profile_version_ref": route["selected_profile_version_ref"],
            "rejection_reasons": route["rejection_reasons"]}
        if route["outcome"] != "selected":
            blocked("route", "model_route_rejected")
        else:
            def profile_check():
                selected = router.get_profile(route["selected_profile_version_ref"])
                if _profile_wire(selected) != selected:
                    raise ResearchVerificationError("selected profile binding drifted")
                _validate_route(route, work, selected, now)
                _budget(work, selected)
                return selected
            selected = checked("selected_profile", profile_check)
            if selected is not None:
                report["route_preview"]["selected_profile_hash"] = selected["content_hash"]
        # Even a rejected route can reveal an independent budget blocker. The
        # synthetic route reference remains entirely within this memory copy.
        mission = service.writer.coverage_mission.mission(context["mission_version_ref"])
        scope = {"mission_ref": mission["mission_ref"], "mission_version_ref": mission["id"],
            "mission_version_hash": mission["content_hash"],
            "max_daily_paid_calls": mission["budget"]["max_daily_paid_calls"],
            "max_daily_cost_micros": int(Decimal(str(mission["budget"]["max_daily_cost_usd"])) * 1000000),
            "outer_budget": outer}
        try:
            budget.admit(policy_version_id=config["budget_policy_ref"], day=now.date().isoformat(),
                work_order_ref=work.id, attempt_number=1, phase="assessment", route_decision_ref=route["id"],
                reserved_micros=int(Decimal(str(work.budget["max_cost_usd"])) * 1000000), mission_binding=scope)
        except ThesisImpactDayBudgetExceeded as exc:
            blocked("admission", exc.rejection.get("reason", "owner_budget_exceeded"))
        except errors as exc:
            blocked("admission", "shared_budget_admission_rejected", error=exc)
        else:
            report["checks"].append({"check": "admission", "status": "passed"})
            report["admission_preview"] = {"preview_only": True, "would_admit_new_call": True,
                                            "required_reservation_micros": 50000}
        # Recheck the source/config and exact outer authority after the backups.
        # This catches observed drift, but cannot guarantee atomic cross-store freshness.
        current = checked("final_context_recheck", lambda: service.context(
            review_id, expected_review_hash, offset, actor_ref))
        if current is not None and current != context:
            blocked("context", "snapshot_changed_during_preflight")
    report["local_checks_passed"] = not report["blockers"]
    if report["local_checks_passed"]:
        report["status"] = "local_checks_passed"
    return report
