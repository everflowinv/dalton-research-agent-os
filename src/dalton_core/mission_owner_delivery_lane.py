"""Vision-gap #13: the writer's side of owner delivery -- decide, spawn, reap.

This lane never sends anything and never touches the delivery ledger.  On a
controller tick it answers one question cheaply -- *is a delivery pass due?* --
and if so starts :mod:`dalton_core.owner_delivery` as a child process, the
same way every other lane that reaches outside the machine runs its work as a
child.  The child does the reading (``mode=ro``), the sending (through the
OpenClaw host tool) and the receipts; this lane reaps it on a later tick and
reports what it said.

A pass is due when any of these holds:

* the owner pressed emergency stop or resume-all since the last pass (one
  ``mode=ro`` ``MAX(sequence)`` read of the lever receipts);
* the model catalog lane wrote a fallback notice in this process
  (``model_selection.notice_delivery`` calls :func:`owner_delivery.request_wake`);
* ``poll_interval_minutes`` passed since the last pass (weekly briefs and the
  「需要你处理」 digest are picked up this way; their caps live in the child).

No approved contract, no child: the tick says ``not_granted`` and why.  An
emergency stop does not silence this lane (``owner_levers.EMERGENCY_EXEMPT_LANES``)
-- telling the owner the stop took effect is the one thing that must still go
out -- but a pause of *this* lane by name is obeyed like any other.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .lane_registry import LaneSpec, register_lane

OPERATION = "dispatch_owner_delivery"
LAUNCHER_KWARG = "owner_delivery_launcher"
RUN_DIR_NAME = "owner-delivery-runs"
SUMMARY_FILE_NAME = "last-pass.json"
LOG_FILE_NAME = "last-pass.log"


class OwnerDeliveryLauncher:
    """At most one delivery child at a time, started only when a pass is due."""

    def __init__(
        self,
        *,
        state_dir: str | Path,
        clock: Callable[[], datetime] | None = None,
        popen: Callable[..., Any] | None = None,
        python: str | None = None,
    ) -> None:
        self.state_dir = Path(state_dir).expanduser()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.popen = popen or subprocess.Popen
        self.python = python or sys.executable
        self.run_dir = self.state_dir / RUN_DIR_NAME
        self._child: Any = None
        self._last_spawn: datetime | None = None
        self._last_lever_sequence: int | None = None
        self._last_wake: int | None = None
        self._last_result: dict[str, Any] | None = None

    # -- reaping ----------------------------------------------------------

    def _summary(self) -> dict[str, Any] | None:
        path = self.run_dir / SUMMARY_FILE_NAME
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def reap(self) -> dict[str, Any] | None:
        if self._child is None:
            return None
        code = self._child.poll()
        if code is None:
            return None
        self._child = None
        summary = self._summary() or {"status": "failed",
                                      "reason": f"delivery child exited {code} without a summary"}
        self._last_result = {
            "status": summary.get("status"),
            "delivered": len(summary.get("delivered") or []),
            "failed": len(summary.get("failed") or []),
            "waiting": len(summary.get("waiting") or []),
            "reason": summary.get("reason"),
            "exit_code": code,
        }
        return self._last_result

    # -- the decision -----------------------------------------------------

    def _due(self, now: datetime, settings: Mapping[str, Any]) -> str | None:
        from .owner_delivery import latest_lever_sequence, wake_counter

        lever = latest_lever_sequence(self.state_dir)
        wake = wake_counter()
        if self._last_spawn is None:
            return "first_pass"
        if self._last_lever_sequence is not None and lever != self._last_lever_sequence:
            return "owner_lever"
        if self._last_wake is not None and wake != self._last_wake:
            return "fallback_notice"
        interval = timedelta(minutes=int(settings["poll_interval_minutes"]))
        if now - self._last_spawn >= interval:
            return "poll_interval"
        return None

    def _environment(self) -> dict[str, str]:
        from .owner_delivery import child_environment

        env = child_environment()
        env["PYTHONUNBUFFERED"] = "1"
        # The child imports this package exactly as the writer did.
        package_root = str(Path(__file__).resolve().parents[1])
        existing = env.get("PYTHONPATH")
        if package_root not in (existing or "").split(os.pathsep):
            env["PYTHONPATH"] = package_root + (os.pathsep + existing if existing else "")
        return env

    def dispatch_once(self) -> dict[str, Any]:
        from .owner_delivery import load_contract, load_settings, latest_lever_sequence, \
            wake_counter

        reaped = self.reap()
        if self._child is not None:
            return {"status": "running", "pid": getattr(self._child, "pid", None),
                    "last_pass": self._last_result}
        contract = load_contract(self.state_dir)
        if not contract.approved:
            return {"status": "not_granted", "reason": contract.reason}
        try:
            settings = load_settings(self.state_dir)
        except Exception as exc:  # noqa: BLE001 - a bad config is a tick reason
            return {"status": "unconfigured",
                    "reason": f"owner_delivery settings: {exc}"[:400]}
        now = self.clock()
        why = self._due(now, settings)
        if why is None:
            return {"status": "idle", "reason": "no delivery pass due",
                    "last_pass": reaped or self._last_result}
        self.run_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.run_dir, 0o700)
        summary = self.run_dir / SUMMARY_FILE_NAME
        summary.unlink(missing_ok=True)
        log = open(self.run_dir / LOG_FILE_NAME, "ab")  # noqa: SIM115 - handed to the child
        try:
            self._child = self.popen(
                [self.python, "-m", "dalton_core.owner_delivery", "run",
                 "--state-dir", str(self.state_dir), "--summary-path", str(summary)],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                env=self._environment(), start_new_session=True)
        except OSError as exc:
            return {"status": "failed", "reason": f"cannot start delivery child: {exc}"[:400]}
        finally:
            log.close()
        self._last_spawn = now
        self._last_lever_sequence = latest_lever_sequence(self.state_dir)
        self._last_wake = wake_counter()
        return {"status": "dispatched", "why": why, "pid": getattr(self._child, "pid", None),
                "last_pass": reaped or self._last_result}

    def close(self) -> None:
        """Reap a finished child; a running one finishes on its own timeouts."""

        self.reap()


def dispatch(server: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    del params
    launcher = server.lane_launcher(LAUNCHER_KWARG)
    if launcher is None:
        return {"status": "unconfigured", "reason": "no owner delivery lane on this writer"}
    return launcher.dispatch_once()


def add_arguments(parser: Any) -> None:
    parser.add_argument(
        "--owner-delivery-state-dir", type=Path,
        help="vision-gap #13: the state directory whose owner delivery contract to follow",
    )


def build_launcher(args: Any) -> Any | None:
    path = getattr(args, "owner_delivery_state_dir", None)
    if not path:
        return None
    return OwnerDeliveryLauncher(state_dir=path)


def argv_fragment(context: Any) -> list[str]:
    # Batch t: the lane is on wherever this code runs; the approved contract is
    # the switch for sending, and the launcher answers ``not_granted`` (no
    # child, no message) until it exists and is approved.  Gating the lane on
    # the contract *file* made it depend on ``install.sh`` having seeded one --
    # ``deploy.sh`` never does -- so after batch s no environment ran it.
    return ["--owner-delivery-state-dir", str(context.state)]


LANE = register_lane(LaneSpec(
    operation=OPERATION,
    order=156,
    driver_key="owner_delivery",
    handler=dispatch,
    init_kwarg=LAUNCHER_KWARG,
    argparse=add_arguments,
    launcher_factory=build_launcher,
    argv_fragment=argv_fragment,
    note="vision-gap #13: push the weekly brief, the 需要你处理 digest, model "
         "fallback notices and the owner's emergency stop / resume to Feishu "
         "through the OpenClaw host tool, as a child process. Runs late (after "
         "every lane that produces what it announces, before the weekly "
         "reflection, which must stay last). Makes no model call and opens no "
         "socket itself.",
))


__all__ = [
    "LANE", "LAUNCHER_KWARG", "OPERATION", "OwnerDeliveryLauncher", "add_arguments",
    "argv_fragment", "build_launcher", "dispatch",
]
