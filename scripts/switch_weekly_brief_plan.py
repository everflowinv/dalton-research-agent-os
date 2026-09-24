#!/usr/bin/env python3
"""Switch an environment's weekly brief from one schedule plan to the next.

Plan 0.2 (``weekly-brief-plan:us-it-services:v4``, commit 02b5990b) rebuilds
the evidence pack from the Ledger before each issue.  Two records decide which
plan the controller may run, and both have to move:

1. **Governance.**  ``run_weekly_brief_cycle`` refuses any plan whose exact
   ``{plan_ref, plan_hash}`` is not in the *active* policy's
   ``weekly_brief_auto_publish.allowed_plan_bindings``.  This script publishes
   the next policy version with the new binding *appended* -- the old binding
   stays, so the still-running controller keeps working on the old plan until
   it is restarted -- and cascades: the research constitution is republished
   binding that policy (and, as a record, ``bindings.weekly_brief_plan`` =
   the new plan), and the coverage mission is republished binding that
   constitution.  A mission whose constitution binds a stale policy cannot
   spend, so the cascade is not optional.  Nothing else in any record moves;
   the mandate is rebound unchanged.  No INSERT is hand-written: rehearsal goes
   through ``DaltonStore`` and the authorities, ``--apply`` through the
   environment's writer as its ephemeral human principal (``--actor``).

2. **Service config.**  ``weekly_brief.config.plan`` in ``service.json`` is
   replaced by the new plan file's content.  The write is atomic (temporary
   file in the same directory, fsync, ``os.replace``), keeps the file's mode
   and every other field byte-for-byte in value, and the previous file is kept
   next to it as ``<name>.pre-weekly-brief-<plan>-<UTC timestamp>.json``.  It
   refuses if the file changed between reading and replacing it.

Governance is published first, so there is no moment at which the configured
plan is unauthorized.  Both steps are idempotent: a second run reports
``already-switched``; a run interrupted between the two steps finishes the
missing one only.

Restart or reload?
------------------

* The **controller** (``dalton_core.service``, launchd label
  ``space.lumos.dalton.controller`` on the legacy install) parses
  ``service.json`` exactly once, in ``main`` -> ``ServiceConfig.from_file``,
  and builds ``WeeklyBriefCoordinator`` with that plan.  There is no file
  watch and no SIGHUP handler (only SIGTERM/SIGINT, which stop it).  It
  **must be restarted** to pick up the new plan.  This script never restarts
  anything; the summary carries the exact ``launchctl kickstart -k`` command.
* The **writer** does **not** read ``service.json``.  The controller sends the
  plan with every ``run_weekly_brief_cycle`` call, and the writer reads the
  active policy from the Core on each call, so the new policy takes effect
  without a writer restart.  The writer must, however, run a release that
  understands plan schema 0.2 -- so must the controller: an older release
  rejects the 0.2 plan while parsing ``service.json`` and the controller would
  crash-loop under ``KeepAlive``.  ``--apply`` therefore checks both launchd
  jobs' interpreters can import the 0.2 code and refuses otherwise
  (``--skip-runtime-check`` overrides; do not use it on a live install).

already_issued
--------------

``max_issues_per_week`` is per brief, not per plan.  If the new plan's latest
due slot was already issued by *any* plan (e.g. v3 issued W39 at 11:00Z and
v4 is switched in at 11:30Z with an earlier ``effective_from``), the writer
returns ``{"status": "already_issued", ...}`` without admitting, publishing or
enqueuing anything, and the controller records it as its weekly-brief state.
That is the correct, terminal outcome for that slot -- not an error, and never
retried into a second issue.  This script predicts it before switching
(``first_cycle``), rehearses the real cycle on the copy (``cycle_probe``), and
``--verify`` accepts ``already_issued`` / ``waiting`` / ``ready`` from the
controller heartbeat as healthy.

Usage
-----

    PY=.venv/bin/python
    S="$HOME/Library/Application Support/Dalton/state/dalton-core"
    C="$HOME/Library/Application Support/Dalton/config/service.json"

    # read-only: before/after, the cascade, the first v4 cycle, runtime check
    $PY scripts/switch_weekly_brief_plan.py --state-dir "$S" --service-config "$C"

    # the whole switch on copies of the Core and service.json
    $PY scripts/switch_weekly_brief_plan.py --state-dir "$S" --service-config "$C" \
        --rehearse /tmp/weekly-brief-v4-rehearsal --actor human:lumos

    # for real: policy cascade through the writer, then service.json
    $PY scripts/switch_weekly_brief_plan.py --state-dir "$S" --service-config "$C" \
        --apply --actor human:lumos
    launchctl kickstart -k gui/$(id -u)/space.lumos.dalton.controller

    # afterwards, read-only
    $PY scripts/switch_weekly_brief_plan.py --state-dir "$S" --service-config "$C" --verify
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from dalton_core.weekly_brief_coordinator import (  # noqa: E402
    _utc,
    WeeklyBriefCoordinatorConfig,
    WeeklyBriefCoordinatorError,
    WeeklyBriefSchedulePlan,
)
from scripts.publish_extraction_authority_chain import BODY_FIELDS, _ref_hash  # noqa: E402
from scripts.sign_auto_commit_rules import (  # noqa: E402
    PlanError,
    _next_version_id,
    read_current,
)

DEFAULT_PLAN = ROOT / "deploy/phase1/weekly-brief-schedule-us-it-services-v4.json"
#: What governance is asked to bind when the default plan file is used; a
#: plan file edited after review is refused instead of silently signed.
DEFAULT_PLAN_HASH = "c4f46d171738075711db4149c45efa644e8590ba6f97257dc7391eff68e889ec"
LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
DEFAULT_CONTROLLER_PLIST = LAUNCH_AGENTS / "space.lumos.dalton.controller.plist"
DEFAULT_WRITER_PLIST = LAUNCH_AGENTS / "space.lumos.dalton.writer.plist"
#: Cycle outcomes of a healthy controller after the switch.
HEALTHY_CYCLE_STATES = ("waiting", "already_issued", "ready", "running", "pending")
_RUNTIME_PROBE = (
    "import sys\n"
    "from dalton_core.weekly_brief_coordinator import WeeklyBriefSchedulePlan\n"
    "import json\n"
    "plan = WeeklyBriefSchedulePlan.from_mapping(json.loads(sys.argv[1]))\n"
    "print(plan.content_hash)\n"
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _instant(raw: str) -> datetime:
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise PlanError("--as-of must include a timezone")
    return value


# --------------------------------------------------------------------------
# inputs


def load_plan(path: Path, *, expect_hash: str | None) -> tuple[dict[str, Any], WeeklyBriefSchedulePlan]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        plan = WeeklyBriefSchedulePlan.from_mapping(raw)
    except (OSError, ValueError, WeeklyBriefCoordinatorError) as exc:
        raise PlanError(f"cannot read the plan at {path}: {exc}") from exc
    if expect_hash is not None and plan.content_hash != expect_hash:
        raise PlanError(
            f"{path} hashes to {plan.content_hash}, not the reviewed {expect_hash}")
    # The service config carries the plan's canonical wire, so what the
    # controller sends is exactly what governance binds.
    return plan.to_dict(), plan


def read_service_config(path: Path) -> dict[str, Any]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise PlanError(f"cannot read service config {path}: {exc}") from exc
    try:
        mapping = json.loads(data)
    except ValueError as exc:
        raise PlanError(f"service config {path} is not JSON: {exc}") from exc
    weekly = mapping.get("weekly_brief") if isinstance(mapping, Mapping) else None
    if not isinstance(weekly, Mapping) or not isinstance(weekly.get("config"), Mapping):
        raise PlanError(f"service config {path} has no weekly_brief.config")
    try:
        configured = WeeklyBriefSchedulePlan.from_mapping(weekly["config"]["plan"])
    except (KeyError, TypeError, WeeklyBriefCoordinatorError) as exc:
        raise PlanError(f"weekly_brief.config.plan in {path} is invalid: {exc}") from exc
    return {"path": path, "sha256": hashlib.sha256(data).hexdigest(),
            "mapping": mapping, "plan": configured,
            "mode": stat.S_IMODE(path.stat().st_mode)}


def check_pairing(config: dict[str, Any], state_dir: Path) -> None:
    """The service config must drive *this* Core's writer, not another one."""

    weekly = config["mapping"]["weekly_brief"]["config"]
    for key in ("writer_socket", "token_config"):
        value = weekly.get(key)
        if not isinstance(value, str) or Path(value).resolve().parent not in {
                state_dir, state_dir / "run"}:
            raise PlanError(
                f"weekly_brief.config.{key}={value!r} does not belong to {state_dir}; "
                "the service config and --state-dir must be the same environment")


def switched_config(mapping: Mapping[str, Any], plan_wire: Mapping[str, Any]) -> dict[str, Any]:
    new = json.loads(json.dumps(mapping))
    new["weekly_brief"]["config"]["plan"] = json.loads(json.dumps(plan_wire))
    # Exactly one path may differ; anything else is a bug in this script.
    before = json.loads(json.dumps(mapping))
    before["weekly_brief"]["config"].pop("plan")
    after = json.loads(json.dumps(new))
    after["weekly_brief"]["config"].pop("plan")
    if before != after:  # pragma: no cover - defensive
        raise PlanError("refusing a service config rewrite that touches other fields")
    return new


def validate_service_config(old: Mapping[str, Any], new: Mapping[str, Any]) -> dict[str, Any]:
    """Would the controller of *this* build start on the new file?"""

    from dalton_core.service import ServiceConfig

    WeeklyBriefCoordinatorConfig.from_mapping(new["weekly_brief"]["config"])
    try:
        ServiceConfig.from_mapping(old)
    except Exception as exc:  # the original is not a full config (fixtures)
        return {"full_config_checked": False, "reason": f"{type(exc).__name__}: {exc}"}
    try:
        ServiceConfig.from_mapping(new)
    except Exception as exc:
        raise PlanError(f"the switched service config would not load: {exc}") from exc
    return {"full_config_checked": True}


def serialize_config(mapping: Mapping[str, Any]) -> bytes:
    # The format every service.json in this repo is written in.
    return (json.dumps(mapping, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def write_service_config(config: dict[str, Any], new: Mapping[str, Any], *,
                         plan_ref: str, now: datetime) -> dict[str, Any]:
    """Back up, then atomically replace; the file's mode is carried over."""

    path: Path = config["path"]
    suffix = plan_ref.rsplit(":", 1)[-1]
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.stem}.pre-weekly-brief-{suffix}-{stamp}{path.suffix}")
    if backup.exists():
        raise PlanError(f"backup {backup} already exists")
    shutil.copy2(path, backup)
    os.chmod(backup, config["mode"])
    body = serialize_config(new)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                          dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, config["mode"])
        current = hashlib.sha256(path.read_bytes()).hexdigest()
        if current != config["sha256"]:
            raise PlanError(f"{path} changed while this script ran; nothing replaced")
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return {"path": str(path), "backup": str(backup),
            "sha256_before": config["sha256"],
            "sha256_after": hashlib.sha256(body).hexdigest(),
            "mode": oct(config["mode"])}


# --------------------------------------------------------------------------
# governance


def _policy_body(current: Mapping[str, Any]) -> dict[str, Any]:
    wire = current["policy"]
    return dict(wire["policy"] if isinstance(wire.get("policy"), Mapping) else wire)


def _rule(current: Mapping[str, Any]) -> dict[str, Any]:
    rule = _policy_body(current).get("weekly_brief_auto_publish")
    if not isinstance(rule, Mapping) or not isinstance(rule.get("allowed_plan_bindings"), list):
        # Enabling scheduled publication at all is a different decision from
        # switching plans; this script only does the latter.
        raise PlanError(
            f"active policy {current['policy_id']} has no weekly_brief_auto_publish rule "
            "to extend")
    return json.loads(json.dumps(rule))


def governance_state(current: Mapping[str, Any], plan: WeeklyBriefSchedulePlan) -> dict[str, Any]:
    binding = {"plan_ref": plan.plan_ref, "plan_hash": plan.content_hash}
    rule = _rule(current)
    constitution = current["constitution"]
    policy_binding = constitution["bindings"]["governance_policy_version"]
    return {
        "policy_binds_plan": binding in rule["allowed_plan_bindings"],
        "constitution_binds_plan": constitution["bindings"].get("weekly_brief_plan") == {
            "ref": plan.plan_ref, "hash": plan.content_hash},
        "constitution_binds_active_policy": policy_binding["ref"] == current["policy_id"],
        "mission_binds_constitution": (
            current["mission"]["bindings"]["constitution_version"]["ref"] == constitution["id"]),
    }


def build_switch_chain(current: Mapping[str, Any], plan: WeeklyBriefSchedulePlan, *,
                       now: str) -> dict[str, Any]:
    """Policy (only if it lacks the binding), constitution and mission."""

    state = governance_state(current, plan)
    binding = {"plan_ref": plan.plan_ref, "plan_hash": plan.content_hash}
    rule_before = _rule(current)
    stamp = plan.content_hash[:12]
    chain: dict[str, Any] = {"rule_before": rule_before, "rule_after": rule_before,
                             "policy": None}
    if not state["policy_binds_plan"]:
        rule_after = dict(rule_before)
        rule_after["allowed_plan_bindings"] = [*rule_before["allowed_plan_bindings"], binding]
        body = _policy_body(current)
        body["weekly_brief_auto_publish"] = rule_after
        version = current["policy_version_number"] + 1
        chain["rule_after"] = rule_after
        chain["policy"] = {
            "policy": body,
            "policy_version_id": _next_version_id(current["policy_id"], version),
            "version_number": version,
            "activate": True,
            "policy_ref": current["policy_ref"],
            "effective_from": now,
            "effective_until": None,
            "prior_version_ref": current["policy_id"],
            "change_reason": (
                f"authorize weekly brief schedule plan {plan.plan_ref} "
                f"(hash {plan.content_hash}) in weekly_brief_auto_publish."
                "allowed_plan_bindings so the controller may switch to it; the prior "
                "plan binding stays until the controller runs the new plan, "
                "max_issues_per_week and every other rule are unchanged, and the "
                "constitution and mission are rebound to this policy version only"),
            "content_hash_value": None,
        }
    constitution = current["constitution"]
    mission = current["mission"]
    constitution_version = int(constitution["version"]) + 1
    mission_version = int(mission["version"]) + 1
    bindings = json.loads(json.dumps(constitution["bindings"]))
    bindings["weekly_brief_plan"] = {"ref": plan.plan_ref, "hash": plan.content_hash}
    chain["constitution"] = {
        "constitution_ref": constitution["constitution_ref"],
        "industry_ref": constitution["industry_ref"],
        "title": constitution["title"],
        "bindings": bindings,
        "method": json.loads(json.dumps(constitution["method"])),
        "version_id": _next_version_id(constitution["id"], constitution_version),
        "prior_version_ref": constitution["id"],
        "idempotency_key": (
            f"{constitution['constitution_ref']}:{constitution_version}"
            f":weekly-brief-plan:{stamp}"),
    }
    chain["mission"] = {
        "mission_ref": mission["mission_ref"],
        **{field: json.loads(json.dumps(mission[field])) for field in BODY_FIELDS},
        "version_id": _next_version_id(mission["id"], mission_version),
        "prior_version_ref": mission["id"],
        "idempotency_key": (
            f"{mission['mission_ref']}:{mission_version}:weekly-brief-plan:{stamp}"),
    }
    chain["active_policy"] = {"ref": current["policy_id"],
                              "hash": current.get("policy_hash")}
    chain["needed"] = not all(state.values())
    return chain


def apply_switch_chain(chain: Mapping[str, Any],
                       apply: Callable[[str, dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
    """``publish_extraction_authority_chain.apply_chain``, with the policy optional.

    A run interrupted after the policy publish must still be able to finish
    the constitution and mission, so a policy that already carries the binding
    is rebound as-is instead of being republished.
    """

    out: dict[str, Any] = {}
    if chain["policy"] is not None:
        policy = apply("create_policy", chain["policy"])
        policy_ref, policy_hash = _ref_hash(policy, chain["policy"]["policy_version_id"])
        out["policy"] = {"ref": policy_ref, "hash": policy_hash}
    else:
        policy_ref, policy_hash = chain["active_policy"]["ref"], chain["active_policy"]["hash"]
        out["policy"] = {"ref": policy_ref, "hash": policy_hash, "status": "unchanged"}
    mandate = chain["mission"]["bindings"]["mandate_version"]
    out["mandate"] = {"ref": mandate["ref"], "hash": mandate["hash"], "status": "unchanged"}
    constitution_params = json.loads(json.dumps(chain["constitution"]))
    constitution_params["bindings"]["governance_policy_version"] = {
        "ref": policy_ref, "hash": policy_hash}
    constitution_params["bindings"]["mandate_version"] = dict(mandate)
    constitution = apply("publish_research_constitution", constitution_params)
    constitution_ref, constitution_hash = _ref_hash(constitution, constitution_params["version_id"])
    out["constitution"] = {"ref": constitution_ref, "hash": constitution_hash}
    mission_params = json.loads(json.dumps(chain["mission"]))
    mission_params["bindings"]["constitution_version"] = {
        "ref": constitution_ref, "hash": constitution_hash}
    mission = apply("create_coverage_mission", mission_params)
    mission_ref, mission_hash = _ref_hash(mission, mission_params["version_id"])
    out["mission"] = {"ref": mission_ref, "hash": mission_hash, "status": mission.get("status")}
    return out


def _read_governance(connection: sqlite3.Connection, *, mission_ref: str | None) -> dict[str, Any]:
    current = read_current(connection, mission_ref=mission_ref)
    row = connection.execute(
        "SELECT content_hash FROM governance_policy_versions WHERE policy_version_id=?",
        (current["policy_id"],)).fetchone()
    current["policy_hash"] = row[0]
    return current


def _open_ro(state_dir: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{state_dir / 'core.sqlite'}?mode=ro", uri=True)


# --------------------------------------------------------------------------
# the first cycle under the new plan


def predict_cycle(connection: sqlite3.Connection, plan: WeeklyBriefSchedulePlan,
                  as_of: datetime) -> dict[str, Any]:
    """What ``run_weekly_brief_cycle`` will do with this plan at ``as_of``.

    Mirrors its pre-admission branches, read-only: ``waiting`` before the
    plan's first slot, ``duplicate`` when this plan already admitted the slot,
    ``already_issued`` when another plan did (terminal, nothing is published),
    otherwise ``would_issue`` -- a publish on the controller's next poll.
    """

    from dalton_core.store import content_hash

    connection.row_factory = sqlite3.Row
    due = plan.latest_due(as_of)
    anchor = max(as_of, _instant(plan.effective_from))
    following = plan.latest_due(anchor + timedelta(days=7))
    result: dict[str, Any] = {
        "as_of": _iso(as_of), "plan_ref": plan.plan_ref, "plan_hash": plan.content_hash,
        "next_slot": None if following is None else _iso(following),
    }
    if due is None:
        return {**result, "status": "waiting", "scheduled_for": None,
                "note": "no slot is due on or after the plan's effective_from yet"}
    scheduled_for = _utc(due)  # the coordinator's exact string
    digest = content_hash({"plan_ref": plan.plan_ref, "plan_hash": plan.content_hash,
                           "scheduled_for": scheduled_for})[:32]
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='weekly_brief_cycle_admissions'"
    ).fetchone()
    own = issued = None
    if table is not None:
        own = connection.execute(
            "SELECT cycle_id FROM weekly_brief_cycle_admissions WHERE cycle_id=?",
            (f"weekly-brief-cycle:{digest}",)).fetchone()
        issued = connection.execute(
            "SELECT cycle_id,plan_ref,issue_version_ref FROM weekly_brief_cycle_admissions "
            "WHERE brief_ref=? AND scheduled_for=? LIMIT 1",
            (plan.brief_ref, scheduled_for)).fetchone()
    result["scheduled_for"] = scheduled_for
    if own is not None:
        return {**result, "status": "duplicate", "cycle_ref": own["cycle_id"]}
    if issued is not None:
        return {**result, "status": "already_issued",
                "issued_cycle_ref": issued["cycle_id"],
                "issued_plan_ref": issued["plan_ref"],
                "issue_version_ref": issued["issue_version_ref"],
                "note": "slot already issued by another plan; the writer returns "
                        "already_issued and publishes nothing"}
    return {**result, "status": "would_issue",
            "note": "the controller would publish this slot on its first poll after restart"}


# --------------------------------------------------------------------------
# runtime


def runtime_check(plist: Path, plan_wire: Mapping[str, Any]) -> dict[str, Any]:
    """Can this launchd job's interpreter parse the plan?  Read-only."""

    if not plist.is_file():
        return {"plist": str(plist), "supports_plan": None, "reason": "plist not found"}
    try:
        with plist.open("rb") as handle:
            job = plistlib.load(handle)
        python = job["ProgramArguments"][0]
        label = job["Label"]
    except Exception as exc:
        return {"plist": str(plist), "supports_plan": None, "reason": f"unreadable: {exc}"}
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    env.update({str(k): str(v) for k, v in (job.get("EnvironmentVariables") or {}).items()})
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        probe = subprocess.run(
            [python, "-c", _RUNTIME_PROBE, json.dumps(plan_wire)],
            env=env, cwd="/", capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"plist": str(plist), "label": label, "python": python,
                "supports_plan": False, "reason": str(exc)}
    reported = probe.stdout.strip().splitlines()[-1:] if probe.returncode == 0 else []
    ok = reported == [WeeklyBriefSchedulePlan.from_mapping(plan_wire).content_hash]
    return {"plist": str(plist), "label": label, "python": python, "supports_plan": ok,
            **({} if ok else {"reason": (probe.stderr.strip().splitlines() or ["?"])[-1]})}


def restart_plan(controller: Mapping[str, Any]) -> dict[str, Any]:
    label = controller.get("label") or "space.lumos.dalton.controller"
    return {
        "controller": {
            "needed": True, "label": label,
            "command": f"launchctl kickstart -k gui/{os.getuid()}/{label}",
            "why": "dalton_core.service reads service.json once at start; no reload",
        },
        "writer": {
            "needed": False,
            "why": "the writer does not read service.json; it reads the active policy "
                   "per cycle and receives the plan from the controller",
        },
    }


# --------------------------------------------------------------------------
# modes


def _inputs(state_dir: Path, service_config: Path, plan_path: Path, *,
            expect_hash: str | None, mission_ref: str | None) -> dict[str, Any]:
    plan_wire, plan = load_plan(plan_path, expect_hash=expect_hash)
    config = read_service_config(service_config)
    check_pairing(config, state_dir)
    connection = _open_ro(state_dir)
    try:
        current = _read_governance(connection, mission_ref=mission_ref)
    finally:
        connection.close()
    return {"plan_wire": plan_wire, "plan": plan, "config": config, "current": current}


def _base(state_dir: Path, inputs: Mapping[str, Any]) -> dict[str, Any]:
    current, plan, config = inputs["current"], inputs["plan"], inputs["config"]
    return {
        "state_dir": str(state_dir),
        "service_config": str(config["path"]),
        "mission_ref": current["mission_ref"],
        "active_policy": current["policy_id"],
        "active_constitution": current["constitution"]["id"],
        "active_mission": current["mission"]["id"],
        "target_plan": {"plan_ref": plan.plan_ref, "plan_hash": plan.content_hash,
                        "schema": inputs["plan_wire"]["schema_version"],
                        "effective_from": plan.effective_from},
        "configured_plan": {"plan_ref": config["plan"].plan_ref,
                            "plan_hash": config["plan"].content_hash},
        "constitution_weekly_brief_plan": current["constitution"]["bindings"].get(
            "weekly_brief_plan"),
    }


def plan_switch(state_dir: Path, service_config: Path, plan_path: Path = DEFAULT_PLAN, *,
                expect_hash: str | None = DEFAULT_PLAN_HASH, mission_ref: str | None = None,
                as_of: datetime | None = None,
                controller_plist: Path | None = DEFAULT_CONTROLLER_PLIST,
                writer_plist: Path | None = DEFAULT_WRITER_PLIST) -> dict[str, Any]:
    """Read-only: exactly what would change, and what happens next."""

    inputs = _inputs(state_dir, service_config, plan_path, expect_hash=expect_hash,
                     mission_ref=mission_ref)
    plan, config = inputs["plan"], inputs["config"]
    chain = build_switch_chain(inputs["current"], plan, now=_iso(_now()))
    config_needed = config["plan"].content_hash != plan.content_hash
    new_config = switched_config(config["mapping"], inputs["plan_wire"])
    validation = validate_service_config(config["mapping"], new_config)
    connection = _open_ro(state_dir)
    try:
        first = predict_cycle(connection, plan, as_of or _now())
        running = predict_cycle(connection, config["plan"], as_of or _now())
    finally:
        connection.close()
    runtime = {
        name: runtime_check(path, inputs["plan_wire"])
        for name, path in (("controller", controller_plist), ("writer", writer_plist))
        if path is not None
    }
    publishes = [] if not chain["needed"] else [
        *([chain["policy"]["policy_version_id"]] if chain["policy"] else []),
        chain["constitution"]["version_id"], chain["mission"]["version_id"]]
    status = ("already-switched" if not publishes and not config_needed
              else "would-publish")
    return {
        **_base(state_dir, inputs),
        "mode": "dry-run", "status": status,
        "steps": {
            "governance": "would-publish" if publishes else "already-published",
            "service_config": "would-rewrite" if config_needed else "already-switched",
        },
        "publishes": publishes,
        "diff": {
            "policy.weekly_brief_auto_publish.allowed_plan_bindings": {
                "before": chain["rule_before"]["allowed_plan_bindings"],
                "after": chain["rule_after"]["allowed_plan_bindings"]},
            "constitution.bindings.weekly_brief_plan": {
                "before": inputs["current"]["constitution"]["bindings"].get("weekly_brief_plan"),
                "after": chain["constitution"]["bindings"]["weekly_brief_plan"]},
            "service_config.weekly_brief.config.plan": {
                "before": {"plan_ref": config["plan"].plan_ref,
                           "plan_hash": config["plan"].content_hash},
                "after": {"plan_ref": plan.plan_ref, "plan_hash": plan.content_hash}},
        },
        "change_reason": None if chain["policy"] is None else chain["policy"]["change_reason"],
        "service_config_validation": validation,
        "configured_plan_cycle_now": running,
        "first_cycle": first,
        "runtime": runtime,
        "runtime_ready": all(item.get("supports_plan") for item in runtime.values()),
        "restart": restart_plan(runtime.get("controller", {})),
    }


def _copy_core(state_dir: Path, target: Path) -> Path:
    target.mkdir(parents=True, exist_ok=True)
    destination_path = target / "core.sqlite"
    if destination_path.exists():
        raise PlanError(f"{destination_path} already exists; rehearse into an empty directory")
    source = _open_ro(state_dir)
    destination = sqlite3.connect(str(destination_path))
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return destination_path


def rehearse(state_dir: Path, service_config: Path, target: Path, *, actor: str,
             plan_path: Path = DEFAULT_PLAN, expect_hash: str | None = DEFAULT_PLAN_HASH,
             mission_ref: str | None = None, as_of: datetime | None = None) -> dict[str, Any]:
    """The whole switch on a copy of the Core and of service.json."""

    from dalton_core.agenda import AgendaStore
    from dalton_core.coverage_mission import CoverageMissionAuthority
    from dalton_core.industry_research import IndustryResearchAuthority
    from dalton_core.research_constitution import ResearchConstitutionAuthority
    from dalton_core.store import DaltonStore
    from dalton_core.weekly_brief import WeeklyBriefAuthority
    from dalton_core.weekly_brief_coordinator import run_weekly_brief_cycle

    inputs = _inputs(state_dir, service_config, plan_path, expect_hash=expect_hash,
                     mission_ref=mission_ref)
    plan = inputs["plan"]
    database = _copy_core(state_dir, target)
    config_copy = target / service_config.name
    shutil.copy2(service_config, config_copy)
    store = DaltonStore(str(database))
    try:
        current = _read_governance(store.connection, mission_ref=mission_ref)
        chain = build_switch_chain(current, plan, now=_iso(_now()))
        governance: dict[str, Any] = {"status": "already-published"}
        if chain["needed"]:
            constitutions = ResearchConstitutionAuthority(store)
            missions = CoverageMissionAuthority(store)

            def apply(operation: str, params: dict[str, Any]) -> dict[str, Any]:
                values = dict(params)
                if operation == "create_policy":
                    policy = values.pop("policy")
                    return {"policy_version": store.create_policy(
                        policy, actor_ref=actor, **values)}
                if operation == "publish_research_constitution":
                    return constitutions.publish_constitution(
                        values.pop("constitution_ref"), actor_ref=actor, **values)
                if operation == "create_coverage_mission":
                    return missions.create_mission(
                        values.pop("mission_ref"), actor_ref=actor, **values)
                raise PlanError(operation)

            governance = {"status": "published", "chain": apply_switch_chain(chain, apply)}
        after = _read_governance(store.connection, mission_ref=mission_ref)
        state = governance_state(after, plan)
        copy_config = read_service_config(config_copy)
        config_result: dict[str, Any] = {"status": "already-switched"}
        if copy_config["plan"].content_hash != plan.content_hash:
            new = switched_config(copy_config["mapping"], inputs["plan_wire"])
            validate_service_config(copy_config["mapping"], new)
            config_result = {"status": "rewritten", **write_service_config(
                copy_config, new, plan_ref=plan.plan_ref, now=_now())}
        reread = read_service_config(config_copy)
        # The real cycle, on the copy, as the controller will send it.  A slot
        # another plan issued must come back already_issued, not a second issue.
        clock = as_of or _now()
        predicted = predict_cycle(store.connection, plan, clock)
        try:
            weekly = WeeklyBriefAuthority(store, IndustryResearchAuthority(store))
            probe = run_weekly_brief_cycle(
                store, weekly, AgendaStore(store), plan=reread["plan"].to_dict(),
                as_of=_iso(clock), actor_ref="core")
        except Exception as exc:
            probe = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        return {
            "mode": "rehearsal", "target": str(target),
            "mission_ref": current["mission_ref"],
            "governance": governance, "governance_state": state,
            "active_policy": after["policy_id"],
            "allowed_plan_bindings": _rule(after)["allowed_plan_bindings"],
            "service_config": config_result,
            "configured_plan": {"plan_ref": reread["plan"].plan_ref,
                                "plan_hash": reread["plan"].content_hash},
            "first_cycle": predicted,
            "cycle_probe": probe,
            "cycle_probe_matches_prediction": (
                probe.get("status") == predicted["status"]
                or (predicted["status"] in {"would_issue", "duplicate"}
                    and probe.get("status") == "ready")),
        }
    finally:
        store.close()


def live(state_dir: Path, service_config: Path, *, actor: str,
         plan_path: Path = DEFAULT_PLAN, expect_hash: str | None = DEFAULT_PLAN_HASH,
         mission_ref: str | None = None,
         controller_plist: Path | None = DEFAULT_CONTROLLER_PLIST,
         writer_plist: Path | None = DEFAULT_WRITER_PLIST,
         skip_runtime_check: bool = False) -> dict[str, Any]:
    """Publish through the writer, then rewrite service.json.  Never restarts."""

    from dalton_core.governance_cli import ephemeral_call

    inputs = _inputs(state_dir, service_config, plan_path, expect_hash=expect_hash,
                     mission_ref=mission_ref)
    plan = inputs["plan"]
    runtime = {
        name: runtime_check(path, inputs["plan_wire"])
        for name, path in (("controller", controller_plist), ("writer", writer_plist))
        if path is not None
    }
    if not skip_runtime_check:
        missing = [name for name, item in runtime.items() if not item.get("supports_plan")]
        if missing or not runtime:
            raise PlanError(
                "the deployed runtime cannot run plan schema "
                f"{inputs['plan_wire']['schema_version']} ({missing or 'no plist checked'}); "
                "deploy the release carrying 02b5990b first: "
                + json.dumps(runtime, sort_keys=True))
    token_config = state_dir / "writer-tokens.json"
    socket = state_dir / "run" / "writer.sock"
    chain = build_switch_chain(inputs["current"], plan, now=_iso(_now()))
    governance: dict[str, Any] = {"status": "already-published"}
    if chain["needed"]:
        if not token_config.is_file():
            raise PlanError(f"no writer token config at {token_config}")

        def apply(operation: str, params: dict[str, Any]) -> dict[str, Any]:
            result = ephemeral_call(token_config, socket, actor_ref=actor,
                                    operation=operation, params=params)
            return result if isinstance(result, dict) else {"result": result}

        governance = {"status": "published", "chain": apply_switch_chain(chain, apply)}
    connection = _open_ro(state_dir)
    try:
        after = _read_governance(connection, mission_ref=mission_ref)
        state = governance_state(after, plan)
    finally:
        connection.close()
    if not (state["policy_binds_plan"] and state["constitution_binds_active_policy"]
            and state["mission_binds_constitution"]):
        raise PlanError(f"governance did not settle; service config untouched: {state}")
    config = read_service_config(service_config)
    config_result: dict[str, Any] = {"status": "already-switched"}
    if config["plan"].content_hash != plan.content_hash:
        new = switched_config(config["mapping"], inputs["plan_wire"])
        validate_service_config(config["mapping"], new)
        config_result = {"status": "rewritten", **write_service_config(
            config, new, plan_ref=plan.plan_ref, now=_now())}
    connection = _open_ro(state_dir)
    try:
        first = predict_cycle(connection, plan, _now())
    finally:
        connection.close()
    return {
        "mode": "live", "mission_ref": after["mission_ref"],
        "governance": governance, "governance_state": state,
        "active_policy": after["policy_id"],
        "service_config": config_result,
        "first_cycle": first,
        "runtime": runtime,
        "restart": restart_plan(runtime.get("controller", {})),
        "next": "run the restart.controller.command, then this script with --verify",
    }


def verify(state_dir: Path, service_config: Path, plan_path: Path = DEFAULT_PLAN, *,
           expect_hash: str | None = DEFAULT_PLAN_HASH,
           mission_ref: str | None = None) -> dict[str, Any]:
    """Read-only: governance, config and the controller heartbeat agree."""

    inputs = _inputs(state_dir, service_config, plan_path, expect_hash=expect_hash,
                     mission_ref=mission_ref)
    plan, config = inputs["plan"], inputs["config"]
    state = governance_state(inputs["current"], plan)
    checks = {**state, "service_config_plan": config["plan"].content_hash == plan.content_hash}
    heartbeat_path = config["mapping"].get("heartbeat_path")
    heartbeat: dict[str, Any] = {"path": heartbeat_path, "read": False}
    if isinstance(heartbeat_path, str) and Path(heartbeat_path).is_file():
        try:
            beat = json.loads(Path(heartbeat_path).read_text(encoding="utf-8"))
            weekly = beat.get("weekly_brief") or {}
            last = weekly.get("last_result") or {}
            heartbeat = {
                "path": heartbeat_path, "read": True, "started_at": beat.get("started_at"),
                "state": weekly.get("state"), "last_error": weekly.get("last_error"),
                "last_result_status": last.get("status"),
                "last_result_plan_ref": last.get("plan_ref"),
                "last_result_plan_hash": last.get("plan_hash"),
                "last_completed_at": weekly.get("last_completed_at"),
            }
        except (OSError, ValueError) as exc:
            heartbeat["error"] = str(exc)
    controller_on_plan = heartbeat.get("last_result_plan_hash") == plan.content_hash
    checks["controller_runs_plan"] = controller_on_plan
    checks["controller_healthy"] = (
        heartbeat.get("last_error") is None
        and heartbeat.get("state") in HEALTHY_CYCLE_STATES)
    connection = _open_ro(state_dir)
    try:
        first = predict_cycle(connection, plan, _now())
    finally:
        connection.close()
    ok = all(checks.values())
    return {
        **_base(state_dir, inputs), "mode": "verify",
        "status": "switched" if ok else "incomplete",
        "checks": checks, "heartbeat": heartbeat, "next_cycle": first,
        **({} if controller_on_plan else {
            "hint": "heartbeat has not reported the new plan yet: restart the controller "
                    f"({restart_plan({})['controller']['command']}) or wait for its first poll"}),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", type=Path, required=True,
                        help="the dalton-core state directory of the environment")
    parser.add_argument("--service-config", type=Path, required=True,
                        help="that environment's controller service.json")
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN,
                        help="schedule plan file to switch to (default: v4)")
    parser.add_argument("--expect-plan-hash", default=None,
                        help="reviewed hash of --plan (default: the v4 hash when --plan "
                             "is the default file; unchecked otherwise)")
    parser.add_argument("--mission-ref", default=None)
    parser.add_argument("--as-of", default=None,
                        help="clock for the first-cycle prediction (dry-run/rehearse)")
    parser.add_argument("--controller-plist", type=Path, default=DEFAULT_CONTROLLER_PLIST)
    parser.add_argument("--writer-plist", type=Path, default=DEFAULT_WRITER_PLIST)
    parser.add_argument("--skip-runtime-check", action="store_true",
                        help="--apply without checking the launchd runtimes parse the plan")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true",
                      help="publish through the writer as --actor, then rewrite service.json")
    mode.add_argument("--rehearse", type=Path,
                      help="copy the Core and service.json here and switch the copies")
    mode.add_argument("--verify", action="store_true", help="read-only post-switch check")
    parser.add_argument("--actor", default=None,
                        help="human:<owner> principal that signs the policy version")
    args = parser.parse_args(argv)
    expand = lambda value: Path(os.path.expanduser(str(value))).resolve()  # noqa: E731
    state_dir, service_config, plan_path = (
        expand(args.state_dir), expand(args.service_config), expand(args.plan))
    expect = args.expect_plan_hash or (
        DEFAULT_PLAN_HASH if plan_path == DEFAULT_PLAN.resolve() else None)
    if (args.apply or args.rehearse) and not (args.actor or "").startswith("human:"):
        raise PlanError("--actor human:<owner> is required to sign a policy version")
    if not (state_dir / "core.sqlite").is_file():
        raise PlanError(f"no Core at {state_dir / 'core.sqlite'}")
    as_of = None if args.as_of is None else _instant(args.as_of)
    common = {"expect_hash": expect, "mission_ref": args.mission_ref}
    if args.apply:
        result = live(state_dir, service_config, actor=args.actor, plan_path=plan_path,
                      controller_plist=args.controller_plist,
                      writer_plist=args.writer_plist,
                      skip_runtime_check=args.skip_runtime_check, **common)
    elif args.rehearse:
        result = rehearse(state_dir, service_config, expand(args.rehearse),
                          actor=args.actor, plan_path=plan_path, as_of=as_of, **common)
    elif args.verify:
        result = verify(state_dir, service_config, plan_path, **common)
    else:
        result = plan_switch(state_dir, service_config, plan_path, as_of=as_of,
                             controller_plist=args.controller_plist,
                             writer_plist=args.writer_plist, **common)
    print(json.dumps(result, ensure_ascii=False, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - an owner-run script
    sys.exit(main())
