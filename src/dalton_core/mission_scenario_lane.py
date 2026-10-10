"""ADR-0012: the two scenario lanes -- propose, then observe.

``dispatch_scenario_proposal``
    One child at a time; one company per child; one model call per company
    (purpose ``scenario_proposal``, per-call cap from
    ``call_budget_defaults.json``).  A company is owed a proposal when its
    dossier has a variant view and it has no set yet, or its last set was
    decided and drawn from a dossier body that has since moved and is at
    least :data:`MIN_REPROPOSE_DAYS` old.  At most
    :data:`MAX_LAUNCHES_PER_DAY` launches in any 24 hours (counted from the
    ticket files, so a writer restart does not reset it), at least
    :data:`MIN_LAUNCH_INTERVAL_SECONDS` apart, and a company whose run did not
    publish is not asked again on the same dossier for
    :data:`FAILED_RETRY_SECONDS`.  Like the quality lane it needs no writer
    argument: it is on wherever the state holds the dossier's (or the
    Initial Screen's) model configuration, so a release switch that never
    re-renders a plist still turns it on, and a new workspace needs nothing.

``dispatch_scenario_observation``
    In process, no model, no network.  When any input moved (or at least
    every :data:`MAX_IDLE_SECONDS`), every condition of every in-force
    scenario -- and of the newest undecided proposal, so the owner sees
    whether a proposed trigger is even observable -- is evaluated against the
    latest observed value and an observation is appended when its status,
    value or source changed.  A condition of an *in force* scenario that turns
    ``triggered`` records a ``scenario_trigger`` ResearchEvent (the judgement
    lane takes it from there: ADR-0007's candidate or proposal) and re-times
    the company's adjustable tracking sources through
    ``tracking_cadence.publish_priority``.  When no in-force condition of a
    company is triggered any more and the scenario lane's own prioritisation
    is older than :data:`PRIORITY_HOLD_DAYS`, the baseline is published back.
    An owner's own 「优先看这家」 is never undone here.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .lane_child_launcher import (
    LaneChildConflict,
    LaneChildLauncher,
    LaneChildRejected,
    LaneChildTicketNotFound,
)
from .lane_registry import LaneSpec, register_lane

PROPOSAL_DRIVER_KEY = "scenario_proposal"
PROPOSAL_LAUNCHER_KWARG = "scenario_proposal_launcher"
OBSERVATION_DRIVER_KEY = "scenario_observation"
OBSERVATION_STATE_KEY = "scenario_observation_state"
TICKET_PREFIX = "scenario-proposal-run"
MODEL_CONFIGS = ("dossier-model-config.json", "initial-screen-model-config.json")

MAX_LAUNCHES_PER_DAY = 6
MIN_LAUNCH_INTERVAL_SECONDS = 600
FAILED_RETRY_SECONDS = 86_400
MIN_REPROPOSE_DAYS = 7
MIN_SCAN_SECONDS = 300
MAX_IDLE_SECONDS = 6 * 3600
PRIORITY_HOLD_DAYS = 14
TRIGGER_DECISION = "scenario_trigger"
SETTLED_DECISION = "scenario_settled"
MAX_DETAIL = 500
INPUT_TABLES = (
    "company_scenario_sets", "company_scenario_versions", "company_scenario_decisions",
    "forecast_model_versions", "market_price_series_versions",
    "valuation_snapshot_versions", "claim_versions", "event_judgements",
    "claim_retirement_decisions",
)


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _moment(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _current_mission(server: Any) -> Mapping[str, Any] | None:
    pointer = server.store.connection.execute(
        "SELECT mission_version_id FROM coverage_mission_pointer ORDER BY mission_ref LIMIT 1"
    ).fetchone()
    return None if pointer is None else server.coverage_mission.mission(pointer["mission_version_id"])


# ---------------------------------------------------------------------------
# the proposal lane
# ---------------------------------------------------------------------------


def run_digest(company_ref: str, body_hash: str, day: str) -> str:
    payload = "|".join([TICKET_PREFIX, company_ref, body_hash or "-", day])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


class ScenarioProposalLauncher(LaneChildLauncher):
    """Spawn ``scenario_proposal_cli`` for one company."""

    TICKET_PREFIX = TICKET_PREFIX
    TICKETS_DIRNAME = "scenario-proposal-runs"
    CHILD_MODULE = "dalton_core.scenario_proposal_cli"

    def __init__(self, *, state_dir: str | Path, model_config_path: str | Path,
                 scheduler_db: str | Path | None = None, **kwargs: Any) -> None:
        super().__init__(state_dir=state_dir, **kwargs)
        self.model_config_path = Path(model_config_path).expanduser().resolve()
        self.scheduler_db = (None if scheduler_db is None
                             else Path(scheduler_db).expanduser().resolve())

    def _command(self, *, ticket_dir: Path, company_ref: str) -> list[str]:
        command = [self.python_executable, "-m", self.CHILD_MODULE,
                   "--state-dir", str(self.state_dir), "--company-ref", company_ref,
                   "--model-config", str(self.model_config_path),
                   "--summary-dir", str(ticket_dir)]
        if self.scheduler_db is not None:
            command += ["--scheduler-db", str(self.scheduler_db)]
        return command

    def start(self, *, company_ref: str, dossier_body_hash: str, day: str) -> dict[str, Any]:
        if not isinstance(company_ref, str) or not company_ref:
            raise LaneChildRejected("a scenario proposal needs a company")
        from .scenario_proposal import CONTRACT_VERSION

        return self.spawn(
            digest=run_digest(company_ref, dossier_body_hash, day),
            record={"company_ref": company_ref, "dossier_body_hash": dossier_body_hash,
                    "contract": CONTRACT_VERSION},
            company_ref=company_ref)

    def tickets(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for path in self.tickets_dir.glob("*/ticket.json"):
            try:
                rows.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        rows.sort(key=lambda row: str(row.get("started_at") or ""), reverse=True)
        return rows


class ScenarioProposalCoordinator:
    """Launch and settle proposal runs within the lane's limits."""

    def __init__(self, *, server: Any, launcher: Any,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.server = server
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
        return {"status": ticket.get("status"), "ticket_ref": ticket.get("id"),
                "company_ref": ticket.get("company_ref"),
                "proposal_status": summary.get("proposal_status"),
                "set_ref": summary.get("set_ref"), "cost_micros": summary.get("cost_micros"),
                "reason": (str(summary.get("reason"))[:MAX_DETAIL]
                           if summary.get("reason") else None)}

    def limits(self, tickets: list[dict[str, Any]]) -> dict[str, Any]:
        now = _utc(self.clock())
        starts = [moment for moment in (_moment(row.get("started_at")) for row in tickets)
                  if moment is not None]
        in_day = [moment for moment in starts if now - moment < timedelta(days=1)]
        latest = max(starts, default=None)
        return {
            "launches_last_24h": len(in_day), "max_launches_per_day": MAX_LAUNCHES_PER_DAY,
            "day_cap_reached": len(in_day) >= MAX_LAUNCHES_PER_DAY,
            "interval_open": (latest is None or now - latest
                              >= timedelta(seconds=MIN_LAUNCH_INTERVAL_SECONDS)),
        }

    def _recently_tried(self, tickets: list[dict[str, Any]], target: Mapping[str, Any]) -> bool:
        from .scenario_proposal import CONTRACT_VERSION

        now = _utc(self.clock())
        for row in tickets:
            if (row.get("company_ref") != target["company_ref"]
                    or row.get("dossier_body_hash") != (target.get("proposal_input_hash") or target["dossier_body_hash"])):
                continue
            # A run under an older proposal contract says nothing about the
            # current one: a refusal the contract fix removes is retried on the
            # next open slot, not a day later.  (The daily cap still counts it.)
            if row.get("contract") != CONTRACT_VERSION:
                continue
            moment = _moment(row.get("started_at"))
            if moment is not None and now - moment < timedelta(seconds=FAILED_RETRY_SECONDS):
                return True
        return False

    def dispatch_once(self) -> dict[str, Any]:
        from .company_scenario import PROPOSAL_WRITE_SCOPE
        from .scenario_proposal import eligible_companies

        settled = self._settle_open()
        if self._open is not None:
            return {"status": "running", "ticket_ref": self._open, "settled": settled}
        mission = _current_mission(self.server)
        if mission is None:
            return {"status": "idle", "settled": settled, "reason": "no active mission"}
        if PROPOSAL_WRITE_SCOPE not in mission["autonomy"]["may_write"]:
            return {"status": "ungranted", "settled": settled,
                    "reason": f"任务没有授予 {PROPOSAL_WRITE_SCOPE} 写入范围，不起草情景"}
        now = _utc(self.clock())
        try:
            owed = eligible_companies(self.server.store.connection, mission, now=now,
                                      min_repropose_days=MIN_REPROPOSE_DAYS)
        except Exception as exc:  # noqa: BLE001 - one lane's failure is not the tick's
            return {"status": "unavailable", "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"[:MAX_DETAIL]}
        tickets = self.launcher.tickets()
        limits = self.limits(tickets)
        eligible = [item for item in owed if not self._recently_tried(tickets, item)]
        base = {"settled": settled, "owed": len(owed),
                "held_after_attempt": len(owed) - len(eligible), "limits": limits}
        if not eligible:
            return {"status": "idle", **base,
                    "reason": ("没有公司需要新的情景提案" if not owed
                               else "剩下的公司今天已经试过，等档案更新或明天再试")}
        if limits["day_cap_reached"]:
            return {"status": "held", **base, "reason": "已到每日起草上限"}
        if not limits["interval_open"]:
            return {"status": "held", **base, "reason": "两次起草之间的最短间隔未到"}
        target = eligible[0]
        try:
            ticket = self.launcher.start(company_ref=target["company_ref"],
                                         dossier_body_hash=target.get("proposal_input_hash") or target["dossier_body_hash"] or "",
                                         day=now.date().isoformat())
        except LaneChildConflict as exc:
            return {"status": "busy", **base, "reason": f"{type(exc).__name__}: {exc}"}
        except LaneChildRejected as exc:
            return {"status": "rejected", **base, "reason": f"{type(exc).__name__}: {exc}"}
        self._open = ticket["id"]
        return {"status": "launched", **base, "ticket_ref": ticket["id"],
                "company_ref": target["company_ref"]}


def dispatch_proposal(server: Any, params: Any) -> dict[str, Any]:
    launcher = server.lane_launcher(PROPOSAL_LAUNCHER_KWARG)
    if launcher is None:
        return {"status": "unconfigured",
                "reason": "no scenario proposal lane on this writer: it needs "
                          + " or ".join(MODEL_CONFIGS)}
    coordinator = server.lane_state.get(PROPOSAL_LAUNCHER_KWARG)
    if coordinator is None:
        coordinator = ScenarioProposalCoordinator(server=server, launcher=launcher)
        server.lane_state[PROPOSAL_LAUNCHER_KWARG] = coordinator
    return coordinator.dispatch_once()


def build_proposal_launcher(args: Any) -> Any | None:
    """On wherever the state has a dossier or Initial Screen model config."""

    try:
        state = Path(args.db).expanduser().resolve().parent
        config = next((state / name for name in MODEL_CONFIGS if (state / name).is_file()), None)
        if config is None:
            return None
        return ScenarioProposalLauncher(state_dir=state, model_config_path=config,
                                        scheduler_db=getattr(args, "scheduler", None))
    except Exception as exc:  # noqa: BLE001 - an optional lane never stops the writer
        print(json.dumps({"lane": PROPOSAL_DRIVER_KEY, "status": "unavailable",
                          "reason": f"{type(exc).__name__}: {exc}"[:MAX_DETAIL]},
                         ensure_ascii=False), file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# the observation lane
# ---------------------------------------------------------------------------


SCENARIO_TABLES = INPUT_TABLES[:3]


def input_signature(connection: Any, day: str, tables: tuple[str, ...] = INPUT_TABLES) -> str:
    parts = [day]
    for table in tables:
        try:
            value = connection.execute(f"SELECT MAX(rowid) FROM {table}").fetchone()[0]
        except Exception:  # noqa: BLE001 - a missing table is a state, not an error
            value = "missing"
        parts.append(f"{table}:{value}")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _trigger_payload(version: Mapping[str, Any], condition: Mapping[str, Any],
                     observation: Mapping[str, Any]) -> dict[str, Any]:
    def text(value: Any) -> str | None:
        return None if value is None else str(value)[:600]

    return {
        "scenario_ref": version["scenario_ref"], "scenario_version_ref": version["id"],
        "scenario_name": text(version["name"]), "probability": version["probability"],
        "condition_id": condition["condition_id"], "condition_kind": condition["kind"],
        "condition_label": text(condition["label"]), "observed_by": condition["observed_by"],
        "metric_ref": text(condition.get("metric_ref")), "transform": condition.get("transform"),
        "comparator": condition.get("comparator"), "threshold": condition.get("threshold"),
        "horizon": condition["horizon"], "observed_value": text(observation.get("value")),
        "value_as_of": text(observation.get("value_as_of")),
        "source_ref": text(observation.get("source_ref")),
        "observation_ref": observation["id"],
        "thesis_ref": (list(version.get("linked_thesis_refs") or ()) or [None])[0],
    }


def _settle_priority(cadences: Any, *, company_ref: str, policy: Mapping[str, Any],
                     now: datetime) -> bool:
    """True when every adjustable source still carries this lane's own prioritisation
    and it is older than the hold."""

    seen = False
    for source_key, baseline in policy["cadences"].items():
        if not baseline["adjustable"]:
            continue
        latest = cadences.latest(company_ref, source_key)
        if latest is None:
            continue
        if latest.get("decision") != TRIGGER_DECISION:
            return False
        created = _moment(latest.get("created_at"))
        if created is None or now - created < timedelta(days=PRIORITY_HOLD_DAYS):
            return False
        seen = True
    return seen


def observe_tick(store: Any, mission: Mapping[str, Any], *, now: datetime,
                 policy_loader: Callable[[], Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Evaluate every condition once, append what changed, hand off what fired."""

    from .company_scenario import (
        OBSERVATION_WRITE_SCOPE, CompanyScenarioAuthority, Observer, evaluate_condition,
        in_force, pending_set, read_set_versions,
    )

    actor = mission["autonomy"]["automation_principal"]
    authority = CompanyScenarioAuthority(store)
    connection = store.connection
    observer = Observer(connection)
    today = _utc(now).date()
    universe = {member["company_ref"] for member in mission["universe"]}
    may_write = set(mission["autonomy"]["may_write"])
    counts = {"conditions": 0, "appended": 0, "fired": 0, "events": 0,
              "cadence_versions": 0, "settled": 0, "triggered_in_force": 0}
    notes: list[str] = []
    policy = None
    for company_ref in authority.companies():
        if company_ref not in universe:
            continue
        live = in_force(connection, company_ref)
        live_ids = {item["id"] for item in live}
        pending = pending_set(connection, company_ref)
        versions = list(live) + ([] if pending is None else read_set_versions(connection, pending["id"]))
        fired: list[tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]] = []
        any_triggered = False
        for version in versions:
            is_live = version["id"] in live_ids
            for condition in list(version["triggers"]) + list(version["invalidators"]):
                counts["conditions"] += 1
                observed = observer.read(company_ref, condition["observed_by"],
                                         condition.get("metric_ref"), condition.get("transform") or "level",
                                         since=version["created_at"])
                outcome = evaluate_condition(condition, observed, today=today,
                                             base_date=version["created_at"])
                written = authority.record_observation(
                    version=version, condition=condition, outcome=outcome, in_force=is_live,
                    mission=mission, actor_ref=actor)
                if written["status_change"] != "unchanged":
                    counts["appended"] += 1
                if is_live and outcome["status"] == "triggered":
                    any_triggered = True
                    counts["triggered_in_force"] += 1
                if written["status_change"] == "fired":
                    fired.append((version, condition, written))
        counts["fired"] += len(fired)
        if fired and "market_event" in may_write:
            from .research_event import ResearchEventAuthority, record_event

            events = ResearchEventAuthority(store)
            for version, condition, observation in fired:
                result = record_event(
                    events, company_ref=company_ref, kind="scenario_trigger",
                    occurred_at=_utc(now).isoformat(timespec="seconds"),
                    source_refs=[observation["id"], version["id"]]
                    + ([observation["source_ref"]] if observation.get("source_ref") else []),
                    payload=_trigger_payload(version, condition, observation),
                    mission=mission, actor_ref=actor)
                if result.get("status") == "fresh":
                    counts["events"] += 1
        elif fired:
            notes.append("任务没有授予 market_event，触发只记录观察，不交给事件研判")
        if OBSERVATION_WRITE_SCOPE not in may_write or (not fired and any_triggered):
            continue
        if policy is None and policy_loader is not None:
            try:
                policy = policy_loader()
            except Exception as exc:  # noqa: BLE001 - cadence is a consequence, not the tick
                notes.append(f"跟踪频率策略读不到：{type(exc).__name__}"[:120])
                policy_loader = None
        if policy is None:
            continue
        from .tracking_cadence import TrackingCadenceAuthority, publish_priority

        cadences = TrackingCadenceAuthority(store)
        if fired:
            names = "；".join(f"{v['name']}：{c['label']}" for v, c, _o in fired)[:900]
            outcome = publish_priority(
                cadences, company_ref=company_ref, mode="prioritize",
                because=f"情景触发（ADR-0012）——{names}",
                evidence_refs=[observation["id"] for _v, _c, observation in fired],
                change_reason="driver_event", policy=policy, mission=mission,
                actor_ref=actor, decision=TRIGGER_DECISION)
            counts["cadence_versions"] += len(outcome["published"])
        elif _settle_priority(cadences, company_ref=company_ref, policy=policy, now=_utc(now)):
            outcome = publish_priority(
                cadences, company_ref=company_ref, mode="normal",
                because=f"情景触发已解除超过 {PRIORITY_HOLD_DAYS} 天，恢复常规频率（ADR-0012）",
                evidence_refs=[item["id"] for item in live] or [company_ref],
                change_reason="driver_event", policy=policy, mission=mission,
                actor_ref=actor, decision=SETTLED_DECISION)
            counts["settled"] += len(outcome["published"])
    return {**counts, "notes": notes}


def dispatch_observation(server: Any, params: Any) -> dict[str, Any]:
    from .company_scenario import OBSERVATION_WRITE_SCOPE, table_exists

    mission = _current_mission(server)
    if mission is None:
        return {"status": "idle", "reason": "no active mission"}
    if OBSERVATION_WRITE_SCOPE not in mission["autonomy"]["may_write"]:
        return {"status": "ungranted",
                "reason": f"任务没有授予 {OBSERVATION_WRITE_SCOPE} 写入范围，不记录情景观察"}
    connection = server.store.connection
    if not table_exists(connection, "company_scenario_sets"):
        return {"status": "idle", "reason": "还没有任何情景提案"}
    now = datetime.now(timezone.utc)
    state = server.lane_state.setdefault(OBSERVATION_STATE_KEY, {})
    signature = input_signature(connection, now.date().isoformat())
    # A new proposal, decision or owner revision is observed on the next tick;
    # only the high-churn inputs (claims, prices) wait out the minimum gap.
    scenarios = input_signature(connection, "", SCENARIO_TABLES)
    last = state.get("at")
    if (last is not None and now - last < timedelta(seconds=MIN_SCAN_SECONDS)
            and state.get("scenarios") == scenarios):
        return {"status": "idle", "reason": "距上次观察不足五分钟"}
    if (state.get("signature") == signature and last is not None
            and now - last < timedelta(seconds=MAX_IDLE_SECONDS)):
        return {"status": "idle", "reason": "情景的输入没有变化"}
    result = observe_tick(server.store, mission, now=now,
                          policy_loader=getattr(server, "_tracking_policy", None))
    state.update({"signature": input_signature(connection, now.date().isoformat()),
                  "scenarios": input_signature(connection, "", SCENARIO_TABLES), "at": now})
    status = "fired" if result["fired"] else ("observed" if result["appended"] else "idle")
    reason = (f"检查 {result['conditions']} 个条件，新增 {result['appended']} 条观察，"
              f"{result['fired']} 个触发")
    if result["notes"]:
        reason += "；" + "；".join(result["notes"])
    return {"status": status, "reason": reason[:MAX_DETAIL],
            **{key: value for key, value in result.items() if key != "notes"}}


PROPOSAL_LANE = register_lane(LaneSpec(
    operation="dispatch_scenario_proposal",
    # After the dossier (137) it reads.
    order=144,
    driver_key=PROPOSAL_DRIVER_KEY,
    handler=dispatch_proposal,
    init_kwarg=PROPOSAL_LAUNCHER_KWARG,
    launcher_factory=build_proposal_launcher,
    note="ADR-0012: one bounded model call per company turns the dossier's scenario "
         "prose into a structured ScenarioSet the owner adjudicates; daily capped.",
))

OBSERVATION_LANE = register_lane(LaneSpec(
    operation="dispatch_scenario_observation",
    order=145,
    driver_key=OBSERVATION_DRIVER_KEY,
    handler=dispatch_observation,
    note="ADR-0012: deterministic evaluation of every in-force scenario condition; "
         "a trigger becomes a scenario_trigger ResearchEvent and re-times tracking.",
))


__all__ = [
    "FAILED_RETRY_SECONDS", "MAX_LAUNCHES_PER_DAY", "MIN_REPROPOSE_DAYS", "OBSERVATION_LANE",
    "PRIORITY_HOLD_DAYS", "PROPOSAL_LANE", "ScenarioProposalCoordinator",
    "ScenarioProposalLauncher", "build_proposal_launcher", "dispatch_observation",
    "dispatch_proposal", "input_signature", "observe_tick", "run_digest",
]
