"""The cockpit card 「核验独立性 · 起草模型顺序」 (owner request 2026-10-08).

One row per drafting stage that an independent verifier checks.  Each row
shows, for every research environment on the host, the effective draft chain,
the verifier chain and whether the two are independent today; and it carries
an ordered editor over the models the draft stage may use, a proposed minimal
reorder, the tier's own chain to go back to, and the per-call cost of the
first link.

Read-only.  Saving goes out through the existing ``model_select`` action --
the governed ``set_model_selection`` writer operation for one purpose, fanned
out to every environment exactly like a save on the model page.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from .verifier_independence import (
    CONFLICT_STATUSES,
    STATUS_UNBOUND,
    discover_pairs,
    environment_report,
    evaluate_pair,
    link_independence,
    per_call_cost_usd,
    recommend_order,
    status_label,
)

#: Used when the draft stage's call budget cannot be read: a mid-sized prompt.
DEFAULT_ESTIMATE_TOKENS = (8_000, 2_000)
DEFAULT_REASON = "核验独立性：起草改用与核验不同家族的模型，避免独立核验被拒"


def _environments(plane: Any) -> list[dict[str, Any]]:
    """Every environment to report on, the local one first."""

    local = {"environment_id": "local", "name": "本环境", "local": True,
             "state_dir": plane.config.state_dir,
             "cockpit_model_config_path": plane.config.model_config_path}
    manager = plane.config.workspace_manager_config_path
    if manager is None:
        return [local]
    from .model_routing_sync import environment_for_state_dir, host_environments

    try:
        found = host_environments(manager)
    except Exception:  # noqa: BLE001 - the local view still stands
        return [local]
    origin = environment_for_state_dir(found, plane.config.state_dir)
    if origin is not None:
        local.update({"environment_id": origin.environment_id, "name": origin.name})
    others = [{"environment_id": item.environment_id, "name": item.name, "local": False,
               "state_dir": item.state_dir, "cockpit_model_config_path": None}
              for item in found if origin is None or item.environment_id != origin.environment_id]
    return [local, *others]


def _estimate_tokens(plane: Any, purpose: str) -> tuple[int, int, str]:
    from .model_budget_configuration import call_budget_view
    from .model_selection import purpose_policy_bindings

    try:
        binding = purpose_policy_bindings(
            plane.config.state_dir,
            cockpit_model_config_path=plane.config.model_config_path).get(purpose)
        view = call_budget_view(plane.config.state_dir, purpose, binding=binding)
        effective = view.get("effective") or {}
        inbound = int(effective["max_input_tokens"])
        outbound = int(effective["max_output_tokens"])
        return inbound, outbound, f"按这个环节的单次上限估算（输入 {inbound:,}、输出 {outbound:,} tokens），实际通常更低"
    except Exception:  # noqa: BLE001 - an estimate, never a refusal
        inbound, outbound = DEFAULT_ESTIMATE_TOKENS
        return inbound, outbound, f"按一次中等长度调用估算（输入 {inbound:,}、输出 {outbound:,} tokens）"


def build_card(plane: Any, *, now: datetime) -> dict[str, Any]:
    """The card's whole payload."""

    discovered = discover_pairs()
    pairs = discovered["pairs"]
    environments = _environments(plane)
    reports: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for environment in environments:
        report = environment_report(
            environment["state_dir"], now=now, pairs=pairs,
            cockpit_model_config_path=environment["cockpit_model_config_path"])
        reports.append((environment, report))
    local_report = reports[0][1]
    if not local_report.get("available"):
        return {"available": False, "reason": local_report.get("reason")
                or "本环境的模型配置读不出来", "rows": []}
    catalogue: Mapping[str, Mapping[str, Any]] = local_report.get("profiles") or {}
    cooldowns: Mapping[str, Any] = local_report.get("cooldowns") or {}

    def name(profile_id: str | None) -> str:
        return plane._model_display_name(profile_id, catalogue) if profile_id else ""

    def by_pair(report: Mapping[str, Any]) -> dict[tuple[str, str], Mapping[str, Any]]:
        return {(row["draft_purpose"], row["verifier_purpose"]): row
                for row in report.get("pairs") or ()}

    indexed = [(environment, by_pair(report), report) for environment, report in reports]
    local_rows = indexed[0][1]
    from .model_fallback_chain import profile_unusable
    from .model_selection import PURPOSE_LABELS
    from .cockpit_plane import MODEL_TIER_LABELS

    allowed = sorted(
        profile_id for profile_id, profile in catalogue.items()
        if profile_unusable(profile, now=now) is None
    )
    drafts: dict[str, dict[str, Any]] = {}
    for pair in pairs:
        draft = pair["draft_purpose"]
        key = (draft, pair["verifier_purpose"])
        local = local_rows.get(key) or {}
        group = drafts.setdefault(draft, {
            "draft_purpose": draft,
            "draft_label": PURPOSE_LABELS.get(draft, draft),
            "draft_tier": pair["draft_tier"],
            "draft_tier_label": MODEL_TIER_LABELS.get(pair["draft_tier"], pair["draft_tier"]),
            "pairs": [],
            "_local": [],
        })
        environments_view = []
        for environment, rows, report in indexed:
            row = rows.get(key)
            if not report.get("available") or row is None:
                environments_view.append({
                    "environment_id": environment["environment_id"],
                    "name": environment["name"], "local": environment["local"],
                    "status": "unavailable",
                    "status_label": report.get("reason") or "读不到这个环境",
                    "draft_chain": [], "verifier_chain": [],
                    "refusals_48h": 0, "refusals_unrecorded_48h": 0})
                continue
            profiles = report.get("profiles") or catalogue
            evaluation = row.get("evaluation") or {"status": row["status"]}
            environments_view.append({
                "environment_id": environment["environment_id"],
                "name": environment["name"], "local": environment["local"],
                "status": row["status"],
                "status_label": status_label(
                    evaluation, display_name=lambda pid, p=profiles: plane._model_display_name(pid, p)),
                "in_conflict": row["status"] in CONFLICT_STATUSES,
                "draft_mode": row.get("draft_mode"),
                "draft_chain": [plane._model_display_name(item, profiles)
                                for item in row.get("draft_chain") or ()],
                "draft_chain_ids": list(row.get("draft_chain") or ()),
                "verifier_chain": [plane._model_display_name(item, profiles)
                                   for item in row.get("verifier_chain") or ()],
                "fallback_conflicts": [plane._model_display_name(item, profiles)
                                       for item in (evaluation.get("fallback_conflicts") or ())],
                "refusals_48h": row.get("refusals_48h", 0),
                "refusals_unrecorded_48h": row.get("refusals_unrecorded_48h", 0),
            })
        group["pairs"].append({
            "verifier_purpose": pair["verifier_purpose"],
            "verifier_label": PURPOSE_LABELS.get(pair["verifier_purpose"], pair["verifier_purpose"]),
            "requires": pair["requires"],
            "environments": environments_view,
            "bound_anywhere": any(item["status"] not in {STATUS_UNBOUND, "unavailable"}
                                  for item in environments_view),
        })
        if local and local.get("status") != STATUS_UNBOUND:
            group["_local"].append(local)
    rows: list[dict[str, Any]] = []
    for draft, group in drafts.items():
        local_pairs = group.pop("_local")
        group["bound_anywhere"] = any(pair["bound_anywhere"] for pair in group["pairs"])
        group["in_conflict"] = any(item.get("in_conflict") for pair in group["pairs"]
                                   for item in pair["environments"])
        group["refusals_48h"] = sum(item["refusals_48h"] + item["refusals_unrecorded_48h"]
                                    for pair in group["pairs"] for item in pair["environments"])
        inbound, outbound, basis = _estimate_tokens(plane, draft)
        if not local_pairs:
            group["editor"] = None
            group["editor_note"] = "本环境没有配置这个起草环节；可以在配置了它的环境里调整。"
            rows.append(group)
            continue
        head = local_pairs[0]
        verifier_chains = [list(item.get("verifier_chain") or ()) for item in local_pairs]
        chain = list(head.get("draft_chain") or ())
        tier_chain = list(head.get("draft_tier_chain") or ())

        def cost(profile_id: str | None) -> str | None:
            if not profile_id:
                return None
            return per_call_cost_usd(catalogue.get(profile_id), input_tokens=inbound,
                                     output_tokens=outbound)

        def choice(profile_id: str) -> dict[str, Any]:
            profile = catalogue.get(profile_id) or {}
            facts = link_independence(profile_id, verifier_chains, catalogue, cooldowns, now)
            return {
                "model": profile_id, "display_name": name(profile_id),
                "family": profile.get("family"),
                "family_label": plane._model_family_label(profile.get("family")),
                "conflict": not facts["independent"],
                "cooling": profile_id in cooldowns,
                "usable": profile_unusable(profile, now=now) is None if profile else False,
                "cost_per_call_usd": cost(profile_id),
            }

        recommendation = recommend_order(
            chain, purpose=draft, verifier_chains=verifier_chains, profiles=catalogue,
            cooldowns=cooldowns, now=now, candidates=allowed,
            input_tokens=inbound, output_tokens=outbound)

        def preview(candidate: Sequence[str]) -> list[str]:
            labels = []
            for verifier_chain in verifier_chains:
                evaluation = evaluate_pair(draft_chain=candidate, verifier_chain=verifier_chain,
                                           profiles=catalogue, cooldowns=cooldowns, now=now)
                labels.append(status_label(evaluation, display_name=name))
            return labels

        group["editor"] = {
            "mode": head.get("draft_mode"),
            "explicit": isinstance(head.get("draft_override"), Mapping)
            and (head.get("draft_override") or {}).get("mode") == "explicit",
            "chain": chain,
            "chain_choices": [choice(item) for item in chain],
            "choices": [choice(item) for item in allowed],
            "tier_chain": tier_chain,
            "tier_chain_names": [name(item) for item in tier_chain],
            "tier_status": preview(tier_chain) if tier_chain else [],
            "recommended": {
                **recommendation,
                "names": [name(item) for item in recommendation["chain"]],
                "status_after": preview(recommendation["chain"]),
                "cost_per_call_usd": cost((recommendation["chain"] or [None])[0]),
            },
            "cost": {
                "first_link": chain[0] if chain else None,
                "first_link_name": name(chain[0]) if chain else None,
                "per_call_usd": cost(chain[0] if chain else None),
                "basis": basis,
            },
            "default_reason": DEFAULT_REASON,
        }
        rows.append(group)
    rows.sort(key=lambda row: (not row["in_conflict"], not row["bound_anywhere"],
                               row["draft_purpose"]))
    environment_summary = [{
        "environment_id": environment["environment_id"], "name": environment["name"],
        "local": environment["local"], "available": bool(report.get("available")),
        "conflicts": sum(1 for row in report.get("pairs") or ()
                         if row.get("status") in CONFLICT_STATUSES),
        "reason": report.get("reason"),
    } for environment, report in reports]
    return {
        "available": True,
        "as_of": now.isoformat(timespec="seconds"),
        "rows": rows,
        "environments": environment_summary,
        "unpaired_verifiers": discovered["unpaired"],
        "conflict_count": sum(item["conflicts"] for item in environment_summary),
    }


__all__ = ["DEFAULT_REASON", "build_card"]
