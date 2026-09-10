"""P14-M2: keep Dalton's model catalog following the gateway's, hourly.

The owner asked that new models register themselves as available rather than
waiting for somebody to notice.  This is the lane that does it.

Once an hour it reads two subtrees of ``openclaw.json`` -- ``models.providers``
and the broker plugin's own entry -- and makes the router's catalog agree with
what the broker offers:

* a model the broker offers with no profile here is registered as a live
  profile, with the provider's own rate card;
* a model the gateway offers with no published price is registered too, marked
  ``unpriced``, which the router reads as "only ever the last link of a chain":
  a call whose cost cannot be estimated cannot be admitted against a day budget
  honestly, and hiding the model would be worse than admitting it last;
* a profile the broker no longer offers is **retired, never deleted** -- the
  existing catalog sync appends a version saying so, so a route decision from
  June still resolves its profile and the version chain still reads end to end.

Three things it deliberately does not do.  It never writes ``openclaw.json``:
letting a model through is a change to a host configuration file and therefore
the owner's decision, taken in the cockpit through a governance operation.  It
never calls a model: registering a profile is bookkeeping, and a lane that
smoke-tested every new model would spend money on the owner's behalf every time
the gateway grew one.  And it holds no discretion about *which* models to
register -- the broker's allow-list is the decision, and this lane's whole job
is to stop that decision from having to be repeated here.

Costs nothing when nothing has changed: the sync's three moves are each
conditioned on a difference, so the second run of an hour writes nothing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Mapping

from .lane_registry import LaneSpec, register_lane

LAUNCHER_KWARG = "model_catalog_launcher"
CONFIG_FILE_NAME = "model-catalog-sync.json"
# Once an hour.  The broker's catalog changes when a person edits a file; the
# cost of noticing an hour late is that a new model is available an hour late,
# and the cost of looking more often is a pointless read of somebody else's
# configuration every tick.
WINDOW_SECONDS = 3600
MAX_NAMES_IN_SUMMARY = 8
MAX_FAILURE_DETAIL_CHARS = 500


class ModelCatalogLaneError(RuntimeError):
    """The catalog lane cannot be configured as asked."""


def load_lane_config(path: str | Path) -> dict[str, Any]:
    """Read the switch file: which configuration to follow, and into what.

    Both paths are named rather than derived.  This process must not go looking
    for the host's OpenClaw configuration on its own -- a Core installed
    without the gateway has nothing to follow and should have no lane -- and
    the router database is wherever the installer put the state directory.
    """

    target = Path(path).expanduser()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ModelCatalogLaneError(f"{target} cannot be read: {exc}") from exc
    if not isinstance(raw, Mapping) or set(raw) - {
        "openclaw_config_path", "model_router_db"
    } or not {"openclaw_config_path", "model_router_db"} <= set(raw):
        raise ModelCatalogLaneError(
            f"{target} must name openclaw_config_path and model_router_db"
        )
    values: dict[str, Any] = {}
    for key in ("openclaw_config_path", "model_router_db"):
        value = raw[key]
        if not isinstance(value, str) or not Path(value).expanduser().is_absolute():
            raise ModelCatalogLaneError(f"{key} must be an absolute path")
        values[key] = str(Path(value).expanduser())
    return values


def _names(values: Any) -> list[str]:
    items = list(values or [])
    return items[:MAX_NAMES_IN_SUMMARY]


class ModelCatalogSyncCoordinator:
    """One hourly reconciliation of the router's catalog with the broker's."""

    def __init__(
        self,
        *,
        config_path: str | Path,
        clock: Callable[[], Any] | None = None,
    ) -> None:
        self.config_path = Path(config_path).expanduser()
        if clock is None:
            from datetime import datetime, timezone

            clock = lambda: datetime.now(timezone.utc)  # noqa: E731
        self.clock = clock
        self._last_window: int | None = None

    def close(self) -> None:
        """Nothing is held open between runs; the writer closes every lane."""

    def window(self) -> int:
        return int(self.clock().timestamp()) // WINDOW_SECONDS

    def run(self) -> dict[str, Any]:
        """Reconcile once, unconditionally.  The window check is the caller's."""

        from .model_router import ModelRouter
        from .openclaw_catalog_reconcile import load_openclaw_config
        from .openclaw_model_discovery import discover_models, summarise
        from .openclaw_catalog_reconcile import sync_openclaw_model_catalog

        settings = load_lane_config(self.config_path)
        openclaw_path = Path(settings["openclaw_config_path"])
        if not openclaw_path.is_file():
            return {"status": "unavailable",
                    "reason": f"no OpenClaw configuration at {openclaw_path}"}
        router_db = Path(settings["model_router_db"])
        if not router_db.is_file():
            return {"status": "unavailable",
                    "reason": f"no model router database at {router_db}"}
        config = load_openclaw_config(openclaw_path)
        checked_at = self.clock()
        with ModelRouter(str(router_db)) as router:
            sync = sync_openclaw_model_catalog(
                router, config, checked_at=checked_at
            )
            discovery = discover_models(config, router=router, checked_at=checked_at)
        return {
            "status": "changed" if sync["changed"] else "current",
            "catalog_in_sync": sync["catalog_in_sync"],
            "broker_catalog_hash": sync["broker_catalog_hash"],
            "registered": _names(sync["added_profile_ids"]),
            "retired": _names(sync["retired_profile_ids_this_run"]),
            "revived": _names(sync["revived_profile_ids"]),
            # The three diff sets, by name and capped: a tick summary is read
            # in a heartbeat file and must not grow with the catalog.
            "in_openclaw_not_allowed": _names(discovery["in_openclaw_not_allowed"]),
            "allowed_not_in_dalton": _names(discovery["allowed_not_in_dalton"]),
            "dalton_not_in_openclaw": _names(discovery["dalton_not_in_openclaw"]),
            "unpriced_profile_ids": _names(discovery.get("unpriced_profile_ids")),
            "counts": summarise(discovery),
        }

    def dispatch_once(self) -> dict[str, Any]:
        window = self.window()
        if window == self._last_window:
            return {"status": "idle",
                    "reason": "the catalog has already been read this hour"}
        try:
            result = self.run()
        except Exception as exc:  # noqa: BLE001 - a bad host file never fails a tick
            return {"status": "failed",
                    "reason": f"{type(exc).__name__}: {exc}"[:MAX_FAILURE_DETAIL_CHARS]}
        if result["status"] != "unavailable":
            self._last_window = window
        return result


def dispatch(server: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    """Controller tick (P14-M2)."""

    launcher = server.lane_launcher(LAUNCHER_KWARG)
    if launcher is None:
        return {"status": "unconfigured",
                "reason": "no model catalog lane on this writer"}
    return launcher.dispatch_once()


def add_arguments(parser: Any) -> None:
    parser.add_argument(
        "--model-catalog-config", type=Path,
        help="P14-M2: which OpenClaw configuration the model catalog follows",
    )


def build_launcher(args: Any) -> Any | None:
    path = getattr(args, "model_catalog_config", None)
    if not path:
        return None
    return ModelCatalogSyncCoordinator(config_path=path)


def argv_fragment(context: Any) -> list[str]:
    # The switch file is the switch, as a policy file is for the tracking lane:
    # a Core with no gateway configuration has nothing to follow, and a lane
    # that guessed at ``~/.openclaw/openclaw.json`` would be a Dalton process
    # reading a host file nobody pointed it at.
    settings = context.state / CONFIG_FILE_NAME
    if not settings.is_file():
        return []
    return ["--model-catalog-config", str(settings)]


LANE = register_lane(LaneSpec(
    operation="dispatch_catalog_sync",
    order=170,
    driver_key="catalog_sync",
    handler=dispatch,
    init_kwarg=LAUNCHER_KWARG,
    argparse=add_arguments,
    launcher_factory=build_launcher,
    argv_fragment=argv_fragment,
    # The pool is declared centrally, in ``budget_pools.LANE_POOLS``, like
    # every other lane's: assigning pools must not mean editing fifteen lane
    # modules at once.
    note="P14-M2: follow the gateway's model catalog -- register what the "
         "broker offers, retire what it stopped offering, and name what is "
         "available but not yet let through. Runs last (170) because nothing "
         "else in a tick depends on it and it reads a file outside the state "
         "directory. Makes no model call and no network request.",
))


__all__ = [
    "CONFIG_FILE_NAME",
    "LANE",
    "LAUNCHER_KWARG",
    "MAX_NAMES_IN_SUMMARY",
    "ModelCatalogLaneError",
    "ModelCatalogSyncCoordinator",
    "WINDOW_SECONDS",
    "add_arguments",
    "argv_fragment",
    "build_launcher",
    "dispatch",
    "load_lane_config",
]
