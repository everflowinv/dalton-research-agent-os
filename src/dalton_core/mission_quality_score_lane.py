"""Q3: score each published Initial Screen once, with an independent verifier.

The quality loop (Q1/Q2) had everything but a caller.  ``research_quality_cli
score`` ran the deterministic checks, the judge and the verifier and recorded
the result, but it was operator-invoked only, and the verifier's configuration
was written by ``install.sh`` behind an environment variable nobody ever set.
Live on 2026-09-24 the score tables held zero rows in every environment.

This lane is that caller, and it installs the missing configuration itself:

* **Configuration.**  ``quality-verifier-model-config.json`` is derived, once,
  from the installed ``company-dossier-verifier-model-config.json`` -- the
  same routing policy the other verifier stages of this kind already run on
  (dossier, registered annual report, localization), the same broker, budget
  ledger and credential slots -- with one purpose budget,
  ``quality_verifier``, capped at :data:`QUALITY_VERIFIER_MAX_COST_USD` per
  call, the cap those stages use.  Written once and never again: after that
  the file is the owner's, editable on the models page like any other stage.
  The judge runs where Q1 put it, on the Initial Screen's own configuration
  (purpose ``quality``, already capped there).

* **What gets scored.**  The latest version of every ``initial_screen``
  deliverable, once per content hash.  A version already scored under the
  Initial Screen rubric is done; a new version is new work.  Nothing else --
  the other deliverable kinds have their own verifiers and no rubric here.

* **Why it cannot burst.**  One child at a time; at most one launch every
  :data:`MIN_LAUNCH_INTERVAL_SECONDS`; at most :data:`MAX_LAUNCHES_PER_DAY`
  launches in any 24 hours (counted from the ticket files, so a writer restart
  does not reset it); and a target whose run failed is not asked again for
  :data:`FAILED_RETRY_SECONDS`.  Each launch is at most two paid calls (judge
  and verifier), each admitted against the shared day ledger under its own
  per-call cap, so the hard ceiling is ``2 * MAX_LAUNCHES_PER_DAY`` calls a
  day.  Both purposes spend from the ``maintenance`` pool.

The lane needs no writer argument: it is on wherever the writer's state holds
the Initial Screen configuration and a dossier verifier to derive from.  That
matters because a release switch only repoints the interpreter in each
LaunchAgent and never rewrites its arguments, so a lane that needed a new flag
would stay off after every deploy until someone re-rendered the plist.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .lane_child_launcher import (
    LaneChildConflict,
    LaneChildLauncher,
    LaneChildRejected,
    LaneChildTicketNotFound,
)
from .lane_registry import LaneSpec, register_lane

DRIVER_KEY = "quality_scoring"
LAUNCHER_KWARG = "quality_score_launcher"
TICKET_PREFIX = "quality-score-run"
RUBRIC = "initial_screen"
ACTOR_REF = "automation:quality-scoring"
DELIVERABLE_PREFIX = "mission-deliverable:initial_screen:"

JUDGE_MODEL_CONFIG = "initial-screen-model-config.json"
VERIFIER_MODEL_CONFIG = "quality-verifier-model-config.json"
#: The verifier stage whose route this one reuses.
VERIFIER_TEMPLATE_CONFIG = "company-dossier-verifier-model-config.json"
VERIFIER_PURPOSE = "quality_verifier"
#: Per call, the same cap as the dossier, annual-report and localization
#: verifiers.  A realistic call is a few cents: ~15k tokens in, ~1k out.
QUALITY_VERIFIER_MAX_COST_USD = 1.0

MAX_LAUNCHES_PER_DAY = 6
MIN_LAUNCH_INTERVAL_SECONDS = 1_800
FAILED_RETRY_SECONDS = 86_400
MAX_FAILURE_DETAIL_CHARS = 500


class QualityScoreLaneError(RuntimeError):
    """The quality scoring lane cannot be set up as asked."""


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _moment(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


# -- the configuration -------------------------------------------------------


def derive_quality_verifier_config(template: Mapping[str, Any]) -> dict[str, Any]:
    """The quality verifier's configuration, derived from the dossier verifier's.

    Everything that says *where* a call goes and *whose budget* it spends is
    kept byte for byte; only the purpose budgets are replaced, so the file
    carries exactly one purpose and its cap.  Run-shaped budgets belong to the
    lane they were written for and are dropped.
    """

    from .document_extraction import validate_model_config

    derived = {
        key: copy.deepcopy(value) for key, value in template.items()
        if key not in {"call_budget", "run_budget", "purpose_run_budgets",
                       "purpose_call_budgets"}
    }
    derived["purpose_call_budgets"] = {
        VERIFIER_PURPOSE: {"max_cost_usd": QUALITY_VERIFIER_MAX_COST_USD},
    }
    return dict(validate_model_config(derived))


def ensure_quality_verifier_config(state_dir: str | Path) -> dict[str, Any]:
    """Install ``quality-verifier-model-config.json`` once, if it is missing.

    ``present`` when the owner's file is already there (never touched),
    ``installed`` when it was derived just now, ``unavailable`` when there is
    no dossier verifier configuration to derive it from.
    """

    state = Path(state_dir).expanduser().resolve()
    target = state / VERIFIER_MODEL_CONFIG
    if target.exists() or target.is_symlink():
        return {"status": "present", "path": str(target)}
    source = state / VERIFIER_TEMPLATE_CONFIG
    if not source.is_file() or source.is_symlink():
        return {"status": "unavailable", "path": str(target),
                "reason": f"no {VERIFIER_TEMPLATE_CONFIG} to derive from"}
    try:
        template = json.loads(source.read_text(encoding="utf-8"))
        derived = derive_quality_verifier_config(template)
    except Exception as exc:  # noqa: BLE001 - reported, the lane stays off
        return {"status": "unavailable", "path": str(target),
                "reason": f"{type(exc).__name__}: {exc}"[:MAX_FAILURE_DETAIL_CHARS]}
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(derived, ensure_ascii=False, indent=1, sort_keys=True))
            handle.write("\n")
        os.chmod(temporary, 0o600)
        # link, not replace: an owner file that appeared in between wins.
        try:
            os.link(temporary, target)
        except FileExistsError:
            return {"status": "present", "path": str(target)}
    finally:
        temporary.unlink(missing_ok=True)
    return {"status": "installed", "path": str(target),
            "routing_policy_ref": derived["routing_policy_ref"],
            "max_cost_usd": QUALITY_VERIFIER_MAX_COST_USD}


# -- the child ---------------------------------------------------------------


def run_digest(target_ref: str, target_hash: str) -> str:
    payload = "|".join([TICKET_PREFIX, RUBRIC, target_ref, target_hash])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


class QualityScoreLauncher(LaneChildLauncher):
    """Spawn ``research_quality_cli score`` against the writer's state."""

    TICKET_PREFIX = TICKET_PREFIX
    TICKETS_DIRNAME = "quality-score-runs"
    CHILD_MODULE = "dalton_core.research_quality_cli"

    def __init__(self, *, state_dir: str | Path, judge_model_config_path: str | Path,
                 verifier_model_config_path: str | Path,
                 scheduler_db: str | Path | None = None, **kwargs: Any) -> None:
        super().__init__(state_dir=state_dir, **kwargs)
        self.judge_model_config_path = Path(judge_model_config_path).expanduser().resolve()
        self.verifier_model_config_path = Path(
            verifier_model_config_path).expanduser().resolve()
        self.scheduler_db = (None if scheduler_db is None
                             else Path(scheduler_db).expanduser().resolve())

    def _command(self, *, ticket_dir: Path, target_ref: str) -> list[str]:
        command = [
            self.python_executable, "-m", self.CHILD_MODULE, "score",
            "--state-dir", str(self.state_dir),
            "--rubric", RUBRIC, "--target", target_ref,
            "--model-config", str(self.judge_model_config_path),
            "--verifier-model-config", str(self.verifier_model_config_path),
            "--actor-ref", ACTOR_REF,
            "--summary-dir", str(ticket_dir),
        ]
        if self.scheduler_db is not None:
            command += ["--scheduler-db", str(self.scheduler_db)]
        return command

    def start(self, *, target_ref: str, target_hash: str,
              deliverable_ref: str | None = None) -> dict[str, Any]:
        if not isinstance(target_ref, str) or not target_ref.strip():
            raise LaneChildRejected("a quality score run needs a target version")
        if not isinstance(target_hash, str) or not target_hash.strip():
            raise LaneChildRejected("a quality score run needs the target's hash")
        return self.spawn(
            digest=run_digest(target_ref, target_hash),
            record={"target_ref": target_ref, "target_hash": target_hash,
                    "deliverable_ref": deliverable_ref, "rubric": RUBRIC},
            target_ref=target_ref,
        )

    def tickets(self) -> list[dict[str, Any]]:
        """Every ticket this lane has written, newest first."""

        rows: list[dict[str, Any]] = []
        for path in self.tickets_dir.glob("*/ticket.json"):
            try:
                rows.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        rows.sort(key=lambda row: str(row.get("started_at") or ""), reverse=True)
        return rows


# -- the tick ----------------------------------------------------------------


def pending_targets(connection: Any) -> list[dict[str, str]]:
    """Latest Initial Screen versions not yet scored under this rubric."""

    from .research_quality_rubrics import rubric as get_rubric

    rubric_ref = get_rubric(RUBRIC).rubric_ref
    rows = connection.execute(
        "SELECT p.deliverable_ref AS deliverable_ref, v.version_id AS version_id, "
        "v.record_json AS record_json FROM mission_deliverable_pointer p "
        "JOIN mission_deliverable_versions v ON v.version_id=p.version_id "
        "WHERE p.deliverable_ref LIKE ? ORDER BY v.created_at, p.deliverable_ref",
        (DELIVERABLE_PREFIX + "%",),
    ).fetchall()
    scored_table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='research_quality_score_versions'").fetchone() is not None
    pending: list[dict[str, str]] = []
    for row in rows:
        record = json.loads(row["record_json"])
        target_ref = str(record.get("id") or row["version_id"])
        target_hash = str(record.get("content_hash") or "")
        if not target_hash:
            continue
        if scored_table and connection.execute(
            "SELECT 1 FROM research_quality_score_versions WHERE target_ref=? "
            "AND target_hash=? AND rubric_ref=? LIMIT 1",
            (target_ref, target_hash, rubric_ref),
        ).fetchone() is not None:
            continue
        pending.append({"deliverable_ref": row["deliverable_ref"],
                        "target_ref": target_ref, "target_hash": target_hash})
    return pending


class QualityScoreLaneCoordinator:
    """Launch and settle the quality scoring lane, within its rate limits."""

    def __init__(self, *, connection: Callable[[], Any], launcher: Any,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.connection = connection
        self.launcher = launcher
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._open: str | None = None

    def _settle_open(self) -> dict[str, Any] | None:
        if self._open is None:
            return None
        try:
            ticket = self.launcher.status(self._open)
        except LaneChildTicketNotFound:
            self._open = None
            return {"status": "orphaned"}
        except Exception:  # noqa: BLE001 - unreadable now; try next tick
            return None
        if ticket.get("status") == "running":
            return {"status": "running", "ticket_ref": self._open}
        self._open = None
        summary = ticket.get("summary") or {}
        settled = {"status": ticket.get("status"), "ticket_ref": ticket.get("id"),
                   "target_ref": ticket.get("target_ref"),
                   "recorded": summary.get("recorded"),
                   "verified": summary.get("verified")}
        if summary.get("reason"):
            settled["reason"] = str(summary["reason"])[:MAX_FAILURE_DETAIL_CHARS]
        return settled

    def limits(self, tickets: list[dict[str, Any]]) -> dict[str, Any]:
        now = _utc(self.clock())
        starts = [moment for moment in (_moment(row.get("started_at")) for row in tickets)
                  if moment is not None]
        in_day = [moment for moment in starts if now - moment < timedelta(days=1)]
        latest = max(starts, default=None)
        return {
            "launches_last_24h": len(in_day),
            "max_launches_per_day": MAX_LAUNCHES_PER_DAY,
            "next_launch_not_before": (
                None if latest is None
                else (latest + timedelta(seconds=MIN_LAUNCH_INTERVAL_SECONDS)).isoformat()),
            "day_cap_reached": len(in_day) >= MAX_LAUNCHES_PER_DAY,
            "interval_open": (latest is None or now - latest
                              >= timedelta(seconds=MIN_LAUNCH_INTERVAL_SECONDS)),
        }

    def _recently_failed(self, tickets: list[dict[str, Any]], target: Mapping[str, str]) -> bool:
        now = _utc(self.clock())
        for row in tickets:
            if (row.get("target_ref") != target["target_ref"]
                    or row.get("target_hash") != target["target_hash"]):
                continue
            moment = _moment(row.get("started_at"))
            if moment is not None and now - moment < timedelta(seconds=FAILED_RETRY_SECONDS):
                return True
        return False

    def dispatch_once(self) -> dict[str, Any]:
        settled = self._settle_open()
        if self._open is not None:
            return {"status": "running", "ticket_ref": self._open, "settled": settled}
        tickets = self.launcher.tickets()
        limits = self.limits(tickets)
        try:
            pending = pending_targets(self.connection())
        except Exception as exc:  # noqa: BLE001 - one lane's failure is not the tick's
            return {"status": "unavailable", "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"[:MAX_FAILURE_DETAIL_CHARS]}
        # A target asked within the retry window and still unscored failed; it
        # waits out the window rather than paying for the same answer again.
        eligible = [item for item in pending if not self._recently_failed(tickets, item)]
        base = {"settled": settled, "pending": len(pending),
                "held_after_failure": len(pending) - len(eligible), "limits": limits}
        if not eligible:
            return {"status": "idle", **base,
                    "reason": "every Initial Screen version is scored"
                    if not pending else "unscored versions are waiting out a failure"}
        if limits["day_cap_reached"]:
            return {"status": "held", **base, "reason": "daily launch cap reached"}
        if not limits["interval_open"]:
            return {"status": "held", **base, "reason": "minimum launch interval"}
        target = eligible[0]
        try:
            ticket = self.launcher.start(**target)
        except LaneChildConflict as exc:
            return {"status": "busy", **base, "reason": f"{type(exc).__name__}: {exc}"}
        except LaneChildRejected as exc:
            return {"status": "rejected", **base, "reason": f"{type(exc).__name__}: {exc}"}
        self._open = ticket["id"]
        return {"status": "launched", **base, "ticket_ref": ticket["id"],
                "target_ref": target["target_ref"],
                "deliverable_ref": target["deliverable_ref"]}


# -- registration ------------------------------------------------------------


def dispatch(server: Any, params: Any) -> dict[str, Any]:
    launcher = server.lane_launcher(LAUNCHER_KWARG)
    if launcher is None:
        return {"status": "unconfigured",
                "reason": "no quality scoring lane on this writer: it needs "
                          f"{JUDGE_MODEL_CONFIG} and {VERIFIER_MODEL_CONFIG} "
                          f"(derived from {VERIFIER_TEMPLATE_CONFIG})"}
    coordinator = server.lane_state.get(LAUNCHER_KWARG)
    if coordinator is None:
        coordinator = QualityScoreLaneCoordinator(
            connection=lambda: server.store.connection, launcher=launcher)
        server.lane_state[LAUNCHER_KWARG] = coordinator
    return coordinator.dispatch_once()


def build_launcher(args: Any) -> Any | None:
    """On wherever the state has a judge and a verifier; never fails the writer."""

    try:
        state = Path(args.db).expanduser().resolve().parent
        judge = getattr(args, "initial_screen_model_config", None)
        judge_path = Path(judge) if judge else state / JUDGE_MODEL_CONFIG
        if not judge_path.is_file():
            return None
        installed = ensure_quality_verifier_config(state)
        if installed["status"] == "unavailable":
            print(json.dumps({"lane": DRIVER_KEY, **installed}, ensure_ascii=False),
                  file=sys.stderr)
            return None
        return QualityScoreLauncher(
            state_dir=state, judge_model_config_path=judge_path,
            verifier_model_config_path=state / VERIFIER_MODEL_CONFIG,
            scheduler_db=getattr(args, "scheduler", None),
        )
    except Exception as exc:  # noqa: BLE001 - an optional lane never stops the writer
        print(json.dumps({"lane": DRIVER_KEY, "status": "unavailable",
                          "reason": f"{type(exc).__name__}: {exc}"[:MAX_FAILURE_DETAIL_CHARS]},
                         ensure_ascii=False), file=sys.stderr)
        return None


LANE = register_lane(LaneSpec(
    operation="dispatch_quality_scoring",
    # After the Initial Screen (110) that it scores.
    order=112,
    driver_key=DRIVER_KEY,
    handler=dispatch,
    init_kwarg=LAUNCHER_KWARG,
    launcher_factory=build_launcher,
    # Pool: maintenance, declared centrally in budget_pools.LANE_POOLS.
    note="Q3: score each Initial Screen version once (deterministic checks, "
         "judge, independent verifier), rate limited to a few runs a day.",
))


__all__ = [
    "ACTOR_REF",
    "FAILED_RETRY_SECONDS",
    "LANE",
    "LAUNCHER_KWARG",
    "MAX_LAUNCHES_PER_DAY",
    "MIN_LAUNCH_INTERVAL_SECONDS",
    "QUALITY_VERIFIER_MAX_COST_USD",
    "QualityScoreLaneCoordinator",
    "QualityScoreLauncher",
    "VERIFIER_MODEL_CONFIG",
    "VERIFIER_TEMPLATE_CONFIG",
    "build_launcher",
    "derive_quality_verifier_config",
    "dispatch",
    "ensure_quality_verifier_config",
    "pending_targets",
    "run_digest",
]
