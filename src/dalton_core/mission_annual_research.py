"""Mission-bound admission for one targeted, registered annual-report read.

This authority does not create, approve, or start a ResearchPlan and never
calls a model.  It records that the active signed mission permits one exact
Dossier repair target to use the already-acquired SEC annual-report workflow.
A later dispatcher must call ``resolve_for_execution`` before enqueueing.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from .annual_report_runtime import (
    load_annual_report_model_configs,
    plan_model_execution,
)
from .agenda import read_exact_mandate_version
from .dossier_repair_feedback import read_dossier_repair_feedback
from .model_fallback_chain import tier_for
from .model_router import canonical_hash as router_hash, resolve_chain
from .registered_annual_report import OPERATION, RegisteredAnnualReportRegistry
from .sec_company_facts_lane import read_active_annual_budget_mission
from .store import (
    DaltonStore, authorization_flag, authorized_flag, canonical_json, content_hash,
)
from .thesis_impact_budget import ThesisImpactBudgetStore


SCHEMA_VERSION = "0.1"
WORKFLOW_CONTRACT_REF = "workflow:mission-targeted-registered-annual-report:0.1"
DRAFT_PURPOSE = "registered_annual_report_draft"
VERIFIER_PURPOSE = "registered_annual_report_verifier"
DRAFT_CAPABILITY = "capability:dalton:model:qualitative-research"
VERIFIER_CAPABILITY = "capability:dalton:model:qualitative-verifier"
_SCHEMA_PATH = Path(__file__).with_name("mission_annual_research_schema.sql")


class MissionAnnualResearchError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MissionAnnualResearchError(f"{name} must be non-empty text")
    return value.strip()


def _hash(value: Any, name: str) -> str:
    value = _text(value, name)
    if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        raise MissionAnnualResearchError(f"{name} must be lowercase SHA-256")
    return value


def _canonical_record(raw: Any) -> dict[str, Any]:
    try:
        wire = json.loads(raw)
    except (TypeError, ValueError, RecursionError) as exc:
        raise MissionAnnualResearchError("annual research admission record is invalid") from exc
    if not isinstance(wire, Mapping):
        raise MissionAnnualResearchError("annual research admission record must be an object")
    return dict(wire)


class MissionAnnualResearchAuthority:
    """Append and re-resolve exact mission annual-research admissions."""

    _authorized = authorized_flag()

    def __init__(
        self,
        store: DaltonStore,
        *,
        state_dir: str | Path,
        registry: RegisteredAnnualReportRegistry,
        router: Any,
        clock: Callable[[], datetime] = _now,
        draft_config_path: str | Path | None = None,
        verifier_config_path: str | Path | None = None,
    ) -> None:
        if not isinstance(store, DaltonStore):
            raise TypeError("store must be DaltonStore")
        if not isinstance(registry, RegisteredAnnualReportRegistry):
            raise TypeError("registry must be RegisteredAnnualReportRegistry")
        if getattr(router, "connection", None) is None:
            raise TypeError("router must expose its exact authority connection")
        self.store = store
        self.connection = store.connection
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.registry = registry
        if registry.connection is not self.connection:
            raise TypeError("registry must read the same Core authority")
        self.router = router
        self.clock = clock
        self.draft_config_path = draft_config_path
        self.verifier_config_path = verifier_config_path
        self._authorization_flag = authorization_flag(
            self.connection, "dalton_mission_annual_research_authorized"
        )
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        if self._authorized:
            raise RuntimeError("MissionAnnualResearchAuthority operation cannot be nested")
        self._authorized = True
        try:
            with self.store._transaction() as cursor:
                yield cursor
        finally:
            self._authorized = False

    def _active_mission(
        self, mission_ref: str, mission_hash: str, company_ref: str
    ) -> dict[str, Any]:
        mission = read_active_annual_budget_mission(
            self.connection, mission_ref, company_ref, now=self.clock()
        )
        if mission["content_hash"] != mission_hash:
            raise MissionAnnualResearchError("active mission hash binding failed")
        if "research_task" not in set(mission["autonomy"]["may_write"]):
            raise MissionAnnualResearchError(
                "active mission does not grant research_task automation"
            )
        if mission["autonomy"]["automation_principal"] == "":
            raise MissionAnnualResearchError("active mission has no automation principal")
        # The exact Mandate is already revalidated (including its active
        # pointer/window) by read_active_annual_budget_mission.  Bind the
        # company through the mandate-covered mission universe; mandates may
        # name either the company directly or the mission industry.
        try:
            mandate = read_exact_mandate_version(
                self.connection.cursor(),
                mission["bindings"]["mandate_version"]["ref"],
            )
            refs = set(mandate["scope_refs"])
        except Exception as exc:
            raise MissionAnnualResearchError("Mandate scope authority is invalid") from exc
        if mandate["content_hash"] != mission["bindings"]["mandate_version"]["hash"]:
            raise MissionAnnualResearchError("Mandate hash binding failed")
        if company_ref not in refs and mission["industry_ref"] not in refs:
            raise MissionAnnualResearchError("company is outside the exact Mandate scope")
        return mission

    def _feedback(
        self, company_ref: str, feedback_ref: str, feedback_hash: str,
        target_ref: str, target_hash: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        feedback = read_dossier_repair_feedback(self.state_dir).get(company_ref)
        if (
            feedback is None
            or feedback["id"] != feedback_ref
            or feedback["content_hash"] != feedback_hash
        ):
            raise MissionAnnualResearchError("Dossier repair feedback is stale or unavailable")
        target = next(
            (item for item in feedback["repair_targets"] if item["id"] == target_ref), None
        )
        if target is None or target["content_hash"] != target_hash:
            raise MissionAnnualResearchError("Dossier repair target is stale or unavailable")
        return feedback, target

    @staticmethod
    def _ground_terms(target: Mapping[str, Any], query_terms: list[str]) -> None:
        material = " ".join(
            str(value) for key, value in target.items()
            if key not in {"id", "content_hash"}
        ).casefold()
        for term in query_terms:
            normalized = " ".join(str(term).casefold().split())
            if normalized not in material:
                raise MissionAnnualResearchError(
                    "every annual-report query term must occur in the exact repair target"
                )

    def _router_record(self, table: str, ref_column: str, ref: str, hash_column: str,
                       json_column: str) -> dict[str, Any]:
        row = self.router.connection.execute(
            f"SELECT {hash_column},{json_column} FROM {table} WHERE {ref_column}=?", (ref,)
        ).fetchone()
        if row is None:
            raise MissionAnnualResearchError(f"model authority {ref} is unavailable")
        wire = _canonical_record(row[json_column])
        body = dict(wire)
        asserted = body.pop("content_hash", None)
        if asserted != row[hash_column] or asserted != router_hash(body):
            raise MissionAnnualResearchError(f"model authority {ref} hash drifted")
        return wire

    def _model_authority(self) -> tuple[dict[str, Any], dict[str, Any]]:
        draft_config, verifier_config = load_annual_report_model_configs(
            self.state_dir,
            draft_path=self.draft_config_path,
            verifier_path=self.verifier_config_path,
        )
        executions = {
            "draft": plan_model_execution(draft_config, DRAFT_PURPOSE),
            "verifier": plan_model_execution(verifier_config, VERIFIER_PURPOSE),
        }
        configured_router = Path(draft_config["model_router_db"]).expanduser().resolve()
        actual_router = Path(str(self.router.path)).expanduser().resolve()
        if configured_router != actual_router:
            raise MissionAnnualResearchError(
                "installed annual-report configs do not bind the supplied Router authority"
            )
        configs = {"draft": draft_config, "verifier": verifier_config}
        purposes = {"draft": DRAFT_PURPOSE, "verifier": VERIFIER_PURPOSE}
        capabilities = {"draft": DRAFT_CAPABILITY, "verifier": VERIFIER_CAPABILITY}
        proof: dict[str, Any] = {}
        families: dict[str, set[str]] = {}
        for stage in ("draft", "verifier"):
            policy_ref = executions[stage]["routing_policy_ref"]
            policy = self._router_record(
                "model_routing_policy_versions", "policy_version_ref", policy_ref,
                "policy_hash", "policy_json",
            )
            profiles = {item["id"]: item for item in self.router.latest_profiles()}
            resolved = resolve_chain(
                policy, tier=tier_for(purposes[stage]), purpose=purposes[stage],
                profiles=profiles,
            )
            chain = [] if resolved is None else list(resolved["chain"])
            if not chain:
                chain = list(policy["filters"]["allowed_profile_ids"])
            eligible = []
            allowed_slots = set(executions[stage]["credential_slot_refs"])
            for profile_id in chain:
                profile = profiles.get(profile_id)
                if profile is None or profile.get("status") == "retired":
                    continue
                exact = self._router_record(
                    "model_endpoint_profile_versions", "profile_version_ref",
                    profile["profile_version_ref"], "profile_hash", "profile_json",
                )
                if (
                    exact["credential_slot_ref"] in allowed_slots
                    and capabilities[stage] in set(exact["capabilities"])
                    and exact["availability"]["state"] == "available"
                ):
                    eligible.append({
                        "profile_ref": exact["id"],
                        "profile_version_ref": exact["profile_version_ref"],
                        "profile_hash": exact["content_hash"],
                        "family": exact["family"],
                    })
            if not eligible:
                raise MissionAnnualResearchError(
                    f"installed annual-report {stage} route has no eligible model"
                )
            if stage == "verifier" and capabilities[stage] not in set(
                policy["filters"]["family_independence_capabilities"]
            ):
                raise MissionAnnualResearchError(
                    "annual-report verifier policy does not require family independence"
                )
            families[stage] = {item["family"] for item in eligible}
            proof[stage] = {
                "purpose": purposes[stage],
                "config_hash": content_hash(configs[stage]),
                "routing_policy_ref": policy_ref,
                "routing_policy_hash": policy["content_hash"],
                "eligible_profiles": eligible,
            }
            try:
                with ThesisImpactBudgetStore(
                    executions[stage]["budget_db"], read_only=True
                ) as budget:
                    budget_policy = budget.policy(
                        executions[stage]["budget_policy_ref"]
                    )
            except Exception as exc:
                raise MissionAnnualResearchError(
                    f"installed annual-report {stage} budget policy is unavailable"
                ) from exc
            required_micros = int(executions[stage]["max_cost_usd"] * 1_000_000)
            if budget_policy["day_cap_micros"] < required_micros:
                raise MissionAnnualResearchError(
                    f"installed annual-report {stage} budget cannot admit one call"
                )
            proof[stage]["budget_policy"] = {
                "policy_version_ref": budget_policy["policy_version_id"],
                "policy_hash": budget_policy["content_hash"],
                "day_cap_micros": budget_policy["day_cap_micros"],
            }
        if any(not (families["verifier"] - {family}) for family in families["draft"]):
            raise MissionAnnualResearchError(
                "installed verifier cannot remain independent of every producer family"
            )
        return executions, proof

    def _derive(
        self, *, operation: str, workflow_contract_ref: str,
        mission_version_ref: str, mission_version_hash: str,
        company_ref: str, actor_ref: str, repair_feedback_ref: str,
        repair_feedback_hash: str, repair_target_ref: str, repair_target_hash: str,
        review_ref: str, issuer_cik: str, accession: str, query_terms: list[str],
        document_read_proof_ref: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        mission_version_ref = _text(mission_version_ref, "mission_version_ref")
        mission_version_hash = _hash(mission_version_hash, "mission_version_hash")
        company_ref = _text(company_ref, "company_ref")
        actor_ref = _text(actor_ref, "actor_ref")
        repair_feedback_hash = _hash(repair_feedback_hash, "repair_feedback_hash")
        repair_target_hash = _hash(repair_target_hash, "repair_target_hash")
        if operation != OPERATION or workflow_contract_ref != WORKFLOW_CONTRACT_REF:
            raise MissionAnnualResearchError("unsupported annual research workflow")
        mission = self._active_mission(
            mission_version_ref, mission_version_hash, company_ref
        )
        if actor_ref != mission["autonomy"]["automation_principal"]:
            raise MissionAnnualResearchError("actor is not the active mission principal")
        feedback, target = self._feedback(
            company_ref, repair_feedback_ref, repair_feedback_hash,
            repair_target_ref, repair_target_hash,
        )
        self._ground_terms(target, query_terms)
        executions, model_authority = self._model_authority()
        request = self.registry.bind_request(
            mission_version_ref=mission_version_ref,
            company_ref=company_ref,
            review_ref=review_ref,
            issuer_cik=issuer_cik,
            accession=accession,
            query_terms=query_terms,
            limits={
                "max_query_terms": 12, "max_results": 20,
                "max_source_bytes": 8 * 1024 * 1024,
                "context_before_chars": 240, "context_after_chars": 480,
            },
            model_execution=executions,
            document_read_proof_ref=document_read_proof_ref,
        )
        identity = {
            "schema_version": SCHEMA_VERSION,
            "operation": operation,
            "workflow_contract_ref": workflow_contract_ref,
            "mission_version_ref": mission["id"],
            "mission_version_hash": mission["content_hash"],
            "mission_ref": mission["mission_ref"],
            "company_ref": company_ref,
            "actor_ref": actor_ref,
            "mandate_binding": dict(mission["bindings"]["mandate_version"]),
            "outer_budget": dict(mission["outer_budget"]),
            "repair_feedback_ref": feedback["id"],
            "repair_feedback_hash": feedback["content_hash"],
            "repair_target_ref": target["id"],
            "repair_target_hash": target["content_hash"],
            "request": request,
            "model_authority": model_authority,
        }
        return identity, request

    def admit(self, **kwargs: Any) -> dict[str, Any]:
        identity, _request = self._derive(**kwargs)
        identity_hash = content_hash(identity)
        admission_id = "mission-annual-research-admission:" + identity_hash[:32]
        existing = self.connection.execute(
            "SELECT * FROM mission_annual_research_admissions WHERE admission_id=?",
            (admission_id,),
        ).fetchone()
        if existing is not None:
            wire = self._read_row(existing)
            if existing["identity_hash"] != identity_hash:
                raise MissionAnnualResearchError("annual research admission identity collided")
            return {"status_marker": "duplicate", **wire}
        created_at = self.clock().astimezone(timezone.utc).isoformat(timespec="microseconds")
        wire = {
            **identity,
            "id": admission_id,
            "status": "admitted",
            "created_at": created_at,
            "identity_hash": identity_hash,
        }
        wire["content_hash"] = content_hash(wire)
        with self._transaction() as cursor:
            cursor.execute(
                "INSERT INTO mission_annual_research_admissions("
                "admission_id,identity_hash,mission_version_ref,mission_version_hash,"
                "company_ref,repair_feedback_ref,repair_feedback_hash,repair_target_ref,"
                "repair_target_hash,source_content_hash,actor_ref,record_json,content_hash,created_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    admission_id, identity_hash, wire["mission_version_ref"],
                    wire["mission_version_hash"], wire["company_ref"], wire["repair_feedback_ref"],
                    wire["repair_feedback_hash"], wire["repair_target_ref"],
                    wire["repair_target_hash"], wire["request"]["source_content_hash"],
                    wire["actor_ref"], canonical_json(wire), wire["content_hash"], created_at,
                ),
            )
        return {"status_marker": "fresh", **wire}

    def _read_row(self, row: sqlite3.Row) -> dict[str, Any]:
        wire = _canonical_record(row["record_json"])
        expected_fields = {
            "schema_version", "id", "status", "created_at", "identity_hash",
            "operation", "workflow_contract_ref", "mission_version_ref",
            "mission_version_hash", "mission_ref", "company_ref", "actor_ref",
            "mandate_binding", "outer_budget", "repair_feedback_ref",
            "repair_feedback_hash", "repair_target_ref", "repair_target_hash",
            "request", "model_authority", "content_hash",
        }
        body = dict(wire)
        asserted = body.pop("content_hash", None)
        request = wire.get("request")
        columns = {
            "id": row["admission_id"], "identity_hash": row["identity_hash"],
            "mission_version_ref": row["mission_version_ref"],
            "mission_version_hash": row["mission_version_hash"],
            "company_ref": row["company_ref"],
            "repair_feedback_ref": row["repair_feedback_ref"],
            "repair_feedback_hash": row["repair_feedback_hash"],
            "repair_target_ref": row["repair_target_ref"],
            "repair_target_hash": row["repair_target_hash"],
            "actor_ref": row["actor_ref"], "created_at": row["created_at"],
        }
        if (
            asserted != row["content_hash"]
            or set(wire) != expected_fields
            or asserted != content_hash(body)
            or any(wire.get(key) != value for key, value in columns.items())
            or wire.get("schema_version") != SCHEMA_VERSION
            or wire.get("operation") != OPERATION
            or wire.get("workflow_contract_ref") != WORKFLOW_CONTRACT_REF
            or wire.get("status") != "admitted"
            or not isinstance(request, Mapping)
            or request.get("source_content_hash")
            != row["source_content_hash"]
        ):
            raise MissionAnnualResearchError("annual research admission authority drifted")
        identity = {
            key: value for key, value in wire.items()
            if key not in {"id", "status", "created_at", "identity_hash", "content_hash"}
        }
        if content_hash(identity) != row["identity_hash"]:
            raise MissionAnnualResearchError("annual research admission identity drifted")
        return wire

    def admission(self, admission_ref: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM mission_annual_research_admissions WHERE admission_id=?",
            (_text(admission_ref, "admission_ref"),),
        ).fetchone()
        if row is None:
            raise MissionAnnualResearchError("annual research admission is unavailable")
        return self._read_row(row)

    def resolve_for_execution(self, admission_ref: str) -> dict[str, Any]:
        """Fail closed unless every mutable authority still matches admission."""

        wire = self.admission(admission_ref)
        identity, request = self._derive(
            operation=wire["operation"],
            workflow_contract_ref=wire["workflow_contract_ref"],
            mission_version_ref=wire["mission_version_ref"],
            mission_version_hash=wire["mission_version_hash"],
            company_ref=wire["company_ref"], actor_ref=wire["actor_ref"],
            repair_feedback_ref=wire["repair_feedback_ref"],
            repair_feedback_hash=wire["repair_feedback_hash"],
            repair_target_ref=wire["repair_target_ref"],
            repair_target_hash=wire["repair_target_hash"],
            review_ref=wire["request"]["review_ref"],
            issuer_cik=wire["request"]["issuer_cik"],
            accession=wire["request"]["accession"],
            query_terms=list(wire["request"]["query_terms"]),
            document_read_proof_ref=wire["request"]["document_read_proof_ref"],
        )
        if content_hash(identity) != wire["identity_hash"] or request != wire["request"]:
            raise MissionAnnualResearchError("annual research admission is no longer executable")
        self.registry.verify_request(request)
        return wire


__all__ = [
    "MissionAnnualResearchAuthority", "MissionAnnualResearchError",
    "SCHEMA_VERSION", "WORKFLOW_CONTRACT_REF",
]
