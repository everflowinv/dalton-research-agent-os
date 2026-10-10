"""P12e: the industry's framework gets a little deeper each week, or nothing happens.

Queueless, like the dossier and specification lanes.  What needs doing is
derived from the Ledger every tick, and this lane's signature is the one place
in the cognition layer that watches *filings* as well as Claims -- because this
deliverable's central table is computed from filed statements, and a newly
filed quarter changes what the framework says without a single Claim arriving.
A signature that only counted Claims would have kept this lane silent through
every earnings season.

**Weekly, not per-event.**  The coordinator holds a minimum interval between
launches, because an industry framework that is redrafted the moment any one of
five companies files is a document nobody can read a version chain of -- and
the blueprint asks for weekly.  Between those, an explicit run (``--revise``)
is the door.

**Silence is the resting state and it is cheap.**  The coordinator does not
plan; the child does, and the child is the only thing that reads the aspect
index, the Constitution, the driver pack and the policy.  The coordinator's
whole job is to avoid spawning a child that will report ``nothing_new``: it
keeps a signature of the evidence and does not launch again on an unchanged
signature after a run that found nothing.  A signature cannot disagree with the
child, because it is not an opinion about what to do.

One child at a time, settled on the following tick.  A child inspected in the
same breath it was spawned is always still running.
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .lane_child_launcher import (
    LaneChildConflict,
    LaneChildRejected,
    LaneChildTicketNotFound,
)
from .lane_registry import LaneSpec, register_lane
from .lane_failure_ledger import lane_budget

MAX_FAILURE_DETAIL_CHARS = 500
LAUNCHER_KWARG = "industry_framework_launcher"
# The blueprint's cadence for this deliverable, as seconds. Held by the
# coordinator rather than by the scheduler because the scheduler's tick is what
# drives every lane and this is the only one that wants to be slower than it.
MIN_INTERVAL_SECONDS = 7 * 24 * 60 * 60
MATERIAL_PROBE_SECONDS = 30 * 60
# Statuses that mean "this run looked and found nothing to do". After one of
# these, an unchanged signature is a reason to stay quiet.
QUIET_STATUSES = frozenset({
    "nothing_new", "no_mission",
    "causal_chain_unmapped", "no_driver_pack",
})
CONTENT_TERMINAL_STATUSES = frozenset({
    "rubric_refused", "constitution_refused", "not_independent",
    "unverified", "no_new_evidence",
})
# 2026-10-07 (ws-7d): until a first version exists the weekly cadence does not
# apply.  ws-7d's first run crashed after paying for the draft and the
# verifier, and the coordinator then waited a week from that *launch* -- so a
# new workspace had no framework at all, and the model-stage gate (ADR-0011)
# could never pass for it.  A run that published nothing is retried after this
# much shorter pause, and the waiting reason names what was missing.
FIRST_DRAFT_RETRY_SECONDS = 6 * 60 * 60
# 2026-10-08 (ws-7d META): the weekly cadence held a redraft for six days
# while the industry_model gate named three deficiencies a redraft fixes and
# the peer set had just grown.  A gate deficiency a redraft can address, with
# inputs that moved since the current version, redrafts after this spacing
# instead of the week -- still one child at a time, still under the run's
# own cost bound and the lane's failure budget.
GATE_REDRAFT_SECONDS = FIRST_DRAFT_RETRY_SECONDS
# The industry_model criteria a redraft can change.  The rest (mission
# binding, the company's update calendar, its metric base) are other lanes'.
REDRAFTABLE_CRITERIA = (
    "inputs_source_bound",
    "five_company_revenue_growth_margin_comparison",
    "comparison_limits_explained",
    "unconnected_inputs_explicitly_deferred",
)
# What a run that produced no version was missing, in the owner's words.  The
# technical detail (``failure_reason``) travels separately.
MISSING_INPUT_TEXT = {
    "no_mission": "没有当前的研究任务（CoverageMission）",
    "no_driver_pack": "研究任务没有可用的驱动因素包（driver pack）",
    "no_policy": "行业框架策略文件读不出来或不合法",
    "binding_drift": "研究任务绑定的方法版本已经变化",
    "causal_chain_unmapped": "研究宪法的因果链在行业框架策略里没有对应标题",
    "not_authorized": "研究任务没有授权行业框架这项产出",
    "gated": "没有配置起草模型",
    "no_verifier": "没有配置独立核验模型",
    "verification_failed": "独立核验没有通过",
    "unverified": "独立核验没有给出结论",
    "not_independent": "核验模型与起草模型不独立",
    "rubric_refused": "草稿没有通过输出评分标准",
    "constitution_refused": "草稿没有通过研究宪法检查",
    "nothing_new": "还没有可写进框架的新证据",
    "no_new_evidence": "草稿没有引用任何新证据",
}


def missing_input_text(settled: Mapping[str, Any] | None) -> str:
    """What the last run lacked, in Chinese, naming the input."""

    if not isinstance(settled, Mapping):
        return "还没有运行过"
    status = str(settled.get("framework_status") or "")
    if status in MISSING_INPUT_TEXT:
        return MISSING_INPUT_TEXT[status]
    if settled.get("status") == "orphaned":
        return "上一次运行的记录丢失（多半是写入服务重启）"
    if settled.get("failure_reason"):
        return "上一次运行中途失败（详见 last_failure）"
    return f"上一次运行没有产出版本（{status or settled.get('status')}）"


def latest_version_epoch(connection: Any) -> float | None:
    """When the newest framework version was recorded, from the Ledger."""

    try:
        row = connection.execute(
            "SELECT MAX(created_at) AS newest FROM industry_framework_versions"
        ).fetchone()
    except Exception:  # noqa: BLE001 - no framework table yet is a valid state
        return None
    newest = None if row is None else row[0]
    if not newest:
        return None
    from datetime import datetime, timezone

    try:
        moment = datetime.fromisoformat(str(newest))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def _count_since(connection: Any, sql: str, since: str) -> int:
    try:
        row = connection.execute(sql, (since,)).fetchone()
    except Exception:  # noqa: BLE001 - an absent authority has nothing new
        return 0
    return int((row[0] if row is not None else 0) or 0)


def redraft_changes(connection: Any, framework: Mapping[str, Any],
                    mission: Mapping[str, Any], *,
                    expected_gaps: list[dict[str, Any]] | None) -> list[str]:
    """What moved since this framework version, in the owner's words.

    A company with filed statements that the version's comparison does not
    carry; statement filings recorded after it; Claims written after it; and
    a gap list the code would now build differently (a rule or a source's
    connection status changed).
    """

    changes: list[str] = []
    since = str(framework.get("created_at") or "")
    held = {str(row.get("company_ref")) for row in
            ((framework.get("cross_company_comparison") or {}).get("companies") or ())}
    joined = []
    for member in mission.get("universe") or ():
        company_ref = str(member.get("company_ref"))
        if company_ref in held:
            continue
        try:
            filed = connection.execute(
                "SELECT 1 FROM coverage_mission_statement_filings WHERE company_ref=? "
                "LIMIT 1", (company_ref,)).fetchone()
        except Exception:  # noqa: BLE001 - no statement authority yet
            filed = None
        if filed is not None:
            joined.append(str(member.get("ticker") or company_ref.rsplit(":", 1)[-1]))
    if joined:
        changes.append("新纳入同业：" + "、".join(joined))
    if since:
        filings = _count_since(
            connection, "SELECT COUNT(*) FROM coverage_mission_statement_filings "
            "WHERE recorded_at > ?", since)
        if filings:
            changes.append(f"新申报报表 {filings} 份")
        claims = _count_since(
            connection, "SELECT COUNT(*) FROM claim_versions WHERE created_at > ?", since)
        if claims:
            changes.append(f"新增 Claim {claims} 条")
    if expected_gaps is not None:
        from .industry_framework import gap_state_ref

        if gap_state_ref(expected_gaps) != gap_state_ref(framework.get("gaps") or []):
            changes.append("缺口清单变化（高频数据替代来源或来源接入状态）")
    return changes


def gate_redraft_trigger(connection: Any, launcher: Any) -> dict[str, Any] | None:
    """The industry_model deficiencies a redraft can address, and what moved.

    None when there is no active mission or framework, or the gate names no
    deficiency a redraft can fix.  ``changes`` may be empty: then the gate
    has something to say but nothing new to say it with, and the week holds.
    """

    from .coverage_mission import CoverageMissionAuthority
    from .industry_framework import IndustryFrameworkAuthority, assess_gaps, load_policy
    from .mission_model_stage_lane import _latest, _reader, mission_version_refs
    from .model_stage_readiness import criterion_label, industry_model_readiness

    pointer = connection.execute(
        "SELECT mission_version_id FROM coverage_mission_pointer "
        "ORDER BY mission_ref LIMIT 1").fetchone()
    if pointer is None:
        return None
    mission = _reader(connection, CoverageMissionAuthority)._mission_record(
        str(pointer[0]))
    framework = _latest(connection, IndustryFrameworkAuthority,
                        str(mission["industry_ref"]))
    if framework is None:
        return None
    verdict = industry_model_readiness(
        framework, mission=mission,
        mission_version_refs=mission_version_refs(connection, mission))
    failing = [reason for reason in verdict.get("reasons") or ()
               if reason in REDRAFTABLE_CRITERIA]
    if not failing:
        return None
    expected_gaps = None
    policy_path = getattr(launcher, "policy_path", None)
    if policy_path is not None:
        try:
            expected_gaps = assess_gaps(
                load_policy(policy_path), mission=mission,
                long_term=framework.get("long_term_drivers"),
                short_term=framework.get("short_term_drivers"))
        except Exception:  # noqa: BLE001 - an unreadable policy moves no gap
            expected_gaps = None
    return {
        "deficiencies": [criterion_label(reason) for reason in failing],
        "changes": redraft_changes(connection, framework, mission,
                                   expected_gaps=expected_gaps),
        "framework_ref": framework.get("id"),
    }


def permission_key(connection: Any, launcher: Any, signature: str) -> str:
    parts = [signature]
    try:
        rows = connection.execute(
            "SELECT mission_ref, mission_version_id FROM coverage_mission_pointer "
            "ORDER BY mission_ref").fetchall()
        parts += [f"{row['mission_ref']}:{row['mission_version_id']}" for row in rows]
    except Exception:
        parts.append("mission:none")
    try:
        rows = connection.execute(
            "SELECT pointer_id, policy_version_id FROM governance_policy_pointer "
            "ORDER BY pointer_id").fetchall()
        parts += [f"policy:{row['pointer_id']}:{row['policy_version_id']}" for row in rows]
    except Exception:
        parts.append("policy:none")
    for name, value in sorted(vars(launcher).items()):
        if not any(word in name for word in ("config", "policy")) or value is None:
            continue
        path = Path(value)
        try:
            parts.append(f"{name}:{hashlib.sha256(path.read_bytes()).hexdigest()}")
        except OSError:
            parts.append(f"{name}:missing")
    return f"{signature}|permission:{hashlib.sha256('|'.join(parts).encode()).hexdigest()[:16]}"


def clear_obsolete_permissions(budget: Any, current: str, signature: str) -> None:
    for row in budget.permission_items():
        if row["item_key"] != current:
            budget.retire(row["item_key"])


def ledger_signature(connection: Any) -> str:
    """A cheap digest of everything that could give this lane something to do.

    Four reads and one of them is the point: how many statement lines have been
    filed. The framework's comparison table is computed from them, so a new
    quarter is new evidence for this deliverable even in a week when nobody
    wrote a Claim -- and that is the ordinary case, not the exception.
    """

    from .cockpit_model import verifier_provider_contract_fingerprint
    parts: list[str] = [verifier_provider_contract_fingerprint(
        "industry_framework_verifier")]
    row = connection.execute(
        "SELECT COUNT(*) AS n, MAX(created_at) AS newest FROM claim_versions"
    ).fetchone()
    parts += [str(row["n"]), str(row["newest"] or "-")]
    # 2026-09-25: a retired Claim recorded as the industry's is new industry
    # evidence without a single new Claim version.
    from .claim_industry_reattribution import reattribution_state_probe

    try:
        parts.append(reattribution_state_probe(connection))
    except Exception:  # noqa: BLE001 - an unreadable probe is a valid state
        parts.append("reattributions:none")
    for sql, label in (
        ("SELECT COUNT(*) AS n FROM statement_ingest_lines", "lines"),
        # 2026-10-08: the filed statements live in the coverage-mission
        # authority; ``statement_ingest_lines`` does not exist on a mission
        # Core, so a new quarter never moved this signature there.
        ("SELECT COUNT(*) AS n FROM coverage_mission_statement_filings", "filings"),
        ("SELECT COUNT(*) AS n FROM company_dossier_versions", "dossiers"),
        ("SELECT COUNT(*) AS n FROM debate_map_versions", "debates"),
    ):
        try:
            parts.append(f"{label}:{connection.execute(sql).fetchone()['n']}")
        except Exception:  # noqa: BLE001 - an absent authority is a valid state
            parts.append(f"{label}:none")
    try:
        heads = connection.execute(
            "SELECT framework_ref, MAX(version_number) AS v "
            "FROM industry_framework_versions GROUP BY framework_ref "
            "ORDER BY framework_ref"
        ).fetchall()
        parts += [f"{item['framework_ref']}:{item['v']}" for item in heads]
    except Exception:  # noqa: BLE001 - no framework table yet is a valid state
        parts.append("no-frameworks")
    from .research_followup_automation import groups as followup_groups
    repairs = [g for g in followup_groups(connection) if g["route"] == "framework_repair"]
    if repairs:
        from .store import content_hash
        parts.append("existing-artefact-repair:" + content_hash(repairs))
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


class MissionIndustryFrameworkLaneCoordinator:
    """Launch and settle the industry-framework lane."""

    def __init__(
        self,
        *,
        connection: Any,
        launcher: Any,
        min_interval_seconds: int = MIN_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.time,
        failure_ledger_dir: Any | None = None,
        failure_clock: Any | None = None,
    ) -> None:
        self.connection = connection
        self.launcher = launcher
        self.state_dir = getattr(launcher, "state_dir", None)
        self._material_cache: tuple[str, float, Mapping[str, Any]] | None = None
        self.min_interval_seconds = int(min_interval_seconds)
        self.clock = clock
        self._open: str | None = None
        self._last_launch: float | None = None
        # The signature under which the last run found nothing, and the
        # signatures whose runs failed. Held for this process only: a restart
        # is nearly always a deploy, which is the likeliest thing to have
        # fixed it.
        self._quiet_signature: str | None = None
        # Whether a run of this process published a version, and what the last
        # settled run said: together with the Ledger they decide whether the
        # weekly cadence applies yet and what the waiting reason names.
        self._published = False
        self._last_settled: dict[str, Any] | None = None
        self.first_draft_retry_seconds = min(
            int(min_interval_seconds), FIRST_DRAFT_RETRY_SECONDS)
        self.gate_redraft_seconds = min(int(min_interval_seconds), GATE_REDRAFT_SECONDS)
        # (signature, trigger): the gate is re-read only when the Ledger moved.
        self._gate_cache: tuple[str, dict[str, Any] | None] | None = None
        self.budget = lane_budget("industry_framework", state_dir=failure_ledger_dir,
                                  clock=failure_clock)

    def _settle(self, ticket_ref: str) -> dict[str, Any] | None:
        try:
            ticket = self.launcher.status(ticket_ref)
        except LaneChildTicketNotFound:
            return {"status": "orphaned", "ticket_ref": ticket_ref}
        except Exception:  # noqa: BLE001 - unreadable now; try again next tick
            return None
        if ticket.get("status") == "running":
            return {"status": "running", "ticket_ref": ticket_ref}
        summary = ticket.get("summary") or {}
        settled = {
            "status": ticket.get("status"),
            "ticket_ref": ticket_ref,
            "signature": ticket.get("signature"),
            "industry_ref": summary.get("industry_ref"),
            "framework_status": summary.get("framework_status"),
            "version_ref": summary.get("version_ref"),
            "units_drafted": summary.get("units_drafted"),
            "new_refs": summary.get("new_refs"),
            "open_gaps": summary.get("open_gaps"),
            "comparison": summary.get("comparison"),
            "cost_micros": summary.get("cost_micros"),
        }
        reason = summary.get("failure_reason")
        if reason:
            settled["failure_reason"] = str(reason)[:MAX_FAILURE_DETAIL_CHARS]
        return settled

    def _settle_open(self) -> dict[str, Any] | None:
        if self._open is None:
            return None
        settled = self._settle(self._open)
        if settled is None or settled.get("status") == "running":
            return settled
        self._open = None
        self._last_settled = dict(settled)
        if settled.get("version_ref") or settled.get("framework_status") == "published":
            self._published = True
        signature = settled.get("signature")
        status = str(settled.get("framework_status") or "")
        if status in {"not_authorized", "no_policy"} and signature:
            self.budget.record(permission_key(self.connection, self.launcher, str(signature)),
                               status="gated:not permitted",
                               reason=f"gated:not permitted: {status}")
        elif status in QUIET_STATUSES and signature:
            settled["resumed"] = self.budget.clear(str(signature))
            self._quiet_signature = str(signature)
        elif settled.get("status") not in ("succeeded", "orphaned"):
            if signature:
                self.budget.record_settled(str(signature), settled)
        elif status in CONTENT_TERMINAL_STATUSES and signature:
            self.budget.record(str(signature), status=f"content_refused:{status}",
                               reason=settled.get("failure_reason") or status)
        elif signature:
            self.budget.clear(str(signature))
        return settled

    def _has_version(self) -> bool:
        return self._published or latest_version_epoch(self.connection) is not None

    def _gate(self, signature: str) -> dict[str, Any] | None:
        if self._gate_cache is not None and self._gate_cache[0] == signature:
            return self._gate_cache[1]
        try:
            trigger = gate_redraft_trigger(self.connection, self.launcher)
        except Exception:  # noqa: BLE001 - an unreadable gate leaves the week to decide
            trigger = None
        self._gate_cache = (signature, trigger)
        return trigger

    def _material_probe(self, now: float, *, source_signature: str = "") -> Mapping[str, Any] | None:
        """The current mission's arrivals and due state, without writes."""
        if self.state_dir is None:
            return None
        from .coverage_mission import CoverageMissionAuthority
        from .industry_framework import IndustryFrameworkAuthority
        from .mission_model_stage_lane import _reader
        from .redraft_policy import lane_probe

        exists = self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='coverage_mission_pointer'").fetchone()
        if exists is None:
            return None
        pointer = self.connection.execute(
            "SELECT mission_version_id FROM coverage_mission_pointer "
            "ORDER BY mission_ref LIMIT 1").fetchone()
        if pointer is None:
            return None
        mission = _reader(self.connection, CoverageMissionAuthority)._mission_record(
            str(pointer[0]))
        exists = self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='industry_framework_versions'").fetchone()
        target = str(mission["industry_ref"])
        prior = (None if exists is None else
                 _reader(self.connection, IndustryFrameworkAuthority).latest(target))
        kwargs = dict(product="industry_framework", target=target,
            company_refs=[str(member["company_ref"]) for member in mission["universe"]],
            prior=prior, now=datetime.fromtimestamp(now, timezone.utc))
        light = lane_probe(self.state_dir, self.connection, **kwargs, light_only=True)
        key = hashlib.sha256(f"{source_signature}|{light['fingerprint']}".encode()).hexdigest()
        cached = self._material_cache
        if cached is not None and cached[0] == key and 0 <= now - cached[1] < MATERIAL_PROBE_SECONDS:
            return cached[2]
        probe = lane_probe(self.state_dir, self.connection, **kwargs)
        self._material_cache = (key, now, probe)
        return probe

    def dispatch_once(self) -> dict[str, Any]:
        settled = self._settle_open()
        if self._open is not None:
            return {"status": "running", "ticket_ref": self._open, "settled": settled}
        try:
            now = self.clock()
            signature = ledger_signature(self.connection)
            probe = self._material_probe(now, source_signature=permission_key(
                self.connection, self.launcher, signature))
            if probe is not None:
                signature = hashlib.sha256(
                    f"{signature}|material-review:{probe['fingerprint']}".encode()
                ).hexdigest()[:32]
        except Exception as exc:  # noqa: BLE001 - one lane's failure is not the tick's
            return {"status": "unavailable", "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        if signature == self._quiet_signature:
            idle = {"status": "idle", "settled": settled, "signature": signature,
                    "reason": "nothing has moved since the last run found nothing"}
            if not self._has_version():
                missing = missing_input_text(self._last_settled)
                idle["reason"] = (f"行业分析框架还没有第一个版本：{missing}；"
                                  "相关输入变化后会自动重试。")
                idle["missing_input"] = missing
            return idle
        permission = permission_key(self.connection, self.launcher, signature)
        clear_obsolete_permissions(self.budget, permission, signature)
        held = (self.budget.blocked(permission)
                or self.budget.blocked(signature))
        if held is not None:
            return {"status": held.action, "settled": settled, "signature": signature,
                    "reason": held.classification.reason,
                    "failure_budget": self.budget.summary()}
        trigger, trigger_reason = "weekly", "行业框架每周例行重写"
        if not self._has_version():
            trigger, trigger_reason = "first_draft", "行业框架还没有第一个版本，起草第一版"
            # No framework yet: the weekly cadence does not apply.  A run that
            # published nothing is retried after a short pause, and the reason
            # names what it was missing.
            if self._last_launch is not None and (
                now - self._last_launch < self.first_draft_retry_seconds
            ):
                missing = missing_input_text(self._last_settled)
                remaining = int(self.first_draft_retry_seconds - (now - self._last_launch))
                waiting = {
                    "status": "waiting", "settled": settled, "signature": signature,
                    "reason": (f"行业分析框架还没有第一个版本：{missing}；"
                               f"{remaining} 秒后重试。"),
                    "missing_input": missing,
                }
                if (self._last_settled or {}).get("failure_reason"):
                    waiting["last_failure"] = self._last_settled["failure_reason"]
                return waiting
        elif probe is not None and probe["urgent"] is True:
            trigger, trigger_reason = "material_policy", str(probe["label"])
        else:
            since = max(filter(lambda value: value is not None, (
                self._last_launch, latest_version_epoch(self.connection))),
                default=None)
            if since is not None and now - since < self.min_interval_seconds:
                # The cadence, and it is deliberately checked after the
                # signature: an operator reading the tick summary should be
                # able to see that there *is* something to do and that the
                # lane is waiting, rather than seeing nothing and wondering
                # whether the lane is wired.  Measured from the newest
                # version in the Ledger as well, so a writer restart does not
                # reset the week.
                gate = self._gate(signature)
                if gate and gate.get("changes"):
                    trigger = "gate"
                    trigger_reason = (
                        "模型阶段门缺：" + "、".join(gate["deficiencies"])
                        + "；自上一版以来：" + "；".join(gate["changes"]))
                    if now - since < self.gate_redraft_seconds:
                        remaining = int(self.gate_redraft_seconds - (now - since))
                        return {
                            "status": "waiting", "settled": settled,
                            "signature": signature, "trigger": "gate",
                            "reason": (f"{trigger_reason}；按缺口提前重写，"
                                       f"{remaining} 秒后起草")[:500],
                        }
                else:
                    waiting = {
                        "status": "waiting", "settled": settled, "signature": signature,
                        "trigger": "weekly",
                        "reason": ("the industry framework is a weekly deliverable; "
                                   f"{int(self.min_interval_seconds - (now - since))}"
                                   "s of its interval remain"),
                    }
                    if gate:
                        waiting["gate_check"] = (
                            "模型阶段门缺：" + "、".join(gate["deficiencies"])
                            + "；但自上一版以来输入没有变化，按每周节奏等待")
                    return waiting
        try:
            ticket = self.launcher.start(signature=signature)
        except LaneChildConflict as exc:
            return {"status": "busy", "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        except LaneChildRejected as exc:
            return {"status": "rejected", "settled": settled,
                    "reason": f"{type(exc).__name__}: {exc}"}
        self._open = ticket["id"]
        self._last_launch = now
        return {"status": "launched", "ticket_ref": ticket["id"],
                "signature": signature, "settled": settled,
                "trigger": trigger, "reason": trigger_reason[:500],
                **({} if probe is None else {"material_probe": dict(probe)})}


def dispatch(server: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    """Controller tick (P12e): the industry's framework, a few parts a week."""

    launcher = server.lane_launcher(LAUNCHER_KWARG)
    if launcher is None:
        return {"status": "unconfigured",
                "reason": "no industry-framework lane on this writer"}
    coordinator = server.lane_state.get(LAUNCHER_KWARG)
    if coordinator is None:
        coordinator = MissionIndustryFrameworkLaneCoordinator(
            connection=server.store.connection, launcher=launcher,
            failure_ledger_dir=getattr(launcher, "state_dir", None))
        server.lane_state[LAUNCHER_KWARG] = coordinator
    return coordinator.dispatch_once()


def add_arguments(parser: Any) -> None:
    from pathlib import Path as _Path

    parser.add_argument(
        "--industry-framework-model-config", type=_Path, default=None,
        help="Model configuration the framework drafts with. Omit and the lane "
             "computes its comparison table and reports gated: the table needs "
             "no model, the prose does.",
    )
    parser.add_argument(
        "--industry-framework-policy", type=_Path, default=None,
        help="P12e policy: the causal-chain titles, the driver horizons, the gap "
             "checklist and the Constitution's output_rubric bindings.",
    )
    parser.add_argument(
        "--industry-framework-verifier-model-config", type=_Path, default=None,
        help="The configuration the independent verifier runs on. Without it "
             "the child holds: one configuration routes both calls the same "
             "way, so the verdict could never be shown to be independent.",
    )


def build_launcher(args: Any) -> Any | None:
    if getattr(args, "industry_framework_policy", None) is None:
        return None
    from pathlib import Path as _Path

    from .industry_framework_launcher import IndustryFrameworkLauncher

    return IndustryFrameworkLauncher(
        state_dir=_Path(args.db).expanduser().resolve().parent,
        model_config_path=getattr(args, "industry_framework_model_config", None),
        verifier_model_config_path=getattr(
            args, "industry_framework_verifier_model_config", None),
        scheduler_db=getattr(args, "scheduler", None),
        policy_path=args.industry_framework_policy,
    )


# What this lane needs on disk before it is worth turning on. The policy alone,
# and not the model configuration: this is the one drafting lane whose product
# is half deterministic, so a Core with the policy and no model still gets the
# cross-company table computed and reported every week -- which is exactly the
# state the live Core is in until the deliverable scope is granted.
FRAMEWORK_MODEL_CONFIG = "initial-screen-model-config.json"
FRAMEWORK_VERIFIER_MODEL_CONFIG = "company-dossier-verifier-model-config.json"
LEGACY_FRAMEWORK_VERIFIER_MODEL_CONFIG = "dossier-verifier-model-config.json"
FRAMEWORK_POLICY = "p12e-industry-framework-policy-v1.json"


def argv_fragment(context: Any) -> list[str]:
    policy = context.state / FRAMEWORK_POLICY
    if not policy.is_file():
        return []
    argv = ["--industry-framework-policy", str(policy)]
    config = context.state / FRAMEWORK_MODEL_CONFIG
    if config.is_file():
        argv += ["--industry-framework-model-config", str(config)]
    for name in (FRAMEWORK_VERIFIER_MODEL_CONFIG,
                 LEGACY_FRAMEWORK_VERIFIER_MODEL_CONFIG):
        verifier = context.state / name
        if verifier.is_file():
            argv += ["--industry-framework-verifier-model-config", str(verifier)]
            break
    return argv


LANE = register_lane(LaneSpec(
    operation="dispatch_industry_framework",
    failure_ledger="industry_framework",
    # 139, after the dossier at 137: the framework reads the company files'
    # demand and supply sections, so it runs behind the lane that writes them.
    order=139,
    driver_key="industry_framework",
    handler=dispatch,
    init_kwarg=LAUNCHER_KWARG,
    argparse=add_arguments,
    launcher_factory=build_launcher,
    argv_fragment=argv_fragment,
    note="P12e: the industry framework -- the Constitution's causal chain, the "
         "driver pack's drivers, a computed five-company comparison, and a gap "
         "list derived from the source capability map. Weekly.",
))


__all__ = [
    "FRAMEWORK_MODEL_CONFIG",
    "FRAMEWORK_POLICY",
    "FRAMEWORK_VERIFIER_MODEL_CONFIG",
    "LEGACY_FRAMEWORK_VERIFIER_MODEL_CONFIG",
    "LANE",
    "LAUNCHER_KWARG",
    "MAX_FAILURE_DETAIL_CHARS",
    "FIRST_DRAFT_RETRY_SECONDS",
    "GATE_REDRAFT_SECONDS",
    "REDRAFTABLE_CRITERIA",
    "gate_redraft_trigger",
    "redraft_changes",
    "MIN_INTERVAL_SECONDS",
    "QUIET_STATUSES",
    "latest_version_epoch",
    "missing_input_text",
    "MissionIndustryFrameworkLaneCoordinator",
    "add_arguments",
    "argv_fragment",
    "build_launcher",
    "dispatch",
    "ledger_signature",
]
