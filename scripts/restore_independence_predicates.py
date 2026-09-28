#!/usr/bin/env python3
"""Restore the producer/verifier independence predicate a budget edit dropped.

A fresh Core installs ``dalton_core.policy.DEFAULT_POLICY`` as ``policy-1``
(``DaltonStore._ensure_default_policy``), and that policy carries exactly one
independence predicate:

    {"left_path": "producer.model_family", "operator": "ne",
     "right_path": "verifier.model_family"}

A cockpit budget edit published ``policy`` without the predicates that
``GovernancePolicyVersion.to_dict`` keeps *beside* it, so the gate silently
went empty: legacy from ``policy-14``, ws-7d from ``policy-2``.  The edit was
fixed in b6bc016a; the parity check has reported the drift since.

This script publishes the next policy version with ``independence_predicates``
set back to the fresh-Core default -- read from ``DEFAULT_POLICY``, never typed
here -- and every other field byte for byte what the active policy holds, then
rebinds the constitution and the mission to it (the same policy ->
constitution -> mission cascade as ``sign_research_plan_auto_start.py``).  The
mandate does not move.

Before anything is published, the dry-run says who the predicate would bite:

* ``gate_consumers`` -- every Core path that evaluates the predicate (thesis
  commit, claim adjudication, capability evaluation, thesis-impact
  verification) with how many records it holds and how many of those pairs
  would fail the restored predicate;
* ``route_check`` -- per producer/verifier stage pair, the live chain families
  on both sides.  ``policy_gated`` pairs are the ones the predicate applies to
  (thesis impact); a verifier chain sharing a family with its producer chain
  there is listed under ``affected_routes``.  ``router_enforced`` pairs
  (claim support, document verifiers, deliverable verifiers) are *not* gated
  by the policy -- the model router's own family filter skips a same-family
  verifier link -- and are listed for information.

Usage
-----

    PY=.venv/bin/python
    S="$HOME/Library/Application Support/Dalton/state/dalton-core"   # or a workspace

    # read-only: what would be published, and who it would affect
    $PY scripts/restore_independence_predicates.py --state-dir "$S"

    # the whole cascade on a copy of the Core
    $PY scripts/restore_independence_predicates.py --state-dir "$S" \\
        --rehearse /tmp/independence-rehearsal --actor human:owner

    # for real, through that environment's writer as its ephemeral human
    $PY scripts/restore_independence_predicates.py --state-dir "$S" --apply --actor human:owner

``--routing-state-dir`` points the route check at another state directory's
model configurations (read-only), for a dry-run against a copied Core.

A second run after ``--apply`` reports ``already-restored``.  If a writer call
came back unavailable after the policy landed, the next dry-run reports
``would-resume-cascade`` and ``--apply`` publishes only the constitution and
mission that still bind the older policy.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from dalton_core.policy import DEFAULT_POLICY, canonical_policy, predicate_holds  # noqa: E402
from dalton_core.store import content_hash  # noqa: E402
from scripts.publish_extraction_authority_chain import BODY_FIELDS, _ref_hash  # noqa: E402
from scripts.sign_auto_commit_rules import (  # noqa: E402
    PlanError,
    _next_version_id,
    read_current,
)

FIELD = "independence_predicates"

#: The fresh-Core default, canonicalised exactly as the store stores it.
DEFAULT_PREDICATES: list[dict[str, Any]] = canonical_policy(DEFAULT_POLICY)[FIELD]

#: Stage pairs whose producer/verifier invocations the governance policy's
#: predicates are evaluated on (``thesis_impact.ThesisImpactAuthority``).
POLICY_GATED_PAIRS: tuple[tuple[str, str], ...] = (
    ("thesis_impact_assessment", "thesis_impact_verifier"),
)
#: Verifier stages whose producer is not ``<verifier minus _verifier>``.
_PRODUCER_OF: dict[str, str] = {
    "claim_support_verifier": "document_extraction",
    "claim_support_backfill": "document_extraction",
    "registered_annual_report_verifier": "registered_annual_report_draft",
    "mission_directed_document_verifier": "mission_directed_document_draft",
}
#: Claim support runs on the extraction lane's own model configuration
#: (``claim_support_verification``: "the extraction lane's own model
#: configuration: its pinned routing policy").
_BINDING_OF: dict[str, str] = {
    "claim_support_verifier": "document_extraction",
    "claim_support_backfill": "document_extraction",
}
RETRYABLE_CODES = frozenset({"transport_error", "unavailable", "timeout", "busy"})


# ---------------------------------------------------------------------------
# What the policy holds, and what it would hold.


def _policy_body(policy_wire: Mapping[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(dict(policy_wire["policy"] if "policy" in policy_wire else policy_wire))


def _predicates(body: Mapping[str, Any]) -> list[dict[str, Any]]:
    return canonical_policy(body)[FIELD]


def restored_body(body: Mapping[str, Any]) -> dict[str, Any]:
    """``body`` with the default predicates and nothing else changed."""

    after = copy.deepcopy(dict(body))
    after[FIELD] = copy.deepcopy(DEFAULT_PREDICATES)
    canonical_policy(after)  # the store would refuse anything this refuses
    if {k: v for k, v in after.items() if k != FIELD} != {
            k: v for k, v in body.items() if k != FIELD}:
        raise PlanError("internal: the restored policy differs outside independence_predicates")
    return after


def _check_restorable(body: Mapping[str, Any]) -> None:
    held = _predicates(body)
    if held and held != DEFAULT_PREDICATES:
        # Someone chose a different gate on purpose; replacing it is theirs.
        raise PlanError(
            "the active policy carries independence predicates that are not the "
            f"fresh-Core default: {held}; this script only restores an empty list")


def _row(connection: sqlite3.Connection, sql: str, args: tuple[Any, ...]) -> Any:
    connection.row_factory = sqlite3.Row
    return connection.execute(sql, args).fetchone()


def _policy_hash(connection: sqlite3.Connection, policy_id: str) -> str:
    return _row(connection, "SELECT content_hash FROM governance_policy_versions "
                "WHERE policy_version_id=?", (policy_id,))["content_hash"]


def _cascade_state(current: Mapping[str, Any]) -> str:
    """``restore`` | ``resume`` | ``done`` for the active chain."""

    body = _policy_body(current["policy"])
    _check_restorable(body)
    restored = _predicates(body) == DEFAULT_PREDICATES
    bound = (current["constitution"]["bindings"]["governance_policy_version"]["ref"]
             == current["policy_id"])
    if not restored:
        return "restore"
    return "done" if bound else "resume"


def build_chain(current: Mapping[str, Any], *, now: str, policy_hash: str) -> dict[str, Any]:
    """The publishes still needed: policy (unless it landed), constitution, mission."""

    state = _cascade_state(current)
    if state == "done":
        raise PlanError("the active policy already carries the default independence "
                        "predicates and the constitution binds it")
    body = _policy_body(current["policy"])
    constitution = current["constitution"]
    mission = current["mission"]
    constitution_version = int(constitution["version"]) + 1
    mission_version = int(mission["version"]) + 1
    stamp = content_hash(DEFAULT_PREDICATES)[:12]
    policy: dict[str, Any] | None = None
    if state == "restore":
        version = current["policy_version_number"] + 1
        policy = {
            "policy": restored_body(body),
            "policy_version_id": _next_version_id(current["policy_id"], version),
            "version_number": version,
            "activate": True,
            "policy_ref": current["policy_ref"],
            "effective_from": now,
            "effective_until": None,
            "prior_version_ref": current["policy_id"],
            "change_reason": (
                "restore the fresh-Core independence predicate "
                "producer.model_family != verifier.model_family that a cockpit budget "
                f"edit dropped (active since {current['policy_id']} without it); every "
                "other field of this policy is unchanged and the constitution and "
                "mission are rebound to this policy version only"),
            "content_hash_value": None,
        }
    return {
        "state": state,
        "before": _predicates(body),
        "after": copy.deepcopy(DEFAULT_PREDICATES),
        "policy": policy,
        "active_policy": {"ref": current["policy_id"], "hash": policy_hash},
        "constitution": {
            "constitution_ref": constitution["constitution_ref"],
            "industry_ref": constitution["industry_ref"],
            "title": constitution["title"],
            "bindings": json.loads(json.dumps(constitution["bindings"])),
            "method": json.loads(json.dumps(constitution["method"])),
            "version_id": _next_version_id(constitution["id"], constitution_version),
            "prior_version_ref": constitution["id"],
            "idempotency_key": (
                f"{constitution['constitution_ref']}:{constitution_version}"
                f":independence-predicates:{stamp}"),
        },
        "mission": {
            "mission_ref": mission["mission_ref"],
            **{field: json.loads(json.dumps(mission[field])) for field in BODY_FIELDS},
            "version_id": _next_version_id(mission["id"], mission_version),
            "prior_version_ref": mission["id"],
            "idempotency_key": (
                f"{mission['mission_ref']}:{mission_version}:independence-predicates:{stamp}"),
        },
    }


def publish_cascade(chain: Mapping[str, Any],
                    apply: Callable[[str, dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
    """policy (when needed) -> constitution -> mission, each bound to the last."""

    out: dict[str, Any] = {}
    if chain["policy"] is not None:
        record = apply("create_policy", copy.deepcopy(chain["policy"]))
        policy_ref, policy_hash = _ref_hash(record, chain["policy"]["policy_version_id"])
        out["policy"] = {"ref": policy_ref, "hash": policy_hash}
    else:
        policy_ref, policy_hash = chain["active_policy"]["ref"], chain["active_policy"]["hash"]
        out["policy"] = {"ref": policy_ref, "hash": policy_hash, "status": "already-published"}
    mandate = chain["mission"]["bindings"]["mandate_version"]
    out["mandate"] = {"ref": mandate["ref"], "hash": mandate["hash"], "status": "unchanged"}
    constitution_params = json.loads(json.dumps(chain["constitution"]))
    constitution_params["bindings"]["governance_policy_version"] = {
        "ref": policy_ref, "hash": policy_hash}
    record = apply("publish_research_constitution", constitution_params)
    constitution_ref, constitution_hash = _ref_hash(record, constitution_params["version_id"])
    out["constitution"] = {"ref": constitution_ref, "hash": constitution_hash}
    mission_params = json.loads(json.dumps(chain["mission"]))
    mission_params["bindings"]["constitution_version"] = {
        "ref": constitution_ref, "hash": constitution_hash}
    record = apply("create_coverage_mission", mission_params)
    mission_ref, mission_hash = _ref_hash(record, mission_params["version_id"])
    out["mission"] = {"ref": mission_ref, "hash": mission_hash}
    return out


# ---------------------------------------------------------------------------
# Who the predicate would bite.


def gate_consumers(connection: sqlite3.Connection,
                   predicates: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Every record kind the predicate is evaluated on, and how many would fail."""

    predicates = DEFAULT_PREDICATES if predicates is None else predicates
    connection.row_factory = sqlite3.Row

    def invocation(ref: Any) -> dict[str, Any] | None:
        if not ref:
            return None
        row = connection.execute(
            "SELECT * FROM model_invocations WHERE invocation_id=?", (str(ref),)).fetchone()
        return None if row is None else dict(row)

    def tally(pairs: list[tuple[Any, Any]]) -> dict[str, Any]:
        failing: dict[str, int] = {}
        for producer_ref, verifier_ref in pairs:
            producer, verifier = invocation(producer_ref), invocation(verifier_ref)
            if producer is None or verifier is None:
                continue
            if not all(predicate_holds(p, producer, verifier) for p in predicates):
                key = f"{producer.get('model_family')} -> {verifier.get('model_family')}"
                failing[key] = failing.get(key, 0) + 1
        return {"records": len(pairs), "would_fail": sum(failing.values()),
                "failing_family_pairs": failing}

    def rows(sql: str) -> list[sqlite3.Row]:
        try:
            return connection.execute(sql).fetchall()
        except sqlite3.OperationalError:  # table absent in an older Core
            return []

    adjudications: list[tuple[Any, Any]] = []
    for row in rows("SELECT adjudicator_invocation_id, adjudication_json FROM adjudication_versions"):
        try:
            subjects = json.loads(row["adjudication_json"]).get("subject_invocation_refs") or []
        except (TypeError, ValueError, AttributeError):
            subjects = []
        adjudications += [(subject, row["adjudicator_invocation_id"]) for subject in subjects]
    return {
        "thesis_commit": tally([
            (r["producer_invocation_id"], r["verifier_invocation_id"])
            for r in rows("SELECT producer_invocation_id, verifier_invocation_id "
                          "FROM verification_records")]),
        "claim_adjudication": tally(adjudications),
        "capability_evaluation": tally([
            (r["builder_invocation_id"], r["evaluator_invocation_id"])
            for r in rows("SELECT builder_invocation_id, evaluator_invocation_id "
                          "FROM capability_evaluations")]),
        "thesis_impact_verification": tally([
            (r["producer_invocation_ref"], r["verifier_invocation_ref"])
            for r in rows("SELECT a.producer_invocation_ref, v.verifier_invocation_ref "
                          "FROM thesis_impact_verifications v JOIN thesis_impact_assessments a "
                          "ON a.assessment_id=v.assessment_ref")]),
    }


def _stage_chains(state_dir: Path) -> dict[str, dict[str, Any]]:
    """purpose -> the live chain (profile, family) its pinned policy runs today."""

    from dalton_core.model_fallback_chain import effective_chain, profile_families, purpose_tiers
    from dalton_core.model_router import ModelRouter, live_links
    from dalton_core.model_selection import purpose_policy_bindings

    bindings = purpose_policy_bindings(state_dir)
    tiers = purpose_tiers()
    routers: dict[str, Any] = {}
    out: dict[str, dict[str, Any]] = {}
    try:
        for purpose in sorted(set(bindings) | set(tiers)):
            binding = bindings.get(_BINDING_OF.get(purpose, purpose)) or {}
            ref, db = binding.get("policy_version_ref"), binding.get("model_router_db")
            if not ref or not db or not Path(str(db)).is_file():
                out[purpose] = {"status": "unconfigured", "chain": []}
                continue
            if db not in routers:
                routers[db] = ModelRouter(str(db), read_only=True)
            router = routers[db]
            held = profile_families(router)
            try:
                resolved = effective_chain(router.get_policy(ref), purpose,
                                           tier=tiers.get(purpose), profiles=held)
            except Exception as exc:  # noqa: BLE001 - reported, not raised
                out[purpose] = {"status": f"unreadable: {type(exc).__name__}: {exc}",
                                "chain": [], "policy_version_ref": ref}
                continue
            out[purpose] = {
                "status": "configured", "policy_version_ref": ref,
                "chain": [{"profile_id": p, "family": held.get(p, {}).get("family")}
                          for p in live_links(resolved["chain"], held)],
            }
    finally:
        for router in routers.values():
            router.close()
    return out


def route_check(state_dir: Path) -> dict[str, Any]:
    """Per producer/verifier stage pair: would the predicate hold on these routes?"""

    try:
        chains = _stage_chains(state_dir)
    except Exception as exc:  # noqa: BLE001 - the dry-run says so instead of crashing
        return {"status": f"unreadable: {type(exc).__name__}: {exc}", "policy_gated": [],
                "router_enforced": [], "affected_routes": []}
    pairs = list(POLICY_GATED_PAIRS)
    for purpose in sorted(chains):
        if purpose.endswith("_verifier") or purpose in _PRODUCER_OF:
            producer = _PRODUCER_OF.get(purpose, purpose[: -len("_verifier")])
            if producer in chains and (producer, purpose) not in pairs:
                pairs.append((producer, purpose))
    gated, enforced, affected = [], [], []
    for producer, verifier in pairs:
        p, v = chains.get(producer) or {}, chains.get(verifier) or {}
        p_families = [link["family"] for link in p.get("chain") or []]
        v_families = [link["family"] for link in v.get("chain") or []]
        shared = sorted({f for f in v_families if f in p_families})
        row = {
            "producer": producer, "verifier": verifier,
            "producer_families": p_families, "verifier_families": v_families,
            "shared_families": shared,
        }
        is_gated = (producer, verifier) in POLICY_GATED_PAIRS
        if not p_families or not v_families:
            row["verdict"] = "unconfigured"
        elif shared and not is_gated:
            # Not the policy's gate: the router's family filter skips these
            # verifier links whenever the producer was served by that family.
            row["verdict"] = "router-enforced: same-family verifier links are skipped"
        elif shared:
            first_same = p_families[0] == v_families[0]
            row["verdict"] = ("violates: first links share a family" if first_same
                              else "at-risk: a fallback link shares a family")
        else:
            row["verdict"] = "independent"
        if is_gated:
            gated.append(row)
            if shared:
                affected.append(row)
        else:
            enforced.append(row)
    return {"status": "ok", "policy_gated": gated, "router_enforced": enforced,
            "affected_routes": affected}


# ---------------------------------------------------------------------------
# The three modes.


def _read_state(state_dir: Path, *, mission_ref: str | None) -> tuple[dict[str, Any], str]:
    connection = sqlite3.connect(f"file:{state_dir / 'core.sqlite'}?mode=ro", uri=True)
    try:
        current = read_current(connection, mission_ref=mission_ref)
        return current, _policy_hash(connection, current["policy_id"])
    finally:
        connection.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def plan(state_dir: Path, *, mission_ref: str | None = None,
         routing_state_dir: Path | None = None) -> dict[str, Any]:
    """Read-only: exactly what would be published, and who it would affect."""

    current, policy_hash = _read_state(state_dir, mission_ref=mission_ref)
    connection = sqlite3.connect(f"file:{state_dir / 'core.sqlite'}?mode=ro", uri=True)
    try:
        consumers = gate_consumers(connection)
    finally:
        connection.close()
    routes = route_check(routing_state_dir or state_dir)
    base = {
        "mode": "dry-run",
        "state_dir": str(state_dir),
        "mission_ref": current["mission_ref"],
        "active_mission": current["mission"]["id"],
        "active_constitution": current["constitution"]["id"],
        "constitution_binds_policy":
            current["constitution"]["bindings"]["governance_policy_version"]["ref"],
        "active_mandate": current["mandate"]["id"],
        "active_policy": current["policy_id"],
        "default_predicates": DEFAULT_PREDICATES,
        "default_source": "dalton_core.policy.DEFAULT_POLICY (policy-1 of a fresh Core)",
        "gate_consumers": consumers,
        "route_check": routes,
        "affected_routes": routes["affected_routes"],
        "historical_pairs_that_would_fail": sum(
            item["would_fail"] for item in consumers.values()),
    }
    state = _cascade_state(current)
    if state == "done":
        return {**base, "status": "already-restored", "publishes": [],
                "diff": {f"policy.{FIELD}": {"before": DEFAULT_PREDICATES,
                                             "after": DEFAULT_PREDICATES}}}
    chain = build_chain(current, now=_now(), policy_hash=policy_hash)
    publishes = ([chain["policy"]["policy_version_id"]] if chain["policy"] else []) + [
        chain["constitution"]["version_id"], chain["mission"]["version_id"]]
    return {
        **base,
        "status": "would-publish" if state == "restore" else "would-resume-cascade",
        "next_policy": (chain["policy"]["policy_version_id"] if chain["policy"]
                        else current["policy_id"]),
        "next_constitution": chain["constitution"]["version_id"],
        "next_mission": chain["mission"]["version_id"],
        "change_reason": chain["policy"]["change_reason"] if chain["policy"] else None,
        "diff": {f"policy.{FIELD}": {"before": chain["before"], "after": chain["after"]}},
        "other_policy_fields_unchanged": True,
        "publishes": publishes,
    }


def rehearse(state_dir: Path, target: Path, *, actor: str, mission_ref: str | None = None,
             routing_state_dir: Path | None = None) -> dict[str, Any]:
    """Apply the cascade on a copy of the Core; the source is only read."""

    from dalton_core.coverage_mission import CoverageMissionAuthority
    from dalton_core.policy import evaluate_gate
    from dalton_core.research_constitution import ResearchConstitutionAuthority
    from dalton_core.store import DaltonStore
    from dalton_core.workspace_governance_baseline import governance_baseline_checks

    target.mkdir(parents=True, exist_ok=True)
    if (target / "core.sqlite").exists():
        raise PlanError(f"{target / 'core.sqlite'} already exists; rehearse into an empty directory")
    source = sqlite3.connect(f"file:{state_dir / 'core.sqlite'}?mode=ro", uri=True)
    destination = sqlite3.connect(str(target / "core.sqlite"))
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    store = DaltonStore(str(target / "core.sqlite"))
    try:
        current = read_current(store.connection, mission_ref=mission_ref)
        before_body = _policy_body(current["policy"])
        chain = build_chain(current, now=_now(),
                            policy_hash=_policy_hash(store.connection, current["policy_id"]))
        constitutions = ResearchConstitutionAuthority(store)
        missions = CoverageMissionAuthority(store)

        def apply(operation: str, params: dict[str, Any]) -> dict[str, Any]:
            values = dict(params)
            if operation == "create_policy":
                policy = values.pop("policy")
                return {"policy_version": store.create_policy(policy, actor_ref=actor, **values)}
            if operation == "publish_research_constitution":
                return constitutions.publish_constitution(
                    values.pop("constitution_ref"), actor_ref=actor, **values)
            if operation == "create_coverage_mission":
                return missions.create_mission(values.pop("mission_ref"), actor_ref=actor, **values)
            raise PlanError(operation)

        result = publish_cascade(chain, apply)
        after = read_current(store.connection, mission_ref=mission_ref)
        after_body = _policy_body(after["policy"])
        version = store.active_policy_version().to_dict()
        row = next(item for item in governance_baseline_checks(version, mission=None)
                   if item["check"] == f"policy.{FIELD}")
        # The gate itself, on the families the routes carry.
        gate = {
            "same_family": evaluate_gate(after_body, "pass",
                                         {"id": "p", "model_family": "deepseek-v4"},
                                         {"id": "v", "model_family": "deepseek-v4"}),
            "different_family": evaluate_gate(after_body, "pass",
                                              {"id": "p", "model_family": "deepseek-v4"},
                                              {"id": "v", "model_family": "google-gemini-3"}),
        }
        return {
            "mode": "rehearsal",
            "target": str(target),
            "mission_ref": current["mission_ref"],
            "chain": result,
            f"active_{FIELD}": after_body.get(FIELD),
            "matches_fresh_core_default": after_body.get(FIELD) == DEFAULT_PREDICATES,
            "other_policy_fields_unchanged": (
                {k: v for k, v in after_body.items() if k != FIELD}
                == {k: v for k, v in before_body.items() if k != FIELD}),
            "constitution_binds_new_policy": (
                after["constitution"]["bindings"]["governance_policy_version"]["ref"]
                == result["policy"]["ref"]),
            "mission_binds_new_constitution": (
                after["mission"]["bindings"]["constitution_version"]["ref"]
                == result["constitution"]["ref"]),
            "mandate_unchanged": (after["mission"]["bindings"]["mandate_version"]
                                  == current["mission"]["bindings"]["mandate_version"]),
            "parity_row_after": {"status": row["status"], "detail": row["detail"]},
            "gate_probe": {k: {"allowed": v[0], "reason": v[1]} for k, v in gate.items()},
            "gate_consumers_after": gate_consumers(store.connection),
            "route_check": route_check(routing_state_dir or state_dir),
        }
    finally:
        store.close()


def _retryable(exc: BaseException) -> bool:
    code = str(getattr(exc, "code", "") or "")
    return code in RETRYABLE_CODES or "unavailable" in str(exc).lower()


def _landed(state_dir: Path, operation: str, params: Mapping[str, Any]) -> dict[str, Any] | None:
    """The record an unanswered call may still have written, read back (mode=ro)."""

    connection = sqlite3.connect(f"file:{state_dir / 'core.sqlite'}?mode=ro", uri=True)
    try:
        if operation == "create_policy":
            row = _row(connection, "SELECT policy_version_id, content_hash FROM "
                       "governance_policy_versions WHERE policy_version_id=?",
                       (params["policy_version_id"],))
            return None if row is None else {
                "policy_version": {"policy_version_id": row["policy_version_id"],
                                   "content_hash": row["content_hash"]}}
        table, column = {
            "publish_research_constitution": ("research_constitution_versions",
                                              "constitution_version_id"),
            "create_coverage_mission": ("coverage_mission_versions", "mission_version_id"),
        }[operation]
        row = _row(connection, f"SELECT record_json FROM {table} WHERE {column}=?",
                   (params["version_id"],))
        return None if row is None else json.loads(row["record_json"])
    finally:
        connection.close()


def live(state_dir: Path, *, actor: str, mission_ref: str | None = None,
         call: Callable[[str, dict[str, Any]], Any] | None = None,
         attempts: int = 5, retry_delay: float = 15.0) -> dict[str, Any]:
    """Publish through this environment's writer, as its ephemeral human.

    A call that comes back "writer service is unavailable" may still have been
    completed by the writer, so before each retry the record is looked for in
    the Core; when it is there it is used, not published twice.
    """

    current, policy_hash = _read_state(state_dir, mission_ref=mission_ref)
    chain = build_chain(current, now=_now(), policy_hash=policy_hash)
    if call is None:
        from dalton_core.governance_cli import ephemeral_call

        token_config = state_dir / "writer-tokens.json"
        socket = state_dir / "run" / "writer.sock"
        if not token_config.is_file():
            raise PlanError(f"no writer token config at {token_config}")

        def call(operation: str, params: dict[str, Any]) -> Any:
            return ephemeral_call(token_config, socket, actor_ref=actor,
                                  operation=operation, params=params)

    def apply(operation: str, params: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(1, attempts + 1):
            try:
                result = call(operation, params)
                return result if isinstance(result, dict) else {"result": result}
            except Exception as exc:  # noqa: BLE001 - retried or re-raised below
                if not _retryable(exc) or attempt == attempts:
                    raise
                print(f"[retry] {operation}: {exc} (attempt {attempt}/{attempts}); "
                      f"waiting {retry_delay:g}s", file=sys.stderr)
                time.sleep(retry_delay)
                landed = _landed(state_dir, operation, params)
                if landed is not None:
                    print(f"[retry] {operation}: the writer had completed it; using that record",
                          file=sys.stderr)
                    return landed
        raise AssertionError("unreachable")

    result = publish_cascade(chain, apply)
    after, _ = _read_state(state_dir, mission_ref=mission_ref)
    body = _policy_body(after["policy"])
    return {"mode": "live", "mission_ref": current["mission_ref"],
            "resumed": chain["policy"] is None, "chain": result,
            "active_policy": after["policy_id"],
            f"active_{FIELD}": body.get(FIELD),
            "matches_fresh_core_default": body.get(FIELD) == DEFAULT_PREDICATES}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", type=Path, required=True,
                        help="the dalton-core state directory of any environment")
    parser.add_argument("--routing-state-dir", type=Path, default=None,
                        help="read model configurations from this state directory for "
                             "the route check (default: --state-dir); opened read-only")
    parser.add_argument("--mission-ref", default=None,
                        help="which active mission to rebind; only needed when the Core "
                             "has more than one")
    parser.add_argument("--apply", action="store_true",
                        help="publish through this environment's writer as --actor")
    parser.add_argument("--rehearse", type=Path,
                        help="copy the Core here and publish the cascade on the copy")
    parser.add_argument("--actor", default=None,
                        help="human:<owner> principal that signs this policy version")
    args = parser.parse_args(argv)
    state_dir = Path(os.path.expanduser(str(args.state_dir))).resolve()
    routing = (None if args.routing_state_dir is None
               else Path(os.path.expanduser(str(args.routing_state_dir))).resolve())
    if args.apply and args.rehearse:
        raise PlanError("--apply and --rehearse are different runs; pick one")
    if (args.apply or args.rehearse) and not (args.actor or "").startswith("human:"):
        raise PlanError("--actor human:<owner> is required to sign a policy version")
    if not (state_dir / "core.sqlite").is_file():
        raise PlanError(f"no Core at {state_dir / 'core.sqlite'}")
    if args.apply:
        result = live(state_dir, actor=args.actor, mission_ref=args.mission_ref)
    elif args.rehearse:
        result = rehearse(state_dir, Path(args.rehearse).expanduser().resolve(),
                          actor=args.actor, mission_ref=args.mission_ref,
                          routing_state_dir=routing)
    else:
        result = plan(state_dir, mission_ref=args.mission_ref, routing_state_dir=routing)
    print(json.dumps(result, ensure_ascii=False, indent=1, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover - an owner-run script
    sys.exit(main())
