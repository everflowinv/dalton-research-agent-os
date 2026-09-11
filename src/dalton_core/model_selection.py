"""P14-M2: the owner chooses which model each calling stage uses.

The owner's instruction: in the cockpit, pick the model for each stage of the
work.  The mechanism is deliberately not a new authority.  **A selection is a
new pinned routing-policy version**, whose content carries the per-purpose
override, and whose every predecessor stays byte-identical.

That single decision buys four things that a selection table would have had to
re-earn one at a time:

* it takes effect on the *next call*, not on the next restart, because every
  lane already resolves its chain out of the policy version it pinned;
* it rolls back by being published again -- the previous content is still
  there, still hashed, still the thing a June route decision names;
* it cannot drift from what actually ran, because the route decision records
  the policy hash it routed under, so "which model did the owner have selected
  when this Claim was produced" is answerable from the decision alone;
* it is append-only for free.

What this module adds on top is the bookkeeping the choice needs: a lane's
model configuration file pins a policy *version*, so publishing a new one and
stopping would leave every lane pinned to the old selection.  Repointing those
files is the same move ``scripts/raise_day_budget_cap.py`` already makes when
it appends a day-budget policy version, and it uses the same registry of
configuration file names, so a lane that registered its configuration is
repointed without this module having heard of it.

Nothing here opens the Core, writes a Claim, or touches the OpenClaw
configuration.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .model_configurations import model_config_names
from .model_fallback_chain import (
    FallbackChainError,
    purpose_selection,
    purpose_tiers,
    tier_for,
    validate_selection,
)
from .model_router import (
    ModelRouter,
    canonical_json,
    live_links,
    policy_chain,
    resolve_chain,
)
from .store import content_hash

SELECTION_MODES: tuple[str, ...] = ("tier", "explicit")
PURPOSE_MODEL_CONFIGS: dict[str, tuple[str, ...]] = {
    "draft": ("initial-screen-model-config.json",),
    "document_extraction": ("document-extraction-model-config.json",),
    "claim_index": ("claim-index-model-config.json",),
    "quality_verifier": ("quality-verifier-model-config.json",),
    "quality": ("initial-screen-model-config.json",),
    "model_spec": ("initial-screen-model-config.json",),
    "debate_map": ("initial-screen-model-config.json",),
    "debate_map_verifier": ("initial-screen-model-config.json",),
    "conviction_call": ("initial-screen-model-config.json",),
    "conviction_call_verifier": ("initial-screen-model-config.json",),
    "event_judgement": ("event-judgement-model-config.json",),
    "event_judgement_verifier": ("event-verifier-model-config.json",),
    "thesis_reflection": ("event-judgement-model-config.json",),
    "thesis_reflection_verifier": ("event-verifier-model-config.json",),
    "zero_base_review": ("zero-base-review-model-config.json",),
    "zero_base_review_verifier": ("zero-base-review-verifier-model-config.json",),
    "dossier": ("dossier-model-config.json", "initial-screen-model-config.json"),
    "dossier_verifier": ("company-dossier-verifier-model-config.json",
                         "dossier-verifier-model-config.json"),
    "deep_insight_gate": ("dossier-model-config.json", "initial-screen-model-config.json"),
    "deep_insight_gate_verifier": ("company-dossier-verifier-model-config.json",
                                   "dossier-verifier-model-config.json"),
    "industry_framework": ("initial-screen-model-config.json",),
    "industry_framework_verifier": ("company-dossier-verifier-model-config.json",
                                      "dossier-verifier-model-config.json"),
    "investment_memo": ("dossier-model-config.json", "initial-screen-model-config.json"),
    "investment_memo_verifier": ("company-dossier-verifier-model-config.json",
                                  "dossier-verifier-model-config.json"),
    "earnings_preview": ("earnings-season-model-config.json",),
    "earnings_calibration": ("earnings-season-model-config.json",),
    "earnings_preview_verifier": ("earnings-season-verifier-model-config.json",),
    "earnings_calibration_verifier": ("earnings-season-verifier-model-config.json",),
}
_SERVICE_PURPOSE_PINS = {
    "plan": ("bounded_planner", "planner_routing_policy_ref", "planner_model_router_db"),
    "agenda_planning": ("agenda", "routing_policy_ref", "model_router_db"),
    "thesis_impact_assessment": ("thesis_impact", "assessment_routing_policy_ref", "model_router_db"),
    "thesis_impact_verifier": ("thesis_impact", "verifier_routing_policy_ref", "model_router_db"),
}
# Each calling stage in the owner's words. One map, used both by the cockpit's
# model page and by the text of a fallback notice, so the owner reads the same
# name for a stage wherever it appears.
PURPOSE_LABELS: dict[str, str] = {
    "ask": "问答",
    "goal": "把目标拆成计划",
    "steer": "调整方向",
    "draft": "起草交付物",
    "plan": "决定下一步做什么",
    "model_spec": "写公司模型规格",
    "claim_index": "给结论打标签",
    "quality": "给产出打分",
    "document_extraction": "从文档抽取研究事实",
    "agenda_planning": "旧议程规划",
    "thesis_impact_assessment": "评估新事实对论点的影响",
    "thesis_impact_verifier": "核验论点影响评估",
    "quality_verifier": "核验产出评分",
    "event_judgement": "判断新发生的事",
    "event_judgement_verifier": "核验事件判断",
    "zero_base_review": "从零复盘研究判断",
    "zero_base_review_verifier": "核验从零复盘",
    "thesis_reflection": "回看论点还成不成立",
    "thesis_reflection_verifier": "核验论点复盘",
    "dossier": "写公司档案",
    "dossier_verifier": "核验公司档案",
    "debate_map": "整理市场在吵什么",
    "debate_map_verifier": "独立核验市场争议图",
    "deep_insight_gate": "回答深度认知门的十二问",
    "deep_insight_gate_verifier": "核验深度认知门",
    "industry_framework": "写行业框架",
    "industry_framework_verifier": "核验行业框架",
    "investment_memo": "起草投资备忘录",
    "investment_memo_verifier": "核验投资备忘录",
    "earnings_preview": "写业绩前瞻",
    "earnings_preview_verifier": "核验业绩前瞻",
    "earnings_calibration": "业绩后对账",
    "earnings_calibration_verifier": "核验业绩对账",
    "conviction_call": "提出值得下注的判断",
    "conviction_call_verifier": "独立核验投资判断",
    "street_estimate": "读研报里的目标价",
}
# The seam the delivery slice attaches to. It is a module-level function rather
# than a parameter because the point is that the lane never changes: when
# Discord or Feishu delivery is built, it replaces this body and nothing else.
# Until then the notice lives in the cockpit, which is why the tick summary
# says ``notification_channel: "cockpit"`` rather than "none" -- the owner does
# get told; the telling is a page rather than a push.
NOTIFICATION_CHANNEL = "cockpit"


def notice_delivery(notice: Mapping[str, Any]) -> None:
    """Push one fallback notice somewhere the owner will see it unprompted.

    A deliberate no-op today.  Delivery (Discord / Feishu) was deferred by the
    owner to the end of the plan, and a lane that half-built it would be a lane
    that has to be edited again when the real thing lands.  What this seam
    guarantees is that the *decision* about what to say, when to say it and how
    often has already been made and tested: one message per (model, stage),
    written once, deduplicated in the ledger.
    """

    del notice


# The keys that make a policy version a *version* rather than content. Two
# versions are "the same selection" when everything except these agrees.
_VERSION_KEYS = frozenset({"policy_version_ref", "version", "created_at",
                           "prior_version_ref", "content_hash"})


class ModelSelectionError(RuntimeError):
    """The selection cannot be published as asked."""


def purpose_policy_bindings(
    state_dir: str | Path, *, cockpit_model_config_path: str | Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Resolve each stage to the policy pin its actual consumer reads.

    Missing dynamic launch arguments remain visibly unconfigured.  They must
    never inherit the cockpit extraction pin merely because it is available.
    """

    directory = Path(state_dir).expanduser().resolve()
    result: dict[str, dict[str, Any]] = {}

    def file_binding(purpose: str, candidates: Sequence[Path]) -> None:
        for path in candidates:
            if not path.is_file():
                continue
            raw = _read_model_binding(path)
            result[purpose] = {"status": "configured", "source": str(path),
                               "policy_version_ref": raw["routing_policy_ref"],
                               "model_router_db": raw.get("model_router_db"),
                               "editable": (path.parent == directory and
                                            path.name in model_config_names())}
            return
        result[purpose] = {"status": "unconfigured", "source": str(candidates[0]),
                           "policy_version_ref": None, "model_router_db": None}

    if cockpit_model_config_path is not None:
        cockpit = Path(cockpit_model_config_path).expanduser().resolve()
        for purpose in ("ask", "goal", "steer"):
            file_binding(purpose, (cockpit,))
    else:
        for purpose in ("ask", "goal", "steer"):
            result[purpose] = {"status": "unconfigured", "source": "cockpit.model_config_path",
                               "policy_version_ref": None, "model_router_db": None}
    for purpose, names in PURPOSE_MODEL_CONFIGS.items():
        file_binding(purpose, tuple(directory / name for name in names))

    service_path = directory.parents[1] / "config" / "service.json"
    service = _load_model_json(service_path) if service_path.is_file() else None
    for purpose, (section, field, router_field) in _SERVICE_PURPOSE_PINS.items():
        block = service.get(section) if isinstance(service, Mapping) else None
        nested = block.get("config") if isinstance(block, Mapping) else None
        ref = nested.get(field) if isinstance(nested, Mapping) else None
        result[purpose] = {
            "status": "configured" if isinstance(ref, str) else "unconfigured",
            "source": f"{service_path}#{section}.config.{field}",
            "policy_version_ref": ref if isinstance(ref, str) else None,
            "model_router_db": ((nested.get(router_field) or service.get("model_router_db"))
                                if isinstance(nested, Mapping) and isinstance(service, Mapping)
                                else None),
            "requires_restart": True,
            "editable": True,
        }
    if result["plan"]["status"] == "unconfigured":
        file_binding("plan", (directory / "research-planner-model-config.json",))
    # These consumers receive a path at launch time.  No installed path in the
    # cockpit contract means there is no truthful resident pin to display.
    # The installed consensus lane parses broker notes deterministically.  A
    # registered future model purpose must not make that look like a missing,
    # selectable runtime model today.
    result["street_estimate"] = {
        "status": "deterministic", "source": "mission_consensus_lane",
        "policy_version_ref": None, "model_router_db": None, "editable": False,
    }
    return result


def _load_model_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ModelSelectionError(f"{path} cannot be read: {exc}") from exc


def _read_model_binding(path: Path) -> Mapping[str, Any]:
    raw = _load_model_json(path)
    if not isinstance(raw, Mapping) or not isinstance(raw.get("routing_policy_ref"), str):
        raise ModelSelectionError(f"{path} names no routing policy version")
    return raw


def _write_owner_only(path: Path, value: Any) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _write_configs_atomically(items: Sequence[tuple[Path, Mapping[str, Any]]]) -> None:
    """Stage every changed config, then replace them as one recoverable set."""

    staged: list[tuple[Path, Path, bytes, int]] = []
    try:
        for path, value in items:
            tmp = path.with_name(f".{path.name}.model-selection.tmp")
            tmp.write_text(
                json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.chmod(tmp, 0o600)
            staged.append((path, tmp, path.read_bytes(), path.stat().st_mode & 0o777))
        replaced: list[tuple[Path, bytes, int]] = []
        for path, tmp, original, mode in staged:
            os.replace(tmp, path)
            replaced.append((path, original, mode))
    except Exception:
        # A policy version may have been appended, but no lane may be left on
        # a mixed set of policy pins. Restore every file already replaced.
        for path, original, mode in reversed(locals().get("replaced", [])):
            restore = path.with_name(f".{path.name}.model-selection.restore")
            restore.write_bytes(original)
            os.chmod(restore, mode)
            os.replace(restore, path)
        raise
    finally:
        for _, tmp, _, _ in staged:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass


def model_configs(state_dir: str | Path) -> list[dict[str, Any]]:
    """Every registered model configuration file present in the state directory.

    The registry rather than a list here, for the reason it exists: a lane that
    spends money on a model registers its own configuration name, and a list in
    this module could only ever be right about the lanes that existed the day it
    was written.
    """

    # A registration happens at import, so the registry only knows what has been
    # imported. Loading the lane registry imports every tick lane; the
    # claim-index tagger spends on its own configuration without being a tick
    # lane, so it is named here for the same reason the cap raise names it.
    from .lane_registry import load_lanes

    load_lanes()
    import dalton_core.claim_index_tagging  # noqa: F401

    directory = Path(state_dir).expanduser().resolve()
    found: list[dict[str, Any]] = []
    for name in model_config_names():
        path = directory / name
        if not path.is_file():
            continue
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ModelSelectionError(f"{path} cannot be read: {exc}") from exc
        if not isinstance(config, dict) or not isinstance(
            config.get("routing_policy_ref"), str
        ):
            raise ModelSelectionError(f"{path} names no routing policy version")
        found.append({"name": name, "path": path, "config": config})
    return found


def _runtime_policy_config(state_dir: str | Path, purpose: str) -> dict[str, Any] | None:
    """One resident service pin not represented by a lane model-config file."""

    path = Path(state_dir).expanduser().resolve().parents[1] / "config" / "service.json"
    if not path.is_file():
        return None
    service = json.loads(path.read_text(encoding="utf-8"))
    locations = {
        "plan": ("bounded_planner", "planner_routing_policy_ref",
                 "planner_credential_slot_refs", "planner_model_router_db"),
        "agenda_planning": ("agenda", "routing_policy_ref",
                            "credential_slot_refs", "model_router_db"),
        "thesis_impact_assessment": ("thesis_impact", "assessment_routing_policy_ref",
                                     "credential_slot_refs", "model_router_db"),
        "thesis_impact_verifier": ("thesis_impact", "verifier_routing_policy_ref",
                                   "credential_slot_refs", "model_router_db"),
    }
    location = locations.get(purpose)
    if location is None:
        return None
    section, field, slots_field, router_field = location
    block = service.get(section)
    config = block.get("config") if isinstance(block, Mapping) else None
    if not isinstance(config, Mapping) or not isinstance(config.get(field), str):
        raise ModelSelectionError(
            f"service.json does not configure the {purpose} runtime policy pin")
    router_db = config.get(router_field) or service.get("model_router_db")
    return {"name": f"service.json#{section}.{field}", "path": path,
            "config": service, "runtime_config": config, "field": field,
            "slots_field": slots_field, "router_db": router_db,
            "routing_policy_ref": config[field]}


def _next_version_ref(latest: Mapping[str, Any]) -> str:
    root, separator, tail = str(latest["policy_version_ref"]).rpartition(":")
    version = int(latest["version"]) + 1
    if separator and tail.isdigit():
        return f"{root}:{version}"
    slug = str(latest["id"]).split(":", 1)[1]
    return f"model-routing-policy-version:{slug}:{version}"


def _latest_policy(router: ModelRouter, policy_id: str) -> dict[str, Any]:
    row = router.connection.execute(
        "SELECT policy_json FROM model_routing_policy_versions WHERE policy_id=? "
        "ORDER BY version DESC LIMIT 1",
        (policy_id,),
    ).fetchone()
    if row is None:
        raise ModelSelectionError(f"{policy_id} has no versions to build on")
    return json.loads(row["policy_json"])


def publish_selection(
    router: ModelRouter,
    *,
    policy_version_ref: str,
    purpose: str,
    mode: str,
    chain: Sequence[str] = (),
    actor_ref: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Append the routing-policy version that carries this selection.

    Built from the *pinned* version's content rather than from scratch, so a
    selection changes one purpose and leaves every filter, preference and chain
    exactly as the lane pinned them.  Republishing the selection that is
    already current is a no-op and says so -- pressing 「保存」 twice must not
    grow the chain by a version that changed nothing.

    Refuses when the pinned version is not the latest of its own lineage.  The
    new version is built from the pinned content and appended after the latest,
    so if something else has appended in between -- an installer run, another
    selection -- publishing would silently discard whatever it changed.  A
    refusal costs one re-read; the alternative costs a change nobody can see
    was undone.
    """

    pinned = router.get_policy(policy_version_ref)
    checked = validate_selection(router, purpose=purpose, mode=mode, chain=chain)
    latest = _latest_policy(router, pinned["id"])
    overrides = dict(pinned.get("purpose_overrides") or {})
    entry: dict[str, Any] = {"mode": checked["mode"]}
    if checked["mode"] == "explicit":
        entry["chain"] = list(checked["chain"])
    if actor_ref:
        # Who chose it, in the content, so a route decision's policy hash
        # carries the answer to "who had selected this model when this Claim
        # was produced".
        entry["actor_ref"] = actor_ref
    overrides[purpose] = entry
    wire = {
        key: value for key, value in pinned.items() if key not in _VERSION_KEYS
    }
    wire["purpose_overrides"] = overrides
    comparable = {
        key: value for key, value in latest.items() if key not in _VERSION_KEYS
    }
    if canonical_json(comparable) == canonical_json(wire):
        return {
            "status": "duplicate",
            "policy_id": pinned["id"],
            "policy_version_ref": latest["policy_version_ref"],
            "prior_version_ref": latest["prior_version_ref"],
            **checked,
        }
    if latest["policy_version_ref"] != pinned["policy_version_ref"]:
        raise ModelSelectionError(
            f"{pinned['id']} has moved on since this was read "
            f"({latest['policy_version_ref']} is current); read it again and "
            "choose against that"
        )
    wire.update({
        "version": int(latest["version"]) + 1,
        "prior_version_ref": latest["policy_version_ref"],
        "policy_version_ref": _next_version_ref(latest),
        "created_at": (now or datetime.now(timezone.utc)).isoformat(
            timespec="microseconds"
        ),
    })
    wire["content_hash"] = content_hash(wire)
    result = router.register_policy(wire)
    if result["status"] == "conflict":
        raise ModelSelectionError(result.get("reason", "the policy version conflicted"))
    return {
        "status": result["status"],
        "policy_id": pinned["id"],
        "policy_version_ref": wire["policy_version_ref"],
        "prior_version_ref": wire["prior_version_ref"],
        **checked,
    }


def set_model_selection(
    state_dir: str | Path,
    *,
    purpose: str,
    mode: str,
    chain: Sequence[str] = (),
    actor_ref: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Publish one stage's selection and repoint every lane that pins a policy.

    Every registered model configuration is repointed, not only the one whose
    lane happens to own this purpose.  A purpose is a stage of the work, and
    the stages share policies: pinning the selection into one configuration and
    not another would mean the same purpose ran different models depending on
    which lane happened to make the call, which is exactly the confusion
    per-stage selection exists to end.

    Configurations that pin different policies each get their own new version;
    a configuration pinning a policy that is already at this selection is left
    alone rather than rewritten.
    """

    configs = model_configs(state_dir)
    runtime = _runtime_policy_config(state_dir, purpose)
    if runtime is not None:
        configs.append(runtime)
    if not configs:
        raise ModelSelectionError(
            "this machine has no model configuration to point at a selection"
        )
    if purpose not in purpose_tiers():
        raise ModelSelectionError(
            f"{purpose} is not a calling stage this Core knows about"
        )
    published: dict[tuple[str, str], dict[str, Any]] = {}
    repointed: list[str] = []
    unchanged: list[str] = []
    # Validate every router and pinned policy before appending any immutable
    # version. An invalid late config must not leave half the roles selected.
    for item in configs:
        config = item.get("runtime_config", item["config"])
        policy_ref = config[item.get("field", "routing_policy_ref")]
        router_db = item.get("router_db", config.get("model_router_db"))
        if not isinstance(router_db, str) or not Path(router_db).is_file():
            raise ModelSelectionError(
                f"{item['name']} names a model router database that is not here"
            )
        with ModelRouter(router_db, read_only=True) as router:
            router.get_policy(policy_ref)
            try:
                validate_selection(
                    router, purpose=purpose, mode=mode, chain=chain)
            except FallbackChainError as exc:
                raise ModelSelectionError(str(exc)) from exc
    for item in configs:
        config = item.get("runtime_config", item["config"])
        policy_ref = config[item.get("field", "routing_policy_ref")]
        router_db = item.get("router_db", config.get("model_router_db"))
        if not isinstance(router_db, str) or not Path(router_db).is_file():
            raise ModelSelectionError(
                f"{item['name']} names a model router database that is not here"
            )
        key = (router_db, policy_ref)
        if key not in published:
            with ModelRouter(router_db) as router:
                try:
                    published[key] = publish_selection(
                        router,
                        policy_version_ref=policy_ref,
                        purpose=purpose, mode=mode, chain=chain,
                        actor_ref=actor_ref, now=now,
                    )
                except FallbackChainError as exc:
                    raise ModelSelectionError(str(exc)) from exc
        outcome = published[key]
        new_ref = outcome["policy_version_ref"]
        changed = new_ref != policy_ref
        config[item.get("field", "routing_policy_ref")] = new_ref
        with ModelRouter(router_db, read_only=True) as router:
            profiles = {row["id"]: row for row in router.latest_profiles()}
        selected_slots = [profiles[profile_id]["credential_slot_ref"]
                          for profile_id in outcome["chain"]]
        slots_field = item.get("slots_field", "credential_slot_refs")
        prior_slots = list(config.get(slots_field) or [])
        merged_slots = list(dict.fromkeys([*prior_slots, *selected_slots]))
        if merged_slots != prior_slots:
            config[slots_field] = merged_slots
            changed = True
        (repointed if changed else unchanged).append(item["name"])
    _write_configs_atomically([
        (item["path"], item["config"])
        for item in configs if item["name"] in repointed
    ])
    versions = sorted(
        {
            (outcome["policy_id"], outcome["policy_version_ref"], outcome["status"])
            for outcome in published.values()
        }
    )
    return {
        "status": "published" if repointed else "unchanged",
        "purpose": purpose,
        "tier": tier_for(purpose),
        "mode": mode,
        "actor_ref": actor_ref,
        "chain": list(chain) if mode == "explicit" else [],
        "policy_versions": [
            {"policy_id": policy_id, "policy_version_ref": ref, "publication": status}
            for policy_id, ref, status in versions
        ],
        "model_configs_repointed": repointed,
        "model_configs_unchanged": unchanged,
        "requires_restart": runtime is not None,
        "reload_note": (
            ("需要重启常驻服务后生效；service.json 已原子更新。" if runtime is not None
             else "不用重启：每条流水线下一次调用时会读到新的策略版本。")
            + "回滚就是把上一版的选择再发布一次。"
        ),
    }


def _purpose_label(purpose: str) -> str:
    label = PURPOSE_LABELS.get(purpose)
    return f"{label}（{purpose}）" if label else purpose


def retirement_fallbacks(
    router: ModelRouter,
    *,
    policy: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Which stages are pointed at a retired model, and what each runs instead.

    Read from **current state** rather than from "what this run happened to
    retire", and that is the whole point of it.  The installer's own catalog
    sync retires models before this lane has ever had an hour, so a notice set
    built from one run's delta would miss every model the first deploy dropped
    -- six of them, on the machine this shipped against -- and would lose the
    rest to any error between the sync committing and the notice being written.
    A pass over what is retired *now* has no such window: it is idempotent on
    (model, stage), so running it hourly forever writes each notice once.

    Both chains are looked at, not just the one in force: a stage whose owner
    selection is intact still wants to know that the tier chain behind it has
    lost a link, because that is the chain it falls back to.
    """

    held = {profile["id"]: profile for profile in router.latest_profiles()}
    retired = {
        profile_id for profile_id, profile in held.items()
        if profile.get("status") == "retired"
    }
    if not retired:
        return []
    declared = (policy.get("fallback_chains") or {}).get("tiers", {})
    overrides = policy.get("purpose_overrides") or {}
    affected: list[dict[str, Any]] = []
    for purpose, tier in sorted(purpose_tiers().items()):
        named: list[str] = []
        override = overrides.get(purpose)
        if isinstance(override, Mapping) and override.get("mode") == "explicit":
            named.extend(override.get("chain") or [])
        named.extend(declared.get(tier) or [])
        named = list(dict.fromkeys(named))
        if not named:
            continue
        lost = [profile_id for profile_id in named if profile_id in retired]
        if not lost:
            continue
        now = resolve_chain(policy, tier=tier, purpose=purpose, profiles=held)
        chain = list(now["chain"]) if now is not None else []
        remaining = live_links(chain, held)
        replacement = remaining[0] if remaining else None
        superseded = (now or {}).get("superseded_chain")
        if replacement is None:
            message = (
                f"模型 {'、'.join(lost)} 已在 OpenClaw 消失；环节 "
                f"{_purpose_label(purpose)} 没有可用的替代模型，现在一次也调不了；"
                "请到 cockpit 模型页选择"
            )
        else:
            message = (
                f"模型 {'、'.join(lost)} 已在 OpenClaw 消失；环节 "
                f"{_purpose_label(purpose)} 已自动回退到 {replacement}；"
                "如需更改请到 cockpit 模型页选择"
            )
        affected.append({
            "purpose": purpose,
            "tier": tier,
            "retired_profile_ids": lost,
            "replacement_profile_id": replacement,
            "chain": chain,
            "superseded_chain": superseded,
            "message": message,
        })
    return affected


def record_retirement_notices(
    router: ModelRouter,
    *,
    state_dir: str | Path,
    delivery: Any = None,
) -> dict[str, Any]:
    """Write one notice per (model, stage) pointed at a retired model.

    Deduplicated by the ledger rather than by this function: the lane runs
    every hour and will see the same retirement every hour, and a notice keyed
    on the moment would be an hourly alarm nobody reads.  Delivery is attempted
    only for a notice that is *new*, for the same reason.

    Takes no list of what was just retired, deliberately -- see
    :func:`retirement_fallbacks`.  The state is the question.
    """

    deliver = delivery if delivery is not None else notice_delivery
    written: list[dict[str, Any]] = []
    repeated: list[str] = []
    affected: list[dict[str, Any]] = []
    seen_policies: set[str] = set()
    for item in model_configs(state_dir):
        policy_ref = item["config"]["routing_policy_ref"]
        if policy_ref in seen_policies:
            continue
        seen_policies.add(policy_ref)
        try:
            policy = router.get_policy(policy_ref)
        except Exception:  # noqa: BLE001 - a pin this router cannot read is not ours
            continue
        affected.extend(retirement_fallbacks(router, policy=policy))
    for item in affected:
        for profile_id in item["retired_profile_ids"]:
            result = router.record_fallback_notice(
                profile_id=profile_id,
                purpose=item["purpose"],
                tier=item["tier"],
                replacement_profile_id=item["replacement_profile_id"],
                reason="not_in_broker_catalog",
                message=item["message"],
                detail={
                    "chain": item["chain"],
                    "superseded_chain": item["superseded_chain"],
                },
            )
            if result["status"] == "fresh":
                written.append(result["notice"])
                deliver(result["notice"])
            else:
                repeated.append(result["notice"]["id"])
    return {
        "notification_channel": NOTIFICATION_CHANNEL,
        "notices_written": [notice["id"] for notice in written],
        "notices_repeated": repeated,
        "affected_purposes": [item["purpose"] for item in affected],
        "messages": [notice["message"] for notice in written],
    }


def current_selection(
    state_dir: str | Path, *, router_db: str | Path | None = None
) -> dict[str, Any]:
    """What each stage runs today, read from the configurations that pin it.

    Read-only, and read through the *pinned* versions rather than the latest,
    because the pinned version is the one a call will use.
    """

    configs = model_configs(state_dir)
    if not configs:
        return {"available": False,
                "reason": "这台机器上还没有任何模型配置，所以没有可选的环节"}
    chosen = configs[0]
    for item in configs:
        if router_db is not None and item["config"].get("model_router_db") == str(
            router_db
        ):
            chosen = item
            break
    path = chosen["config"]["model_router_db"]
    if not Path(str(path)).is_file():
        return {"available": False, "reason": "这台机器上还没有模型路由库"}
    with ModelRouter(str(path), read_only=True) as router:
        try:
            policy: Mapping[str, Any] | None = router.get_policy(
                chosen["config"]["routing_policy_ref"]
            )
        except Exception:  # noqa: BLE001 - an unreadable pin is an empty column
            policy = None
        rows = purpose_selection(
            router, policy=policy, links=router.chain_links()
        )
    return {
        "available": True,
        "model_config": chosen["name"],
        "policy_version_ref": chosen["config"]["routing_policy_ref"],
        "purposes": rows,
        "modes": list(SELECTION_MODES),
    }


__all__ = [
    "NOTIFICATION_CHANNEL",
    "PURPOSE_LABELS",
    "SELECTION_MODES",
    "ModelSelectionError",
    "current_selection",
    "model_configs",
    "notice_delivery",
    "publish_selection",
    "record_retirement_notices",
    "retirement_fallbacks",
    "set_model_selection",
]
