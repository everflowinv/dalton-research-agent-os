"""Install reusable research foundations into one isolated workspace.

This setup deliberately does not create a CoverageMissionVersion.  Connector
infrastructure named by the workspace's pinned shared catalog is re-authorized
locally by the workspace owner; no legacy approval record or research state is
copied.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .connector_governance import (
    ConnectorGovernanceError,
    build_governance_record,
    governance_kind_for_capability,
)
from .connector_inventory import load_packaged_connector_inventory
from .conviction_call import CONVICTION_POLICY
from .debate_map import DEBATE_POLICY
from .driver_template import (
    COST_DRIVER_TEMPLATES,
    COST_REGISTRY_HASH,
    DRIVER_TEMPLATES,
    REGISTRY_HASH,
)
from .service import ServiceConfig
from .store import content_hash
from .store import DaltonStore
from .research_playbook import ResearchPlaybookAuthority
from .coverage_mission import AUTOMATION_WRITE_SCOPES, CHECKPOINT_KINDS, DELIVERABLE_KINDS
from .workspace import WorkspaceError, load_workspace_manifest, path_is_declared

SCHEMA_VERSION = "dalton-workspace-runtime-foundation-0.1"
OWNER_ACTOR_PREFIX = "human:"


def _awaiting_mission_output_policies(workspace_slug: str) -> dict[str, dict[str, Any]]:
    """Valid lane-enabling policies that make no mission or method claim."""
    from .company_dossier import validate_policy as validate_dossier_policy
    from .industry_framework import validate_policy as validate_framework_policy

    dossier = validate_dossier_policy({
        "schema_version": "0.1",
        "policy_ref": f"dossier-policy:{workspace_slug}:awaiting-mission",
        "causal_chain_maps": [], "output_rubric_bindings": [],
    })
    framework = validate_framework_policy({
        "schema_version": "0.1",
        "policy_ref": f"industry-framework-policy:{workspace_slug}:awaiting-mission",
        "causal_chain_titles": [], "driver_horizons": [],
        "gap_checklist": [{
            "gap_ref": f"gap:{workspace_slug}:awaiting-mission",
            "label": "Research mission not configured",
            "what_is_missing": "A published mission and its constitution are required before industry research can begin.",
            "content_kind": "mission_authority", "driver_refs": [],
            "cost_note": "No research or provider call is authorized while the workspace is awaiting its first mission.",
            "blocks_links": [],
        }],
        "output_rubric_bindings": [],
    })
    return {"p12a-dossier-policy-v1.json": dossier,
            "p12e-industry-framework-policy-v1.json": framework}

# These are method defaults.  They contain no issuer, industry, mission or
# inherited decision.  A mission may version them later without changing the
# setup contract.
TRACKING_POLICY: Mapping[str, Any] = {
    "schema_version": "0.1",
    "policy_ref": "tracking-policy:workspace-default:v1",
    "title": "Workspace tracking baselines",
    "cadences": [
        {"source_key": "yfinance", "interval_seconds": 21600,
         "adjustable": False, "because": "one close plus one bounded intraday refresh"},
        {"source_key": "sec", "interval_seconds": 86400,
         "adjustable": False, "because": "daily public filing discovery"},
        {"source_key": "sec-ownership", "interval_seconds": 86400,
         "adjustable": False, "because": "daily ownership filing discovery"},
        {"source_key": "alphaengine", "interval_seconds": 43200,
         "adjustable": True, "because": "bounded twice-daily research discovery"},
        {"source_key": "gemini-web-search", "interval_seconds": 43200,
         "adjustable": True, "because": "bounded public web discovery"},
        {"source_key": "guidepoint", "interval_seconds": 604800,
         "adjustable": True, "because": "weekly expert-library discovery"},
        {"source_key": "catalyst-calendar", "interval_seconds": 86400,
         "adjustable": False, "because": "daily event-calendar discovery"},
    ],
    "abnormal_move": {
        "threshold_ref": "abnormal-move:workspace-default:v1",
        "absolute_move_percent": "3.0", "excess_vs_basket_percent": "2.5",
        "excess_vs_benchmark_percent": "3.0", "min_basket_members": 3,
        "benchmark_refs": ["company:benchmark:SPY"],
        "max_lookback_trading_days": 5, "window_trading_days": 10,
        "divergence_vs_basket_percent": "6.0",
    },
    "immediate_pull": {
        "trigger_kinds": ["price_move"],
        "source_keys": ["alphaengine", "gemini-web-search"],
        "within_seconds": 21600,
    },
    "thesis_stances": {"default": "long", "overrides": {}},
}

CONSTITUTION_METHOD: Mapping[str, Any] = {
    "question_admission": ["A question must be able to change a thesis or driver view."],
    "causal_chain": ["State the observable input, transmission mechanism, and outcome."],
    "source_standards": {
        "hierarchy": ["Primary filings and issuer records govern reported facts."],
        "conflict_adjudication": ["Keep material conflicts explicit until resolved."],
        "minimum_independent_sources": 1,
    },
    "falsification": {
        "required_falsifier_searches": ["Search for evidence against the proposed mechanism."],
        "alternative_explanations": ["Test timing, mix, price, volume, and external effects."],
    },
    "materiality": ["State direction, magnitude, horizon, and affected financial line."],
    "lifecycle": {
        "continue_when": ["New evidence can change a thesis, driver, or forecast."],
        "refresh_when": ["Evidence exceeds its declared freshness window."],
        "stop_when": ["The admitted question and its falsifiers are resolved."],
        "escalate_when": ["A material thesis, scope, or budget change is proposed."],
    },
    "output_rubric": {
        "criteria": ["Separate facts, inference, uncertainty, and next verification."],
        "good_samples": [], "bad_samples": [],
    },
}


def generic_driver_pack_template() -> dict[str, Any]:
    """Return a publishable, industry-neutral driver-pack body."""
    generic = next(item for item in DRIVER_TEMPLATES["templates"]
                   if item["classification"] == DRIVER_TEMPLATES["generic_classification"])
    drivers, metrics, theses = [], [], []
    for slot in generic["slots"]:
        suffix = str(slot["slot_id"])
        driver_ref = f"driver:workspace-generic:{suffix}"
        metric_ref = f"metric:workspace-generic:{suffix}"
        drivers.append({"driver_ref": driver_ref, "label": slot["label"],
                        "mechanism": slot["question"], "metric_refs": [metric_ref]})
        metrics.append({
            "metric_ref": metric_ref, "label": slot["label"],
            "definition": slot["question"], "unit": "mission_defined",
            "periodicity": "mission_defined",
            "preferred_source_refs": ["source:sec-edgar"],
            "verification_kind": "numeric_and_semantic", "caveats": [],
        })
        theses.append({
            "template_ref": f"thesis-template:workspace-generic:{suffix}",
            "statement": f"Changes in {slot['label']} may change company outcomes.",
            "mechanism": slot["question"], "driver_refs": [driver_ref],
            "implied_expectation": f"The mission must state an observable expectation for {slot['label']}.",
            "falsifier_refs": [f"falsifier:workspace-generic:{suffix}"],
        })
    return {"drivers": drivers, "metric_specs": metrics, "thesis_templates": theses}


def _atomic_seed(path: Path, value: Mapping[str, Any]) -> str:
    """Create one owner-only JSON file and preserve any existing owner edit."""
    if path.exists():
        return "preserved"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        try:
            os.link(temporary, path)
        except FileExistsError:
            return "preserved"
        return "created"
    finally:
        Path(temporary).unlink(missing_ok=True)


def _catalog(workspace: Any) -> Mapping[str, Any] | None:
    metadata_path = workspace.workspace_root / "display.json"
    if not metadata_path.is_file():
        return None
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    binding = metadata.get("shared_connection_catalog")
    if binding is None:
        return None
    if not isinstance(binding, Mapping) or set(binding) != {
            "path", "content_hash", "model_count", "source_count"}:
        raise WorkspaceError("workspace shared connection catalog binding is invalid")
    path = Path(binding["path"]).expanduser().resolve()
    if not path_is_declared(path, workspace) or path.is_relative_to(workspace.workspace_root):
        raise WorkspaceError("workspace shared connection catalog is outside its binding")
    raw = json.loads(path.read_text(encoding="utf-8"))
    body = {key: raw[key] for key in raw if key != "content_hash"}
    if raw.get("schema_version") != "dalton-shared-connection-catalog-0.1" \
            or raw.get("content_hash") != content_hash(body) \
            or raw["content_hash"] != binding["content_hash"]:
        raise WorkspaceError("workspace shared connection catalog hash differs")
    return raw


def _connector_records(workspace: Any, actor_ref: str) -> tuple[list[str], list[str]]:
    catalog = _catalog(workspace)
    if catalog is None:
        return [], []
    created: list[str] = []
    unsupported: list[str] = []
    seen: set[tuple[str, int]] = set()
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    kinds: set[str] = set()
    for source in catalog["sources"]:
        capability = source.get("capability_id")
        try:
            kinds.add(governance_kind_for_capability(capability))
        except (ConnectorGovernanceError, KeyError, TypeError):
            unsupported.append(str(capability))
            continue
        # Creating an environment reuses the connected source's complete
        # read workflow: searching without being able to read the result is
        # not a usable research connection. These are the existing read-only
        # operations, authorized by the owner's shared-source creation action.
        kinds.update({
            "connector:alphaengine-library": {"alphaengine-search-library", "alphaengine-get-document"},
            "connector:guidepoint-library": {"guidepoint-search-library", "guidepoint-get-transcript"},
            "connector:sec-edgar": {"sec-filings-index", "sec-company-facts", "sec-financial-statements"},
        }.get(source.get("connector_ref"), set()))
    for kind in sorted(kinds):
        # Current consumers require the corrected contracts for these two
        # capabilities.  Every other registered connector remains v1.
        version = 3 if kind in {"sec-company-facts", "sec-financial-statements"} else 1
        identity = (kind, version)
        if identity in seen:
            continue
        seen.add(identity)
        record = build_governance_record(
            kind, approved_by=actor_ref, status="approved",
            effective_from=now, version=version,
        )
        target = workspace.state_dir / "connector-governance" / f"{kind}-v{version}.json"
        _atomic_seed(target, record)
        created.append(target.name)
    return sorted(created), sorted(set(unsupported))


def _source_ref(source: Mapping[str, Any]) -> str:
    """Resolve a catalog profile to the packaged connector's source identity."""
    connector_ref = source.get("connector_ref")
    # Discovery uses the search capability, while downloaded pages retain the
    # public-web evidence identity. Both are needed by the existing engine.
    if connector_ref == "connector:gemini-web-search":
        return "source:web-search"
    connector_ref = {
        "connector:host-tool:company-wiki:get_document": "connector:company-wiki",
        "connector:host-tool:company-wiki:list_documents": "connector:company-wiki",
        "connector:host-tool:sales-notes:get_note": "connector:sales-notes",
        "connector:host-tool:sales-notes:list_notes": "connector:sales-notes",
    }.get(connector_ref, connector_ref)
    inventory = load_packaged_connector_inventory()["templates"]
    matches = [template["source_identity"]["source_ref"]
               for template in inventory.values()
               if template["connector_ref"] == connector_ref]
    if len(matches) != 1:
        raise WorkspaceError(
            f"shared connector {connector_ref!r} has no unique packaged source identity")
    return str(matches[0])


def _playbook_manifest() -> dict[str, Any]:
    candidates = (
        Path(sys.prefix) / "share/dalton-core/runtime-defaults/p9a-research-playbook-v1.json",
        Path(__file__).resolve().parents[2] / "deploy/phase9/p9a-research-playbook-v1.json",
    )
    for path in candidates:
        if path.is_file():
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("schema_version") != "0.1":
                raise WorkspaceError("packaged research playbook has an invalid schema")
            return value
    raise WorkspaceError("packaged research playbook is unavailable")


def _publish_playbook(workspace: Any, actor_ref: str) -> dict[str, str]:
    manifest = _playbook_manifest()
    values = {key: value for key, value in manifest.items()
              if key not in {"schema_version", "playbook_ref"}}
    values["actor_ref"] = actor_ref
    with DaltonStore(workspace.state_dir / "core.sqlite") as store:
        published = ResearchPlaybookAuthority(store).publish_playbook(
            manifest["playbook_ref"], **values)
    return {"ref": published["id"], "hash": published["content_hash"]}


def install(workspace_manifest: str | Path, *, actor_ref: str) -> dict[str, Any]:
    """Install idempotent, mission-neutral research foundations."""
    if not isinstance(actor_ref, str) or not actor_ref.startswith(OWNER_ACTOR_PREFIX) \
            or not actor_ref.removeprefix(OWNER_ACTOR_PREFIX).strip():
        raise WorkspaceError("runtime setup actor_ref must identify the workspace owner")
    workspace = load_workspace_manifest(workspace_manifest)
    config = ServiceConfig.from_file(workspace.config_path)
    if config.workspace is None or config.workspace.workspace_id != workspace.workspace_id:
        raise WorkspaceError("runtime setup requires a workspace-bound service config")
    if not (workspace.state_dir / "core.sqlite").is_file():
        raise WorkspaceError("bootstrap the workspace before runtime setup")

    defaults = {
        "market-proxy-mappings.json": {"schema_version": "0.1", "mappings": []},
        "tracking-policy.json": TRACKING_POLICY,
        **_awaiting_mission_output_policies(workspace.slug),
    }
    files = {name: _atomic_seed(workspace.state_dir / name, value)
             for name, value in defaults.items()}
    connectors, unsupported = _connector_records(workspace, actor_ref)
    debate_hash = content_hash(DEBATE_POLICY)
    conviction_hash = content_hash(CONVICTION_POLICY)
    playbook_template = {
        "template_ref": "research-playbook:team-analyst-manual",
        "publication_required": False,
    }
    catalog_sources = ((_catalog(workspace) or {}).get("sources", []))
    by_source = {_source_ref(source): source for source in catalog_sources}
    default_source_plan = [
        {"source_ref": source_ref, "role": "shared connected evidence source",
         "status": "connected"}
        for source_ref in sorted(by_source)
    ]
    playbook_binding = _publish_playbook(workspace, actor_ref)
    driver_pack = generic_driver_pack_template()
    foundation_body = {
        "schema_version": SCHEMA_VERSION,
        "workspace_id": workspace.workspace_id,
        "setup_state": "awaiting_mission",
        "methods": {
            "playbook": {**playbook_template, "binding": playbook_binding,
                         "content_hash": content_hash(playbook_template)},
            "driver_templates": {"registry_ref": DRIVER_TEMPLATES["registry_ref"],
                                 "content_hash": REGISTRY_HASH},
            "cost_driver_templates": {
                "registry_ref": COST_DRIVER_TEMPLATES["registry_ref"],
                "content_hash": COST_REGISTRY_HASH,
            },
            "debate_policy": {"policy_ref": DEBATE_POLICY["policy_ref"],
                              "content_hash": debate_hash},
            "conviction_policy": {"policy_ref": CONVICTION_POLICY["policy_ref"],
                                  "content_hash": conviction_hash},
            "tracking_policy": {
                "path": str(workspace.state_dir / "tracking-policy.json"),
                "policy_ref": TRACKING_POLICY["policy_ref"],
                "content_hash": content_hash(TRACKING_POLICY),
            },
            "constitution_method": {
                "value": CONSTITUTION_METHOD,
                "content_hash": content_hash(CONSTITUTION_METHOD),
            },
            "driver_pack_template": {
                "template_registry_ref": DRIVER_TEMPLATES["registry_ref"],
                "required_fields": ["industry_ref", "title", "drivers", "metric_specs",
                                    "thesis_templates"],
                "value": driver_pack,
                "content_hash": content_hash(driver_pack),
            },
        },
        "mission_defaults": {
            "source_capability_refs": sorted(
                source["capability_id"] for source in catalog_sources),
            "source_plan": default_source_plan,
            "deliverables": list(DELIVERABLE_KINDS),
            "autonomy": {
                "automation_principal": "automation:coverage-mission",
                "may_write": list(AUTOMATION_WRITE_SCOPES),
                "human_checkpoints": list(CHECKPOINT_KINDS),
            },
            "budget_ceilings": {
                "max_daily_paid_calls": 100,
                "max_daily_cost_usd": 100.0,
                "max_alphaengine_calls_24h": 50,
                "max_alphaengine_probe_calls_24h": 10,
            },
        },
        "mission_generated_files": [
            "constitution", "coverage-mission", "discovery-plans",
            "feed-plan", "ir-pages", "crowd-map",
        ],
        "setup_planning_budget": {
            # 2026-09-16: 6 -> 30. The allowance counts admissions, not
            # successes, and a first-goal attempt that dies on an external
            # error (a quota-walled gateway, a throttled provider) burns one
            # of them. Six proved to be a lockout: retries after a morning of
            # transport failures exhausted the day's setup allowance by lunch
            # and every further attempt read 超出费用或用量限制. Thirty still
            # bounds a runaway loop -- the $10 day cap binds regardless --
            # while letting a workspace actually start.
            "max_model_calls": 30, "max_input_tokens": 120000,
            "max_output_tokens": 24000, "max_cost_usd": 10.0,
        },
        "connector_governance_records": connectors,
        "unsupported_shared_capabilities": unsupported,
    }
    foundation = {**foundation_body, "content_hash": content_hash(foundation_body)}
    files["research-foundation.json"] = _atomic_seed(
        workspace.state_dir / "research-foundation.json", foundation)
    return {"status": "installed", "workspace_id": workspace.workspace_id,
            "setup_state": "awaiting_mission", "files": files,
            "connector_governance_records": connectors,
            "unsupported_shared_capabilities": unsupported,
            "research_state_copied": False, "legacy_approvals_copied": False}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--actor-ref", required=True)
    args = parser.parse_args(argv)
    print(json.dumps(install(args.manifest, actor_ref=args.actor_ref), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
