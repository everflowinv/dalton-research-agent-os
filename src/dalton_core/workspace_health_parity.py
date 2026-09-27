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
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "dalton-workspace-health-parity-0.1"

OK, GAP, WARN, DRIFT, PENDING, INFO = "ok", "gap", "warn", "drift", "pending", "info"

#: Scripts an owner runs to close a gap.  Relative to the repository root.
SIGN_RULES_FIX = ("python scripts/sign_auto_commit_rules.py --state-dir {state} "
                  "[--rehearse /tmp/x] --apply --actor human:owner")
SIGN_PLAN_FIX = ("python scripts/sign_research_plan_auto_start.py --state-dir {state} "
                 "--apply --actor human:owner")
BUDGET_FIX = ("cockpit 预算设置保存一次（writer 操作 set_research_budget_authority_chain），"
              "它会把 closed research_budget 写进 policy/mandate/constitution/mission")

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
                 "recorded", "queued", "settled", "entered", "ok", "committed"}
_UNCONFIGURED_CODES = {"lane_unconfigured_hold"}


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
        self.core = _connect_ro(self.state / "core.sqlite")
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
            status = PENDING
            item = {**item, "fix": "首个研究任务发布时由创建流程签入（workspace_mission_setup."
                                   "ensure_first_mission_auto_commit_policy）"}
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
                         fix="在 cockpit 里确认首个研究任务"))
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
            "tick_started_at": tick["started_at"],
        }
    return result


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
        if installed is not None and configured and spec.argv_fragment is not None:
            flags = {token for token in spec.argv_fragment(context) if token.startswith("--")}
            if flags - installed:
                status = GAP
                problems.append("the installed writer plist lacks " + ", ".join(sorted(flags - installed))
                                + " -- re-render the writer (deploy) to pick it up")
        missing = _rules_missing(env, key) if env.mission is not None else []
        if missing:
            status = GAP
            problems.append("active policy does not list " + ", ".join(missing))
        if tick is not None:
            word = str(tick["word"])
            reason = tick["reason"]
            if tick["reason_code"] in _UNCONFIGURED_CODES or word == "unconfigured":
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
        elif spec.driver_key is not None and ticks:
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
    return rows


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
    else:
        rows.append(_row(section, "mission", PENDING, "no mission to authorize lanes yet"))
    rows.append(_core_principal(env))
    return rows


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


STRANDED_REVIEWS_SQL = (
    "SELECT r.source_ref AS source_ref, v.version_number AS review_version, "
    "p.version_number AS active_version, COUNT(*) AS n "
    "FROM coverage_mission_document_reviews r "
    "JOIN coverage_mission_versions v ON v.mission_version_id=r.mission_version_ref "
    "JOIN coverage_mission_pointer p ON p.mission_ref=v.mission_ref "
    "WHERE r.state='awaiting_human_extraction' AND r.mission_version_ref<>p.mission_version_id "
    "AND NOT EXISTS (SELECT 1 FROM coverage_mission_document_reviews n "
    "JOIN coverage_mission_versions nv ON nv.mission_version_id=n.mission_version_ref "
    "WHERE nv.mission_ref=v.mission_ref AND nv.version_number>v.version_number "
    "AND n.document_ref=r.document_ref) "
    "GROUP BY r.source_ref, v.version_number, p.version_number "
    "ORDER BY r.source_ref, v.version_number"
)


def check_mission_reviews(env: Environment) -> list[dict[str, Any]]:
    section = "mission"
    if env.core is None or env.mission is None:
        return [_row(section, "reviews", PENDING, "no mission published yet")]
    rows: list[dict[str, Any]] = []
    try:
        stranded = [dict(row) for row in env.core.execute(STRANDED_REVIEWS_SQL)]
        active_open = env.core.execute(
            "SELECT COUNT(*) FROM coverage_mission_document_reviews r "
            "JOIN coverage_mission_pointer p ON p.mission_version_id=r.mission_version_ref "
            "WHERE r.state='awaiting_human_extraction'").fetchone()[0]
    except sqlite3.Error as exc:
        return [_row(section, "reviews", WARN, f"cannot read reviews: {exc}")]
    total = sum(item["n"] for item in stranded)
    summary = ", ".join(f"{item['source_ref']} v{item['review_version']}->v{item['active_version']}:"
                        f" {item['n']}" for item in stranded)
    rows.append(_row(
        section, "reviews.stranded_on_superseded_version", OK if not total else GAP,
        (f"{active_open} open review(s) under the active version; none stranded"
         if not total else
         f"{total} open review(s) sit on superseded mission versions ({summary}); "
         f"{active_open} open under the active version"
         + (" -- extraction sees nothing to do and its child (and the claim-support "
            "recheck inside it) never starts" if not active_open else "")),
        fix=("deploy the release with CoverageMissionAuthority.carry_open_reviews_forward: "
             "the document-extraction tick carries them into the active version by itself"),
        stranded=stranded or None))
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
    if env.mission is not None:
        rows.extend(_mission_plan_rows(env))
        rows.extend(_policy_file_rows(env))
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


# -- the report -----------------------------------------------------------------------------


def check_environment(env: Environment, *, source_state_dir: str | Path | None = None,
                      manager_config_path: str | Path | None = None,
                      include_host: bool = True) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for step in (check_governance, check_authorization, check_mission_reviews, check_lanes,
                 check_config):
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
    "Environment", "SCHEMA_VERSION", "STRANDED_REVIEWS_SQL", "check_authorization",
    "check_config", "check_environment", "check_governance", "check_host", "check_lanes",
    "check_mission_reviews", "render",
]
