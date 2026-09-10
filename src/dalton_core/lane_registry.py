"""One place a tick-driven lane declares itself, so adding one is a line.

A lane is the same shape everywhere it appears: a writer operation the
controller tick calls, a launcher the writer owns and closes, a couple of
command-line arguments the installer passes, and one key in the tick summary.
Before this module those four facts were written out five times -- in
``writer_server``'s operation sets, its ``OPERATION_FIELDS``, its constructor,
its ``close()``, its ``main()``, in ``bounded_planner_driver.run_once`` and in
``macos_launchagent.render`` -- and adding a lane meant finding all of them.
Two lanes added in the same week meant two agents editing the same ten regions.

So a lane says it once, as a :class:`LaneSpec`, in its own module, and the
three places that need to know derive what they need from the registry.  The
registry is deliberately dumb: it holds frozen records and refuses a duplicate.
It does not import a coordinator, open a database or read a file; a lane's
expensive imports stay lazy inside its handler and its launcher factory, which
is what keeps ``writer_server`` free of an import cycle.

``order`` is explicit and unique because the controller tick's lane order is a
real decision -- the filings index runs before web search because they share a
fetch slot -- and an ordering that emerged from the order of ``import``
statements would change silently when someone reorders them.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping


class LaneRegistryError(RuntimeError):
    """A lane registration is malformed or collides with a registered one."""


@dataclass(frozen=True)
class LaunchAgentContext:
    """What a lane may look at when it decides its LaunchAgent arguments.

    ``state`` is the resolved live state directory.  The other two are the
    facts the writer's plist already derives from the service config and that
    two lanes depend on: a lane that drafts with the extraction model
    configuration is absent when there is none, and a lane that stages into
    the Cockpit's inbox is absent when that file is not configured.
    """

    state: Path
    extraction_model_config_path: str | None = None
    candidate_staging_path: str | None = None


@dataclass(frozen=True)
class LaneSpec:
    """Everything the shared machinery needs to know about one lane.

    ``operation``
        The writer operation name, e.g. ``dispatch_mission_statements``.
    ``order``
        Position in the controller tick.  Unique across the registry.
    ``driver_key``
        The key this lane's result takes in the tick summary; ``None`` for a
        lane the controller does not drive.
    ``core_discovery``
        Whether the operation belongs in ``CORE_DISCOVERY_OPERATIONS``, i.e.
        whether the unrestricted core principal may call it as a tick rather
        than only a human.
    ``param_fields``
        The operation's ``OPERATION_FIELDS`` entry.  Usually empty: a tick
        takes no arguments.
    ``handler``
        ``(writer_server, params) -> result``.  It returns
        ``{"status": "unconfigured", "reason": ...}`` rather than raising when
        its launcher is absent, because the tick summary is where an operator
        reads why a lane did nothing.  ``None`` means the writer implements
        ``_op_<operation>`` itself; the registry still supplies the operation
        sets, the fields and the tick order.
    ``init_kwarg``
        The ``WriterServer(...)`` keyword its launcher arrives on.  The writer
        stores every lane launcher by this name and closes it on shutdown.
    ``argparse``
        ``(parser) -> None``; adds this lane's writer command-line arguments.
    ``launcher_factory``
        ``(args) -> launcher | None``; builds the launcher from those
        arguments, or returns ``None`` when the lane is not installed.
    ``argv_fragment``
        ``(LaunchAgentContext) -> list[str]``; the writer LaunchAgent
        arguments that turn this lane on, or ``[]`` when this Core has no
        reason to run it.
    ``budget_pool``
        C2: which of the day's four capacity pools this lane spends from, or
        ``None`` to take the answer from ``budget_pools.LANE_POOLS``, which
        defaults to ``coverage`` -- what every lane effectively was before
        pools existed.  Declared here only by a lane that wants to say it
        itself; the mapping is central so that assigning pools did not mean
        editing fifteen lane modules at once.
    ``pool_share``
        C2: the fraction of its pool this lane declares it needs.  Advisory in
        v1.0: it is reported by ``pool_status`` and not enforced, because a
        per-lane sub-cap would need per-lane attribution in the day ledger,
        which today exists only for cockpit-shaped calls.
    """

    operation: str
    order: int
    driver_key: str | None = None
    core_discovery: bool = True
    param_fields: frozenset[str] = frozenset()
    handler: Callable[[Any, Mapping[str, Any]], Any] | None = None
    init_kwarg: str | None = None
    argparse: Callable[[Any], None] | None = None
    launcher_factory: Callable[[Any], Any] | None = None
    argv_fragment: Callable[[LaunchAgentContext], list[str]] | None = None
    budget_pool: str | None = None
    pool_share: float | None = None
    # Free-form note for the report and for whoever reads the registry next.
    note: str = ""

    def __post_init__(self) -> None:
        if not self.operation or not isinstance(self.operation, str):
            raise LaneRegistryError("a lane needs an operation name")
        if not self.operation.startswith("dispatch_"):
            raise LaneRegistryError(
                f"{self.operation}: a lane operation is a dispatch_* tick"
            )
        if not isinstance(self.order, int):
            raise LaneRegistryError(f"{self.operation}: order must be an integer")
        if not isinstance(self.param_fields, frozenset):
            object.__setattr__(self, "param_fields", frozenset(self.param_fields))
        if self.launcher_factory is not None and self.init_kwarg is None:
            raise LaneRegistryError(
                f"{self.operation}: a launcher factory needs an init_kwarg to arrive on"
            )
        if self.budget_pool is not None:
            # Imported inside the check, not at module scope: ``budget_pools``
            # reads this registry back, and a lane module is imported while the
            # registry is loading.
            from .budget_pools import POOL_NAMES

            if self.budget_pool not in POOL_NAMES:
                raise LaneRegistryError(
                    f"{self.operation}: {self.budget_pool!r} is not a budget pool; "
                    "the pools are " + ", ".join(POOL_NAMES)
                )
        if self.pool_share is not None and (
            isinstance(self.pool_share, bool)
            or not isinstance(self.pool_share, (int, float))
            or not 0 < self.pool_share <= 1
        ):
            raise LaneRegistryError(
                f"{self.operation}: pool_share is a fraction of its pool, 0 < n <= 1"
            )


# The explicit import list.  A lane module is named here once; importing it is
# what registers its spec.  Nothing else may happen at import time.
LANE_MODULES: tuple[str, ...] = (
    "dalton_core.writer_lanes",
    "dalton_core.mission_sec_quarters",
    "dalton_core.mission_statement_lane",
    "dalton_core.mission_market_price_lane",
    "dalton_core.mission_tracking_lane",
    "dalton_core.mission_catalyst_lane",
    "dalton_core.mission_ownership_lane",
    "dalton_core.mission_model_spec_lane",
    "dalton_core.mission_guidepoint_lane",
    "dalton_core.mission_model_forecast_lane",
    "dalton_core.mission_claim_index_lane",
    "dalton_core.research_planner_launcher",
    "dalton_core.initial_screen_launcher",
    "dalton_core.mission_crowd_source_lane",
    "dalton_core.mission_feed_lane",
    "dalton_core.mission_research_task_lane",
    "dalton_core.mission_event_judgement_lane",
    "dalton_core.mission_reopen_lane",
    "dalton_core.mission_reflection_lane",
    "dalton_core.mission_debate_map_lane",
    "dalton_core.mission_dossier_lane",
    "dalton_core.mission_conviction_lane",
)

# The keys the controller tick's summary already uses for things that are not
# lanes: its own status, the probe accounting, and the two calls that run
# outside the lane loop.  A lane whose ``driver_key`` collided with one of
# these would overwrite it -- the lane results are spread last -- and the
# damage would be silent: a tick reporting a lane's result as its own status.
RESERVED_DRIVER_KEYS: frozenset[str] = frozenset({
    "status", "active_loop_count", "probes_executed", "executed", "skipped",
    "mission_sec_dispatch", "forecast_reconciliation",
    # C2: whether this tick wrote itself into the tick ledger.  It is in the
    # summary rather than only in a log because a tick whose own bookkeeping
    # failed must not be indistinguishable from a tick that had nothing to do.
    "tick_ledger",
})

_LANES: dict[str, LaneSpec] = {}
_LOADING = False
_LOADED = False


def register_lane(spec: LaneSpec) -> LaneSpec:
    """Register one lane.  A second lane by the same name is refused."""

    if not isinstance(spec, LaneSpec):
        raise LaneRegistryError("register_lane takes a LaneSpec")
    if spec.operation in _LANES:
        raise LaneRegistryError(f"{spec.operation} is already a registered lane")
    if spec.driver_key in RESERVED_DRIVER_KEYS:
        raise LaneRegistryError(
            f"{spec.operation}: driver key {spec.driver_key!r} is the tick "
            "summary's own; a lane may not overwrite it"
        )
    for existing in _LANES.values():
        if existing.order == spec.order:
            raise LaneRegistryError(
                f"{spec.operation}: order {spec.order} is already taken by "
                f"{existing.operation}; lane order is explicit"
            )
        if spec.driver_key is not None and existing.driver_key == spec.driver_key:
            raise LaneRegistryError(
                f"{spec.operation}: driver key {spec.driver_key!r} is already "
                f"taken by {existing.operation}"
            )
        if spec.init_kwarg is not None and existing.init_kwarg == spec.init_kwarg:
            raise LaneRegistryError(
                f"{spec.operation}: init kwarg {spec.init_kwarg!r} is already "
                f"taken by {existing.operation}"
            )
    _LANES[spec.operation] = spec
    return spec


def unregister_lane(operation: str) -> None:
    """Remove a lane.  For tests; a live process never unregisters."""

    _LANES.pop(operation, None)


def load_lanes() -> None:
    """Import every module in :data:`LANE_MODULES` exactly once."""

    global _LOADING, _LOADED
    if _LOADED or _LOADING:
        return
    _LOADING = True
    try:
        for name in LANE_MODULES:
            importlib.import_module(name)
        _LOADED = True
    finally:
        _LOADING = False


def require_lanes_loaded() -> None:
    """Refuse to derive anything from a registry that is still loading.

    ``load_lanes()`` returns silently on re-entry, which is what keeps a
    circular import from recursing.  The cost is that a lane module importing
    ``writer_server`` -- directly or three modules down -- would have
    ``writer_server`` fold in whatever happened to be registered by then: the
    lane would be in ``registered_lanes()``, so the tick would call it and the
    plist would wire it, and absent from ``OPERATION_FIELDS``, so the writer
    would answer "unknown operation" for the life of the process.  A tick
    reporting ``unavailable:PermissionError`` forever is a bad way to learn
    about an import cycle, so the derivation refuses instead.
    """

    if _LOADING:
        raise LaneRegistryError(
            "the lane registry is still loading; a lane module must not import "
            "a module that derives from the registry (writer_server), even "
            "indirectly -- keep those imports inside the handler"
        )


def registered_lanes() -> tuple[LaneSpec, ...]:
    """Every registered lane, in controller-tick order."""

    load_lanes()
    return tuple(sorted(_LANES.values(), key=lambda spec: spec.order))


def tick_lanes() -> tuple[LaneSpec, ...]:
    """The lanes the controller tick drives, in order."""

    return tuple(spec for spec in registered_lanes() if spec.driver_key is not None)


def lane_for_operation(operation: str) -> LaneSpec | None:
    load_lanes()
    return _LANES.get(operation)


def lane_operations() -> frozenset[str]:
    return frozenset(spec.operation for spec in registered_lanes())


def core_discovery_operations() -> frozenset[str]:
    return frozenset(
        spec.operation for spec in registered_lanes() if spec.core_discovery
    )


def lane_operation_fields() -> dict[str, frozenset[str]]:
    return {spec.operation: spec.param_fields for spec in registered_lanes()}


def lane_init_kwargs() -> frozenset[str]:
    return frozenset(
        spec.init_kwarg for spec in registered_lanes() if spec.init_kwarg is not None
    )


def lane_argv(context: LaunchAgentContext) -> list[str]:
    """Concatenate every lane's writer LaunchAgent arguments, in lane order."""

    argv: list[str] = []
    for spec in registered_lanes():
        if spec.argv_fragment is None:
            continue
        argv.extend(spec.argv_fragment(context))
    return argv


def add_lane_arguments(parser: Any) -> None:
    """Let every lane add its own writer command-line arguments."""

    for spec in registered_lanes():
        if spec.argparse is not None:
            spec.argparse(parser)


def build_lane_launchers(args: Any) -> dict[str, Any]:
    """Build every lane's launcher from parsed writer arguments.

    Absent lanes are absent: a factory that returns ``None`` contributes
    nothing, so the writer never has to distinguish "not installed" from
    "installed and broken".
    """

    launchers: dict[str, Any] = {}
    for spec in registered_lanes():
        if spec.launcher_factory is None:
            continue
        launcher = spec.launcher_factory(args)
        if launcher is not None:
            launchers[spec.init_kwarg] = launcher
    return launchers


__all__ = [
    "LANE_MODULES",
    "RESERVED_DRIVER_KEYS",
    "LaneRegistryError",
    "LaneSpec",
    "LaunchAgentContext",
    "add_lane_arguments",
    "build_lane_launchers",
    "core_discovery_operations",
    "lane_argv",
    "lane_for_operation",
    "lane_init_kwargs",
    "lane_operation_fields",
    "lane_operations",
    "load_lanes",
    "register_lane",
    "registered_lanes",
    "require_lanes_loaded",
    "tick_lanes",
    "unregister_lane",
]
