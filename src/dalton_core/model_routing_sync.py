"""One model configuration for the whole host, not one per environment.

The owner runs several research environments on this Mac -- the legacy one the
IT-services coverage lives in, plus a Cockpit per workspace -- and until now
each carried its own ``model-router.sqlite`` with its own routing policy
lineage.  That was defensible while a workspace was an experiment.  It stopped
being defensible the moment the owner started *choosing* models: a chain
repaired in one Cockpit left the others spending money on an endpoint that had
been taken out of service, and the only way to notice was to open every page.

The owner's instruction of 2026-09-17 is therefore: **the model configuration
is the same in every environment.**  A save in any environment is a save in all
of them.

The mechanism is deliberately the dullest one available.  This module does not
write a database, does not invent a cross-environment policy identity, and does
not make one environment's router authoritative over another's.  It enumerates
the environments read-only from the host manager configuration and then makes
the *same owner call the cockpit just made* against each other environment's
writer -- ``set_model_selection``, through that writer's own socket and token
config, with the same actor.  Every environment therefore still publishes its
own immutable policy version, in its own lineage, owned by its own single
writer.  Nothing here bypasses the single-writer rule; the fan-out is a client.

Three properties follow from doing it that way, and they are the reason it is
done that way:

* **The local save is never undone.**  The fan-out runs after the local
  publication has committed.  An environment that is stopped, or whose writer
  refuses, is reported -- in the save result the owner reads -- and nothing is
  rolled back.  A configuration that is right in three places out of four is
  better than one that is right nowhere because the fourth was asleep.
* **It is idempotent.**  ``publish_tier_selection`` already returns
  ``duplicate`` and appends nothing when the content would not change, so a
  fan-out that arrives at an environment already on that chain is a read.  This
  module additionally *checks before calling*, so a quiet environment is not
  even woken for a selection it already has.
* **It cannot loop.**  A fanned-out call carries no instruction to fan out
  again: only the cockpit's own save handler calls this module, and a writer
  serving ``set_model_selection`` does not.

**Which lineages a comparison asks (2026-10-02).**  An environment holds
service-pin lineages -- ``dalton-openclaw``, ``-assessment``, ``-verifier``,
and in the legacy environment also ``-planner`` and ``-smoke`` -- that declare
one tier or none.  The source side has always ignored a lineage that does not
declare a tier ("not disagreeing about it"); the target side did not, so every
alignment run reported those lineages as "（没有这一项）" for tiers and stages
no writer call can ever give them, and "还有 N 个环境与源环境不同" was a
permanent false positive.  Both sides, and the fan-out's "already there"
check, now ask only ``tier_holders`` / ``purpose_holders``: the lineages that
declare the tier (or, for a stage, carry its pin or declare its tier), falling
back to every lineage when none does.  ``route_activity`` additionally lets the
alignment script resolve a *source* conflict that only idle lineages cause --
lineages that made no route decision recently -- in favour of the lineages that
do route, saying so in ``idle_ignored``; a conflict among routing lineages is
still the owner's decision, now shown per lineage in ``conflict_details``.

Nothing here opens the Core, writes a Claim, or touches the OpenClaw
configuration.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# Where the legacy environment keeps its service configuration when the host
# manager configuration does not say.  The manager config describes the
# *workspaces* it created; the legacy environment predates it and is named there
# only by URL, so its paths come from its own service.json -- the same file the
# launch agent starts it with.
LEGACY_SERVICE_CANDIDATES: tuple[str, ...] = (
    "~/Library/Application Support/Dalton/config/service.json",
)
LEGACY_ENVIRONMENT_ID = "legacy"


class ModelRoutingSyncError(RuntimeError):
    """The host's environments cannot be enumerated as asked."""


@dataclass(frozen=True)
class Environment:
    """One research environment on this host, named by its own files.

    Everything needed to *talk to* it (socket, token config) and to *read* it
    (router database) without talking to it.  Deliberately no port and no URL:
    a routing change goes through the writer, never through the Cockpit's HTTP
    surface, so a fan-out never needs a credential the owner has to rotate.
    """

    environment_id: str
    slug: str
    name: str
    state_dir: Path
    writer_socket: Path
    token_config: Path
    router_db: Path
    budget_db: Path
    core_db: Path

    @property
    def is_running(self) -> bool:
        """Whether this environment's writer is listening right now."""

        return self.writer_socket.is_socket()

    def as_dict(self) -> dict[str, Any]:
        return {
            "environment_id": self.environment_id,
            "slug": self.slug,
            "name": self.name,
            "state_dir": str(self.state_dir),
            "writer_socket": str(self.writer_socket),
            "token_config": str(self.token_config),
            "router_db": str(self.router_db),
            "budget_db": str(self.budget_db),
            "core_db": str(self.core_db),
        }


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ModelRoutingSyncError(f"{path} 读不出来或不是合法 JSON：{exc}") from exc


def _environment(
    *, environment_id: str, slug: str, name: str, state_dir: Path,
    writer_socket: Path | None = None,
) -> Environment:
    """Fill in the file names every environment lays out the same way.

    The state directory is the environment's identity: the writer's socket, its
    token config, its router and its day ledger all sit at fixed names inside
    it, which is what lets a workspace manifest name only the directory.
    """

    state = Path(state_dir).expanduser()
    return Environment(
        environment_id=environment_id,
        slug=slug,
        name=name,
        state_dir=state,
        writer_socket=(Path(writer_socket).expanduser() if writer_socket
                       else state / "run" / "writer.sock"),
        token_config=state / "writer-tokens.json",
        router_db=state / "model-router.sqlite",
        budget_db=state / "thesis-impact-budget.sqlite",
        core_db=state / "core.sqlite",
    )


def legacy_environment(
    manager_config: Mapping[str, Any] | None = None,
    *,
    service_path: str | Path | None = None,
) -> Environment | None:
    """The legacy (IT services) environment, from its own service.json.

    Returns ``None`` rather than raising when the legacy environment is not
    installed on this host: a machine that only has workspaces is a machine
    whose fan-out simply has one fewer target, not a broken one.
    """

    declared_raw = (manager_config or {}).get("legacy_workspace")
    if manager_config is not None and not declared_raw:
        # A host manager configuration that does not declare a legacy
        # environment does not have one. Falling back to the conventional path
        # regardless would make any host -- or any test -- silently enumerate
        # the real installation on this Mac, which is the one mistake a module
        # that fans writes out must not make.
        return None
    declared = dict(declared_raw or {})
    name = declared.get("name") or "现有研究环境"
    if declared.get("state_dir"):
        # A manager configuration that has learned to name the legacy paths is
        # believed over the convention below.
        return _environment(
            environment_id=LEGACY_ENVIRONMENT_ID, slug=LEGACY_ENVIRONMENT_ID,
            name=name, state_dir=Path(declared["state_dir"]),
        )
    candidates: list[Path] = []
    if service_path is not None:
        candidates.append(Path(service_path).expanduser())
    if declared.get("config_path"):
        candidates.append(Path(declared["config_path"]).expanduser())
    candidates.extend(Path(item).expanduser() for item in LEGACY_SERVICE_CANDIDATES)
    for candidate in candidates:
        if not candidate.is_file():
            continue
        service = _read_json(candidate)
        if not isinstance(service, Mapping):
            continue
        control = ((service.get("control") or {}).get("config") or {})
        cockpit = control.get("cockpit") or {}
        state_dir = cockpit.get("state_dir")
        if not isinstance(state_dir, str):
            continue
        return _environment(
            environment_id=LEGACY_ENVIRONMENT_ID, slug=LEGACY_ENVIRONMENT_ID,
            name=name, state_dir=Path(state_dir),
            writer_socket=(Path(control["writer_socket"])
                           if isinstance(control.get("writer_socket"), str) else None),
        )
    return None


def _workspace_name(workspace_root: Path, fallback: str) -> str:
    """The owner's own name for a workspace, if they have given it one."""

    display = workspace_root / "display.json"
    if display.is_file():
        try:
            value = json.loads(display.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            return fallback
        if isinstance(value, Mapping) and isinstance(value.get("display_name"), str):
            return value["display_name"]
    return fallback


def host_environments(
    manager_config_path: str | Path,
    *,
    legacy_service_path: str | Path | None = None,
) -> list[Environment]:
    """Every research environment on this host, legacy first.

    Read-only throughout: manifests and service configurations are parsed, no
    database is opened and no directory is created.  A manifest that cannot be
    read is skipped rather than fatal -- a half-provisioned workspace must not
    stop the owner saving a model in the environment they are looking at.
    """

    config_path = Path(manager_config_path).expanduser()
    config = _read_json(config_path) if config_path.is_file() else {}
    if not isinstance(config, Mapping):
        raise ModelRoutingSyncError(f"{config_path} 不是合法的 manager 配置")
    found: list[Environment] = []
    legacy = legacy_environment(config, service_path=legacy_service_path)
    if legacy is not None:
        found.append(legacy)
    host_root = Path(
        config.get("host_root") or config_path.parent
    ).expanduser()
    for manifest_path in sorted((host_root / "workspaces").glob("*/workspace.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            continue
        if not isinstance(manifest, Mapping) or manifest.get("lifecycle", "active") != "active":
            continue
        state_dir = manifest.get("state_dir")
        slug = manifest.get("slug") or manifest_path.parent.name
        if not isinstance(state_dir, str):
            continue
        found.append(_environment(
            environment_id=str(manifest.get("workspace_id") or slug),
            slug=str(slug),
            name=_workspace_name(manifest_path.parent, str(slug)),
            state_dir=Path(state_dir),
            writer_socket=(Path(manifest["writer_socket"])
                           if isinstance(manifest.get("writer_socket"), str) else None),
        ))
    return found


def environment_for_state_dir(
    environments: Sequence[Environment], state_dir: str | Path,
) -> Environment | None:
    """Which of these environments is the one making the call.

    Compared on the *resolved* path because the legacy environment's state
    directory is a symlink onto the external disk, and the cockpit is
    configured with the symlink while a manifest may name the target.
    """

    target = Path(state_dir).expanduser()
    resolved = target.resolve()
    for environment in environments:
        if environment.state_dir == target or environment.state_dir.resolve() == resolved:
            return environment
    return None


# ---------------------------------------------------------------------------
# What a selection looks like once it has landed, so a fan-out can tell whether
# it needs to happen at all.


_LATEST_POLICIES_SQL = (
    "SELECT policy_id, policy_json FROM model_routing_policy_versions p "
    "WHERE p.version=(SELECT MAX(q.version) FROM model_routing_policy_versions q "
    "WHERE q.policy_id=p.policy_id) ORDER BY p.policy_id"
)


def _query_router(
    router_db: str | Path, sql: str, params: Sequence[Any] = (),
) -> list[Any]:
    """Run one read-only query against one environment's router.

    The strict WAL reader first, because on a *running* environment that is the
    connection that cannot write even by accident and that sees the writer's
    uncommitted-to-main pages.  It refuses a WAL database with no sidecars,
    which is precisely a **stopped** environment -- and a stopped environment is
    exactly the one a fan-out most needs to read, to decide whether to bother
    waking it.  So a plain ``mode=ro`` connection is the fallback: it cannot
    create a file and cannot write, and the main file is the whole truth once
    the writer has gone.

    ``sqlite3.Error`` is left to the caller, which knows whether an unreadable
    table is fatal (the policies) or merely unknown (the route decisions).
    """

    import sqlite3

    from .model_router import ModelRouter

    database = Path(router_db).expanduser()
    if not database.is_file():
        raise ModelRoutingSyncError(f"这里没有模型路由数据库：{database}")
    try:
        opened: Any = ModelRouter(str(database), read_only=True)
        connection = opened.connection
    except (sqlite3.OperationalError, ValueError):
        opened = None
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
    try:
        return connection.execute(sql, tuple(params)).fetchall()
    finally:
        if opened is not None:
            opened.close()
        else:
            connection.close()


def latest_policies(router_db: str | Path) -> dict[str, dict[str, Any]]:
    """The head of every policy lineage in one environment, keyed by policy id.

    Read through ``_query_router``: strict WAL reader on a running environment,
    plain ``mode=ro`` on a stopped one.
    """

    import sqlite3

    try:
        rows = _query_router(router_db, _LATEST_POLICIES_SQL)
    except sqlite3.Error as exc:
        raise ModelRoutingSyncError(
            f"{Path(router_db).expanduser()} 读不出路由策略：{exc}") from exc
    return {row["policy_id"]: json.loads(row["policy_json"]) for row in rows}


_ROUTE_ACTIVITY_SQL = (
    "SELECT v.policy_id AS policy_id, COUNT(*) AS decisions, "
    "MAX(d.created_at) AS last_decision_at "
    "FROM model_route_decisions d JOIN model_routing_policy_versions v "
    "ON v.policy_version_ref = d.policy_version_ref "
    "WHERE d.created_at >= ? GROUP BY v.policy_id"
)


def route_activity(
    router_db: str | Path, *, since: datetime,
) -> dict[str, dict[str, Any]] | None:
    """Which policy lineages actually routed a call since ``since``.

    Keyed by ``policy_id``; a lineage absent from the result made no route
    decision in the window.  ``None`` -- not an empty dict -- when the decision
    table cannot be read at all, so a caller can tell "nobody routed" from "I
    do not know who routed" and never resolves a conflict on the second.
    Read-only, through the same connection rules as ``latest_policies``.
    """

    import sqlite3

    stamp = since.astimezone(timezone.utc).isoformat(timespec="microseconds")
    try:
        rows = _query_router(router_db, _ROUTE_ACTIVITY_SQL, (stamp,))
    except sqlite3.Error:
        return None
    return {
        str(row["policy_id"]): {"decisions": int(row["decisions"]),
                                "last_decision_at": row["last_decision_at"]}
        for row in rows
    }


def current_tier_chains(router_db: str | Path) -> dict[str, dict[str, list[str]]]:
    """Every policy's tier chains in one environment, read-only.

    Keyed by ``policy_id`` because an environment has more than one lineage --
    each lane pins its own -- and "the same configuration" means every one of
    them carries the same chains, not that one of them does.
    """

    chains: dict[str, dict[str, list[str]]] = {}
    for policy_id, policy in latest_policies(router_db).items():
        tiers = (policy.get("fallback_chains") or {}).get("tiers") or {}
        chains[policy_id] = {
            str(tier): [str(link) for link in (links or [])]
            for tier, links in tiers.items()
            if tier != "legacy_default" and not str(tier).startswith("saved_default_")
        }
    return chains


def current_purpose_overrides(router_db: str | Path) -> dict[str, dict[str, Any]]:
    """Every policy's per-stage overrides in one environment, read-only."""

    return {
        policy_id: dict(policy.get("purpose_overrides") or {})
        for policy_id, policy in latest_policies(router_db).items()
    }


def tier_holders(
    tier_chains: Mapping[str, Mapping[str, Sequence[str]]], tier: str,
) -> set[str]:
    """The policy lineages that take part in ``tier`` in one environment.

    A lineage that does not declare a tier is not *disagreeing* about it and is
    not *behind* on it: it has no chain there.  Environments carry such
    lineages for good reasons -- ``dalton-openclaw``, ``-assessment``,
    ``-verifier`` (and in the legacy environment ``-planner`` and ``-smoke``)
    are service-pin lineages that declare one tier or none -- and the writer's
    ``set_model_selection`` / ``set_tier_selection`` only ever reaches the
    lineages its model configurations pin *for that tier's stages*, so no save
    could ever give them the tier.  Counting them as different made every
    alignment report a permanent false positive (2026-10-02).

    This is the same rule ``agreed_selections`` has always applied on the
    source side; the target side, the fan-out's "already there" check and the
    alignment script now all read through this one function.

    When *no* lineage declares the tier, every lineage takes part: an
    environment that is missing the tier altogether is genuinely behind, and a
    save gives the lineages its lanes pin the chain, after which the rule
    above takes over and the report converges.
    """

    declaring = {policy_id for policy_id, held in tier_chains.items() if held.get(tier)}
    return declaring or set(tier_chains)


def purpose_holders(
    tier_chains: Mapping[str, Mapping[str, Sequence[str]]],
    overrides: Mapping[str, Mapping[str, Any]],
    purpose: str,
) -> set[str]:
    """The policy lineages that take part in one stage's pin.

    A lineage takes part when it already carries an entry for the stage, or
    when it declares the tier the stage belongs to.  The second clause is what
    keeps the post-apply residual honest: a lane lineage can use the shared
    default without a purpose override and must still be included in the
    comparison. A service-pin lineage that does
    not declare the tier (``dalton-openclaw`` for a cheap-tier stage) is not.
    As with ``tier_holders``, when nothing qualifies every lineage takes part.
    """

    from .model_fallback_chain import purpose_tiers

    tier = purpose_tiers().get(purpose)
    lineages = set(tier_chains) | set(overrides)
    holding = {
        policy_id for policy_id in lineages
        if (overrides.get(policy_id) or {}).get(purpose) is not None
        or (tier is not None and (tier_chains.get(policy_id) or {}).get(tier))
    }
    return holding or lineages


def _groups(values: Mapping[str, Any]) -> list[tuple[Any, list[str]]]:
    """Distinct values, each with the lineages holding it, in first-seen order."""

    groups: list[tuple[Any, list[str]]] = []
    for policy_id, value in values.items():
        for held, members in groups:
            if held == value:
                members.append(policy_id)
                break
        else:
            groups.append((value, [policy_id]))
    return groups


def _agreed(values: Mapping[str, Any], subject: str) -> tuple[Any | None, list[str]]:
    """The one value every policy lineage agrees on, or nothing and a complaint.

    An environment has several policy lineages -- each lane pins its own -- and
    "this environment's configuration" only means something if they say the
    same thing.  Where they do not, this refuses to pick a winner: copying one
    lineage's chain over the others would be the machine inventing a decision
    the owner never made.
    """

    distinct = [value for value, _members in _groups(values)]
    if not distinct:
        return None, [f"{subject}：源环境没有这一项"]
    if len(distinct) > 1:
        rendered = " / ".join(json.dumps(item, ensure_ascii=False, sort_keys=True)
                              for item in distinct)
        return None, [f"{subject}：源环境内部就不一致（{rendered}），需要你先决定用哪一条"]
    return distinct[0], []


def _resolve(
    values: Mapping[str, Any], subject: str, *, kind: str, name: str,
    active_policy_ids: set[str] | None,
    conflict_details: list[dict[str, Any]],
    idle_ignored: list[dict[str, Any]],
) -> tuple[Any | None, list[str]]:
    """``_agreed``, plus the two things an owner needs to act on a conflict.

    With ``active_policy_ids`` given, a disagreement that exists *only*
    because of lineages which routed no call in the caller's window is
    resolved in favour of the lineages that did route -- provided at least one
    did and all of those agree.  The ignored lineages are returned in
    ``idle_ignored`` so the resolution is said out loud, never silent.  A
    disagreement among routing lineages, or one where nobody routed, stays a
    conflict, and ``conflict_details`` names which lineage holds which value
    and whether it routes.
    """

    value, complaints = _agreed(values, subject)
    if complaints and active_policy_ids is not None and len(values) > 1:
        active = {key: item for key, item in values.items() if key in active_policy_ids}
        if active:
            resolved, still = _agreed(active, subject)
            if not still:
                idle_ignored.append({
                    "kind": kind, "subject": name, "chain": resolved,
                    "ignored": [{"policy_id": key, "chain": item}
                                for key, item in sorted(values.items())
                                if key not in active and item != resolved],
                })
                return resolved, []
    if complaints and len(values) > 1:
        conflict_details.append({
            "kind": kind, "subject": name,
            "values": [
                {"chain": held, "policy_ids": sorted(members),
                 "routing": (None if active_policy_ids is None else
                             sorted(key for key in members if key in active_policy_ids))}
                for held, members in _groups(values)
            ],
        })
    return value, complaints


def agreed_selections(
    router_db: str | Path, *, active_policy_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Every tier chain and per-stage override one environment stands behind.

    The reading half of ``scripts/align_model_routing.py``, moved here when
    workspace creation needed the same answer: a new environment starts on the
    source environment's current selections, and it must read them exactly the
    way the alignment script does or "aligned at creation" and "aligned by the
    script" would be two different configurations.

    ``active_policy_ids`` (2026-10-02) is the set of lineages that routed a call
    recently (see ``route_activity``).  Given it, a conflict that only idle
    lineages cause is resolved to the routing lineages' value and listed in
    ``idle_ignored``; without it (the default, and what workspace creation and
    the readiness row pass) the behaviour is exactly the old one.  Either way
    ``conflict_details`` says, per remaining conflict, which lineages hold
    which value.  ``tiers``, ``purposes`` and ``conflicts`` keep their shape.

    Read-only: it opens the router the same way ``latest_policies`` does and
    writes nothing.
    """

    active = None if active_policy_ids is None else {str(item) for item in active_policy_ids}
    tier_chains = current_tier_chains(router_db)
    overrides = current_purpose_overrides(router_db)
    tiers: dict[str, list[str]] = {}
    purposes: dict[str, list[str]] = {}
    conflicts: list[str] = []
    conflict_details: list[dict[str, Any]] = []
    idle_ignored: list[dict[str, Any]] = []
    for tier in sorted({name for held in tier_chains.values() for name in held}):
        # A lineage that does not declare this tier is not disagreeing about
        # it; it simply has no chain there. Only the lineages that hold one
        # have to agree, or there is nothing to copy.
        declared = {policy_id: held[tier] for policy_id, held in tier_chains.items()
                    if held.get(tier)}
        chain, complaints = _resolve(
            declared, f"类别「{tier}」", kind="tier", name=tier,
            active_policy_ids=active, conflict_details=conflict_details,
            idle_ignored=idle_ignored)
        conflicts.extend(complaints)
        if chain:
            tiers[tier] = list(chain)
    for purpose in sorted({name for held in overrides.values() for name in held}):
        entries = {policy_id: held.get(purpose)
                   for policy_id, held in overrides.items()}
        if any(entry is None for entry in entries.values()):
            # A stage pinned in one lineage and not in another is not a
            # disagreement about the model; it is a lineage that never had that
            # stage. Only the pinned ones are propagated.
            entries = {key: value for key, value in entries.items() if value is not None}
        explicit = any((value or {}).get("mode") == "explicit" for value in entries.values())
        selection, complaints = _resolve(
            {key: (value or {}).get("chain") if (value or {}).get("mode") == "explicit"
             else None for key, value in entries.items()},
            f"环节「{purpose}」", kind="purpose", name=purpose,
            active_policy_ids=active,
            conflict_details=conflict_details if explicit else [],
            idle_ignored=idle_ignored if explicit else [],
        )
        if complaints and explicit:
            conflicts.extend(complaints)
        if selection:
            purposes[purpose] = list(selection)
    return {"tiers": tiers, "purposes": purposes, "conflicts": conflicts,
            "conflict_details": conflict_details, "idle_ignored": idle_ignored}


def _selection_landed(environment: Environment, params: Mapping[str, Any]) -> bool:
    """Is this environment already on this selection?

    Answered from the environment's *own* policies rather than from a record of
    what was sent, so a fan-out that was interrupted halfway, or a chain the
    owner set by hand with a script, both read as "already there".  Only an
    explicit chain can be answered this way; a ``tier`` mode selection means
    "whatever the built-in chain for this tier is", which is resolved by the
    receiving environment and so is always sent.

    Only the lineages that take part in the tier or stage are asked
    (``tier_holders`` / ``purpose_holders``): a service-pin lineage the save
    can never reach must not keep an aligned environment looking unaligned.
    """

    if params.get("thinking_level") is not None:
        try:
            from .model_selection import model_configs, _latest_policy
            from .model_router import ModelRouter, ModelRouterError
            pins = model_configs(environment.state_dir)
            if not pins:
                return False
            with ModelRouter(environment.router_db, read_only=True) as router:
                for pin in pins:
                    policy = _latest_policy(router, router.get_policy(pin["config"]["routing_policy_ref"])["id"])
                    override = (policy.get("thinking_level_overrides") or {}).get(params["purpose"], {})
                    if override.get("level") != params["thinking_level"] or override.get("reason") != params.get("reason"):
                        return False
            return True
        except (OSError, ValueError, KeyError, ModelRouterError):
            return False
    if params.get("mode") != "explicit":
        return False
    wanted = [str(item) for item in (params.get("chain") or [])]
    try:
        chains = current_tier_chains(environment.router_db)
        if params.get("tier") is not None:
            if not chains:
                return False
            tier = str(params["tier"])
            return all(chains[policy_id].get(tier) == wanted
                       for policy_id in tier_holders(chains, tier))
        overrides = current_purpose_overrides(environment.router_db)
        if not overrides:
            return False
        purpose = str(params["purpose"])
        return all(
            (overrides.get(policy_id, {}).get(purpose) or {}).get("mode") == "explicit"
            and [str(item) for item in
                 (overrides.get(policy_id, {}).get(purpose) or {}).get("chain", [])] == wanted
            for policy_id in purpose_holders(chains, overrides, purpose)
        )
    except (ModelRoutingSyncError, OSError, ValueError, KeyError):
        # Cannot read it, cannot claim it is done. The call will be made and
        # the receiving writer will decide; publishing a duplicate is free.
        return False


# ---------------------------------------------------------------------------
# The fan-out itself.


def _default_writer_call(
    environment: Environment, *, actor_ref: str, params: Mapping[str, Any],
) -> Any:
    """Make the owner's selection against one environment's own writer.

    The same ephemeral human principal the governance CLI and the model page
    already use: a one-operation token, written into that environment's token
    config under its lock, removed again in a ``finally``.  No long-lived
    cross-environment credential is created, and an environment that is not
    running simply refuses the connection.
    """

    from .governance_cli import ephemeral_call

    return ephemeral_call(
        environment.token_config, environment.writer_socket,
        actor_ref=actor_ref, operation="set_model_selection", params=dict(params),
    )


def selection_params(
    *, tier: str | None = None, purpose: str | None = None,
    mode: str, chain: Sequence[str] = (),
) -> dict[str, Any]:
    """The ``set_model_selection`` parameters for one selection.

    One place builds them so the local save and every fanned-out copy are
    byte-identical requests; a divergence here would be a configuration that
    differs between environments for no reason a person could see.
    """

    if (tier is None) == (purpose is None):
        raise ModelRoutingSyncError("一次只能保存一个 tier 或一个环节")
    params: dict[str, Any] = {"mode": str(mode)}
    if mode == "explicit":
        params["chain"] = [str(item) for item in chain]
    if tier is not None:
        params["tier"] = str(tier)
    else:
        params["purpose"] = str(purpose)
    return params


def fan_out_selection(
    *,
    manager_config_path: str | Path,
    origin_state_dir: str | Path,
    actor_ref: str,
    params: Mapping[str, Any],
    legacy_service_path: str | Path | None = None,
    writer_call: Callable[..., Any] | None = None,
    environments: Iterable[Environment] | None = None,
) -> dict[str, Any]:
    """Publish one selection into every environment except the one that saved it.

    Never raises for a target's sake.  The local publication has already
    committed by the time this runs, and the owner's instruction is that the
    configuration be the same everywhere -- an instruction that is served by
    three environments out of four agreeing and the fourth being *named*, and
    not at all by an exception that makes the page look as if the save failed.

    Returns the counts and the failures, in the owner's words, for the save
    result to carry back to the model page.
    """

    call = writer_call or _default_writer_call
    try:
        found = (list(environments) if environments is not None
                 else host_environments(manager_config_path,
                                        legacy_service_path=legacy_service_path))
    except ModelRoutingSyncError as exc:
        return {
            "status": "unavailable", "synced": [], "skipped": [], "failed": [],
            "environment_count": 0,
            "note": f"没有同步到其它环境：{exc}",
        }
    origin = environment_for_state_dir(found, origin_state_dir)
    targets = [item for item in found
               if origin is None or item.environment_id != origin.environment_id]
    synced: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for environment in targets:
        if _selection_landed(environment, params):
            skipped.append({"environment_id": environment.environment_id,
                            "name": environment.name, "reason": "already_selected"})
            continue
        try:
            outcome = call(environment, actor_ref=actor_ref, params=params)
        except Exception as exc:  # noqa: BLE001 - one environment, not the save
            # A lost answer is not a lost publication: the writer owns its own
            # databases and finishes what it started. Re-read before calling it
            # a failure, exactly as the repair scripts do.
            if _selection_landed(environment, params):
                synced.append({"environment_id": environment.environment_id,
                               "name": environment.name, "status": "published",
                               "note": "writer 的回执丢了，但新版本已经在那边了"})
                continue
            failed.append({"environment_id": environment.environment_id,
                           "name": environment.name, "reason": str(exc)})
            continue
        status = (outcome or {}).get("status") if isinstance(outcome, Mapping) else None
        if status == "unchanged":
            skipped.append({"environment_id": environment.environment_id,
                            "name": environment.name, "reason": "already_selected"})
            continue
        synced.append({"environment_id": environment.environment_id,
                       "name": environment.name, "status": status or "published"})
    return {
        "status": "ok" if not failed else "partial",
        "environment_count": len(targets),
        "synced": synced,
        "skipped": skipped,
        "failed": failed,
        "note": sync_note(synced, skipped, failed),
    }


def sync_note(
    synced: Sequence[Mapping[str, Any]],
    skipped: Sequence[Mapping[str, Any]],
    failed: Sequence[Mapping[str, Any]],
) -> str:
    """What the owner reads on the model page after a save.

    Written here rather than in the page so the same sentence appears in the
    save result, in the alignment script and in a writer log line: the owner
    should not have to learn two vocabularies for one fact.
    """

    if not synced and not skipped and not failed:
        return "这台机器上只有这一个研究环境。"
    parts: list[str] = []
    if synced:
        parts.append(f"已同步到 {len(synced)} 个环境")
    if skipped:
        parts.append(f"{len(skipped)} 个环境本来就是这个配置")
    if failed:
        names = "、".join(
            f"{item.get('name') or item.get('environment_id')}（{item.get('reason')}）"
            for item in failed
        )
        parts.append(f"{len(failed)} 个环境未同步：{names}")
    return "；".join(parts) + "。"
