"""Read-only: does this environment have everything its lanes need to run?

``workspace_lane_parity`` answers "which file-driven lanes are on this writer".
This module answers the rest of the question an owner asks when a second
environment misbehaves where the first did not -- and it is the list of every
wall ws-7d hit, turned into checks that run before the wall is hit:

``governance``
    The active policy carries the runtime baseline (``workspace_governance_
    baseline``), the constitution binds that policy, and the mandate and the
    policy both hold a closed research budget that covers the mission's.
``lanes``
    Per registered lane: would a freshly rendered writer run it, what did its
    last tick say, and -- for the lanes that check governance themselves --
    does the active policy pass that lane's own precondition.
``authorization``
    The mission grants the automation every write scope the lanes use, the
    SEC source is connected, and the writer's core principal is unrestricted.
``mission``
    No open review is stranded on a superseded mission version, which is how
    extraction stalled after ws-7d's policy-5 cascade.
``config``
    Every JSON config a lane reads exists, parses, and points at this
    environment's own databases and at routing policies that exist there.
``host``
    The machine-wide rows from ``workspace_lane_parity.audit_lanes``: shared
    daily budget, routing alignment with the other environments, credential
    slots, and the per-lane file inputs.

Every row is ``ok``, ``gap`` (a lane will refuse or stall), ``warn`` (worth a
look, not a refusal), ``drift`` (differs from the fresh-Core default; the
owner decides), ``pending`` (nothing to check until the first mission is
published) or ``info``.  A gap carries a ``fix`` -- the command, or who has to
decide.

Nothing here writes: every database is opened ``mode=ro``, no socket is
touched, no file is created.  Secrets in configs (broker keys, writer tokens)
are read only to check their shape and are never put in a row.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .coverage_mission import AWAITING_REVIEW_DISPOSITION_SQL

SCHEMA_VERSION = "dalton-workspace-health-parity-0.1"

OK, GAP, WARN, DRIFT, PENDING, INFO = "ok", "gap", "warn", "drift", "pending", "info"

#: Scripts an owner runs to close a gap.  Relative to the repository root.
SIGN_RULES_FIX = ("python scripts/sign_auto_commit_rules.py --state-dir {state} "
                  "[--rehearse /tmp/x] --apply --actor human:owner")
SIGN_PLAN_FIX = ("python scripts/sign_research_plan_auto_start.py --state-dir {state} "
                 "--apply --actor human:owner")
BUDGET_FIX = ("在「管理员设置 → 预算与费用 → 研究预算上限」保存一次（写入服务操作 "
              "set_research_budget_authority_chain），它会把研究预算写进 policy/mandate/constitution/mission")

#: Lanes that check the governance policy themselves, and what they check.
#: ``sec`` is ``sec_company_facts_lane.check_core_governance_rules`` verbatim.
LANE_POLICY_RULES: dict[str, tuple[str, ...]] = {
    "document_extraction": ("research-auto-commit:mission-document-qualitative:v1",),
    "quantitative_claim_promotion": (
        "research-auto-commit:mission-verified-figure:v1",
        "research-auto-commit:sec-statement-line:v1",
    ),
    "mission_sec_quarters": (
        "research-auto-commit:sec-public-company-facts-growth:v1",
        "research-auto-commit:sec-public-company-facts-growth-annual:v1",
        "research-auto-commit:sec-statement-line-growth-fy-minus-9m:v1",
    ),
}
LANE_PLAN_RULES: dict[str, tuple[str, ...]] = {
    "mission_sec_quarters": (
        "research-plan-auto-start:sec-public-company-facts:v1",
        "research-plan-auto-start:sec-public-company-facts-annual:v1",
    ),
}
#: Tick statuses that mean the lane did what a healthy lane does.
_HEALTHY_TICK = {"idle", "launched", "busy", "dispatched", "waiting", "duplicate",
                 "recorded", "queued", "settled", "entered", "ok", "committed",
                 # 2026-10-06: the owner paused it (owner_levers) -- shown in
                 # the owner_levers section, not alarmed on as a lane fault.
                 "paused"}
_UNCONFIGURED_CODES = {"lane_unconfigured_hold"}
#: A discovery coordinator deferred this many ticks running is a warning: the
#: tick's fair shares and starved-first order should reach it well before.
DISCOVERY_DEFERRED_WARN_TICKS = 3
_DISCOVERY_LANE = "mission_source_discovery"
#: Acquisition words (``discovery_coordinator_state``) that hold a queue.
_SELECTION_HOLD_WORDS = frozenset({"selection_pending", "selection_unavailable"})
#: This many settled discovery searches of one source failing in a row is a
#: warning; a transient failure is followed by a success well before.
DISCOVERY_SEARCH_FAILURE_WARN = 3
_SEARCH_FAILURE_SCAN = 50
_DISCOVERY_SOURCE_NAMES = {"source:alphaengine": "alphaengine",
                           "source:sec-edgar": "sec_filings_index",
                           "source:web-search": "web_search",
                           "source:guidepoint": "guidepoint"}


def _row(section: str, check: str, status: str, detail: str,
         fix: str | None = None, **extra: Any) -> dict[str, Any]:
    row = {"section": section, "check": check, "status": status, "detail": detail}
    if fix and status in {GAP, WARN, DRIFT, PENDING}:
        row["fix"] = fix
    row.update({key: value for key, value in extra.items() if value is not None})
    return row


def _connect_ro(path: Path) -> sqlite3.Connection | None:
    if not path.is_file():
        return None
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        connection.execute("PRAGMA query_only=ON")
    except sqlite3.Error:
        return None
    connection.row_factory = sqlite3.Row
    return connection


def _json(path: Path) -> tuple[Any, str | None]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, "missing"
    except (OSError, UnicodeError, ValueError) as exc:
        return None, f"unreadable: {type(exc).__name__}"


# -- the environment ------------------------------------------------------------


class Environment:
    """One environment, read once: its Core, mission, policy and configs."""

    def __init__(self, name: str, state_dir: str | Path, *,
                 service_config: str | Path | None = None,
                 plist_path: str | Path | None = None) -> None:
        from .service_config_location import service_config_path

        self.name = name
        # Lexical, not resolved: legacy's state directory is a link onto an
        # external volume, and its service.json lives beside the link.
        self.state = Path(str(state_dir)).expanduser()
        self.service_config = (Path(service_config).expanduser() if service_config
                               else service_config_path(self.state))
        self.plist_path = Path(plist_path).expanduser() if plist_path else None
        from .workspace_lifecycle import state_for_core
        self.core = (_connect_ro(self.state / "core.sqlite") if state_for_core(self.state) == "active" else None)
        self.mission: dict[str, Any] | None = None
        self.constitution: dict[str, Any] | None = None
        self.mandate: dict[str, Any] | None = None
        self.policy: dict[str, Any] | None = None
        self.mission_count = 0
        if self.core is not None:
            self._read_core()

    def close(self) -> None:
        if self.core is not None:
            self.core.close()
            self.core = None

    def _read_core(self) -> None:
        assert self.core is not None
        try:
            row = self.core.execute(
                "SELECT v.version_json FROM governance_policy_pointer p "
                "JOIN governance_policy_versions v ON v.policy_version_id=p.policy_version_id "
                "WHERE p.pointer_id=1").fetchone()
            self.policy = json.loads(row[0]) if row is not None else None
        except (sqlite3.Error, ValueError):
            self.policy = None
        try:
            pointers = self.core.execute(
                "SELECT mission_version_id FROM coverage_mission_pointer ORDER BY mission_ref"
            ).fetchall()
        except sqlite3.Error:
            pointers = []
        self.mission_count = len(pointers)
        if not pointers:
            return
        try:
            self.mission = json.loads(self.core.execute(
                "SELECT record_json FROM coverage_mission_versions WHERE mission_version_id=?",
                (pointers[0][0],)).fetchone()[0])
            bindings = self.mission.get("bindings") or {}
            constitution_ref = (bindings.get("constitution_version") or {}).get("ref")
            row = self.core.execute(
                "SELECT record_json FROM research_constitution_versions "
                "WHERE constitution_version_id=?", (constitution_ref,)).fetchone()
            self.constitution = json.loads(row[0]) if row is not None else None
            mandate_ref = (bindings.get("mandate_version") or {}).get("ref")
            row = self.core.execute(
                "SELECT record_json FROM mandate_versions WHERE version_id=?",
                (mandate_ref,)).fetchone()
            self.mandate = json.loads(row[0]) if row is not None else None
        except (sqlite3.Error, TypeError, ValueError):
            pass

    @property
    def service(self) -> dict[str, Any] | None:
        value, _error = _json(self.service_config)
        return value if isinstance(value, dict) else None


# -- governance -------------------------------------------------------------------


def check_governance(env: Environment) -> list[dict[str, Any]]:
    from .coverage_mission import RESEARCH_BUDGET_REQUIRED_FIELDS, research_budget_shape_valid
    from .workspace_governance_baseline import (
        governance_baseline_checks,
        research_specific_policy_keys,
    )

    section = "governance"
    if env.core is None:
        return [_row(section, "core", GAP, f"no Core at {env.state / 'core.sqlite'}")]
    if env.policy is None:
        return [_row(section, "active_policy", GAP, "this Core has no active governance policy")]
    rows: list[dict[str, Any]] = []
    fix = SIGN_RULES_FIX.format(state=env.state) + "; " + SIGN_PLAN_FIX.format(state=env.state)
    for item in governance_baseline_checks(env.policy, mission=env.mission, fix_command=fix):
        status = item["status"]
        if env.mission is None and status == GAP:
            # 2026-09-30 (d1): not "pending".  These nine (six auto-commit
            # rules, two plan auto-start rules, the closed research budget)
            # are signed in by the creation flow itself when the first
            # mission is published; there is nothing for the owner to do
            # about them, and 38 "pending" rows on a blank workspace (ws-e399)
            # read like 38 things left undone.  The one real next step is the
            # ``mission`` row below, which stays pending.
            status = INFO
            item = {**item,
                    "detail": "首个研究任务发布时自动签入，无需手工处理：" + item["detail"]}
        if item["check"] == "policy.research_budget" and status == GAP and env.mission is not None:
            item = {**item, "fix": BUDGET_FIX}
        rows.append(_row(section, item["check"], status, item["detail"], item.get("fix")))
    specific = research_specific_policy_keys(env.policy.get("policy") or {})
    if specific:
        rows.append(_row(section, "policy.research_specific", INFO,
                         f"{env.policy.get('id')} carries this environment's own research "
                         f"content: {specific} (not part of the runtime baseline)"))
    if env.mission is None:
        rows.append(_row(section, "mission", PENDING, "no coverage mission is published yet",
                         fix="在投研工作台「研究设置 → 研究目标」确认首个研究任务"))
        return rows
    if env.mission_count > 1:
        rows.append(_row(section, "mission", WARN,
                         f"{env.mission_count} active missions; only the first is checked"))
    bound = ((env.constitution or {}).get("bindings") or {}).get("governance_policy_version") or {}
    rows.append(_row(
        section, "constitution.binds_active_policy",
        OK if bound.get("ref") == env.policy.get("id")
        and bound.get("hash") == env.policy.get("content_hash") else GAP,
        f"constitution {(env.constitution or {}).get('id')} binds {bound.get('ref')}; "
        f"active policy is {env.policy.get('id')} -- document extraction refuses "
        "'mission constitution does not bind current governance policy' otherwise",
        fix="republish the constitution and mission bound to the active policy "
            "(scripts/sign_auto_commit_rules.py does the cascade)"))
    cap = ((env.mandate or {}).get("constraints") or {}).get("research_budget")
    budget = env.mission.get("budget") or {}
    over = ([key for key in sorted(RESEARCH_BUDGET_REQUIRED_FIELDS)
             if key in budget and isinstance(cap, Mapping) and key in cap
             and budget[key] > cap[key]]
            if research_budget_shape_valid(cap) else [])
    ok = research_budget_shape_valid(cap) and not over
    rows.append(_row(
        section, "mandate.research_budget", OK if ok else GAP,
        f"mandate {(env.mandate or {}).get('id')} research_budget="
        f"{dict(cap) if isinstance(cap, Mapping) else cap!r}"
        + (f"; the mission asks for more: {over}" if over else "")
        + " -- document extraction reads it as a closed three-cap budget",
        fix=BUDGET_FIX))
    return rows


# -- lanes ------------------------------------------------------------------------


def _last_ticks(env: Environment) -> dict[str, dict[str, Any]]:
    connection = _connect_ro(env.state / "tick-ledger.sqlite")
    if connection is None:
        return {}
    try:
        tick = connection.execute(
            "SELECT tick_id, started_at FROM tick_ledger_ticks "
            "ORDER BY started_at DESC LIMIT 1").fetchone()
        if tick is None:
            return {}
        rows = connection.execute(
            "SELECT driver_key, status, status_word, counts_json FROM tick_ledger_lanes "
            "WHERE tick_id=?", (tick["tick_id"],)).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        connection.close()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            counts = json.loads(row["counts_json"])
        except (TypeError, ValueError):
            counts = {}
        result[row["driver_key"]] = {
            "status": row["status"], "word": row["status_word"],
            "reason": str((counts or {}).get("reason") or "")[:240],
            "reason_code": (counts or {}).get("reason_code"),
            "counts": counts if isinstance(counts, dict) else {},
            "tick_started_at": tick["started_at"],
        }
    return result


#: A lane that ran and is waiting for its inputs says so in these words
#: (``{"status": "unconfigured", "reason": "no mission"}`` from most mission
#: lanes; the writer remembers it as ``held`` / ``lane_unconfigured_hold``).
#: That lane is loaded and live -- it is not a missing writer argument.
_WAITING_ON_MISSION = re.compile(
    r"\bno (?:active |current |published )?(?:coverage )?mission\b"
    r"|\bno (?:active |current |research )?plan(?: yet)?\b"
    # The first mission installs the discovery plans (writer
    # ``_install_discovery_plans``); before it the file is not there yet.
    r"|plan is unusable: \[Errno 2\] No such file"
    r"|还没有 ?coverage mission|没有生效的 ?CoverageMission|没有当前的研究任务",
    re.IGNORECASE)


def _tick_waits_on_mission(tick: Mapping[str, Any]) -> bool:
    reason = str(tick.get("reason") or "")
    # "no research plan launcher on this writer" is a missing argument.
    if "on this writer" in reason:
        return False
    return bool(_WAITING_ON_MISSION.search(reason))


def _tick_lacks_launcher(tick: Mapping[str, Any]) -> bool:
    """The running writer did not load this lane (its argument is missing).

    ``unconfigured`` -- or the writer's ``lane_unconfigured_hold`` memory of
    it -- is that, unless the lane says it is waiting for a mission or a plan:
    then it is loaded and ran.  ``held`` / ``pending`` / ``waiting`` on any
    other reason is a live lane too.
    """

    unconfigured = (tick.get("reason_code") in _UNCONFIGURED_CODES
                    or str(tick.get("word")) == "unconfigured")
    return unconfigured and not _tick_waits_on_mission(tick)


def _lane_configured(spec: Any, context: Any) -> bool | None:
    if spec.argv_fragment is None:
        return None
    try:
        return bool(spec.argv_fragment(context))
    except Exception:  # noqa: BLE001 - a lane that cannot even render is not configured
        return False


def _policy_body(env: Environment) -> Mapping[str, Any]:
    return (env.policy or {}).get("policy") or {}


def _rules_missing(env: Environment, lane: str) -> list[str]:
    body = _policy_body(env)
    commit = body.get("research_candidate_auto_commit")
    commit_rules = (commit.get("rules") or []) if isinstance(commit, Mapping) \
        and commit.get("enabled") is True else []
    start = body.get("research_plan_auto_start")
    start_rules = (start.get("rules") or []) if isinstance(start, Mapping) \
        and start.get("enabled") is True else []
    missing = [rule for rule in LANE_POLICY_RULES.get(lane, ()) if rule not in commit_rules]
    missing += [rule for rule in LANE_PLAN_RULES.get(lane, ()) if rule not in start_rules]
    return missing


def check_lanes(env: Environment) -> list[dict[str, Any]]:
    from .lane_registry import LaunchAgentContext, registered_lanes

    section = "lanes"
    service = env.service or {}
    review = (((service.get("control") or {}).get("config") or {}).get("research_review") or {})
    context = LaunchAgentContext(
        state=env.state,
        extraction_model_config_path=review.get("document_extraction_model_config_path"),
        candidate_staging_path=review.get("candidate_staging_path"))
    ticks = _last_ticks(env)
    installed = None
    if env.plist_path is not None:
        from .workspace_lane_parity import installed_writer_flags
        installed = installed_writer_flags(env.plist_path)
    rows: list[dict[str, Any]] = []
    for spec in registered_lanes():
        key = spec.driver_key or spec.operation
        configured = _lane_configured(spec, context)
        tick = ticks.get(key)
        problems: list[str] = []
        status = OK
        if configured is False:
            status = PENDING if env.mission is None else WARN
            problems.append("a freshly rendered writer would not run it (its inputs are absent)")
        plist_lacks: list[str] = []
        if installed is not None and configured and spec.argv_fragment is not None:
            flags = {token for token in spec.argv_fragment(context) if token.startswith("--")}
            plist_lacks = sorted(flags - installed)
        # Batch t: the writer derives lane flags its plist lacks from code at
        # startup (``lane_registry.derived_lane_argv``), so a stale plist alone
        # is not a gap.  What is a gap is a lane registered in code that the
        # *running* writer did not load: its tick says it is unconfigured, or
        # the tick does not report it at all.
        not_live = None
        release_older = False
        if spec.driver_key is not None and ticks:
            if tick is None:
                # The running controller does not know the lane at all: its
                # code predates the lane's registration.
                not_live = "the last controller tick did not report it"
                release_older = True
            elif configured is not False and _tick_lacks_launcher(tick):
                not_live = f"the running writer says {tick['word']}: {tick['reason']}"
        if not_live is not None:
            status = GAP
            problems.append(
                "registered in code but not live on this writer (" + not_live + ")"
                + (" -- its plist also lacks " + ", ".join(plist_lacks) if plist_lacks else "")
                + (" -- the writer is running a release older than the lane; deploy "
                   "(release switch restarts it) to pick it up" if release_older else
                   " -- the running writer has no launcher for it (its lane argument is "
                   "missing); the writer derives lane arguments from code at startup, so "
                   "restart it and check what the lane needs on disk"))
            tick = None  # already said; the tick block below would repeat it
        elif plist_lacks:
            if status == OK:
                status = INFO
            problems.append("the installed writer plist lacks " + ", ".join(plist_lacks)
                            + " (the writer derives them from code at startup)")
        missing = _rules_missing(env, key) if env.mission is not None else []
        if missing:
            status = GAP
            problems.append("active policy does not list " + ", ".join(missing))
        if tick is not None:
            word = str(tick["word"])
            reason = tick["reason"]
            if _tick_waits_on_mission(tick):
                # Live: the lane ran and is waiting for a mission / plan.
                if status == OK:
                    status = PENDING if env.mission is None else WARN
                problems.append(f"live, waiting for a mission or its plan "
                                f"(last tick: {word}: {reason})")
            elif tick["reason_code"] in _UNCONFIGURED_CODES or word == "unconfigured":
                if status == OK:
                    status = PENDING if env.mission is None else WARN
                problems.append(f"last tick: {word}: {reason}")
            elif "precondition" in reason or "not_permitted" in word or "forbidden" in tick["status"]:
                # Before the first mission the policy is the bootstrap one and
                # the creation flow signs the baseline with the mission.
                status = PENDING if env.mission is None and status in {OK, PENDING} else GAP
                problems.append(f"last tick: {tick['status']}: {reason}")
            elif word.startswith("unavailable") or word in {"recovery_required", "error", "failed"}:
                if status == OK:
                    status = WARN
                problems.append(f"last tick: {tick['status']}: {reason}")
            elif word not in _HEALTHY_TICK and word != "held":
                if status == OK:
                    status = INFO
                problems.append(f"last tick: {tick['status']}: {reason}")
        elif spec.driver_key is not None and ticks and not_live is None:
            if status == OK:
                status = WARN
            problems.append("the last controller tick did not report this lane "
                            "(release older than the lane?)")
        detail = "; ".join(problems) if problems else (
            f"last tick: {tick['status']}" + (f" ({tick['reason'][:120]})" if tick and tick["reason"] else "")
            if tick else "no tick recorded")
        fix = None
        if missing:
            fix = (SIGN_RULES_FIX if any(rule.startswith("research-auto-commit") for rule in missing)
                   else SIGN_PLAN_FIX).format(state=env.state)
        rows.append(_row(section, key, status, detail, fix, operation=spec.operation))
    if env.mission is not None and "mission_sec_quarters" in ticks:
        rows.append(_sec_precondition(env))
    if env.mission is not None and "document_extraction" in ticks:
        rows.append(_maintenance_with_empty_queue(ticks["document_extraction"]))
    rows.extend(_discovery_coordinator_rows(env))
    rows.extend(_discovery_search_failure_rows(env))
    rows.extend(connector_health_rows(env))
    if env.mission is not None:
        rows.extend(_initial_screen_foundation_rows(env))
    return rows


def _initial_screen_foundation_rows(env: Environment, *,
                                    now: Any | None = None) -> list[dict[str, Any]]:
    """Companies whose Initial Screen has waited on missing readings too long.

    2026-10-02 (ws-7d): the drafting lane's tick said ``idle`` for fifteen
    days while every company waited on transcripts, a 10-K and broker notes
    that were never going to arrive by themselves.  ``held`` is a healthy
    word here and stays one; what is not healthy is the same company held on
    the same gap for days, so that is the row.
    """

    from datetime import datetime, timezone

    from .data_foundation import ESCALATE_AFTER, latest_summary, overdue

    moment = now or datetime.now(timezone.utc)
    blocks = overdue(latest_summary(env.state), now=moment)
    if not blocks:
        return []
    owner = [block for block in blocks if block.get("needs_owner")]
    detail = "; ".join(
        f"{block.get('ticker')} 等了 {block.get('days_waiting')} 天，缺 "
        + "、".join(f"{item.get('label')}（{item.get('cause_label')}）"
                   for item in block.get("items") or ())
        for block in blocks)
    return [_row(
        "lanes", "initial_screen_foundation", WARN,
        f"{len(blocks)} 家公司的初筛超过 {ESCALATE_AFTER.days} 天卡在资料底座：{detail}"[:1500],
        ("投研工作台「待我处理」里逐家列出了缺什么、为什么没进来、该谁处理"
         + ("；其中需要你决定的：" + "、".join(str(block.get("ticker")) for block in owner)
            if owner else "；都会自动补齐，只是慢")),
        companies=[block.get("company_ref") for block in blocks],
    )]


def _discovery_coordinator_history(env: Environment, limit: int) -> list[dict[str, Any]]:
    """The discovery lane's per-coordinator states over its last ``limit`` ticks,
    newest first. Rows from a release that did not record them are skipped."""

    connection = _connect_ro(env.state / "tick-ledger.sqlite")
    if connection is None:
        return []
    try:
        rows = connection.execute(
            "SELECT started_at, counts_json FROM tick_ledger_lanes WHERE driver_key=? "
            "ORDER BY started_at DESC LIMIT ?", (_DISCOVERY_LANE, limit)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        connection.close()
    history = []
    for row in rows:
        try:
            counts = json.loads(row["counts_json"])
        except (TypeError, ValueError):
            continue
        coordinators = counts.get("coordinators") if isinstance(counts, dict) else None
        if isinstance(coordinators, dict) and coordinators:
            history.append({"started_at": row["started_at"], "coordinators": coordinators})
        else:
            # An older row breaks the run: nothing is known about it.
            break
    return history


def _discovery_coordinator_rows(env: Environment, *,
                                threshold: int = DISCOVERY_DEFERRED_WARN_TICKS
                                ) -> list[dict[str, Any]]:
    """Warn when one discovery coordinator was deferred ``threshold`` ticks running.

    The discovery lane's own status is the AlphaEngine coordinator's, so an SEC
    or web search coordinator reporting ``deferred: tick budget exhausted``
    tick after tick looked, from the lane row, like an idle lane (legacy,
    2026-09-25 to 09-29). The tick ledger now keeps every coordinator's word.
    """

    history = _discovery_coordinator_history(env, threshold)
    if not history:
        return []
    rows = []
    latest = history[0]["coordinators"]
    for name in sorted(latest):
        run = 0
        for tick in history:
            state = tick["coordinators"].get(name) or {}
            if str(state.get("status") or "").split(":", 1)[0] != "deferred":
                break
            run += 1
        state = latest[name]
        streak = state.get("deferred_streak")
        run = max(run, int(streak) if isinstance(streak, int) and not isinstance(streak, bool) else 0)
        check = f"{_DISCOVERY_LANE}.{name}.deferred"
        if run >= threshold:
            rows.append(_row(
                "lanes", check, WARN,
                f"deferred {run} ticks running ({state.get('reason') or 'no reason given'}); "
                "the discovery tick's shared budget is not reaching this coordinator",
                fix="read tick_ledger_lanes.counts_json for mission_source_discovery "
                    "(coordinators.*.share_seconds / elapsed_seconds): the coordinator that "
                    "overruns its share is the one holding the store thread",
                deferred_ticks=run))
        else:
            rows.append(_row(
                "lanes", check, OK,
                f"last tick: {state.get('status')}"
                + (f", deferred {run} tick(s) running" if run else ""),
                deferred_ticks=run))
        rows.extend(_selection_hold_rows(name, history, threshold))
    return rows


def _selection_held(state: Mapping[str, Any]) -> bool:
    return (state.get("acquisition_status") in _SELECTION_HOLD_WORDS
            or (isinstance(state.get("selection_quarantined"), int)
                and not isinstance(state.get("selection_quarantined"), bool)
                and state["selection_quarantined"] > 0))


def _selection_hold_rows(name: str, history: Sequence[Mapping[str, Any]],
                         threshold: int) -> list[dict[str, Any]]:
    """Warn when a coordinator's acquisitions sat behind candidate selection.

    2026-09-29: both environments' AlphaEngine acquisitions answered
    ``selection_pending: CandidateSelectionError`` for days while the
    coordinator's own word was ``idle``, so this report said nothing.  A
    quarantine is the fix working, and still worth a look: it is a discovery
    nobody will read until the rule or the owner releases it.
    """

    run = 0
    for tick in history:
        if not _selection_held(tick["coordinators"].get(name) or {}):
            break
        run += 1
    if not run:
        return []
    state = history[0]["coordinators"].get(name) or {}
    check = f"{_DISCOVERY_LANE}.{name}.selection"
    quarantined = state.get("selection_quarantined")
    what = (f"{state.get('acquisition_status')}: {state.get('acquisition_reason') or 'no reason given'}"
            if state.get("acquisition_status") else "")
    if isinstance(quarantined, int) and quarantined > 0:
        what = (what + "; " if what else "") + f"{quarantined} discovery(ies) quarantined"
    if run >= threshold:
        return [_row(
            "lanes", check, WARN,
            f"acquisition held by candidate selection {run} ticks running ({what})",
            fix=("python -m dalton_core.discovery_selection_quarantine --state-dir <state> list; "
                 "a quarantine lapses when the selection rules version changes, or: ... release "
                 "--discovery-ref <ref> --actor human:<owner> --reason '...' [--apply]"),
            selection_held_ticks=run)]
    return [_row("lanes", check, OK, f"held {run} tick(s) running ({what})",
                 selection_held_ticks=run)]


def _discovery_search_failure_rows(env: Environment, *,
                                   threshold: int = DISCOVERY_SEARCH_FAILURE_WARN) -> list[dict[str, Any]]:
    """Warn when a source's last ``threshold`` discovery searches all failed.

    ws-7d's SEC filings index failed every search from 2026-09-27 with
    ``LeaseNotUsable: capability approval changed after lease issuance``; each
    failure settles one dispatch minutes after it launched, so the coordinator
    was ``idle`` on every tick and nothing here said so.  The settled
    dispatches in the Core are the record of what each search did.
    """

    if env.core is None:
        return []
    try:
        sources = [row[0] for row in env.core.execute(
            "SELECT DISTINCT source_ref FROM coverage_mission_discovery_dispatches")]
    except sqlite3.Error:
        return []
    rows = []
    for source_ref in sorted(str(x) for x in sources if x):
        try:
            recent = env.core.execute(
                "SELECT status, failure_reason, updated_at FROM coverage_mission_discovery_dispatches "
                "WHERE source_ref=? AND status IN ('succeeded','failed') "
                "ORDER BY updated_at DESC LIMIT ?", (source_ref, _SEARCH_FAILURE_SCAN)).fetchall()
        except sqlite3.Error:
            continue
        run = 0
        for row in recent:
            if row["status"] != "failed":
                break
            run += 1
        name = _DISCOVERY_SOURCE_NAMES.get(source_ref, source_ref)
        check = f"{_DISCOVERY_LANE}.{name}.searches"
        if run >= threshold:
            reason = str(recent[0]["failure_reason"] or "no reason recorded")
            rows.append(_row(
                "lanes", check, WARN,
                f"the last {run}{'+' if run == len(recent) else ''} discovery searches for "
                f"{source_ref} all failed (latest {recent[0]['updated_at']}: {reason[:200]})",
                fix=_search_failure_fix(reason), failed_searches=run))
        elif recent:
            rows.append(_row(
                "lanes", check, OK,
                f"last search {recent[0]['status']} at {recent[0]['updated_at']}"
                + (f", {run} failure(s) running" if run else ""),
                failed_searches=run))
    return rows


def connector_health_rows(env: Environment) -> list[dict[str, Any]]:
    """2026-10-08: a host-tool source whose login the provider stopped accepting.

    ``<state>/connector-health/`` (``connector_health``) holds one record per
    source the coordinator put on a ``provider_auth`` hold.  Held is a warning
    -- every search and acquisition of that source is paused until the owner
    re-logs in -- and a record that recovered is an ``ok`` row saying when.
    """

    from .connector_health import HELD, SOURCE_APPS, read_records

    rows = []
    for record in read_records(env.state):
        source_ref = str(record.get("source_ref") or "")
        name = _DISCOVERY_SOURCE_NAMES.get(source_ref, source_ref)
        check = f"connector_health.{name}.login"
        if record.get("status") == HELD:
            blocked = record.get("blocked") or {}
            message = str((record.get("last_detail") or {}).get("message") or "")
            rows.append(_row(
                "lanes", check, WARN,
                f"{source_ref} login lapsed (provider_auth) since {record.get('since')}: "
                f"{int(blocked.get('search') or 0)} search(es) and "
                f"{int(blocked.get('acquisition') or 0)} acquisition(s) refused; held until "
                f"{record.get('hold_until')}, probes {int(record.get('probes') or 0)}"
                + (f"; provider said: {message[:160]}" if message else ""),
                fix=(f"owner: re-login / refresh in {SOURCE_APPS.get(source_ref, source_ref)}; "
                     "the coordinator probes every 15 min and resumes by itself"),
                failure_class="provider_auth", since=record.get("since"),
                hold_until=record.get("hold_until")))
        else:
            rows.append(_row(
                "lanes", check, OK,
                f"{source_ref} login accepted (last hold recovered at {record.get('recovered_at')})"))
    return rows


def _search_failure_fix(reason: str) -> str:
    if "[provider_auth]" in reason or "刷新 token" in reason:
        return ("the provider refused the login/session (provider_auth): the owner re-logs in "
                "the host app (AlphaEngine Desktop); the coordinator resumes after its probe")
    if "approval changed after lease issuance" in reason:
        return ("the published descriptor's approval receipt no longer matches the governance "
                "record: PYTHONPATH=src python scripts/check_capability_approval_receipts.py "
                "--state-dir <state> (read-only)")
    return "read the failed dispatch's ticket summary (coverage_mission_discovery_dispatches.ticket_ref)"


def _maintenance_with_empty_queue(tick: Mapping[str, Any]) -> dict[str, Any]:
    """Do the claim-support checks and the P13i re-check run when nothing is queued?

    They ran only inside a drafting child, and no child was started while the
    queue was empty (ws-7d from 09:00 on 2026-09-27).  Since 93ccbcb8 an empty
    queue starts a support-only child, held between runs with ``support:
    held``; a bare ``idle`` over ``awaiting: 0`` is a release without it.
    """

    counts = tick.get("counts") or {}
    idle_empty = (str(tick.get("word")) == "idle" and counts.get("awaiting") == 0
                  and "support" not in counts and counts.get("mode") != "support_only")
    return _row(
        "lanes", "document_extraction.maintenance_when_queue_empty", WARN if idle_empty else OK,
        ("last tick was idle over an empty queue without a support-only child: the claim-"
         "support recheck/backfill and the P13i subject re-check are not running"
         if idle_empty else
         f"last tick: {tick.get('status')} (awaiting {counts.get('awaiting')}"
         + (f", support {counts.get('support') or counts.get('mode')}" if counts.get('support')
            or counts.get('mode') else "") + ")"),
        fix="deploy a release with the support-only extraction child (93ccbcb8 and later)")


def _sec_precondition(env: Environment) -> dict[str, Any]:
    from .sec_company_facts_lane import LanePreconditionError, check_core_governance_rules

    policy = env.policy or {}
    core = type("PolicyOnly", (), {"active_policy": lambda _self: {
        "policy_version_id": policy.get("id"), "policy": policy.get("policy") or {}}})()
    try:
        check_core_governance_rules(core)
    except LanePreconditionError as exc:
        return _row("lanes", "mission_sec_quarters.precondition", GAP, str(exc)[:400],
                    fix=SIGN_PLAN_FIX.format(state=env.state))
    return _row("lanes", "mission_sec_quarters.precondition", OK,
                f"{policy.get('id')} passes the SEC company-facts lane's own governance check")


# -- authorization ------------------------------------------------------------------


def check_authorization(env: Environment) -> list[dict[str, Any]]:
    from .coverage_mission import AUTOMATION_WRITE_SCOPES
    from .mission_source_discovery import SEC_SOURCE_REF

    section = "authorization"
    rows: list[dict[str, Any]] = []
    if env.mission is not None:
        autonomy = env.mission.get("autonomy") or {}
        may_write = set(autonomy.get("may_write") or [])
        missing = [scope for scope in AUTOMATION_WRITE_SCOPES if scope not in may_write]
        rows.append(_row(
            section, "mission.may_write", OK if not missing else GAP,
            (f"automation may write {len(may_write)} scopes"
             + (f"; missing {missing}" if missing else "")),
            fix="publish a mission version granting the missing scopes (owner decision)"))
        connected = {item.get("source_ref") for item in env.mission.get("source_plan") or []
                     if item.get("status") == "connected"}
        rows.append(_row(
            section, "mission.source_plan.sec", OK if SEC_SOURCE_REF in connected else WARN,
            f"{SEC_SOURCE_REF} is {'connected' if SEC_SOURCE_REF in connected else 'not connected'}"
            " -- the SEC company-facts, statement and annual lanes need it",
            fix="owner decision: connect source:sec-edgar in a new mission version"))
        unresolved = _unresolved_ciks(env)
        rows.append(_row(
            section, "mission.universe.cik", OK if not unresolved else GAP,
            ("every covered company has an SEC CIK" if not unresolved else
             f"no SEC CIK resolved for {unresolved}; the SEC, ownership and catalyst "
             "lanes skip these companies"),
            fix="the writer retries the resolution every 5 minutes "
                "(sec-company-resolution-status.json); check its 'pending' reasons"))
        rows.append(_company_ref_canonical_row(env))
    else:
        rows.append(_row(section, "mission", PENDING, "no mission to authorize lanes yet"))
    rows.append(_core_principal(env))
    return rows


def _company_ref_canonical_row(env: Environment) -> dict[str, Any]:
    """Mission companies whose ``company:sec-cik:`` CIK is not ten digits.

    Information, not a gap: the ref is the company's identity in append-only
    history (legacy DXC ``company:sec-cik:001688568``) and every table uses
    the same spelling; new refs are minted with ten digits and lookups that
    can meet both spellings compare by CIK (``same_company_ref``).
    """

    from .mission_company_cik import canonical_company_ref, is_canonical_company_ref

    odd = [str(item.get("company_ref")) for item in (env.mission or {}).get("universe") or []
           if isinstance(item, Mapping) and not is_canonical_company_ref(item.get("company_ref"))]
    if not odd:
        return _row("authorization", "mission.universe.company_ref_canonical", OK,
                    "every company:sec-cik: ref carries a ten-digit CIK")
    return _row("authorization", "mission.universe.company_ref_canonical", INFO,
                "non-canonical company ref(s) kept as recorded (append-only): "
                + ", ".join(f"{ref} (canonical {canonical_company_ref(ref)})" for ref in odd)
                + "; lookups match either spelling",
                noncanonical=odd)


def _unresolved_ciks(env: Environment) -> list[str]:
    from .mission_company_cik import company_cik

    connected = {item.get("source_ref") for item in (env.mission or {}).get("source_plan") or []
                 if item.get("status") == "connected"}
    if "source:sec-edgar" not in connected:
        return []
    return [str(item.get("ticker")) for item in (env.mission or {}).get("universe") or []
            if company_cik(item.get("company_ref"), state_dir=env.state) is None]


def _core_principal(env: Environment) -> dict[str, Any]:
    section = "authorization"
    value, error = _json(env.state / "writer-tokens.json")
    if error is not None or not isinstance(value, Mapping):
        return _row(section, "writer.core_principal", GAP,
                    f"writer-tokens.json is {error or 'not an object'}",
                    fix="re-run the environment's install/bootstrap")
    principals = {item.get("principal_id"): item for item in value.get("principals") or []
                  if isinstance(item, Mapping)}
    core = principals.get("core")
    if core is None:
        return _row(section, "writer.core_principal", GAP,
                    "writer-tokens.json has no core principal; every lane tick is refused",
                    fix="re-run the environment's install/bootstrap")
    from .lane_registry import lane_operations

    listed = set(core.get("operations") or [])
    unlisted = sorted(lane_operations() - listed)
    detail = ("core principal present, unrestricted" if core.get("unrestricted") is not False
              else "core principal present")
    if unlisted:
        detail += (f"; {len(unlisted)} lane operation(s) are not in its token list "
                   "(granted by code since 2487d28d: principal_may_call)")
    return _row(section, "writer.core_principal", OK, detail)


# -- mission / review consistency -----------------------------------------------------


# 2026-09-28: the carry's own predicate (``AWAITING_REVIEW_DISPOSITION_SQL``),
# not a second copy of it.  This check used to call an open review stranded
# only when no later version had a *review* of the document, while the carry
# refused it when a later version had *any row* of it: the check reported 164
# Guidepoint reviews stranded and said "deploy 70ef64ee", the carry -- already
# deployed -- moved none of them, and neither said why.
REVIEW_DISPOSITIONS_SQL = (
    "SELECT source_ref, review_version, active_version, carry_disposition, COUNT(*) AS n "
    "FROM (" + AWAITING_REVIEW_DISPOSITION_SQL + ") "
    "GROUP BY source_ref, review_version, active_version, carry_disposition "
    "ORDER BY source_ref, review_version, carry_disposition"
)
STRANDED_REVIEWS_SQL = (
    "SELECT source_ref, review_version, active_version, n FROM (" + REVIEW_DISPOSITIONS_SQL + ") "
    "WHERE carry_disposition='carryable' ORDER BY source_ref, review_version"
)


def check_mission_reviews(env: Environment) -> list[dict[str, Any]]:
    section = "mission"
    if env.core is None or env.mission is None:
        return [_row(section, "reviews", PENDING, "no mission published yet")]
    rows: list[dict[str, Any]] = []
    try:
        dispositions = [dict(row) for row in env.core.execute(REVIEW_DISPOSITIONS_SQL)]
        active_open = env.core.execute(
            "SELECT COUNT(*) FROM coverage_mission_document_reviews r "
            "JOIN coverage_mission_pointer p ON p.mission_version_id=r.mission_version_ref "
            "WHERE r.state='awaiting_human_extraction'").fetchone()[0]
    except sqlite3.Error as exc:
        return [_row(section, "reviews", WARN, f"cannot read reviews: {exc}")]
    stranded = [{key: item[key] for key in ("source_ref", "review_version", "active_version", "n")}
                for item in dispositions if item["carry_disposition"] == "carryable"]
    held_later: dict[str, int] = {}
    for item in dispositions:
        if item["carry_disposition"] != "carryable":
            held_later[item["carry_disposition"]] = (
                held_later.get(item["carry_disposition"], 0) + item["n"])
    total = sum(item["n"] for item in stranded)
    summary = ", ".join(f"{item['source_ref']} v{item['review_version']}->v{item['active_version']}:"
                        f" {item['n']}" for item in stranded)
    not_owed = ("; not carried by design: " + ", ".join(
        f"{name} {count}" for name, count in sorted(held_later.items()))) if held_later else ""
    rows.append(_row(
        section, "reviews.stranded_on_superseded_version", OK if not total else GAP,
        (f"{active_open} open review(s) under the active version; none stranded" + not_owed
         if not total else
         f"{total} open review(s) on superseded mission versions are owed a carry ({summary}); "
         f"{active_open} open under the active version"
         + (" -- extraction sees nothing to do and its child (and the claim-support "
            "recheck inside it) never starts" if not active_open else "")
         + not_owed),
        fix=("the document-extraction tick carries these every tick "
             "(carry_forward_awaiting_reviews); if the count does not fall, its tick "
             "summary's carried_awaiting.skipped names the refusal (a grant the active "
             "version no longer gives, or a document the active version already decided)"),
        stranded=stranded or None,
        dispositions=held_later or None))
    try:
        versions = env.core.execute(
            "SELECT COUNT(*) FROM coverage_mission_versions WHERE mission_ref=?",
            (env.mission["mission_ref"],)).fetchone()[0]
    except sqlite3.Error:
        versions = None
    rows.append(_row(section, "mission.version", INFO,
                     f"{env.mission.get('id')} (version {env.mission.get('version')} of {versions}); "
                     f"policy {(env.policy or {}).get('id')}"))
    return rows


# -- config files ----------------------------------------------------------------------


def _routing_policies(router_db: Path) -> set[str] | None:
    connection = _connect_ro(router_db)
    if connection is None:
        return None
    try:
        return {row[0] for row in connection.execute(
            "SELECT policy_version_ref FROM model_routing_policy_versions")}
    except sqlite3.Error:
        return None
    finally:
        connection.close()


def model_cooldown_rows(env: Environment, *, now: Any | None = None) -> list[dict[str, Any]]:
    """One info row: which models this environment's router is not offering.

    2026-10-07: deepseek-v4-flash cooled fourteen times in a row (HTTP 402,
    seen as INVALID_HOST_RESULT) and nothing an operator reads said so.  The
    same reader as the owner's to-do list (``needs_human.model_cooldowns``),
    so the two never disagree.  Info, not a gap: a cooldown is routing doing
    its job; the to-do list carries the decision.
    """

    from datetime import datetime, timezone

    from .needs_human import model_cooldowns

    service = env.service or {}
    router = Path(str(service.get("model_router_db") or env.state / "model-router.sqlite"))
    if not router.is_file():
        return [_row("config", "model_cooldowns", INFO, f"没有模型路由库（{router.name}）")]
    moment = now if now is not None else datetime.now(timezone.utc)
    active = model_cooldowns(router, now=moment)
    if not active:
        return [_row("config", "model_cooldowns", INFO, "没有模型在冷却")]
    parts = []
    for entry in active:
        blocked = "、".join(f"{block['purpose']}×{block['skips']}"
                           for block in entry.get("blocks") or []) or "无"
        parts.append(
            f"{entry['name']}：{entry['failure_code_label']}（{entry['reason_label']}），"
            f"自 {entry['failing_since']} 起，至 {entry['until']}，"
            f"第 {entry.get('cooldowns_in_run')} 轮；受影响环节 {blocked}")
    return [_row("config", "model_cooldowns", INFO,
                 f"{len(active)} 个模型在冷却：" + "；".join(parts),
                 cooldowns=[{key: entry.get(key) for key in (
                     "profile_id", "reason", "failure_code", "failing_since",
                     "started_at", "until", "streak")} for entry in active])]


def check_config(env: Environment) -> list[dict[str, Any]]:
    section = "config"
    rows: list[dict[str, Any]] = []
    service, error = _json(env.service_config)
    rows.append(_row(section, "service.json", OK if isinstance(service, dict) else GAP,
                     f"{env.service_config}: {'ok' if isinstance(service, dict) else error}"))
    if isinstance(service, dict):
        foreign = sorted({key for key in ("core_db", "scheduler_db", "model_router_db", "projection_db")
                          if service.get(key) and not _inside(service[key], env.state)})
        rows.append(_row(section, "service.json.paths", OK if not foreign else GAP,
                         "every database path is this environment's own" if not foreign else
                         f"{foreign} point outside {env.state}",
                         fix="re-render service.json for this environment"))
    routing_cache: dict[str, set[str] | None] = {}
    for path in sorted(env.state.glob("*model-config.json")):
        value, error = _json(path)
        if not isinstance(value, dict):
            rows.append(_row(section, path.name, GAP, error or "not an object",
                             fix="re-run workspace model setup"))
            continue
        problems = []
        if value.get("model_router_db"):
            router = Path(value["model_router_db"])
            if not _inside(router, env.state):
                problems.append(f"model_router_db is another environment's ({router})")
            ref = value.get("routing_policy_ref")
            if ref:
                known = routing_cache.setdefault(str(router), _routing_policies(router))
                if known is None:
                    problems.append(f"cannot read {router.name}")
                elif ref not in known:
                    problems.append(f"routing policy {ref} is not in {router.name}")
        if "credential_slot_refs" in value and not value.get("credential_slot_refs"):
            problems.append("no credential slots")
        rows.append(_row(section, path.name, OK if not problems else GAP,
                         "; ".join(problems) or "parses; routing policy present",
                         fix="scripts/align_model_routing.py --apply, or re-run workspace model setup"))
    rows.extend(_event_triage_rows(env))
    rows.extend(model_cooldown_rows(env))
    rows.extend(alphaengine_query_form_rows(env))
    rows.extend(sec_plan_spec_rows(env))
    if env.mission is not None:
        rows.extend(_mission_plan_rows(env))
        rows.extend(_policy_file_rows(env))
    return rows


def _event_triage_rows(env: Environment) -> list[dict[str, Any]]:
    """The screen in front of the event judge: where it routes, what it decided.

    ``event_triage`` rides the judge's configuration under its own purpose,
    which is the cheap tier.  A pinned policy with a cheap chain routes it
    there; one without routes it single-shot by the policy's own order, which
    still works but is the owner's to know.  The ledger half is a count, so a
    parity run shows both environments screening (or neither).
    """

    from .event_triage import AUDIT_SHARE, TRIAGE_PURPOSE, audit_report
    from .model_fallback_chain import purpose_tiers

    config_path = env.state / "event-judgement-model-config.json"
    value, _error = _json(config_path)
    if not isinstance(value, dict):
        return []
    rows: list[dict[str, Any]] = []
    tier = purpose_tiers().get(TRIAGE_PURPOSE)
    chain = None
    router = _connect_ro(Path(str(value.get("model_router_db") or "")))
    if router is not None:
        try:
            row = router.execute(
                "SELECT policy_json FROM model_routing_policy_versions "
                "WHERE policy_version_ref=?", (value.get("routing_policy_ref"),)).fetchone()
            if row is not None:
                from .model_router import declared_tier_chain

                policy = json.loads(row[0])
                override = (policy.get("purpose_overrides") or {}).get(TRIAGE_PURPOSE) or {}
                chain = tuple(override.get("chain") or ()) or declared_tier_chain(policy, tier)
        except (sqlite3.Error, ValueError, TypeError):
            chain = None
        finally:
            router.close()
    share = f"{AUDIT_SHARE * 100:.0f}%"
    rows.append(_row(
        "config", "event_triage.route", OK if chain else WARN,
        (f"{TRIAGE_PURPOSE} -> {tier}: {', '.join(chain)}; audit sample {share}"
         if chain else
         f"{TRIAGE_PURPOSE}: the pinned policy declares no {tier} chain; it routes "
         f"single-shot by the policy's order (audit sample {share})"),
        fix="在「管理员设置 → AI 服务 → 各档位的 AI 服务选择顺序」给事件研判配置低价档位（cheap）的服务"))
    if env.core is not None:
        try:
            report = audit_report(env.core)
        except sqlite3.Error:
            report = {"present": False}
        if report.get("present"):
            rows.append(_row(
                "config", "event_triage.ledger", WARN if report["audit_misses"] else INFO,
                f"{report['groups']} groups {report['by_outcome_route']}; audited and judged "
                f"{report['audited_groups_judged']}, missed {len(report['audit_misses'])}; "
                f"triage cost {report['triage_cost_micros'] / 1e6:.4f} USD",
                fix="审计样本里有被初筛放过、研判却不是 NO_CHANGE 的组：看 audit_misses，考虑调高抽样或收紧初筛",
                audit_misses=report["audit_misses"] or None))
    return rows


def _inside(path: str | Path, state: Path) -> bool:
    candidate = Path(str(path)).expanduser()
    for root in {state, state.resolve()}:
        for item in {candidate, candidate.resolve()}:
            try:
                item.relative_to(root)
                return True
            except ValueError:
                continue
    return False


def _mission_plan_rows(env: Environment) -> list[dict[str, Any]]:
    from .macos_launchagent import (
        ALPHAENGINE_PLAN_SELECTOR, SEC_PLAN_SELECTOR, WEB_PLAN_SELECTOR,
        _alphaengine_discovery_plan, _sec_discovery_plan, _web_discovery_plan,
    )
    from .mission_source_discovery import (
        ALPHAENGINE_SOURCE_REF, SEC_SOURCE_REF, WEB_SEARCH_SOURCE_REF, load_discovery_plan,
    )

    assert env.mission is not None
    connected = {item.get("source_ref") for item in env.mission.get("source_plan") or []
                 if item.get("status") == "connected"}
    universe = {item.get("company_ref") for item in env.mission.get("universe") or []}
    rows = []
    for source, selector, locate in (
            (SEC_SOURCE_REF, SEC_PLAN_SELECTOR, _sec_discovery_plan),
            (ALPHAENGINE_SOURCE_REF, ALPHAENGINE_PLAN_SELECTOR, _alphaengine_discovery_plan),
            (WEB_SEARCH_SOURCE_REF, WEB_PLAN_SELECTOR, _web_discovery_plan)):
        if source not in connected:
            continue
        name = f"discovery-plans/{selector}"
        try:
            plan = load_discovery_plan(locate(env.state))
        except Exception as exc:  # noqa: BLE001 - reported
            rows.append(_row("config", name, GAP, f"{source} is connected but its plan is "
                             f"unusable: {type(exc).__name__}: {str(exc)[:160]}",
                             fix="the first-mission publish writes it; for SEC, the ticker "
                                 "resolution retry does"))
            continue
        foreign = sorted(set(plan.get("companies") or {}) - universe)
        wrong_mission = plan.get("mission_ref") != env.mission.get("mission_ref")
        status = GAP if wrong_mission else (WARN if foreign else OK)
        rows.append(_row("config", name, status,
                         f"plan {plan.get('id')} for {plan.get('mission_ref')}"
                         + (f"; companies outside the universe: {foreign}" if foreign else ""),
                         fix="regenerate the plan from the active mission"))
    return rows


def alphaengine_query_form_rows(env: Environment) -> list[dict[str, Any]]:
    """Per AlphaEngine spec: are the forms the code's, and what does the owner govern?

    2026-10-06 (owner decision A): the query forms are rendered from
    ``alphaengine_query_forms`` in every environment; the approved plan file
    governs the budget, each spec's lookback and intervals, and the company
    names.  A plan file that still carries its own forms is ``info`` (they are
    ignored), not a warning; an environment whose plan cannot be rendered from
    the code forms is ``warn``.  The governed fields are printed so drift
    between environments is visible side by side.
    """

    from .alphaengine_query_forms import ALPHAENGINE_QUERY_FORMS_HASH, query_form_report
    from .macos_launchagent import _alphaengine_discovery_plan
    from .mission_source_discovery import load_discovery_plan

    prefix = "alphaengine.query_forms"
    try:
        path = _alphaengine_discovery_plan(env.state)
    except Exception as exc:  # noqa: BLE001 - reported
        return [_row("config", prefix, WARN,
                     f"the AlphaEngine plan selector does not resolve: {type(exc).__name__}: "
                     f"{str(exc)[:160]}", fix="the owner re-approves the selector")]
    if not path.is_file():
        return [_row("config", prefix, PENDING if env.mission is None else INFO,
                     f"no approved AlphaEngine plan ({path.name}); the first-mission publish "
                     f"writes one with governed fields only, rendered with code forms "
                     f"{ALPHAENGINE_QUERY_FORMS_HASH[:12]}")]
    try:
        report = query_form_report(load_discovery_plan(path))
    except Exception as exc:  # noqa: BLE001 - reported
        return [_row("config", prefix, WARN,
                     f"{path.name} cannot be rendered: {type(exc).__name__}: {str(exc)[:160]}",
                     fix="the owner publishes a valid plan")]
    head = (f"plan {report['plan_ref']} ({path.name}, schema {report['schema_version']}, "
            f"approved {report['approved_plan_hash'][:12]}, renders "
            f"{str(report.get('effective_plan_hash'))[:12]}); "
            f"max_calls_24h={report['budget'].get('max_calls_24h')} "
            f"search_floor={report['budget'].get('search_floor_calls')}")
    rows = []
    for spec in report["specs"]:
        governed = (f"lookback_days={spec.get('lookback_days')} "
                    f"rediscovery_interval_days={spec.get('rediscovery_interval_days')} "
                    f"retry_interval_days={spec.get('retry_interval_days')} "
                    f"(优先看这家: {spec.get('priority_rediscovery_interval_days')}/"
                    f"{spec.get('priority_retry_interval_days')}d)")
        if spec["rendered_from"] != "code":
            status = WARN
            why = report.get("reason") or "the code has no forms for this spec"
            forms = f"renders the plan file's own forms ({why}): {spec.get('plan_forms')}"
        elif spec.get("plan_forms") is None:
            status, forms = OK, "code forms; the plan file carries none"
        elif spec.get("plan_forms_differ"):
            status = INFO
            forms = (f"code forms; the plan file's forms differ and are ignored: "
                     f"{spec['plan_forms']} -> {spec['code_forms']}")
        else:
            status, forms = OK, "code forms; the plan file's forms are the same"
        rows.append(_row("config", f"{prefix}.{spec['spec_ref']}", status,
                         f"{forms}; {governed}; {head}",
                         fix="this plan predates code-owned forms: the owner re-publishes "
                             "it at schema 0.6/0.7 with company names"))
    if report.get("names_unresolved"):
        rows.append(_row("config", f"{prefix}.names", WARN,
                         f"companies named only by ticker, which no AlphaEngine title uses: "
                         f"{report['names_unresolved']}",
                         fix="the owner must publish these companies' names (plan companies "
                             "or the packaged name table)"))
    elif report.get("names_filled"):
        rows.append(_row("config", f"{prefix}.names", INFO,
                         f"ticker-only plan names rendered from the name table: "
                         f"{report['names_filled']}"))
    return rows


def sec_plan_spec_rows(env: Environment) -> list[dict[str, Any]]:
    """Per SEC spec: the code-owned spec set against the approved plan file.

    2026-10-08 (ADR-0015): the SEC discovery specs and their forms are
    rendered from ``sec_plan_specs`` in every environment, the same way
    AlphaEngine query forms are; the approved file governs the budget, the
    company list and each spec's intervals where it carries the spec.  A spec
    the code adds to a file that lacks it (legacy's 8-K) is ``info`` and says
    which intervals it runs with, so drift is visible side by side.
    """

    from .macos_launchagent import _sec_discovery_plan
    from .mission_source_discovery import load_discovery_plan
    from .sec_plan_specs import SEC_PLAN_SPECS_HASH, sec_plan_report

    prefix = "sec.plan_specs"
    try:
        path = _sec_discovery_plan(env.state)
    except Exception as exc:  # noqa: BLE001 - reported
        return [_row("config", prefix, WARN,
                     f"the SEC plan selector does not resolve: {type(exc).__name__}: {str(exc)[:160]}",
                     fix="the owner re-approves the selector")]
    if not path.is_file():
        return [_row("config", prefix, PENDING if env.mission is None else INFO,
                     f"no approved SEC plan ({path.name}); the first-mission publish writes "
                     f"the code's spec set {SEC_PLAN_SPECS_HASH[:12]}")]
    try:
        report = sec_plan_report(load_discovery_plan(path))
    except Exception as exc:  # noqa: BLE001 - reported
        return [_row("config", prefix, WARN,
                     f"{path.name} cannot be rendered: {type(exc).__name__}: {str(exc)[:160]}",
                     fix="the owner publishes a valid SEC plan")]
    head = (f"plan {report['plan_ref']} ({path.name}, approved {report['approved_plan_hash'][:12]}, "
            f"renders {report['effective_plan_hash'][:12]}, code specs {report['specs_hash'][:12]}); "
            f"max_calls_24h={report['budget'].get('max_calls_24h')}")
    rows = []
    for spec in report["specs"]:
        items = f" items={','.join(spec['item_filter'])}" if spec.get("item_filter") else ""
        governed = (f"form={spec['form']}{items} lookback_days={spec['lookback_days']} "
                    f"rediscovery_interval_days={spec['rediscovery_interval_days']} "
                    f"retry_interval_days={spec['retry_interval_days']} "
                    f"(intervals from {spec['intervals_from']})")
        drift = spec["drift"]
        if drift == "none":
            status, what = OK, "in the plan file and the code"
        elif drift == "added_by_code":
            status, what = INFO, "added by code (the plan file does not carry it)"
        elif drift == "form_from_code":
            status, what = INFO, f"form from code (the plan file says {spec['plan_form']})"
        else:
            status, what = INFO, "plan file only (the code does not declare it); runs as the file says"
        rows.append(_row("config", f"{prefix}.{spec['spec_ref']}", status,
                         f"{what}; {governed}; {head}"))
    return rows


def _policy_file_rows(env: Environment) -> list[dict[str, Any]]:
    rows = []
    constitution = env.constitution or {}
    method = constitution.get("method") or {}
    try:
        from .company_dossier import causal_chain_hash, load_policy as load_dossier
        chain_hash = causal_chain_hash(list(method.get("causal_chain") or []))
    except Exception:  # noqa: BLE001
        chain_hash = None
        load_dossier = None
    for name, loader_name, key in (
            ("p12a-dossier-policy-v1.json", "company_dossier", "causal_chain_maps"),
            ("p12e-industry-framework-policy-v1.json", "industry_framework", "causal_chain_titles")):
        path = env.state / name
        try:
            module = __import__(f"dalton_core.{loader_name}", fromlist=["load_policy"])
            policy = module.load_policy(path)
        except Exception as exc:  # noqa: BLE001 - reported
            rows.append(_row("config", name, GAP, f"{type(exc).__name__}: {str(exc)[:160]}",
                             fix="the first-mission publish writes it"))
            continue
        maps = policy.get(key) or []
        bound = any(item.get("constitution_ref") == constitution.get("constitution_ref")
                    and item.get("causal_chain_hash") == chain_hash for item in maps)
        rows.append(_row("config", name, OK if bound else GAP,
                         f"{len(maps)} causal-chain map(s); "
                         + ("one binds the active constitution's chain" if bound else
                            f"none binds {constitution.get('constitution_ref')} "
                            "with the active causal-chain hash"),
                         fix="regenerate it from the active constitution"))
    for name, check in (("tracking-policy.json", "tracking_cadence"),):
        try:
            module = __import__(f"dalton_core.{check}", fromlist=["load_policy"])
            module.load_policy(env.state / name)
            rows.append(_row("config", name, OK, "parses and validates"))
        except Exception as exc:  # noqa: BLE001 - reported
            rows.append(_row("config", name, GAP, f"{type(exc).__name__}: {str(exc)[:160]}",
                             fix="workspace runtime setup writes it"))
    return rows


# -- host ---------------------------------------------------------------------------------


def check_host(env: Environment, *, source_state_dir: str | Path | None = None,
               manager_config_path: str | Path | None = None) -> list[dict[str, Any]]:
    from .workspace_lane_parity import audit_lanes

    try:
        report = audit_lanes(env.state, plist_path=env.plist_path,
                             manager_config_path=manager_config_path,
                             source_state_dir=source_state_dir)
    except Exception as exc:  # noqa: BLE001 - reported, the rest of the report stands
        return [_row("host", "lane_inputs", WARN, f"audit failed: {type(exc).__name__}: {exc}")]
    rows = []
    for item in report.get("host_scheme") or []:
        status = {"ok": OK, "differs": GAP, "unknown": INFO}.get(item.get("status"), WARN)
        rows.append(_row("host", item["key"], status,
                         "; ".join([item.get("detail") or "", *item.get("blockers", [])[:4]]).strip("; ")))
    for lane in report.get("lanes") or []:
        if lane["configured"] and not lane["blockers"]:
            status = OK
        elif lane["configured"]:
            status = WARN
        else:
            status = PENDING if env.mission is None else WARN
        rows.append(_row("host", f"inputs.{lane['key']}", status,
                         "; ".join(lane["blockers"]) or "inputs present",
                         fix="python scripts/repair_workspace_lane_parity.py --state-dir "
                             f"{env.state} (dry run first)"))
    return rows


def check_owner_levers(env: Environment) -> list[dict[str, Any]]:
    """What the owner has paused or stopped here, from the receipts (read-only).

    2026-10-06 (vision-gap #11).  An emergency stop is a ``warn`` -- research
    is deliberately not running and whoever reads this report should know
    before they go looking for a fault; a paused company or lane is ``info``
    with the owner's own reason and expiry.
    """

    from .owner_levers import lever_state_for_state_dir, paused_sentence

    state = lever_state_for_state_dir(env.state)
    rows: list[dict[str, Any]] = []
    emergency = state.get("emergency")
    if emergency:
        rows.append(_row("owner_levers", "emergency_stop", WARN,
                         f"{paused_sentence(emergency)} by {emergency.get('actor_ref')} "
                         f"at {emergency.get('created_at')} ({emergency.get('id')})",
                         fix="在「管理员设置 → 运行控制 → 暂停所有研究（紧急停机）」点「恢复全部运行」"))
    else:
        rows.append(_row("owner_levers", "emergency_stop", OK, "not set"))
    for kind in ("companies", "lanes"):
        for ref, receipt in sorted((state.get(kind) or {}).items()):
            rows.append(_row("owner_levers", f"paused_{kind[:-1] if kind == 'lanes' else 'company'}",
                             INFO, f"{ref}: {paused_sentence(receipt)} ({receipt.get('id')})"))
    for receipt in state.get("expired") or ():
        rows.append(_row("owner_levers", "auto_resumed", INFO,
                         f"{receipt.get('scope_ref')}: pause expired at {receipt.get('until')}"))
    rows.append(_row("owner_levers", "receipts", INFO,
                     f"{state.get('receipt_count', 0)} owner lever receipt(s)"))
    return rows


def check_verifier_independence(env: Environment, *, now: Any = None) -> list[dict[str, Any]]:
    """Which verified draft stages cannot be independently verified right now.

    2026-10-08.  One ``warn`` naming every (draft -> verifier) pair whose
    effective chains are in conflict (or only independent once a cooldown
    ends) in this environment, or one ``info`` when there is none.  Read-only
    (``verifier_independence.environment_report``); the fix is the cockpit
    card that reorders the draft stage.
    """

    from datetime import datetime, timezone

    from .verifier_independence import conflict_rows, environment_report, status_label

    moment = now or datetime.now(timezone.utc)
    report = environment_report(env.state, now=moment)
    if not report.get("available"):
        return [_row("verifier_independence", "pairs", WARN,
                     str(report.get("reason") or "unreadable"))]
    bound = [row for row in report["pairs"] if row.get("status") != "unbound"]
    conflicts = conflict_rows(report)
    if not conflicts:
        return [_row("verifier_independence", "pairs", INFO,
                     f"{len(bound)} bound verified pair(s), none in conflict")]
    detail = "; ".join(
        f"{row['draft_purpose']} -> {row['verifier_purpose']}: "
        f"{status_label(row.get('evaluation') or {'status': row['status']})}"
        + (f" (refused {row['refusals_48h'] + row['refusals_unrecorded_48h']} in 48 h)"
           if row["refusals_48h"] or row["refusals_unrecorded_48h"] else "")
        for row in conflicts)
    return [_row("verifier_independence", "pairs", WARN,
                 f"{len(conflicts)} of {len(bound)} bound verified pair(s) in conflict: {detail}",
                 fix="在「管理员设置 → AI 服务 → 核验独立性 · 起草服务顺序」填入推荐顺序后保存")]


# -- the report -----------------------------------------------------------------------------


def check_environment(env: Environment, *, source_state_dir: str | Path | None = None,
                      manager_config_path: str | Path | None = None,
                      include_host: bool = True) -> dict[str, Any]:
    from .workspace_lifecycle import state_for_core, archive_integrity, retained_integrity
    lifecycle = state_for_core(env.state)
    if lifecycle != "active":
        integrity = archive_integrity(env.state.parent.parent.parent.parent, env.state.parent.parent.name) if lifecycle == "archived" else retained_integrity(env.state) if lifecycle == "paused" else {"status": "deleted"}
        return {"schema_version": SCHEMA_VERSION, "environment": env.name, "state_dir": str(env.state),
                "service_config": str(env.service_config), "lifecycle": lifecycle, "mission_ref": None,
                "mission_version": None, "active_policy": None, "counts": {INFO if integrity["status"] != "invalid" else GAP: 1},
                "rows": [_row("lifecycle", "integrity", INFO if integrity["status"] != "invalid" else GAP,
                              f"{lifecycle}: {integrity['status']}")] }
    rows: list[dict[str, Any]] = []
    for step in (check_governance, check_authorization, check_mission_reviews, check_lanes,
                 check_config, check_owner_levers, check_verifier_independence):
        try:
            rows.extend(step(env))
        except Exception as exc:  # noqa: BLE001 - one broken section must not hide the rest
            rows.append(_row(step.__name__.removeprefix("check_"), "section", WARN,
                             f"check failed: {type(exc).__name__}: {str(exc)[:200]}"))
    if include_host:
        rows.extend(check_host(env, source_state_dir=source_state_dir,
                               manager_config_path=manager_config_path))
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {
        "schema_version": SCHEMA_VERSION,
        "environment": env.name,
        "state_dir": str(env.state),
        "service_config": str(env.service_config),
        "mission_ref": (env.mission or {}).get("mission_ref"),
        "mission_version": (env.mission or {}).get("id"),
        "active_policy": (env.policy or {}).get("id"),
        "counts": counts,
        "rows": rows,
    }


_STATUS_LABEL = {OK: "ok", GAP: "GAP", WARN: "warn", DRIFT: "drift", PENDING: "pending",
                 INFO: "info"}


def render(reports: Sequence[Mapping[str, Any]], *, verbose: bool = False) -> str:
    lines: list[str] = []
    for report in reports:
        counts = report["counts"]
        lines.append(f"== {report['environment']}  ({report['state_dir']})")
        lines.append(f"   mission {report['mission_version'] or '（未发布）'}  policy "
                     f"{report['active_policy']}  "
                     + "  ".join(f"{key}={counts[key]}" for key in sorted(counts)))
        section = None
        for row in report["rows"]:
            if not verbose and row["status"] in {OK, INFO}:
                continue
            if row["section"] != section:
                section = row["section"]
                lines.append(f"  [{section}]")
            lines.append(f"    {_STATUS_LABEL[row['status']]:<7} {row['check']}: {row['detail']}")
            if row.get("fix"):
                lines.append(f"            fix: {row['fix']}")
        lines.append("")
    return "\n".join(lines)


__all__ = [
    "Environment", "REVIEW_DISPOSITIONS_SQL", "SCHEMA_VERSION", "STRANDED_REVIEWS_SQL", "check_authorization",
    "check_config", "check_environment", "check_governance", "check_host", "check_lanes",
    "check_mission_reviews", "render",
]
