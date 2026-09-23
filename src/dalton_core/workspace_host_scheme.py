"""A new research environment adopts this machine's scheme on the day it is made.

Three things were true of every environment created before 2026-09-18, and all
three had to be repaired by hand afterwards:

* it was **outside the host's daily budget** until somebody ran
  ``scripts/bind_shared_daily_budget.py --apply``.  Until then its own cap was
  the only cap it had, so a second environment doubled what this Mac could
  spend in a day while every page truthfully reported that no cap had been
  passed;
* it started on the **packaged model chains** rather than the ones the owner
  actually chose.  The runtime template is a snapshot of the day it was
  exported; the owner has been choosing models since, in the legacy
  environment, and a new Cockpit began life on models that had been taken out
  of service -- until somebody ran ``scripts/align_model_routing.py --apply``;
* it carried the template's **credential slot list**, which is shorter than
  the one the host now has.  A chain whose first link is served through a slot
  the config does not name is not a chain: routing refuses every link with
  ``credential_slot_unavailable`` and the lane reports ``MODEL_CHAIN_EXHAUSTED``
  at a cost of zero, which reads like a model failure and is in fact a list.

The owner's instruction is that none of this is theirs to remember.  So the
same three things happen at creation, offline, before the new writer is
started, out of the same code the by-hand repairs use:

* the binding is written by :func:`shared_daily_budget.install_binding`;
* the selections are read by :func:`model_routing_sync.agreed_selections` --
  the alignment script's own reader -- and published by
  ``model_selection.set_tier_selection`` / ``set_model_selection``, which is
  the same operation the writer serves when the owner saves a model, run here
  against the workspace's own files while nothing else is holding them;
* the slot lists are copied from the source environment's configurations.

Two deliberate choices.  **Nothing here raises into creation.**  An environment
that exists on packaged defaults can be aligned afterwards by the scripts; an
environment that failed to be created cannot be anything.  Every step therefore
returns its own status and its own reason, and the creation receipt carries
them.  And **the source environment is only ever read**: its router is opened
read-only and its configuration files are parsed, never written, so aligning a
new workspace cannot disturb the environment the owner is working in.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from .model_routing_sync import (
    LEGACY_ENVIRONMENT_ID,
    Environment,
    ModelRoutingSyncError,
    agreed_selections,
    host_environments,
)
from .service_config_location import service_config_path

SCHEMA_VERSION = "dalton-workspace-host-scheme-0.1"
#: The two keys a model configuration or a service section names its broker
#: credential slots with.  Both are lists of slot refs and both gate routing.
SLOT_FIELDS: tuple[str, ...] = ("credential_slot_refs", "planner_credential_slot_refs")
ROUTER_FILENAME = "model-router.sqlite"


# ---------------------------------------------------------------------------
# Finding the environment a new one copies from.


# ``service_config_path`` is imported above and re-exported here, so "the
# service.json of this environment" keeps having exactly one answer.  The
# derivation moved to :mod:`dalton_core.service_config_location` because a
# state directory reached through a symlink belongs to the installation the
# caller named, not to the one whose volume happens to store its bytes.


def source_environment(
    manager_config_path: str | Path,
    *,
    source_id: str = LEGACY_ENVIRONMENT_ID,
    legacy_service_path: str | Path | None = None,
    exclude_state_dir: str | Path | None = None,
) -> Environment | None:
    """The environment a new one copies its configuration from, or ``None``.

    ``None`` rather than an exception for a host that has no such environment:
    the first environment on a machine has nothing to copy from and is not
    broken, it is first.  The packaged defaults are then exactly right.
    """

    try:
        found = host_environments(manager_config_path,
                                  legacy_service_path=legacy_service_path)
    except (ModelRoutingSyncError, OSError, ValueError):
        return None
    excluded = (Path(exclude_state_dir).expanduser().resolve()
                if exclude_state_dir is not None else None)
    for environment in found:
        if environment.environment_id != source_id:
            continue
        try:
            same = (excluded is not None
                    and environment.state_dir.resolve() == excluded)
        except OSError:
            same = False
        return None if same else environment
    return None


def resolve_manager_config(
    state_dir: str | Path, explicit: str | Path | None = None,
) -> Path | None:
    """Which host manifest this environment belongs to, without being told.

    The audit runs from a state directory and has to answer "aligned with
    what?".  Two files in the environment itself already name the host
    manifest -- the shared-budget binding and the cockpit's service
    configuration -- so the answer is read from the environment rather than
    guessed from a conventional path, which would make a test in a temporary
    directory report on the real installation on this Mac.
    """

    if explicit is not None:
        path = Path(explicit).expanduser()
        return path if path.is_file() else None
    state = Path(state_dir).expanduser().resolve()
    from .shared_daily_budget import BINDING_FILENAME

    binding = state / BINDING_FILENAME
    if binding.is_file():
        try:
            value = json.loads(binding.read_text(encoding="utf-8"))
            candidate = Path(str(value["manager_config_path"])).expanduser()
        except (OSError, UnicodeError, ValueError, KeyError, TypeError):
            candidate = None
        if candidate is not None and candidate.is_file():
            return candidate
    service = service_config_path(state)
    try:
        value = json.loads(service.read_text(encoding="utf-8"))
        declared = value["control"]["config"]["cockpit"]["workspace_manager_config_path"]
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        return None
    candidate = Path(str(declared)).expanduser()
    return candidate if candidate.is_file() else None


def resolve_source_state_dir(
    state_dir: str | Path,
    *,
    manager_config_path: str | Path | None = None,
    source_state_dir: str | Path | None = None,
    source_id: str = LEGACY_ENVIRONMENT_ID,
    legacy_service_path: str | Path | None = None,
) -> Path | None:
    """The state directory of the environment this one is compared against."""

    if source_state_dir is not None:
        path = Path(source_state_dir).expanduser().resolve()
        return path if path.is_dir() else None
    manager = resolve_manager_config(state_dir, manager_config_path)
    if manager is None:
        return None
    environment = source_environment(
        manager, source_id=source_id, legacy_service_path=legacy_service_path,
        exclude_state_dir=state_dir)
    return None if environment is None else environment.state_dir


# ---------------------------------------------------------------------------
# The credential slot list.


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None


def _merge(held: Sequence[str], wanted: Sequence[str]) -> list[str]:
    """This environment's slots, then the host's, each named once.

    A merge and not a replacement: a slot this environment has and the source
    does not is a credential somebody bound here on purpose, and the defect
    being repaired is a list that is too *short*.  Order is preserved because
    nothing reads it as a preference and a stable list keeps a rewrite that
    changes nothing from looking like a change.
    """

    return list(dict.fromkeys([*held, *wanted]))


def _slot_lists(value: Any) -> list[list[str]]:
    """Every credential slot list inside one parsed configuration."""

    found: list[list[str]] = []

    def visit(node: Any) -> None:
        if isinstance(node, Mapping):
            for key, child in node.items():
                if key in SLOT_FIELDS and isinstance(child, list):
                    found.append([str(item) for item in child])
                else:
                    visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return found


def source_credential_slots(source_state_dir: str | Path) -> list[str]:
    """Every broker credential slot the source environment's configs name.

    The union of its lane model configurations and its service configuration,
    rather than a constant here: the owner adds a gateway account by binding a
    slot, and a list in this module would be right about the day it was
    written and wrong from the next one.
    """

    state = Path(source_state_dir).expanduser().resolve()
    slots: list[str] = []
    for path in sorted(state.glob("*model-config.json")):
        config = _read_json(path)
        if isinstance(config, Mapping):
            slots = _merge(slots, [str(item) for item
                                   in config.get("credential_slot_refs") or []])
    service = _read_json(service_config_path(state))
    for held in _slot_lists(service):
        slots = _merge(slots, held)
    return slots


def align_credential_slots(
    state_dir: str | Path, *, source_state_dir: str | Path,
) -> dict[str, Any]:
    """Give this environment the host's credential slots, everywhere it lists them.

    Both places, because a chain is refused by whichever one is short: the 21
    lane configurations *and* every section of ``service.json`` that carries
    its own list (the planner, the agenda, thesis-impact's assessment and
    verifier).  The observed failure was the second: the deliverable chain
    resolved, the workspace had the profiles, and every call still died with
    ``credential_slot_unavailable`` because one list in one section named five
    slots where the host had seven.
    """

    from .model_selection import _write_configs_atomically

    state = Path(state_dir).expanduser().resolve()
    wanted = source_credential_slots(source_state_dir)
    if not wanted:
        return {"status": "skipped", "slots": [], "updated": [],
                "note": f"没有从 {source_state_dir} 读到任何模型凭证槽位，"
                        "这个环境保留模板自带的槽位列表。"}
    writes: list[tuple[Path, Mapping[str, Any]]] = []
    updated: list[str] = []
    for path in sorted(state.glob("*model-config.json")):
        config = _read_json(path)
        if not isinstance(config, Mapping):
            continue
        held = [str(item) for item in config.get("credential_slot_refs") or []]
        merged = _merge(held, wanted)
        if merged != held:
            writes.append((path, {**config, "credential_slot_refs": merged}))
            updated.append(path.name)
    service_path = service_config_path(state)
    service = _read_json(service_path)
    if isinstance(service, Mapping):
        service = json.loads(json.dumps(service))
        touched: list[str] = []

        def visit(node: Any, trail: str) -> None:
            if isinstance(node, Mapping):
                for key, child in node.items():
                    if key in SLOT_FIELDS and isinstance(child, list):
                        held = [str(item) for item in child]
                        merged = _merge(held, wanted)
                        if merged != held:
                            node[key] = merged  # type: ignore[index]
                            touched.append(f"{trail}.{key}".lstrip("."))
                    else:
                        visit(child, f"{trail}.{key}".lstrip("."))
            elif isinstance(node, list):
                for child in node:
                    visit(child, trail)

        visit(service, "")
        if touched:
            writes.append((service_path, service))
            updated.extend(f"service.json#{name}" for name in touched)
    if writes:
        _write_configs_atomically(writes)
    return {
        "status": "aligned" if updated else "unchanged",
        "source_state_dir": str(Path(source_state_dir).expanduser().resolve()),
        "slots": wanted,
        "updated": sorted(updated),
        "note": (f"模型凭证槽位已和本机现有环境一致（{len(wanted)} 个），"
                 f"改写了 {len(updated)} 处。" if updated else
                 "模型凭证槽位本来就和本机现有环境一致。"),
    }


# ---------------------------------------------------------------------------
# The model routing selections.


def _source_connection(router_db: Path) -> tuple[Any, sqlite3.Connection]:
    """Open another environment's router without being able to write it.

    The same two-step ``latest_policies`` uses and for the same reason: the
    strict WAL reader is the one that cannot write even by accident and that
    sees a *running* environment's uncommitted-to-main pages, but it refuses a
    database with no sidecars -- which is a stopped environment, and a stopped
    environment is exactly the one a creation most often reads.
    """

    from .model_router import ModelRouter

    try:
        opened = ModelRouter(str(router_db), read_only=True)
        return opened, opened.connection
    except (sqlite3.OperationalError, ValueError):
        connection = sqlite3.connect(f"file:{router_db}?mode=ro", uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        return None, connection


def copy_missing_profiles(
    router_db: str | Path, *, source_router_db: str | Path,
    profile_ids: Sequence[str],
) -> dict[str, Any]:
    """Bring over the model profiles the host's chains name and this router lacks.

    The runtime template copies the profiles its own lane chains reach, which
    is the right rule for a template and the wrong one for a chain the owner
    chose afterwards: selecting a model the workspace has no profile for is
    refused outright by ``validate_selection``, so without this the alignment
    would simply not happen for the tier that names it.

    The whole lineage is copied, oldest version first, because a profile's
    versions are immutable and the router refuses a first version numbered
    anything but one -- the same walk ``workspace_model_setup`` makes when it
    bottles a template.
    """

    from .model_router import ModelRouter
    from .workspace_model_setup import _rows_by_refs

    target_path = Path(router_db).expanduser()
    source_path = Path(source_router_db).expanduser()
    if not source_path.is_file():
        return {"copied": [], "unavailable": list(profile_ids),
                "reason": f"这里没有模型路由数据库：{source_path}"}
    with ModelRouter(target_path) as target:
        held = {str(row["id"]) for row in target.latest_profiles()}
        wanted = [str(item) for item in dict.fromkeys(profile_ids)
                  if str(item) not in held]
        if not wanted:
            return {"copied": [], "unavailable": []}
        opened, connection = _source_connection(source_path)
        copied: list[str] = []
        unavailable: list[dict[str, str]] = []
        try:
            for profile_id in sorted(wanted):
                row = connection.execute(
                    "SELECT profile_version_ref FROM model_endpoint_profile_versions "
                    "WHERE profile_id=? ORDER BY version DESC LIMIT 1",
                    (profile_id,),
                ).fetchone()
                if row is None:
                    unavailable.append({"profile_id": profile_id,
                                        "reason": "源环境里也没有这个模型档案"})
                    continue
                try:
                    lineage = _rows_by_refs(
                        connection, "model_endpoint_profile_versions",
                        "profile_json", "profile_version_ref", {row[0]})
                    refused = next(
                        (outcome for outcome in
                         (target.register_profile(profile) for profile in lineage)
                         if outcome.get("status") == "conflict"), None)
                except Exception as exc:  # noqa: BLE001 - one model, not the set
                    unavailable.append({"profile_id": profile_id, "reason": str(exc)})
                    continue
                if refused is not None:
                    unavailable.append({"profile_id": profile_id,
                                        "reason": str(refused.get("reason", "conflict"))})
                    continue
                copied.append(profile_id)
        finally:
            if opened is not None:
                opened.close()
            else:
                connection.close()
    return {"copied": copied, "unavailable": unavailable}


def align_model_routing(
    state_dir: str | Path,
    *,
    source_state_dir: str | Path,
    actor_ref: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Start this environment's router on the host's current selections.

    Published rather than copied: each selection is appended as this router's
    own immutable policy version, in its own lineage, through the same
    ``set_tier_selection`` / ``set_model_selection`` the writer serves when the
    owner saves a model on the model page.  So the new environment's first
    routing versions are ordinary versions -- readable, rollback-able by
    republishing the previous chain, and carrying the owner as the actor --
    rather than rows somebody inserted at creation.

    Tiers before per-stage pins, for the reason the alignment script
    documents: a tier save drops every purpose override of that tier, so the
    other order would publish the stage pins and then throw them away.
    """

    from .model_router import ModelRouter
    from .model_selection import set_model_selection, set_tier_selection

    state = Path(state_dir).expanduser().resolve()
    source = Path(source_state_dir).expanduser().resolve()
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "source_state_dir": str(source),
        "tiers": {}, "purposes": {}, "conflicts": [],
        "profiles_copied": [], "profiles_unavailable": [],
        "published": [], "unchanged": [], "failed": [],
    }
    try:
        selections = agreed_selections(source / ROUTER_FILENAME)
    except (ModelRoutingSyncError, OSError, ValueError, sqlite3.Error) as exc:
        return {**record, "status": "skipped",
                "note": f"没有按本机现有环境对齐模型路由：{exc}。"
                        "这个环境先用打包的默认链路；等那个环境能读了，"
                        "运行 scripts/align_model_routing.py --apply 即可对齐。"}
    record["tiers"] = selections["tiers"]
    record["purposes"] = selections["purposes"]
    record["conflicts"] = selections["conflicts"]
    if not selections["tiers"] and not selections["purposes"]:
        return {**record, "status": "skipped",
                "note": "本机现有环境没有任何模型选择可以复制，这个环境用打包的默认链路。"}
    router_db = state / ROUTER_FILENAME
    if not router_db.is_file():
        return {**record, "status": "skipped",
                "note": f"这个环境还没有模型路由数据库（{router_db}），没有可以设定的路由。"}
    named = {profile for chain in selections["tiers"].values() for profile in chain}
    named |= {profile for chain in selections["purposes"].values() for profile in chain}
    profiles = copy_missing_profiles(
        router_db, source_router_db=source / ROUTER_FILENAME,
        profile_ids=sorted(named))
    record["profiles_copied"] = profiles["copied"]
    record["profiles_unavailable"] = profiles["unavailable"]
    # One writable connection held open for the whole publication, and the
    # reason is not performance.  ``set_tier_selection`` reads each router
    # through the strict read-only reader, which refuses a WAL database whose
    # sidecars do not exist -- and they do not exist for a router nobody has
    # open, which is exactly a workspace being created before its writer is
    # started.  In a live environment the writer's own connection is what keeps
    # them there; here this is.
    try:
        held = agreed_selections(router_db)
    except (ModelRoutingSyncError, OSError, ValueError, sqlite3.Error):
        held = {"tiers": {}, "purposes": {}}
    from .model_fallback_chain import tier_for

    tiers = {tier: chain for tier, chain in selections["tiers"].items()
             if held["tiers"].get(tier) != chain}
    # A stage pin whose tier is being published has to be published again even
    # when it already matches: a tier save drops every override of that tier,
    # which is what makes the alignment script tell the owner to run it twice.
    # Deciding it here instead means one run settles, and a second run
    # publishes nothing at all rather than oscillating for ever.
    purposes = {}
    for purpose, chain in selections["purposes"].items():
        try:
            republished = tier_for(purpose) in tiers
        except Exception:  # noqa: BLE001 - a stage this Core does not know
            republished = False
        if republished or held["purposes"].get(purpose) != chain:
            purposes[purpose] = chain
    record["unchanged"] = sorted(
        [f"类别 {tier}" for tier in selections["tiers"] if tier not in tiers]
        + [f"环节 {purpose}" for purpose in selections["purposes"]
           if purpose not in purposes])
    with ModelRouter(router_db):
        for tier, chain in sorted(tiers.items()):
            try:
                outcome = set_tier_selection(
                    state, tier=tier, mode="explicit", chain=list(chain),
                    actor_ref=actor_ref, now=now)
            except Exception as exc:  # noqa: BLE001 - one tier, not the creation
                record["failed"].append({"kind": "tier", "subject": tier,
                                         "chain": list(chain), "reason": str(exc)})
                continue
            record["published"].append({"kind": "tier", "subject": tier,
                                        "chain": list(chain),
                                        "status": outcome.get("status", "published")})
        for purpose, chain in sorted(purposes.items()):
            try:
                outcome = set_model_selection(
                    state, purpose=purpose, mode="explicit", chain=list(chain),
                    actor_ref=actor_ref, now=now)
            except Exception as exc:  # noqa: BLE001 - one stage, not the creation
                record["failed"].append({"kind": "purpose", "subject": purpose,
                                         "chain": list(chain), "reason": str(exc)})
                continue
            record["published"].append({"kind": "purpose", "subject": purpose,
                                        "chain": list(chain),
                                        "status": outcome.get("status", "published")})
    status = "partial" if record["failed"] else "aligned"
    names = "、".join(f"{item['subject']}" for item in record["failed"])
    return {**record, "status": status,
            "note": (f"模型路由已按本机现有环境设定："
                     f"新发布 {len(record['published'])} 项，"
                     f"本来就一致的 {len(record['unchanged'])} 项。"
                     if status == "aligned" else
                     f"模型路由已按本机现有环境设定 {len(record['published'])} 项，"
                     f"还有 {len(record['failed'])} 项没设上（{names}），"
                     "可以在驾驶舱的模型页面自己选，或运行 "
                     "scripts/align_model_routing.py。")}


# ---------------------------------------------------------------------------
# What creation calls, and what the audit reads afterwards.


def adopt_host_scheme(
    state_dir: str | Path,
    *,
    manager_config_path: str | Path | None,
    actor_ref: str,
    source_id: str = LEGACY_ENVIRONMENT_ID,
    legacy_service_path: str | Path | None = None,
    source_state_dir: str | Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Copy this machine's model configuration into a newly created environment.

    Never raises.  A creation that fell over because the environment it was
    copying from was mid-write would be a far worse outcome than one that came
    up on packaged defaults with the reason written into its receipt -- and the
    two scripts that do this by hand are still there for the second case.
    """

    state = Path(state_dir).expanduser().resolve()
    source = resolve_source_state_dir(
        state, manager_config_path=manager_config_path,
        source_state_dir=source_state_dir, source_id=source_id,
        legacy_service_path=legacy_service_path)
    if source is None:
        note = ("这台机器上没有可以参照的现有研究环境，"
                "这个环境用打包的默认模型配置。")
        return {"schema_version": SCHEMA_VERSION, "source_state_dir": None,
                "credential_slots": {"status": "skipped", "note": note},
                "model_routing": {"status": "skipped", "note": note}}
    try:
        slots = align_credential_slots(state, source_state_dir=source)
    except Exception as exc:  # noqa: BLE001 - never fail a creation for this
        slots = {"status": "failed", "note": f"模型凭证槽位没有对齐：{exc}"}
    try:
        routing = align_model_routing(
            state, source_state_dir=source, actor_ref=actor_ref, now=now)
    except Exception as exc:  # noqa: BLE001 - never fail a creation for this
        routing = {"status": "failed", "note": f"模型路由没有对齐：{exc}"}
    return {"schema_version": SCHEMA_VERSION, "source_state_dir": str(source),
            "credential_slots": slots, "model_routing": routing}


def _shared_budget_row(state: Path) -> dict[str, Any]:
    from .shared_daily_budget import (
        BINDING_FILENAME,
        SharedDailyBudgetError,
        load_shared_daily_budget_binding,
        load_shared_daily_budget_policy,
    )

    row: dict[str, Any] = {"key": "shared_daily_budget", "label": "共享每日预算",
                           "blockers": []}
    binding_path = state / BINDING_FILENAME
    if not binding_path.is_file():
        return {**row, "status": "missing", "detail": "这个环境没有绑定本机的共享每日预算",
                "blockers": ["它只受自己任务预算的约束，本机的日上限管不到它；"
                             "运行 scripts/bind_shared_daily_budget.py --apply 绑定。"]}
    try:
        binding = load_shared_daily_budget_binding(binding_path)
        policy = load_shared_daily_budget_policy(binding["policy_path"])
    except SharedDailyBudgetError as exc:
        return {**row, "status": "invalid", "detail": str(binding_path),
                "blockers": [f"绑定文件在，但读不出策略：{exc}"]}
    detail = (f"每天 ${policy['max_daily_cost_usd']}、"
              f"{policy['max_daily_paid_calls']} 次付费调用，"
              f"与本机其它研究环境合并计算（策略第 {policy['revision']} 版）")
    if binding["policy_hash"] != policy["content_hash"]:
        return {**row, "status": "stale", "detail": detail,
                "blockers": ["绑定时记下的策略版本和现在这份不一样（上限被改过）；"
                             "重新运行 scripts/bind_shared_daily_budget.py --apply。"]}
    return {**row, "status": "ok", "detail": detail}


def _routing_row(state: Path, source: Path | None) -> dict[str, Any]:
    row: dict[str, Any] = {"key": "model_routing_alignment",
                           "label": "模型路由与本机其它环境一致", "blockers": []}
    if source is None:
        return {**row, "status": "unknown",
                "detail": "没有找到可以比对的现有研究环境",
                "blockers": ["用 --source-state-dir 指定另一个环境的状态目录，"
                             "或先让这个环境绑定本机环境清单。"]}
    try:
        wanted = agreed_selections(source / ROUTER_FILENAME)
        held = agreed_selections(state / ROUTER_FILENAME)
    except (ModelRoutingSyncError, OSError, ValueError, sqlite3.Error) as exc:
        return {**row, "status": "unknown", "detail": str(source),
                "blockers": [f"读不出路由策略：{exc}"]}
    differences = [
        f"类别 {tier}：这里是 {' → '.join(held['tiers'].get(tier) or []) or '（没有）'}，"
        f"那边是 {' → '.join(chain)}"
        for tier, chain in sorted(wanted["tiers"].items())
        if held["tiers"].get(tier) != chain
    ] + [
        f"环节 {purpose}：这里是 {' → '.join(held['purposes'].get(purpose) or []) or '（没有）'}，"
        f"那边是 {' → '.join(chain)}"
        for purpose, chain in sorted(wanted["purposes"].items())
        if held["purposes"].get(purpose) != chain
    ]
    detail = f"比对的环境：{source}"
    if not differences:
        return {**row, "status": "ok", "detail": detail}
    return {**row, "status": "differs", "detail": detail,
            "blockers": [*_first(differences),
                         "运行 scripts/align_model_routing.py --apply 对齐。"]}


def _first(lines: Sequence[str], limit: int = 8) -> list[str]:
    """The first few differences, and a count for the rest.

    A page that prints forty lines of the same difference is a page nobody
    reads to the end; the number is what the owner needs and the names of the
    first few are what makes it concrete.
    """

    if len(lines) <= limit:
        return list(lines)
    return [*lines[:limit], f"……另有 {len(lines) - limit} 项同类差异。"]


def _slot_row(state: Path, source: Path | None) -> dict[str, Any]:
    row: dict[str, Any] = {"key": "credential_slots", "label": "模型凭证槽位",
                           "blockers": []}
    if source is None:
        return {**row, "status": "unknown", "detail": "没有找到可以比对的现有研究环境",
                "blockers": []}
    wanted = source_credential_slots(source)
    if not wanted:
        return {**row, "status": "unknown", "detail": f"{source} 没有声明凭证槽位",
                "blockers": []}
    short: list[str] = []
    for path in sorted(state.glob("*model-config.json")):
        config = _read_json(path)
        if not isinstance(config, Mapping):
            continue
        missing = [slot for slot in wanted
                   if slot not in (config.get("credential_slot_refs") or [])]
        if missing:
            short.append(f"{path.name}：缺 {'、'.join(missing)}")
    service = _read_json(service_config_path(state))
    for index, held in enumerate(_slot_lists(service)):
        missing = [slot for slot in wanted if slot not in held]
        if missing:
            short.append(f"service.json 第 {index + 1} 处：缺 {'、'.join(missing)}")
    detail = f"本机现有环境有 {len(wanted)} 个槽位"
    if not short:
        return {**row, "status": "ok", "detail": detail}
    return {**row, "status": "differs", "detail": detail,
            "blockers": [*_first(short),
                         "少一个槽位就够让那条链路每次调用都报 credential_slot_unavailable；"
                         "运行 scripts/repair_workspace_lane_parity.py --apply 补齐后重启服务。"]}


def audit_host_scheme(
    state_dir: str | Path,
    *,
    manager_config_path: str | Path | None = None,
    source_state_dir: str | Path | None = None,
    source_id: str = LEGACY_ENVIRONMENT_ID,
) -> list[dict[str, Any]]:
    """The three host-wide settings a created environment should already carry.

    Read-only, like the lane audit it is printed beside, and answered from the
    environment's own files rather than from the creation receipt: a receipt
    records what a creation did, and the question an owner is asking here is
    what is true now.
    """

    state = Path(state_dir).expanduser().resolve()
    source = resolve_source_state_dir(
        state, manager_config_path=manager_config_path,
        source_state_dir=source_state_dir, source_id=source_id)
    return [_shared_budget_row(state), _routing_row(state, source),
            _slot_row(state, source)]


__all__ = [
    "ROUTER_FILENAME", "SCHEMA_VERSION", "SLOT_FIELDS", "adopt_host_scheme",
    "align_credential_slots", "align_model_routing", "audit_host_scheme",
    "copy_missing_profiles", "resolve_manager_config", "resolve_source_state_dir",
    "service_config_path", "source_credential_slots", "source_environment",
]
