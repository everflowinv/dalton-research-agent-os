"""One day's money for the whole host, not one day's money per environment.

Until 2026-09-17 every research environment carried its own mission budget, and
``max_daily_cost_usd: 500`` in four environments meant the owner could spend
two thousand dollars in a day while every page truthfully reported that no cap
had been passed.  The caps were real; the number they added up to was not a
number anybody had agreed to.

The owner's instruction is that **the daily budget is shared**: the day's cost
and the day's paid calls are counted across all environments, and a call is
refused when the shared cap would be passed, *in addition to* the mission's own
cap.  A mission cap is still a mission cap -- this does not raise anything -- it
is a second, tighter ceiling over the host.

Three deliberate choices:

* **The policy is a host file, not a row in a database.**  It is written once,
  owner-only, content-hashed, with a ``prior_hash`` chain, exactly like
  ``model-call-budget-policy.json`` -- because no single environment's writer
  may own a number that binds the others.  A database would have had to belong
  to somebody.
* **The count is read-only across environments.**  Each environment's day
  ledger is still owned and written by that environment's own writer; this
  module only ever opens the others ``mode=ro``.  The single-writer rule is
  not bent: a refusal is a *local* decision made with knowledge the local
  writer read for itself.
* **The binding is a file beside the ledger.**  ``admit`` finds it without any
  caller passing it, which is what makes the cap apply to every lane on the
  host -- the planner, the extractor, the cockpit, a canary script -- rather
  than to the handful of call sites somebody remembered to edit.  An
  environment with no binding file behaves exactly as it did before.

A stopped environment reads as zero spend and is *named* in what the cockpit
shows, rather than silently blocking the host: an environment that is not
running is not spending, and refusing every call because a disk is unplugged
would be a worse failure than the one this module exists to prevent.

The AlphaEngine 24-hour figure lives in this policy too, but its enforcement is
elsewhere on purpose: it is counted from each environment's Core connector
ledger over a trailing 24-hour window, not from the day ledger over a UTC
calendar day.  :func:`shared_alphaengine_cap` hands the number to that check;
the counting stays where it is.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from typing import Any

from .store import content_hash

SCHEMA_VERSION = "dalton-shared-daily-budget-policy-0.1"
BINDING_SCHEMA_VERSION = "dalton-shared-daily-budget-binding-0.1"
#: The file each environment keeps beside its day ledger, naming the host
#: policy it is bound to. Written by ``scripts/bind_shared_daily_budget.py``.
BINDING_FILENAME = "shared-daily-budget-binding.json"
#: Where the host policy lives by default. A path, not a constant cap: the
#: numbers are the owner's and live in the file.
DEFAULT_POLICY_PATH = "~/.dalton/connections/shared-daily-budget.json"

_FIELDS = {"schema_version", "max_daily_cost_usd", "max_daily_paid_calls",
           "max_alphaengine_calls_24h", "revision", "prior_hash", "updated_at",
           "actor_ref", "content_hash"}
_BINDING_FIELDS = {"schema_version", "policy_path", "policy_hash",
                   "manager_config_path", "environment_id", "content_hash"}
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class SharedDailyBudgetError(ValueError):
    """The shared daily budget policy or binding cannot be used as written."""


# ---------------------------------------------------------------------------
# The host policy.


def _positive_number(value: Any, label: str) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(float(value)) or float(value) <= 0):
        raise SharedDailyBudgetError(f"{label} must be a positive finite number")
    return float(value)


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SharedDailyBudgetError(f"{label} must be a positive integer")
    return int(value)


def validate_shared_daily_budget_policy(value: Mapping[str, Any]) -> dict[str, Any]:
    """Check the closed shape and the hash without changing either.

    Validated but never re-serialised, for the same reason the shared call-cost
    policy is: ``1`` and ``1.0`` are both acceptable numbers and hash
    differently, so a validator that normalised them would invalidate the file
    it just approved.
    """

    if not isinstance(value, Mapping) or set(value) != _FIELDS:
        raise SharedDailyBudgetError("shared daily budget policy has an invalid closed shape")
    wire = dict(value)
    asserted = wire.pop("content_hash")
    if wire.get("schema_version") != SCHEMA_VERSION or asserted != content_hash(wire):
        raise SharedDailyBudgetError("shared daily budget policy identity or hash is invalid")
    if (not isinstance(wire.get("revision"), int) or isinstance(wire["revision"], bool)
            or wire["revision"] < 1):
        raise SharedDailyBudgetError("shared daily budget policy revision is invalid")
    if wire["prior_hash"] is not None and not _HEX64.fullmatch(str(wire["prior_hash"])):
        raise SharedDailyBudgetError("shared daily budget policy prior_hash is invalid")
    if not isinstance(wire.get("updated_at"), str) or not wire["updated_at"]:
        raise SharedDailyBudgetError("shared daily budget policy updated_at is invalid")
    if not isinstance(wire.get("actor_ref"), str) or not wire["actor_ref"].startswith("human:"):
        raise SharedDailyBudgetError("shared daily budget policy actor_ref is invalid")
    _positive_number(wire["max_daily_cost_usd"], "max_daily_cost_usd")
    _positive_int(wire["max_daily_paid_calls"], "max_daily_paid_calls")
    _positive_int(wire["max_alphaengine_calls_24h"], "max_alphaengine_calls_24h")
    return {**wire, "content_hash": asserted}


def load_shared_daily_budget_policy(path: str | Path) -> dict[str, Any]:
    """Read and check the host policy.

    Re-read on every use rather than cached: a cap the owner lowered has to
    bind the next call, not the next restart, and the file is a few hundred
    bytes beside a model call that costs a dollar.
    """

    target = Path(path).expanduser()
    if not target.is_absolute() or target.is_symlink():
        raise SharedDailyBudgetError(
            "shared daily budget policy path must be absolute and not a symlink")
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SharedDailyBudgetError(
            "shared daily budget policy is unavailable or invalid") from exc
    return validate_shared_daily_budget_policy(value)


def policy_wire(
    *, max_daily_cost_usd: float, max_daily_paid_calls: int,
    max_alphaengine_calls_24h: int, revision: int, prior_hash: str | None,
    updated_at: str, actor_ref: str,
) -> dict[str, Any]:
    """Build one complete, hashed policy record.

    Kept here rather than in the binding script so the file the script writes
    and the file the validator accepts cannot drift apart.
    """

    wire = {
        "schema_version": SCHEMA_VERSION,
        "max_daily_cost_usd": max_daily_cost_usd,
        "max_daily_paid_calls": max_daily_paid_calls,
        "max_alphaengine_calls_24h": max_alphaengine_calls_24h,
        "revision": revision,
        "prior_hash": prior_hash,
        "updated_at": updated_at,
        "actor_ref": actor_ref,
    }
    wire["content_hash"] = content_hash(
        {key: value for key, value in wire.items()})
    return validate_shared_daily_budget_policy(wire)


def shared_day_caps(policy: Mapping[str, Any]) -> dict[str, int]:
    """The policy's two day caps in the units ``admit`` compares against."""

    checked = validate_shared_daily_budget_policy(policy)
    return {
        "max_daily_paid_calls": int(checked["max_daily_paid_calls"]),
        "max_daily_cost_micros": int(
            (Decimal(str(checked["max_daily_cost_usd"])) * 1_000_000).to_integral_value()
        ),
    }


def shared_alphaengine_cap(policy: Mapping[str, Any]) -> int:
    """The host's trailing-24h AlphaEngine cap, for the check that owns it."""

    return int(validate_shared_daily_budget_policy(policy)["max_alphaengine_calls_24h"])


# ---------------------------------------------------------------------------
# The per-environment binding, and the peers it points at.


def binding_wire(
    *, policy_path: str, policy_hash: str, manager_config_path: str,
    environment_id: str,
) -> dict[str, Any]:
    """The file an environment keeps beside its day ledger.

    It names the policy *and* the hash the owner bound, so an environment can
    tell "the owner changed the caps" (hash differs, honour the new file) from
    "somebody swapped the file underneath me" -- the latter being why the hash
    is recorded at all even though the policy carries its own.
    """

    wire = {
        "schema_version": BINDING_SCHEMA_VERSION,
        "policy_path": policy_path,
        "policy_hash": policy_hash,
        "manager_config_path": manager_config_path,
        "environment_id": environment_id,
    }
    wire["content_hash"] = content_hash(dict(wire))
    return validate_shared_daily_budget_binding(wire)


def validate_shared_daily_budget_binding(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _BINDING_FIELDS:
        raise SharedDailyBudgetError("shared daily budget binding has an invalid closed shape")
    wire = dict(value)
    asserted = wire.pop("content_hash")
    if wire.get("schema_version") != BINDING_SCHEMA_VERSION or asserted != content_hash(wire):
        raise SharedDailyBudgetError("shared daily budget binding identity or hash is invalid")
    for key in ("policy_path", "manager_config_path"):
        if not isinstance(wire.get(key), str) or not Path(wire[key]).is_absolute():
            raise SharedDailyBudgetError(f"shared daily budget binding {key} must be absolute")
    if not isinstance(wire.get("policy_hash"), str) or not _HEX64.fullmatch(wire["policy_hash"]):
        raise SharedDailyBudgetError("shared daily budget binding policy_hash is invalid")
    if not isinstance(wire.get("environment_id"), str) or not wire["environment_id"]:
        raise SharedDailyBudgetError("shared daily budget binding environment_id is invalid")
    return {**wire, "content_hash": asserted}


def load_shared_daily_budget_binding(path: str | Path) -> dict[str, Any]:
    target = Path(path).expanduser()
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SharedDailyBudgetError(
            "shared daily budget binding is unavailable or invalid") from exc
    return validate_shared_daily_budget_binding(value)


def binding_path_for_ledger(budget_db: str | Path) -> Path:
    """Where an environment's binding sits, given its day ledger."""

    return Path(budget_db).expanduser().parent / BINDING_FILENAME


def resolve_shared_gate(budget_db: str | Path) -> dict[str, Any] | None:
    """Everything the day gate needs, or ``None`` when nothing is bound.

    ``None`` is the pre-2026-09-17 behaviour and is returned for an unbound
    environment, an in-memory ledger, or a binding whose policy has gone.  A
    *malformed* binding is a different thing and raises: somebody wrote that
    file on purpose, and pretending it is not there would be the one failure
    mode this whole module exists to prevent.
    """

    ledger = Path(budget_db).expanduser()
    binding_file = binding_path_for_ledger(ledger)
    if not binding_file.is_file():
        return None
    binding = load_shared_daily_budget_binding(binding_file)
    policy = load_shared_daily_budget_policy(binding["policy_path"])
    caps = shared_day_caps(policy)
    peers = peer_ledgers(
        binding["manager_config_path"], exclude_ledger=ledger)
    return {
        "policy_hash": policy["content_hash"],
        "policy_revision": policy["revision"],
        "environment_id": binding["environment_id"],
        "peer_ledgers": peers,
        **caps,
    }


def peer_cores(
    manager_config_path: str | Path, *, exclude_core: str | Path,
) -> list[dict[str, str]]:
    """Every *other* environment's Core, for the trailing-24h AlphaEngine count.

    A separate list from :func:`peer_ledgers` because the two budgets are
    counted in different places over different windows: money in the day
    ledger over a UTC calendar day, AlphaEngine calls in the Core's connector
    ledger over the last 24 hours.  Merging them would have meant one of the
    two answering the wrong question.
    """

    from .model_routing_sync import ModelRoutingSyncError, host_environments

    excluded = Path(exclude_core).expanduser()
    try:
        excluded_resolved = excluded.resolve()
    except OSError:
        excluded_resolved = excluded
    try:
        environments = host_environments(manager_config_path)
    except ModelRoutingSyncError:
        return []
    peers: list[dict[str, str]] = []
    for environment in environments:
        core = environment.core_db
        try:
            same = core == excluded or core.resolve() == excluded_resolved
        except OSError:
            same = core == excluded
        if same or not core.is_file():
            continue
        peers.append({"environment_id": environment.environment_id,
                      "name": environment.name, "core_db": str(core)})
    return peers


def shared_alphaengine_view(
    core_db: str | Path, *, as_of: Any = None,
) -> dict[str, Any] | None:
    """The host's AlphaEngine cap and what the *other* environments have used.

    ``None`` when this environment has no shared budget bound.  A peer Core
    that will not open counts as zero and is named, for the same reason an
    unreadable day ledger does: an environment that is not running is not
    calling AlphaEngine, and one unplugged disk must not stop the host.
    """

    from .bounded_alphaengine_probe import count_recent_alphaengine_calls

    state_dir = Path(core_db).expanduser().parent
    binding_file = state_dir / BINDING_FILENAME
    if not binding_file.is_file():
        return None
    binding = load_shared_daily_budget_binding(binding_file)
    policy = load_shared_daily_budget_policy(binding["policy_path"])
    spent_elsewhere = 0
    unavailable: list[dict[str, str]] = []
    for peer in peer_cores(binding["manager_config_path"], exclude_core=core_db):
        try:
            with closing(sqlite3.connect(
                f"file:{peer['core_db']}?mode=ro", uri=True, timeout=5,
            )) as core:
                core.row_factory = sqlite3.Row
                spent_elsewhere += count_recent_alphaengine_calls(core, as_of=as_of)
        except (sqlite3.Error, OSError) as exc:
            unavailable.append({"environment_id": peer["environment_id"],
                                "name": peer["name"], "reason": str(exc)})
    return {"cap": shared_alphaengine_cap(policy),
            "spent_elsewhere": spent_elsewhere,
            "policy_hash": policy["content_hash"],
            "unavailable": unavailable}


def peer_ledgers(
    manager_config_path: str | Path, *, exclude_ledger: str | Path,
) -> list[dict[str, str]]:
    """Every *other* environment's day ledger on this host.

    Enumerated from the host manager configuration through the same read-only
    reader the model fan-out uses, so "which environments exist" has exactly
    one answer on this machine.
    """

    from .model_routing_sync import ModelRoutingSyncError, host_environments

    excluded = Path(exclude_ledger).expanduser()
    try:
        excluded_resolved = excluded.resolve()
    except OSError:
        excluded_resolved = excluded
    try:
        environments = host_environments(manager_config_path)
    except ModelRoutingSyncError:
        return []
    peers: list[dict[str, str]] = []
    for environment in environments:
        ledger = environment.budget_db
        try:
            same = ledger == excluded or ledger.resolve() == excluded_resolved
        except OSError:
            same = ledger == excluded
        if same:
            continue
        peers.append({"environment_id": environment.environment_id,
                      "name": environment.name, "budget_db": str(ledger)})
    return peers


# ---------------------------------------------------------------------------
# Counting a day, in one ledger and across the host.

#: Today's paid calls in one day ledger: every admission of the day, plus every
#: admission of any day that has never settled. Deliberately the same shape as
#: the ``outer_budget`` query inside ``admit`` -- an open reservation from
#: yesterday is money that may still land, and a shared cap that forgot it
#: would let the host overspend on exactly the day a lane crashed.
_DAY_SPEND_SQL = (
    "SELECT a.reserved_micros,"
    "COALESCE(c.corrected_micros,s.actual_micros) AS actual_micros "
    "FROM thesis_impact_day_admissions a "
    "LEFT JOIN thesis_impact_day_settlements s ON s.admission_id=a.admission_id "
    "LEFT JOIN thesis_impact_settlement_corrections c ON c.admission_id=a.admission_id "
    "WHERE a.day=? OR s.admission_id IS NULL"
)


def day_spend_rows(rows: Sequence[Any]) -> dict[str, int]:
    """Fold admission rows into the two numbers a cap is compared against."""

    calls = len(rows)
    micros = 0
    for row in rows:
        actual = row["actual_micros"]
        micros += int(row["reserved_micros"] if actual is None else actual)
    return {"calls": calls, "cost_micros": micros}


def ledger_day_spend(budget_db: str | Path, day: str) -> dict[str, int]:
    """One environment's spend today, read-only.

    A plain ``mode=ro`` connection rather than the strict WAL reader: this is
    the one place that opens a database another writer owns, and a peer whose
    WAL sidecars happen not to exist right now must read as "nothing there",
    not as an exception that stops the local call.  ``mode=ro`` cannot create a
    file, so nothing is provisioned by looking.
    """

    target = Path(budget_db).expanduser()
    if not target.is_file():
        raise sqlite3.OperationalError(f"no day ledger at {target}")
    with closing(sqlite3.connect(
        f"file:{target}?mode=ro", uri=True, timeout=5,
    )) as ledger:
        ledger.row_factory = sqlite3.Row
        return day_spend_rows(ledger.execute(_DAY_SPEND_SQL, (day,)).fetchall())


def cross_environment_spend(
    peers: Sequence[Mapping[str, str]], day: str,
) -> dict[str, Any]:
    """Today's spend in every other environment, and which ones would not read.

    An unreadable peer contributes zero and is named.  That is the honest
    answer -- an environment whose disk is not mounted is not spending -- and
    naming it is what lets the owner tell "nobody else spent anything" from
    "I could not look".
    """

    calls = 0
    micros = 0
    per_environment: list[dict[str, Any]] = []
    unavailable: list[dict[str, str]] = []
    for peer in peers:
        try:
            spend = ledger_day_spend(peer["budget_db"], day)
        except (sqlite3.Error, OSError) as exc:
            unavailable.append({"environment_id": peer.get("environment_id", ""),
                                "name": peer.get("name", ""), "reason": str(exc)})
            continue
        calls += spend["calls"]
        micros += spend["cost_micros"]
        per_environment.append({
            "environment_id": peer.get("environment_id", ""),
            "name": peer.get("name", ""), **spend,
        })
    return {"calls": calls, "cost_micros": micros,
            "environments": per_environment, "unavailable": unavailable}


def shared_day_view(budget_db: str | Path, day: str) -> dict[str, Any] | None:
    """What the cockpit 预算 page shows beside the mission's own numbers.

    Returns ``None`` when this environment has no shared budget bound, so a
    page that has nothing to say says nothing rather than showing a cap of
    zero.
    """

    try:
        gate = resolve_shared_gate(budget_db)
    except SharedDailyBudgetError as exc:
        return {"status": "invalid", "note": f"共享预算配置无法读取：{exc}"}
    if gate is None:
        return None
    try:
        local = ledger_day_spend(budget_db, day)
    except (sqlite3.Error, OSError):
        local = {"calls": 0, "cost_micros": 0}
    peers = cross_environment_spend(gate["peer_ledgers"], day)
    calls = local["calls"] + peers["calls"]
    micros = local["cost_micros"] + peers["cost_micros"]
    return {
        "status": "ok",
        "scope": "all_environments",
        "policy_hash": gate["policy_hash"],
        "policy_revision": gate["policy_revision"],
        "used": calls,
        "cap": gate["max_daily_paid_calls"],
        "cost_usd": round(micros / 1_000_000, 4),
        "cost_cap_usd": round(gate["max_daily_cost_micros"] / 1_000_000, 4),
        "this_environment": {
            "environment_id": gate["environment_id"],
            "used": local["calls"],
            "cost_usd": round(local["cost_micros"] / 1_000_000, 4),
        },
        "environments": peers["environments"],
        "unavailable": peers["unavailable"],
        "note": _shared_note(peers["unavailable"]),
    }


def _shared_note(unavailable: Sequence[Mapping[str, str]]) -> str:
    if not unavailable:
        return "上限与今日花费按这台机器上的所有研究环境合并计算。"
    names = "、".join(item.get("name") or item.get("environment_id", "")
                      for item in unavailable)
    return (
        "上限与今日花费按这台机器上的所有研究环境合并计算；"
        f"其中 {len(unavailable)} 个环境读不到，未计入：{names}。"
    )
