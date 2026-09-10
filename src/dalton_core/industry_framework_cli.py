"""P12e child: recompute the industry's table, draft the stalest parts, publish.

Out of process, like every lane that calls a model, because the writer abandons
a request after 30 seconds and a drafting call is allowed 180.

One run does seven things and stops:

1. check the grant before spending anything -- drafting and then being refused
   costs the mission real model calls;
2. **recompute the cross-company table**, which needs no model at all.  This is
   the step that makes the lane worth running today: the industry's Claim count
   is zero, so the prose has nothing new to say most weeks, while the table
   moves every time one of the five files;
3. decide, per unit, whether it can be drafted at all -- an untitled causal
   chain, an empty industry aspect and an absent driver pack are three
   different reasons, and none of them is a reason to write something;
4. draft the stalest few units, one bounded call each, refusing any reply that
   leaves its contract;
5. verify the whole draft once, with a model of a different family;
6. run Q1's ``industry-framework`` rubric and the Constitution's
   ``output_rubric`` over the assembled record;
7. publish -- where ADR-0008 decides whether this is a version at all.

Exit 0 when the run completed, including when it decided nothing needed doing.

``formal_authority_writes`` is always 0.  A framework is assembled *from*
Claims and filed statements and never writes one; the Ledger is not opened for
writing here.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .cockpit_model import CockpitModel
from .call_budget import resolve_call_budget, resolve_run_budget
from .coverage_mission import CoverageMissionAuthority
from .industry_framework import (
    CHARACTERISTIC_FIELDS,
    DEFAULT_COMPARISON_QUARTERS,
    STATIC_UNITS,
    WRITE_SCOPE,
    FrameworkStructureUnmapped,
    IndustryFrameworkAuthority,
    IndustryFrameworkError,
    assess_gaps,
    build_comparison,
    deliverable_sections,
    chain_titles,
    comparison_hash,
    comparison_material,
    causal_chain_hash,
    drivers_for_horizon,
    framework_artefact,
    load_policy,
    new_refs,
    output_rubric_findings,
    policy_hash,
    render_comparison,
    summarise_debates,
    unit_body,
    unit_slots,
    units_for,
)
from .industry_framework_draft import (
    MAX_COST_USD,
    MAX_INPUT_TOKENS,
    MAX_OUTPUT_TOKENS,
    MAX_RUN_COST_USD,
    MAX_UNITS_PER_RUN,
    TIMEOUT_SECONDS,
    draft_unit,
    independence,
    independence_precheck,
    material_rows,
    router_family_resolver,
    summarise_blocks,
    verify,
)
from .store import DaltonStore, canonical_json

SUMMARY_SCHEMA_VERSION = "0.1"
# The two deterministic checks a draft may not fail.  Everything else the
# rubric reports is recorded and read; these two are this layer's stop-loss.
HARD_CHECKS: tuple[str, ...] = ("numbers_without_refs", "new_version_cites_new_refs")
# Which company dossier sections are industry material.  Two, because they are
# the two the dossier itself takes from the Constitution's causal chain: a
# framework reading a company's ``management_and_capital_allocation`` section
# would be reading about a company, which is what the dossier is for.
DOSSIER_ASPECTS: tuple[str, ...] = ("demand_drivers", "supply_and_cost")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _write_owner_only(path: Path, value: Any) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(canonical_json(value) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def granted_scope(mission: Mapping[str, Any]) -> str | None:
    """The word that lets this run write, or None.

    ``deliverable`` and no fallback.  ``industry_framework`` has been in
    ``DELIVERABLE_KINDS`` since Phase 9, so the framework is a deliverable and
    the word that grants deliverables is the word that grants it.  A run that
    borrowed ``dossier`` would be writing one authority under another's licence.
    """

    may_write = set((mission.get("autonomy") or {}).get("may_write") or [])
    return WRITE_SCOPE if WRITE_SCOPE in may_write else None


def industry_claims(store: Any, industry_ref: str, *, limit: int = 60) -> list[dict[str, Any]]:
    """The canonical Claims whose subject is the industry, importance first.

    Named against ``query_company_research`` by design rather than by accident:
    the parameter is called ``company_ref`` and it filters ``subject_ref``, so
    an industry ref goes in the same door.  The extraction path that files a
    Claim against the industry subject is on another branch; until it lands
    this returns nothing on the live Core, and the lane says ``no_industry_claims``
    rather than inventing an industry view out of company Claims.
    """

    from .claim_aspect_vocabulary import INDUSTRY_ASPECT
    from .company_research_view import query_company_research

    try:
        rows = query_company_research(
            store, company_ref=industry_ref, index_aspect=INDUSTRY_ASPECT,
            canonical_only=True, limit=limit,
        )
    except Exception:  # noqa: BLE001 - a Core with no index answers nothing
        return []
    out = []
    for row in rows:
        if row.get("status") == "retired":
            continue
        out.append({
            "kind": "claim",
            "ref": str(row.get("claim_version_ref") or ""),
            "text": str(row.get("normalized_statement") or ""),
            "period": row.get("period") or row.get("as_of"),
            "importance": row.get("importance"),
        })
    return out


def dossier_material(store: Any, mission: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The demand and supply sections of each company's current dossier.

    The closest thing this system has to industry prose that a person has
    already accepted.  Carried as ``dossier_section`` refs rather than copied
    as Claims: what the framework cites is the *section*, and the section names
    its own Claims one door down.
    """

    from .company_dossier import CompanyDossierAuthority, section_body

    try:
        authority = CompanyDossierAuthority(store)
    except Exception:  # noqa: BLE001 - no dossier authority on this Core
        return []
    rows: list[dict[str, Any]] = []
    for member in mission.get("universe") or []:
        company_ref = str(member.get("company_ref"))
        try:
            record = authority.latest(company_ref)
        except Exception:  # noqa: BLE001
            continue
        if record is None:
            continue
        for section in record.get("sections") or []:
            if section["aspect"] not in DOSSIER_ASPECTS or section["status"] != "drafted":
                continue
            body = section_body(section)
            if not body:
                continue
            rows.append({
                "kind": "dossier_section",
                "ref": f"{record['id']}#{section['aspect']}",
                "text": f"{member.get('ticker')} {section['aspect']}：{body}",
                "period": None,
                "importance": "dossier",
            })
    return rows


def model_input_tables(missions: Any, mission: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One ModelInputTable per covered company that has a specification."""

    tables = []
    for member in mission.get("universe") or []:
        company_ref = str(member.get("company_ref"))
        try:
            spec = missions.latest_company_model_spec(company_ref)
        except Exception:  # noqa: BLE001 - a company with no spec is not an error
            spec = None
        if spec is None:
            continue
        try:
            tables.append(build_model_inputs_safely(missions, spec))
        except Exception:  # noqa: BLE001 - one company's join is not the run
            continue
    return [table for table in tables if table is not None]


def build_model_inputs_safely(missions: Any, spec: Mapping[str, Any]) -> dict[str, Any] | None:
    from .company_model_inputs import build_model_inputs

    return build_model_inputs(missions, spec)


def debate_map_version(store: Any, industry_ref: str) -> dict[str, Any] | None:
    """P12c's current industry map, or None when the authority is not open."""

    from .debate_map import DebateMapAuthority, table_exists

    if not table_exists(store.connection):
        return None
    try:
        return DebateMapAuthority(store).current(industry_ref)
    except Exception:  # noqa: BLE001 - no chain for this subject
        return None


def unavailable_unit(unit: str, reason: str, structure: Sequence[str] = ()) -> dict[str, Any]:
    """The empty shape of one unit, with the reason it is empty."""

    if unit == "characteristics":
        return {
            "status": "unavailable", "reason": reason,
            "values": {field: "insufficient_evidence" for field in CHARACTERISTIC_FIELDS},
            "structure": [], "slots": [], "sources": [], "gaps": [],
        }
    if unit in ("long_term_drivers", "short_term_drivers"):
        return {
            "horizon": "long_term" if unit == "long_term_drivers" else "short_term",
            "status": "unavailable", "reason": reason, "structure": list(structure),
            "stances": {}, "slots": [], "sources": [], "gaps": [],
        }
    return {
        "link_index": int(unit.split(":")[1]), "link": "", "title": "",
        "status": "unavailable", "reason": reason, "structure": list(structure),
        "slots": [], "sources": [], "gaps": [],
    }


def plan_units(
    *,
    store: Any,
    mission: Mapping[str, Any],
    constitution: Mapping[str, Any],
    policy: Mapping[str, Any],
    driver_pack: Mapping[str, Any],
    comparison: Mapping[str, Any],
    prior: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """What each unit could be drafted from, and whether it is worth drafting.

    "Stale" is deliberately blunt: a unit is stale when the material it would
    be shown is not the material the current version cites.  The comparison
    cells are part of that material, which is why a newly filed quarter makes
    the causal-chain sections stale without any Claim arriving -- and that is
    the only thing that will make this lane do anything at all until the
    industry Claim path lands.
    """

    industry_ref = str(mission["industry_ref"])
    claims = industry_claims(store, industry_ref)
    dossier_rows = dossier_material(store, mission)
    cells = comparison_material(comparison)
    chain = list((constitution.get("method") or {}).get("causal_chain") or [])
    titles = chain_titles(constitution, policy)
    prior_units = _prior_units(prior, chain_hash=causal_chain_hash(chain))

    plan: dict[str, Any] = {}
    for unit in units_for(constitution, policy):
        try:
            structure = unit_slots(unit, constitution=constitution, policy=policy,
                                   driver_pack=driver_pack)
        except FrameworkStructureUnmapped as exc:
            plan[unit] = {"status": "unavailable", "reason": "causal_chain_unmapped",
                          "detail": str(exc), "structure": (), "material": [],
                          "new_refs": 0, "stale": False}
            continue
        # Every unit is shown the same three tables. Which of them a unit can
        # actually use is the unit's own question, and the slot structure is
        # what asks it; splitting the material by unit here would be this
        # module deciding what a causal-chain link is allowed to be about.
        material = list(claims) + list(dossier_rows) + list(cells)
        if not material:
            plan[unit] = {"status": "unavailable", "reason": "no_industry_claims",
                          "detail": "no Claim, dossier section or comparison cell "
                                    "is available for this industry",
                          "structure": structure, "material": [], "new_refs": 0,
                          "stale": False}
            continue
        held = {row["ref"] for row in (prior_units.get(unit) or {}).get("sources") or []}
        fresh = sorted({row["ref"] for row in material} - held)
        plan[unit] = {
            "status": "draftable", "reason": None, "detail": None,
            "structure": structure, "material": material,
            "new_refs": len(fresh),
            "stale": bool(fresh) or unit not in prior_units,
            "link": chain[int(unit.split(":")[1])] if unit.startswith("causal_chain:") else "",
            "title": titles[int(unit.split(":")[1])] if unit.startswith("causal_chain:") else "",
        }
    return plan


def _prior_units(
    prior: Mapping[str, Any] | None, *, chain_hash: str | None = None
) -> dict[str, Any]:
    """The current version's parts, by unit -- minus any the chain invalidated.

    ``assemble`` stamps every section with the link text and title the
    Constitution and the policy give it *now*, including sections carried
    forward untouched. That is right while the chain is the same chain: it
    stops a version quoting a methodology nobody published. It is wrong the
    moment the chain is re-mapped, because then a carried-forward section would
    keep last version's prose under this version's heading -- the same words
    silently re-labelled as being about a different link.

    So when the prior version was written against a different causal chain, its
    ``causal_chain:*`` parts are not available to carry forward. They stay in
    their own version, readable by ``replay_link``; the new version drafts them
    again or records them as ``not_drafted_this_run``. The three blocks are not
    chain-shaped and carry forward as before.
    """

    if not prior:
        return {}
    held = ((prior.get("bindings") or {}).get("causal_chain_hash"))
    chain_changed = (
        chain_hash is not None and held is not None and held != chain_hash)
    out: dict[str, Any] = {}
    if not chain_changed:
        for section in prior.get("sections") or []:
            out[f"causal_chain:{section['link_index']}"] = section
    for unit, key in (("characteristics", "industry_characteristics"),
                      ("long_term_drivers", "long_term_drivers"),
                      ("short_term_drivers", "short_term_drivers")):
        block = prior.get(key)
        if block:
            out[unit] = block
    return out


def stale_units(
    plan: Mapping[str, Any], *, limit: int, revise: Sequence[str] = ()
) -> list[str]:
    """The few units worth a call this tick, stalest first, capped."""

    wanted = [unit for unit in plan
              if plan[unit]["status"] == "draftable"
              and (plan[unit]["stale"] or unit in revise)]
    wanted.sort(key=lambda unit: (-plan[unit]["new_refs"], unit))
    return wanted[:max(0, int(limit))]


def assemble(
    *,
    mission: Mapping[str, Any],
    blocks: Mapping[str, Any],
    plan: Mapping[str, Any],
    prior: Mapping[str, Any] | None,
    constitution: Mapping[str, Any],
    policy: Mapping[str, Any],
    driver_pack: Mapping[str, Any],
    comparison: Mapping[str, Any],
    debates: Mapping[str, Any],
    debate_version_ref: str | None,
    actor_ref: str,
    change_reason: str,
    drafted_at: str,
    evidence_refs: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Freshly drafted parts, carried-forward parts, the table and the gaps."""

    from .industry_framework import SOURCE_VERSION_KEY
    from .research_quality_rubrics import rubric as get_rubric

    chain = list((constitution.get("method") or {}).get("causal_chain") or [])
    titles = chain_titles(constitution, policy)
    prior_units = _prior_units(prior, chain_hash=causal_chain_hash(chain))

    def resolve(unit: str) -> dict[str, Any]:
        if unit in blocks:
            return dict(blocks[unit])
        held = prior_units.get(unit)
        if held is not None:
            return dict(held)
        entry = plan.get(unit) or {}
        return unavailable_unit(
            unit, entry.get("reason") or "not_drafted_this_run",
            [slot["slot_id"] for slot in entry.get("structure") or ()])

    sections = []
    for index, link in enumerate(chain):
        section = resolve(f"causal_chain:{index}")
        # The link text and its title come from the Constitution and the
        # policy every time the record is assembled, including for a section
        # carried forward: a version that kept an old link text beside a new
        # chain would be citing a methodology nobody published.
        section.update({"link_index": index, "link": link, "title": titles[index]})
        sections.append(section)

    long_block = resolve("long_term_drivers")
    short_block = resolve("short_term_drivers")
    rubric = get_rubric("industry_framework")
    return {
        SOURCE_VERSION_KEY: None if prior is None else prior["id"],
        "industry_ref": str(mission["industry_ref"]),
        "drafted_at": {
            **((prior or {}).get("drafted_at") or {}),
            **{unit: drafted_at for unit in blocks},
        },
        "sections": sections,
        "industry_characteristics": resolve("characteristics"),
        "long_term_drivers": long_block,
        "short_term_drivers": short_block,
        "cross_company_comparison": dict(comparison),
        "debates_summary": dict(debates),
        "gaps": framework_gaps(policy, mission=mission, long_term=long_block,
                               short_term=short_block),
        "bindings": {
            "constitution_version": {"ref": constitution["id"],
                                     "hash": constitution["content_hash"]},
            "playbook_version": {
                "ref": mission["bindings"]["playbook_version"]["ref"],
                "hash": mission["bindings"]["playbook_version"]["hash"]},
            "driver_pack_version": {"ref": driver_pack["id"],
                                    "hash": driver_pack["content_hash"]},
            "mission_version_ref": mission["id"],
            "debate_map_version_ref": debate_version_ref,
            "policy_ref": policy["policy_ref"],
            "policy_hash": policy_hash(policy),
            "causal_chain_hash": causal_chain_hash(chain),
            "rubric_ref": rubric.rubric_ref,
            "rubric_hash": rubric.content_hash,
            "comparison_hash": comparison_hash(comparison),
        },
        "actor_ref": actor_ref,
        "change_reason": change_reason,
        "evidence_refs": [dict(row) for row in evidence_refs],
    }


def publish_deliverable(
    store: Any,
    record: Mapping[str, Any],
    *,
    mission: Mapping[str, Any],
    actor_ref: str,
) -> dict[str, Any]:
    """Put the published version into the document chain a person reads.

    Every cognition-layer output enters the deliverable version chain; this is
    where P12e's does.  It is a *projection* of the record rather than a second
    drafting, so the two can never say different things, and it is deliberately
    not fatal: a framework version that was published and whose document
    rendering was refused is still a framework version, and the summary says
    which happened rather than losing the run.
    """

    from .mission_deliverable import (
        DELIVERABLE_KINDS,
        MissionDeliverableAuthority,
        MissionDeliverableError,
    )
    from .research_playbook import ResearchPlaybookAuthority

    if DELIVERABLE_KIND not in DELIVERABLE_KINDS:  # pragma: no cover - frozen
        return {"status": "unsupported", "reason": "industry_framework is not a kind"}
    try:
        playbook = ResearchPlaybookAuthority(store).playbook(
            mission["bindings"]["playbook_version"]["ref"])
    except Exception as exc:  # noqa: BLE001 - an unreadable playbook is not the run
        return {"status": "unavailable", "reason": f"{type(exc).__name__}: {exc}"}
    try:
        result = MissionDeliverableAuthority(store).publish(
            kind=DELIVERABLE_KIND,
            subject_ref=str(record["industry_ref"]),
            mission=mission,
            playbook=playbook,
            template_ref=str(record["bindings"]["policy_ref"]),
            sections=deliverable_sections(record),
            summary=(f"行业框架 v{record['version']}：因果链 "
                     f"{len(record['sections'])} 环、横向对比 "
                     f"{len(record['cross_company_comparison'].get('quarters') or ())} 季、"
                     f"未闭合缺口 "
                     f"{sum(1 for gap in record['gaps'] if gap['status'] != 'covered')} 条"),
            gaps=[f"{gap['gap_ref']}: {gap['what_is_missing']}"
                  for gap in record["gaps"] if gap["status"] != "covered"],
            actor_ref=actor_ref,
        )
    except MissionDeliverableError as exc:
        return {"status": "refused", "reason": f"{type(exc).__name__}: {exc}"}
    return {"status": "published", "deliverable_ref": result.get("deliverable_ref"),
            "version_ref": result.get("id"), "version": result.get("version")}


def framework_gaps(
    policy: Mapping[str, Any],
    *,
    mission: Mapping[str, Any],
    long_term: Mapping[str, Any] | None = None,
    short_term: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """The gap list, built the same way for the record and for its occasion.

    One function rather than two calls, because ``_fresh_evidence`` hashes this
    list to decide whether the world moved and ``assemble`` stores it: if the
    two ever built it differently, a version could be occasioned by a digest it
    does not carry.
    """

    return assess_gaps(policy, long_term=long_term, short_term=short_term,
                       mission=mission)


def rubric_gate(
    connection: Any, record: Mapping[str, Any], *, prior: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Q1's deterministic layer, run on the draft before it can be a version.

    One override, and this deliverable needs it more than the dossier does.
    Q1's ``new_version_cites_new_refs`` reads Claim refs, because that is what
    every artefact it grades cites.  A framework's new evidence is usually a
    newly filed accession in the computed table, and today it can be *only*
    that: the industry's Claim count is zero.  Without the override the lane
    would refuse every version it will ever produce until the industry Claim
    path lands.  ADR-0008's ``new_refs`` counts all the kinds; where the two
    disagree the broader one wins, and the summary records that it did.
    """

    from .industry_framework import body_hash
    from .research_quality_rubrics import rubric as get_rubric
    from .research_quality_score import run_deterministic

    digest = body_hash(record)
    art = framework_artefact(
        {**dict(record), "id": f"industry-framework-draft:{digest[:32]}",
         "content_hash": digest,
         "framework_ref": record.get("industry_ref")},
        prior=prior,
    )
    result = run_deterministic(art, get_rubric("industry_framework"), core=connection)
    failed = [name for name in result["failed_checks"] if name in HARD_CHECKS]
    overridden = []
    if "new_version_cites_new_refs" in failed and new_refs(record, prior):
        failed = [name for name in failed if name != "new_version_cites_new_refs"]
        overridden.append("new_version_cites_new_refs")
    return {
        "failed": failed,
        "summary": {
            "passed": result["passed"],
            "failed_checks": result["failed_checks"],
            "hard_failed": failed,
            "overridden_checks": overridden,
            "checks": {item["check"]: {"status": item["status"], "count": item["count"]}
                       for item in result["checks"]},
        },
    }


def run_framework(
    *,
    state_dir: Path,
    model_config_path: Path | None,
    summary_dir: Path,
    scheduler_db: Path | None = None,
    policy_path: Path | None = None,
    change_reason: str = "evidence_thicker",
    revise_units: Sequence[str] = (),
    max_units: int | None = None,
    quarters: int = DEFAULT_COMPARISON_QUARTERS,
    dry_run: bool = False,
    verifier_model_config_path: Path | None = None,
    model_factory: Callable[..., Any] | None = None,
    verifier_model_factory: Callable[..., Any] | None = None,
    family_resolver: Callable[[str | None], str | None] | None = None,
) -> dict[str, Any]:
    state_dir = Path(state_dir).expanduser().resolve()
    summary_dir = Path(summary_dir)
    summary_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    summary: dict[str, Any] = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "created_at": _now(),
        "status": "failed",
        "framework_status": None,
        "industry_ref": None,
        "write_scope": None,
        "comparison": None,
        "units_planned": {},
        "units_drafted": [],
        "refused": [],
        "verification": None,
        "rubric": None,
        "output_rubric_findings": [],
        "dropped_units": [],
        "open_gaps": [],
        "deliverable": None,
        "version_ref": None,
        "version_status": None,
        "new_refs": 0,
        "cost_micros": 0,
        "failure_reason": None,
        "formal_authority_writes": 0,
    }
    store = DaltonStore(str(state_dir / "core.sqlite"))
    try:
        missions = CoverageMissionAuthority(store)
        pointer = store.connection.execute(
            "SELECT mission_version_id FROM coverage_mission_pointer "
            "ORDER BY mission_ref LIMIT 1"
        ).fetchone()
        if pointer is None:
            summary.update({"status": "idle", "framework_status": "no_mission"})
            return summary
        mission = missions.mission(pointer["mission_version_id"])
        summary["industry_ref"] = str(mission["industry_ref"])
        scope = granted_scope(mission)
        if scope is None:
            summary.update({
                "status": "held", "framework_status": "not_authorized",
                "failure_reason": (
                    f"任务 {mission['id']} 还没有授予 {WRITE_SCOPE} 写入范围；"
                    "在授权前不起草，免得白花模型调用"),
            })
            return summary
        summary["write_scope"] = scope
        actor_ref = mission["autonomy"]["automation_principal"]

        from .research_constitution import ResearchConstitutionAuthority

        binding = mission["bindings"]["constitution_version"]
        constitution = ResearchConstitutionAuthority(store).constitution(binding["ref"])
        if constitution["content_hash"] != binding["hash"]:
            summary.update({"status": "failed", "framework_status": "binding_drift",
                            "failure_reason": "the mission's constitution binding drifted"})
            return summary
        pack_binding = constitution["bindings"]["driver_pack_version"]
        driver_pack = _driver_pack(store, pack_binding)
        if driver_pack is None:
            summary.update({"status": "failed", "framework_status": "no_driver_pack",
                            "failure_reason": "the Constitution's driver pack binding "
                                              "does not resolve on this Core"})
            return summary
        try:
            policy = load_policy(policy_path)
        except (OSError, ValueError) as exc:
            summary.update({
                "status": "held", "framework_status": "no_policy",
                "failure_reason": f"the framework policy could not be read: {exc}"})
            return summary

        authority = IndustryFrameworkAuthority(store)
        config: Mapping[str, Any] = {}
        if model_config_path is not None:
            config = json.loads(Path(model_config_path).expanduser().read_text(encoding="utf-8"))
        run_budget = resolve_run_budget(config, "industry_framework", defaults={
            "max_cost_usd": MAX_RUN_COST_USD, "max_units": MAX_UNITS_PER_RUN})
        max_units = int(max_units if max_units is not None
                        else run_budget.get("max_units", MAX_UNITS_PER_RUN))
        run_cost_micros = int(float(run_budget["max_cost_usd"]) * 1_000_000)
        prior = authority.latest(str(mission["industry_ref"]))

        # Step 2, before any model is even constructed. The table is the part
        # of this deliverable that needs no judgement, and it is the part that
        # actually moves week to week.
        tables = model_input_tables(missions, mission)
        comparison = build_comparison(tables, universe=mission["universe"],
                                      quarters=quarters)
        summary["comparison"] = {
            "status": comparison["status"],
            "reason": comparison.get("reason"),
            "quarters": list(comparison.get("quarters") or []),
            "companies": len(comparison.get("companies") or []),
            "computed_cells": sum(1 for cell in comparison.get("cells") or []
                                  if cell["status"] == "computed"),
            "unavailable_cells": sum(1 for cell in comparison.get("cells") or []
                                     if cell["status"] != "computed"),
            "comparison_hash": comparison_hash(comparison),
        }
        debate_record = debate_map_version(store, str(mission["industry_ref"]))
        debates = summarise_debates(debate_record)

        try:
            plan = plan_units(store=store, mission=mission, constitution=constitution,
                              policy=policy, driver_pack=driver_pack,
                              comparison=comparison, prior=prior)
        except FrameworkStructureUnmapped as exc:
            summary.update({"status": "held", "framework_status": "causal_chain_unmapped",
                            "failure_reason": str(exc)})
            return summary
        summary["units_planned"] = {
            unit: {"status": entry["status"], "reason": entry["reason"],
                   "new_refs": entry["new_refs"], "stale": entry["stale"],
                   "slots": len(entry["structure"])}
            for unit, entry in plan.items()
        }
        wanted = stale_units(plan, limit=max_units, revise=revise_units)
        if not wanted:
            summary.update({"status": "idle", "framework_status": "nothing_new",
                            "failure_reason": "no part of the framework has material "
                                              "the current version does not cite"})
            return summary
        if dry_run:
            summary.update({"status": "succeeded", "framework_status": "dry_run",
                            "units_drafted": wanted})
            return summary
        if model_config_path is None and model_factory is None:
            summary.update({"status": "succeeded", "framework_status": "gated",
                            "failure_reason": "no model configured", "units_drafted": []})
            return summary

        def build(settings: Mapping[str, Any]) -> Any:
            return CockpitModel(
                settings,
                scheduler_db=str(scheduler_db or (state_dir / "scheduler.sqlite")),
                max_input_tokens=MAX_INPUT_TOKENS, max_output_tokens=MAX_OUTPUT_TOKENS,
                max_cost_usd=MAX_COST_USD, timeout_seconds=TIMEOUT_SECONDS)

        factory = model_factory or (lambda: build(config))
        verifier_factory = verifier_model_factory
        if verifier_factory is None and verifier_model_config_path is not None:
            verifier_config = json.loads(
                Path(verifier_model_config_path).expanduser().read_text(encoding="utf-8"))
            verifier_factory = lambda: build(verifier_config)  # noqa: E731
        if verifier_factory is None:
            summary.update({
                "status": "held", "framework_status": "no_verifier",
                "failure_reason": ("no separate verifier model configuration is wired; "
                                   "a framework is published only on a verdict from a "
                                   "different model family (D2)")})
            return summary
        model = factory()
        producer_reserve = int(float(getattr(model, "budget_for", lambda p: {
            "max_cost_usd": MAX_COST_USD})("industry_framework")["max_cost_usd"]) * 1_000_000)
        verifier_budget = resolve_call_budget(
            verifier_config if verifier_model_config_path is not None else {},
            "industry_framework_verifier", defaults={"max_input_tokens": MAX_INPUT_TOKENS,
                "max_output_tokens": MAX_OUTPUT_TOKENS, "max_cost_usd": MAX_COST_USD,
                "timeout_seconds": TIMEOUT_SECONDS})
        verifier_reserve = int(float(verifier_budget["max_cost_usd"]) * 1_000_000)
        industry = {
            "industry_ref": str(mission["industry_ref"]),
            "tickers": [str(member.get("ticker")) for member in mission["universe"]],
        }
        table_text = render_comparison(comparison)
        prior_units = _prior_units(prior, chain_hash=causal_chain_hash(
            list((constitution.get("method") or {}).get("causal_chain") or [])))

        blocks: dict[str, Any] = {}
        draft_routes: list[str | None] = []
        spent = 0
        run_bound_blocked = False
        for unit in wanted:
            if spent + producer_reserve + verifier_reserve > run_cost_micros:
                run_bound_blocked = True
                summary["refused"].append({"unit": unit, "reason": "run cost bound reached"})
                continue
            entry = plan[unit]
            held = prior_units.get(unit)
            outcome = draft_unit(
                model, unit=unit, structure=entry["structure"],
                material=material_rows(entry["material"]), industry=industry,
                mission=mission, link=entry.get("link") or "",
                title=entry.get("title") or "",
                link_index=int(unit.split(":")[1]) if unit.startswith("causal_chain:") else 0,
                prior_body="" if held is None else unit_body(held),
                comparison_table=table_text,
            )
            spent += int((outcome.get("model") or {}).get("cost_micros") or 0)
            if outcome["status"] != "drafted":
                summary["refused"].append({"unit": unit, "reason": outcome["reason"]})
                continue
            blocks[unit] = outcome["block"]
            draft_routes.append((outcome.get("model") or {}).get("route_decision_ref"))
        summary["cost_micros"] = spent
        summary["units_drafted"] = sorted(blocks)
        if not blocks:
            summary.update({"status": "succeeded", "framework_status": (
                "unverified" if run_bound_blocked else "nothing_drafted")})
            return summary

        resolve_family = family_resolver or router_family_resolver(config)
        early = independence_precheck(draft_routes=draft_routes, resolve=resolve_family)
        if early is not None:
            summary["verification"] = {
                "status": "skipped", "verdict": None, "findings": [],
                "reason": "not attempted", "independent": False,
                "independence_reason": early["reason"], "verifier_family": None,
            }
            summary.update({"status": "succeeded", "framework_status": "not_independent"})
            return summary
        if spent + verifier_reserve > run_cost_micros:
            summary["verification"] = {
                "status": "skipped", "verdict": None, "findings": [],
                "reason": "the run's cost bound leaves no room for the verifier",
                "independent": False, "independence_reason": None,
                "verifier_family": None,
            }
            summary.update({"status": "succeeded", "framework_status": "unverified"})
            return summary
        verifier = verifier_factory()
        verdict = verify(
            verifier, blocks, industry=industry, mission=mission,
            producer_route_decision_refs=draft_routes,
        )
        spent += int((verdict.get("model") or {}).get("cost_micros") or 0)
        summary["cost_micros"] = spent
        check = independence(
            draft_routes=draft_routes,
            verifier_route=(verdict.get("model") or {}).get("route_decision_ref"),
            resolve=resolve_family,
        )
        summary["verification"] = {
            "status": verdict.get("status"), "verdict": verdict.get("verdict"),
            "findings": verdict.get("findings") or [],
            "reason": verdict.get("reason"),
            "independent": check["independent"],
            "independence_reason": check["reason"],
            "verifier_family": check["verifier_family"],
        }
        if verdict.get("status") != "verified" or verdict.get("verdict") != "pass":
            summary.update({"status": "succeeded",
                            "framework_status": "verification_failed"})
            return summary
        if not check["independent"]:
            summary.update({"status": "succeeded", "framework_status": "not_independent"})
            return summary

        stamped = _now()
        # The gap list the record will carry, built before the occasion is
        # decided: connecting a source moves this list and nothing else, and a
        # run that computed it after deciding "nothing occasioned this" would
        # have thrown away the one thing that did.
        planned_gaps = framework_gaps(
            policy, mission=mission,
            long_term=blocks.get("long_term_drivers") or (prior or {}).get("long_term_drivers"),
            short_term=blocks.get("short_term_drivers") or (prior or {}).get("short_term_drivers"),
        )
        evidence = _fresh_evidence(blocks, comparison, prior, planned_gaps)
        if not evidence:
            summary.update({"status": "succeeded", "framework_status": "no_new_evidence",
                            "failure_reason": "this draft cites nothing the current "
                                              "version does not; the table has not "
                                              "moved and no source changed status"})
            return summary
        record = assemble(
            mission=mission, blocks=blocks, plan=plan, prior=prior,
            constitution=constitution, policy=policy, driver_pack=driver_pack,
            comparison=comparison, debates=debates,
            debate_version_ref=None if debate_record is None else str(debate_record["id"]),
            actor_ref=actor_ref, change_reason=change_reason, drafted_at=stamped,
            evidence_refs=evidence,
        )
        gate = rubric_gate(store.connection, record, prior=prior)
        summary["rubric"] = gate["summary"]
        if gate["failed"]:
            summary.update({"status": "succeeded", "framework_status": "rubric_refused",
                            "failure_reason": "hard checks failed: "
                                              + ", ".join(gate["failed"])})
            return summary
        findings = output_rubric_findings(record, constitution=constitution,
                                          policy=policy, prior=prior)
        summary["output_rubric_findings"] = findings
        if findings:
            summary.update({"status": "succeeded",
                            "framework_status": "constitution_refused",
                            "failure_reason": "the Constitution's output_rubric was "
                                              "not satisfied"})
            return summary
        summary["new_refs"] = len(new_refs(record, prior))
        published = authority.publish(record)
        summary["open_gaps"] = [gap["gap_ref"] for gap in published.get("gaps") or []
                                if gap["status"] != "covered"]
        if published["status"] == "fresh":
            summary["deliverable"] = publish_deliverable(
                store, published, mission=mission, actor_ref=actor_ref)
        summary.update({
            "status": "succeeded",
            "framework_status": ("published" if published["status"] == "fresh"
                                 else "duplicate"),
            "version_ref": published["id"],
            "version_status": published["status"],
            "duplicate_reason": published.get("duplicate_reason"),
            **summarise_blocks(blocks),
        })
        return summary
    except IndustryFrameworkError as exc:
        summary["failure_reason"] = f"{type(exc).__name__}: {exc}"
        summary["status"] = "failed"
        return summary
    except Exception as exc:  # unexpected: record for the parent, then surface
        summary["failure_reason"] = f"unexpected {type(exc).__name__}: {exc}"
        raise
    finally:
        _write_owner_only(summary_dir / "summary.json", summary)
        store.close()


def _driver_pack(store: Any, binding: Mapping[str, str]) -> dict[str, Any] | None:
    row = store.connection.execute(
        "SELECT record_json,content_hash FROM driver_pack_versions WHERE version_id=?",
        (binding["ref"],),
    ).fetchone()
    if row is None or row["content_hash"] != binding["hash"]:
        return None
    return json.loads(row["record_json"])


def _fresh_evidence(
    blocks: Mapping[str, Any],
    comparison: Mapping[str, Any],
    prior: Mapping[str, Any] | None,
    gaps: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """The refs that occasioned this version: what the prior one did not rest on.

    Three kinds, and only the first is prose.  Comparison cells count because a
    quarter landing is the commonest reason this deliverable moves, and a
    version that could not name it as its occasion would have to claim it was
    occasioned by prose that did not change.  The gap state counts because
    connecting a source is the event this whole deliverable exists to provoke.
    """

    from .industry_framework import evidence_scope, gap_state_ref

    held = set() if prior is None else set(evidence_scope(prior))
    fresh: dict[str, dict[str, Any]] = {}
    for block in blocks.values():
        for row in block.get("sources") or []:
            if row["ref"] not in held:
                fresh[row["ref"]] = dict(row)
    for cell in comparison.get("cells") or []:
        for accession in cell.get("source_accessions") or ():
            if accession in held or accession in fresh:
                continue
            fresh[str(accession)] = {
                "kind": "comparison_cell", "ref": str(accession),
                "text": f"newly filed accession behind {cell['ref']}",
                "period": cell["quarter"],
            }
    if gaps:
        digest = gap_state_ref(gaps)
        if digest not in held:
            connected = sorted({
                row["slug"] for gap in gaps
                for row in gap.get("candidate_sources") or ()
                if row.get("connection_status") == "connected"
            })
            fresh[digest] = {
                "kind": "gap_state", "ref": digest,
                "text": ("the gap list's sources and their connection status changed; "
                         "connected today: " + (", ".join(connected) or "none")),
                "period": None,
            }
    return list(fresh.values())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--model-config", type=Path)
    parser.add_argument("--verifier-model-config", type=Path,
                        help="the configuration the independent verifier runs on. "
                             "Without it the run is held: one configuration routes "
                             "both calls the same way (D2).")
    parser.add_argument("--summary-dir", type=Path, help="defaults to the state dir")
    parser.add_argument("--scheduler-db", type=Path)
    parser.add_argument("--framework-policy", type=Path,
                        help="defaults to deploy/phase9/"
                             "p12e-industry-framework-policy-v1.json")
    parser.add_argument("--change-reason", default="evidence_thicker")
    parser.add_argument("--revise", action="append", default=[], dest="revise_units",
                        help="draft this part again even if nothing new arrived; "
                             "repeatable. The version still has to cite something "
                             "the current one does not.")
    parser.add_argument("--max-units", type=int, default=None)
    parser.add_argument("--quarters", type=int, default=DEFAULT_COMPARISON_QUARTERS)
    parser.add_argument("--dry-run", action="store_true",
                        help="plan and compute the table, then stop; no model call "
                             "and no write")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = run_framework(
        state_dir=args.state_dir, model_config_path=args.model_config,
        verifier_model_config_path=args.verifier_model_config,
        summary_dir=args.summary_dir if args.summary_dir is not None else args.state_dir,
        scheduler_db=args.scheduler_db, policy_path=args.framework_policy,
        change_reason=args.change_reason, revise_units=tuple(args.revise_units),
        max_units=args.max_units, quarters=args.quarters, dry_run=args.dry_run,
    )
    if not args.quiet:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=1))
    return 0 if summary["status"] in ("succeeded", "idle", "held") else 1


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())


__all__ = [
    "DOSSIER_ASPECTS",
    "HARD_CHECKS",
    "assemble",
    "build_parser",
    "debate_map_version",
    "dossier_material",
    "granted_scope",
    "industry_claims",
    "main",
    "framework_gaps",
    "model_input_tables",
    "plan_units",
    "publish_deliverable",
    "rubric_gate",
    "run_framework",
    "stale_units",
    "unavailable_unit",
]
