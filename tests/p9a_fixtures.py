"""Shared fixtures for the P9a playbook + coverage mission tests and canary."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from dalton_core.agenda import AgendaStore
from dalton_core.coverage_admission import CoverageAdmissionAuthority
from dalton_core.research_constitution import ResearchConstitutionAuthority
from dalton_core.research_playbook import ResearchPlaybookAuthority
from dalton_core.store import DaltonStore


ROOT = Path(__file__).resolve().parents[1]
PLAYBOOK_MANIFEST = ROOT / "deploy/phase9/p9a-research-playbook-v1.json"
MISSION_MANIFEST = ROOT / "deploy/phase9/p9a-us-it-services-mission-v1.json"
INDUSTRY = "industry:us-it-services"
OWNER = "human:coverage-owner"


def load_playbook_manifest() -> dict[str, Any]:
    manifest = json.loads(PLAYBOOK_MANIFEST.read_text(encoding="utf-8"))
    assert manifest.pop("schema_version") == "0.1"
    return manifest


def load_mission_manifest() -> dict[str, Any]:
    manifest = json.loads(MISSION_MANIFEST.read_text(encoding="utf-8"))
    assert manifest.pop("schema_version") == "0.1"
    return manifest


def playbook_params(*, actor_ref: str = OWNER) -> dict[str, Any]:
    params = load_playbook_manifest()
    params["actor_ref"] = actor_ref
    return params


def constitution_method() -> dict[str, Any]:
    return {
        "question_admission": ["A question must change a Thesis or driver view."],
        "causal_chain": ["Bookings lead revenue by two to four quarters."],
        "source_standards": {
            "hierarchy": ["SEC filings are the primary numeric authority."],
            "conflict_adjudication": ["GAAP filing numbers override transcripts."],
            "minimum_independent_sources": 1,
        },
        "falsification": {
            "required_falsifier_searches": ["Search for bookings contraction."],
            "alternative_explanations": ["FX translation masquerading as inflection."],
        },
        "materiality": ["State magnitude in revenue-growth points."],
        "lifecycle": {
            "continue_when": ["The brief changes a Thesis or driver view."],
            "refresh_when": ["Evidence exceeds its freshness window."],
            "stop_when": ["All falsifiers resolved."],
            "escalate_when": ["A major Thesis change is proposed."],
        },
        "output_rubric": {
            "criteria": ["State what changed and its impact."],
            "good_samples": [],
            "bad_samples": [],
        },
    }


def bootstrap_method_authorities(
    store: DaltonStore,
    *,
    actor_ref: str = OWNER,
    industry_ref: str = INDUSTRY,
    mandate_ref: str = "mandate:us-it-services-constitution-p8a",
) -> dict[str, Any]:
    """Create mandate, driver pack, constitution and playbook in one Core."""

    agenda = AgendaStore(store)
    admission = CoverageAdmissionAuthority(store)
    constitution = ResearchConstitutionAuthority(store)
    playbook = ResearchPlaybookAuthority(store)

    mandate = agenda.create_mandate(
        mandate_ref,
        actor_ref=actor_ref,
        objective="Establish US IT Services coverage.",
        scope_refs=[industry_ref],
        constraints={},
        success_criteria={},
        effective_from="2026-08-23T00:00:00+00:00",
        effective_until=None,
        version_id=f"mandate-version:{mandate_ref.split(':', 1)[1]}:1",
        idempotency_key=f"{mandate_ref}:1",
    )
    pack = admission.register_driver_pack(
        "driver-pack:us-it-services",
        actor_ref=actor_ref,
        industry_ref=industry_ref,
        title="US IT Services Driver Pack",
        drivers=[{
            "driver_ref": "driver:d", "label": "D", "mechanism": "m",
            "metric_refs": ["metric:x"],
        }],
        metric_specs=[{
            "metric_ref": "metric:x", "label": "X", "definition": "d", "unit": "USD",
            "periodicity": "quarterly", "preferred_source_refs": ["source:sec-edgar"],
            "verification_kind": "numeric", "caveats": [],
        }],
        thesis_templates=[{
            "template_ref": "template:x", "statement": "s", "mechanism": "m",
            "driver_refs": ["driver:d"], "implied_expectation": "e",
            "falsifier_refs": ["falsifier:x"],
        }],
        version_id="driver-pack-version:us-it-services:1",
        prior_version_ref=None,
        idempotency_key="driver-pack:us-it-services:1",
    )
    policy = store.active_policy_version()
    published_constitution = constitution.publish_constitution(
        "constitution:us-it-services",
        industry_ref=industry_ref,
        title="US IT Services Research Constitution v1",
        bindings={
            "mandate_version": {"ref": mandate["id"], "hash": mandate["content_hash"]},
            "driver_pack_version": {"ref": pack["id"], "hash": pack["content_hash"]},
            "governance_policy_version": {"ref": policy.id, "hash": policy.content_hash},
            "doctrine_pack_version": None,
            "weekly_brief_plan": None,
        },
        method=constitution_method(),
        actor_ref=actor_ref,
        version_id="constitution-version:us-it-services:1",
        prior_version_ref=None,
        idempotency_key="constitution:us-it-services:1",
    )
    params = playbook_params(actor_ref=actor_ref)
    playbook_ref = params.pop("playbook_ref")
    published_playbook = playbook.publish_playbook(playbook_ref, **params)
    return {
        "mandate": mandate,
        "pack": pack,
        "constitution": published_constitution,
        "playbook": published_playbook,
        "agenda": agenda,
        "admission": admission,
        "constitution_authority": constitution,
        "playbook_authority": playbook,
    }


def mission_params(state: dict[str, Any], *, actor_ref: str = OWNER) -> dict[str, Any]:
    manifest = load_mission_manifest()
    for key in ("playbook_ref", "constitution_ref", "mandate_ref"):
        manifest.pop(key)
    manifest["bindings"] = {
        "playbook_version": {"ref": state["playbook"]["id"], "hash": state["playbook"]["content_hash"]},
        "constitution_version": {
            "ref": state["constitution"]["id"], "hash": state["constitution"]["content_hash"],
        },
        "mandate_version": {"ref": state["mandate"]["id"], "hash": state["mandate"]["content_hash"]},
    }
    manifest["actor_ref"] = actor_ref
    return manifest
