"""Installed model authority for source-neutral directed-document research.

The admission host calls this reader before it writes an admission.  It binds
the exact Cockpit-editable configurations, Router policies/profiles and budget
policy ceilings that the later draft and verifier workers must use.  It makes
no reservation and sends no model request.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping

from .annual_report_runtime import (
    load_annual_report_model_config,
    plan_model_execution,
)
from .annual_report_qualitative import qualitative_router_capability
from .model_configurations import register_model_config_name
from .model_fallback_chain import (
    TIER_BRAIN,
    TIER_VERIFIER,
    register_purpose_tier,
    tier_for,
)
from .model_router import (
    canonical_hash as router_hash,
    independent_families,
    resolve_chain,
)
from .store import content_hash
from .thesis_impact_budget import ThesisImpactBudgetStore


DRAFT_PURPOSE = "mission_directed_document_draft"
VERIFIER_PURPOSE = "mission_directed_document_verifier"
DRAFT_CAPABILITY = "capability:dalton:model:qualitative-research"
VERIFIER_CAPABILITY = "capability:dalton:model:qualitative-verifier"
DRAFT_MODEL_CONFIG_NAME = register_model_config_name(
    "mission-document-draft-model-config.json"
)
VERIFIER_MODEL_CONFIG_NAME = register_model_config_name(
    "mission-document-verifier-model-config.json"
)
register_purpose_tier(DRAFT_PURPOSE, TIER_BRAIN)
register_purpose_tier(VERIFIER_PURPOSE, TIER_VERIFIER)


class MissionDocumentModelAuthorityError(ValueError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def load_mission_document_model_configs(
    state_dir: str | Path,
    *,
    draft_path: str | Path | None = None,
    verifier_path: str | Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load two owner-only configs without falling back to annual filenames."""

    root = Path(state_dir).expanduser().resolve()
    draft = (
        Path(draft_path).expanduser().resolve()
        if draft_path is not None else root / DRAFT_MODEL_CONFIG_NAME
    )
    verifier = (
        Path(verifier_path).expanduser().resolve()
        if verifier_path is not None else root / VERIFIER_MODEL_CONFIG_NAME
    )
    try:
        configs = (
            load_annual_report_model_config(
                draft, "mission document draft model configuration"
            ),
            load_annual_report_model_config(
                verifier, "mission document verifier model configuration"
            ),
        )
    except Exception as exc:
        raise MissionDocumentModelAuthorityError(str(exc)) from exc
    if configs[0]["model_router_db"] != configs[1]["model_router_db"]:
        raise MissionDocumentModelAuthorityError(
            "mission document draft and verifier must use one Router authority"
        )
    if (
        configs[0]["budget_db"] != configs[1]["budget_db"]
        or configs[0]["budget_policy_ref"] != configs[1]["budget_policy_ref"]
    ):
        raise MissionDocumentModelAuthorityError(
            "mission document draft and verifier must use one budget authority"
        )
    return configs


class MissionDocumentModelAuthority:
    """Callable exact config/Router/budget resolver for admission and runtime."""

    def __init__(
        self,
        *,
        state_dir: str | Path,
        router: Any,
        clock: Callable[[], datetime] = _now,
        draft_config_path: str | Path | None = None,
        verifier_config_path: str | Path | None = None,
    ) -> None:
        if getattr(router, "connection", None) is None or not hasattr(router, "path"):
            raise TypeError("router must expose its exact authority connection and path")
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.router = router
        self.clock = clock
        self.draft_config_path = draft_config_path
        self.verifier_config_path = verifier_config_path

    def _router_record(
        self, table: str, ref_column: str, ref: str,
        hash_column: str, json_column: str,
    ) -> dict[str, Any]:
        row = self.router.connection.execute(
            f"SELECT {hash_column},{json_column} FROM {table} WHERE {ref_column}=?",
            (ref,),
        ).fetchone()
        if row is None:
            raise MissionDocumentModelAuthorityError(
                f"model authority {ref} is unavailable"
            )
        try:
            wire = json.loads(row[json_column])
        except (TypeError, ValueError, RecursionError) as exc:
            raise MissionDocumentModelAuthorityError(
                f"model authority {ref} record is invalid"
            ) from exc
        if not isinstance(wire, Mapping):
            raise MissionDocumentModelAuthorityError(
                f"model authority {ref} record is invalid"
            )
        body = dict(wire)
        asserted = body.pop("content_hash", None)
        if asserted != row[hash_column] or asserted != router_hash(body):
            raise MissionDocumentModelAuthorityError(
                f"model authority {ref} hash drifted"
            )
        return dict(wire)

    def _profile_reasons(
        self,
        profile: Mapping[str, Any],
        policy: Mapping[str, Any],
        execution: Mapping[str, Any],
        capability: str,
        *,
        chain: list[str],
    ) -> list[str]:
        reasons: list[str] = []
        filters = policy["filters"]
        for filter_name, profile_name, reason in (
            ("allowed_profile_ids", "id", "profile_not_allowed"),
            ("allowed_providers", "provider", "provider_not_allowed"),
            ("allowed_families", "family", "family_not_allowed"),
            ("allowed_adapter_refs", "adapter_ref", "adapter_not_allowed"),
        ):
            allowed = set(filters[filter_name])
            if allowed and profile[profile_name] not in allowed:
                reasons.append(reason)
        if capability not in set(profile["capabilities"]):
            reasons.append("capability_not_supported")
        if not set(filters["required_modalities"]).issubset(set(profile["modalities"])):
            reasons.append("modality_not_supported")
        if profile["credential_slot_ref"] not in set(execution["credential_slot_refs"]):
            reasons.append("credential_slot_unavailable")
        availability = profile["availability"]
        now = self.clock().astimezone(timezone.utc)
        checked = datetime.fromisoformat(availability["checked_at"].replace("Z", "+00:00"))
        valid = datetime.fromisoformat(availability["valid_until"].replace("Z", "+00:00"))
        if availability["state"] != "available":
            reasons.append("profile_not_available")
        if checked > now:
            reasons.append("availability_check_in_future")
        if valid <= now:
            reasons.append("availability_expired")
        maximum_input = execution["max_input_tokens"]
        maximum_output = execution["max_output_tokens"]
        if maximum_input > profile["context"]["max_context_tokens"]:
            reasons.append("context_window_insufficient")
        if maximum_output > profile["context"]["max_output_tokens"]:
            reasons.append("model_output_limit_insufficient")
        limits = profile["limits"]
        if maximum_input > limits["max_input_tokens"]:
            reasons.append("profile_input_limit_exceeded")
        if maximum_output > limits["max_output_tokens"]:
            reasons.append("profile_output_limit_exceeded")
        if maximum_input + maximum_output > limits["max_total_tokens"]:
            reasons.append("profile_total_limit_exceeded")
        estimated = (
            Decimal(str(profile["cost"]["input_per_million_usd"]))
            * Decimal(maximum_input) / Decimal(1_000_000)
            + Decimal(str(profile["cost"]["output_per_million_usd"]))
            * Decimal(maximum_output) / Decimal(1_000_000)
        )
        if estimated > Decimal(str(limits["max_cost_usd"])):
            reasons.append("profile_cost_limit_exceeded")
        if estimated > Decimal(str(execution["max_cost_usd"])):
            reasons.append("work_order_cost_budget_exceeded")
        live_chain = [item for item in chain if item]
        if profile.get("unpriced") and (
            not live_chain or profile["id"] != live_chain[-1]
        ):
            reasons.append("unpriced_model_not_last_link")
        return sorted(set(reasons))

    def __call__(self) -> tuple[dict[str, Any], dict[str, Any]]:
        configs = load_mission_document_model_configs(
            self.state_dir,
            draft_path=self.draft_config_path,
            verifier_path=self.verifier_config_path,
        )
        purposes = {"draft": DRAFT_PURPOSE, "verifier": VERIFIER_PURPOSE}
        capabilities = {
            "draft": DRAFT_CAPABILITY,
            "verifier": VERIFIER_CAPABILITY,
        }
        router_capabilities = {
            stage: qualitative_router_capability(capabilities[stage])
            for stage in capabilities
        }
        executions: dict[str, dict[str, Any]] = {}
        for stage in ("draft", "verifier"):
            try:
                executions[stage] = plan_model_execution(
                    configs[0 if stage == "draft" else 1], purposes[stage]
                )
            except Exception as exc:
                raise MissionDocumentModelAuthorityError(str(exc)) from exc
        configured_router = Path(configs[0]["model_router_db"]).expanduser().resolve()
        actual_router = Path(str(self.router.path)).expanduser().resolve()
        if configured_router != actual_router:
            raise MissionDocumentModelAuthorityError(
                "installed mission document configs do not bind the supplied Router authority"
            )
        proof: dict[str, Any] = {}
        families: dict[str, set[str]] = {}
        for stage in ("draft", "verifier"):
            execution = executions[stage]
            policy_ref = execution["routing_policy_ref"]
            policy = self._router_record(
                "model_routing_policy_versions", "policy_version_ref", policy_ref,
                "policy_hash", "policy_json",
            )
            profiles = {item["id"]: item for item in self.router.latest_profiles()}
            resolved = resolve_chain(
                policy, tier=tier_for(purposes[stage]),
                purpose=purposes[stage], profiles=profiles,
            )
            chain = (
                list(resolved["chain"])
                if resolved is not None
                else list(policy["filters"]["allowed_profile_ids"])
            )
            candidates = []
            for profile_id in chain:
                profile = profiles.get(profile_id)
                if profile is None or profile.get("status") == "retired":
                    continue
                exact = self._router_record(
                    "model_endpoint_profile_versions", "profile_version_ref",
                    profile["profile_version_ref"], "profile_hash", "profile_json",
                )
                candidates.append({
                    "profile_ref": exact["id"],
                    "profile_version_ref": exact["profile_version_ref"],
                    "profile_hash": exact["content_hash"],
                    "family": exact["family"],
                    "preflight_reasons": self._profile_reasons(
                        exact, policy, execution, router_capabilities[stage],
                        chain=chain
                    ),
                })
            usable = [item for item in candidates if not item["preflight_reasons"]]
            if not usable:
                raise MissionDocumentModelAuthorityError(
                    f"installed mission document {stage} route has no eligible model"
                )
            if stage == "verifier" and router_capabilities[stage] not in set(
                policy["filters"]["family_independence_capabilities"]
            ):
                raise MissionDocumentModelAuthorityError(
                    "mission document verifier policy does not require family independence"
                )
            families[stage] = {item["family"] for item in usable}
            proof[stage] = {
                "purpose": purposes[stage],
                "workflow_capability": capabilities[stage],
                "router_capability": router_capabilities[stage],
                "config_hash": content_hash(configs[0 if stage == "draft" else 1]),
                "routing_policy_ref": policy_ref,
                "routing_policy_hash": policy["content_hash"],
                "configured_candidate_profiles": candidates,
            }
            try:
                with ThesisImpactBudgetStore(execution["budget_db"], read_only=True) as budget:
                    budget_policy = budget.policy(execution["budget_policy_ref"])
            except Exception as exc:
                raise MissionDocumentModelAuthorityError(
                    f"installed mission document {stage} budget policy is unavailable"
                ) from exc
            required_micros = int(execution["max_cost_usd"] * 1_000_000)
            if budget_policy["day_cap_micros"] < required_micros:
                raise MissionDocumentModelAuthorityError(
                    f"installed mission document {stage} budget cannot admit one call"
                )
            proof[stage]["budget_policy_ceiling"] = {
                "policy_version_ref": budget_policy["policy_version_id"],
                "policy_hash": budget_policy["content_hash"],
                "day_cap_micros": budget_policy["day_cap_micros"],
            }
        if any(
            not any(independent_families(verifier, producer)
                    for verifier in families["verifier"])
            for producer in families["draft"]
        ):
            raise MissionDocumentModelAuthorityError(
                "installed mission document verifier cannot remain independent "
                "of every producer family"
            )
        return executions, proof


__all__ = [
    "DRAFT_CAPABILITY", "DRAFT_MODEL_CONFIG_NAME", "DRAFT_PURPOSE",
    "MissionDocumentModelAuthority", "MissionDocumentModelAuthorityError",
    "VERIFIER_CAPABILITY", "VERIFIER_MODEL_CONFIG_NAME", "VERIFIER_PURPOSE",
    "load_mission_document_model_configs",
]
