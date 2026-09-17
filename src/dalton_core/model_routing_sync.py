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

Nothing here opens the Core, writes a Claim, or touches the OpenClaw
configuration.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
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
        if not isinstance(manifest, Mapping):
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


def latest_policies(router_db: str | Path) -> dict[str, dict[str, Any]]:
    """The head of every policy lineage in one environment, keyed by policy id.

    The strict WAL reader first, because on a *running* environment that is the
    connection that cannot write even by accident and that sees the writer's
    uncommitted-to-main pages.  It refuses a WAL database with no sidecars,
    which is precisely a **stopped** environment -- and a stopped environment is
    exactly the one a fan-out most needs to read, to decide whether to bother
    waking it.  So a plain ``mode=ro`` connection is the fallback: it cannot
    create a file and cannot write, and the main file is the whole truth once
    the writer has gone.
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
        rows = connection.execute(_LATEST_POLICIES_SQL).fetchall()
    except sqlite3.Error as exc:
        raise ModelRoutingSyncError(f"{database} 读不出路由策略：{exc}") from exc
    finally:
        if opened is not None:
            opened.close()
        else:
            connection.close()
    return {row["policy_id"]: json.loads(row["policy_json"]) for row in rows}


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
        }
    return chains


def current_purpose_overrides(router_db: str | Path) -> dict[str, dict[str, Any]]:
    """Every policy's per-stage overrides in one environment, read-only."""

    return {
        policy_id: dict(policy.get("purpose_overrides") or {})
        for policy_id, policy in latest_policies(router_db).items()
    }


def _selection_landed(environment: Environment, params: Mapping[str, Any]) -> bool:
    """Is this environment already on this selection?

    Answered from the environment's *own* policies rather than from a record of
    what was sent, so a fan-out that was interrupted halfway, or a chain the
    owner set by hand with a script, both read as "already there".  Only an
    explicit chain can be answered this way; a ``tier`` mode selection means
    "whatever the built-in chain for this tier is", which is resolved by the
    receiving environment and so is always sent.
    """

    if params.get("mode") != "explicit":
        return False
    wanted = [str(item) for item in (params.get("chain") or [])]
    try:
        if params.get("tier") is not None:
            chains = current_tier_chains(environment.router_db)
            if not chains:
                return False
            return all(
                held.get(str(params["tier"])) == wanted for held in chains.values()
            )
        overrides = current_purpose_overrides(environment.router_db)
        if not overrides:
            return False
        purpose = str(params["purpose"])
        return all(
            (held.get(purpose) or {}).get("mode") == "explicit"
            and [str(item) for item in (held.get(purpose) or {}).get("chain", [])] == wanted
            for held in overrides.values()
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
