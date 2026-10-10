"""Which drafting stages are checked by an independent verifier, and can they be.

Owner request, 2026-10-08: "把所有受核验冲突环节影响的，单独做一个模型排序选项
（在 cockpit 里），能够从 cockpit 调整".

The conflict is structural, not a bug in any one lane.  A verifier-tier stage
routes work orders that ask for ``provider-controlled-verify``; on this host
only the direct Gemini profiles declare it, so every verifier chain is
google-gemini-3 from end to end.  ``model_fallback_chain.execute_chain``
refuses a verification whose producer family has no independent live link
(``verifier_not_independent``).  So whenever the *drafting* stage in front of a
verifier is served by a google-gemini-3 model -- because its tier puts one
first, or because the link in front of it is cooling -- the verification is
refused before any route decision exists.

This module answers three questions read-only, from the same registries and
policy content routing reads:

* :func:`discover_pairs` -- every (draft purpose -> verifier purpose) pair the
  code wires, derived from the purpose/tier table (verifier-tier purposes are
  exactly the ones whose work orders require provider-controlled verification
  and family independence) plus the few verifiers whose producers do not share
  their name.
* :func:`evaluate_pair` -- given the two effective chains, the catalog and the
  active cooldowns, is the pair independent today, in conflict, or only
  rescued/broken by a cooldown.
* :func:`recommend_order` -- the minimal reorder of the draft chain that makes
  the pair independent: the first link that is usable, not cooling and of a
  family some capable verifier link is not, moved to the front; nothing else
  moves.

:func:`environment_report` composes them for one environment's state
directory (bindings, pinned policies, catalog, cooldowns, and the 48 h refusal
count from the result envelopes batch w started writing), opening every
database read-only.  Nothing here writes; publishing a new order is the
ordinary governed ``set_model_selection`` path.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .model_router import declared_tier_chain
from .model_fallback_chain import (
    TIER_VERIFIER,
    effective_chain,
    profile_unusable,
    purpose_tiers,
    required_transport,
)

#: The capability a verifier-tier work order requires of its model.
VERIFY_CAPABILITY = "provider-controlled-verify"

#: Verifiers whose producers do not share their name.  Kept next to the code
#: that names them: claim support checks statements drafted by the three
#: document lanes (``producer_route_ref`` of each item), and the localization
#: fidelity check binds both the localization draft and the language revision
#: (``research_language_runtime``: ``producer_route_decision_refs=[draft,
#: brain]``).
VERIFIER_EXTRA_PRODUCERS: dict[str, tuple[str, ...]] = {
    "claim_support_verifier": (
        "document_extraction", "mission_directed_document_draft",
        "registered_annual_report_draft",
    ),
    "claim_support_backfill": (
        "document_extraction", "mission_directed_document_draft",
        "registered_annual_report_draft",
    ),
    "research_localization_verifier": ("research_language_revision",),
}

#: How far back the refusal count looks.
REFUSAL_WINDOW = timedelta(hours=48)

STATUS_INDEPENDENT = "independent"
STATUS_CONFLICT = "conflict"
STATUS_COOLDOWN = "cooldown"
STATUS_NO_VERIFIER = "no_verifier"
STATUS_NO_DRAFT = "no_draft"
STATUS_UNBOUND = "unbound"
#: Statuses a parity report and the card count as "in conflict right now".
CONFLICT_STATUSES = frozenset({STATUS_CONFLICT, STATUS_COOLDOWN, STATUS_NO_VERIFIER})

_COCKPIT_WORK_RE = re.compile(r"^work:cockpit-([a-z][a-z0-9_]*)-[0-9a-f]{16,}$")


def _short(profile_id: str) -> str:
    return profile_id.split(":", 1)[1] if profile_id.startswith("profile:") else profile_id


# ---------------------------------------------------------------------------
# Discovery


def _name_producers(verifier: str) -> list[str]:
    if not verifier.endswith("_verifier"):
        return []
    stem = verifier[: -len("_verifier")]
    return [stem, f"{stem}_draft", f"{stem}_assessment"]


def discover_pairs(
    tiers: Mapping[str, str] | None = None,
    *,
    extra_producers: Mapping[str, Sequence[str]] | None = None,
    retired: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Every (draft -> verifier) pair the registry wires, and any verifier left over.

    A verifier is a purpose in the verifier tier.  Its producers are the
    registered non-verifier purposes that share its stem (``x`` / ``x_draft``
    / ``x_assessment`` for ``x_verifier``) plus the declared extras.  Retired
    stages (ADR-0010) take part on neither side.  ``unpaired`` names a
    verifier no producer could be found for, so a new verifier lane that
    forgets to say what it checks shows up in a test rather than as a silent
    gap on the card.
    """

    from .model_selection import RETIRED_PURPOSES

    tiers = dict(purpose_tiers() if tiers is None else tiers)
    extras = VERIFIER_EXTRA_PRODUCERS if extra_producers is None else extra_producers
    gone = set(RETIRED_PURPOSES if retired is None else retired)
    pairs: list[dict[str, Any]] = []
    unpaired: list[str] = []
    for verifier in sorted(name for name, tier in tiers.items() if tier == TIER_VERIFIER):
        if verifier in gone:
            continue
        candidates = [*_name_producers(verifier), *(extras.get(verifier) or ())]
        producers = list(dict.fromkeys(
            name for name in candidates
            if name in tiers and tiers[name] != TIER_VERIFIER and name not in gone
        ))
        if not producers:
            unpaired.append(verifier)
            continue
        for draft in producers:
            pairs.append({
                "draft_purpose": draft,
                "verifier_purpose": verifier,
                "draft_tier": tiers[draft],
                "verifier_tier": tiers[verifier],
                "requires": [VERIFY_CAPABILITY, "family_independence"],
            })
    return {"pairs": pairs, "unpaired": unpaired}


# ---------------------------------------------------------------------------
# Evaluation


def _usable(profile: Mapping[str, Any] | None, now: datetime) -> bool:
    return isinstance(profile, Mapping) and profile_unusable(profile, now=now) is None


def _capable_verifiers(
    chain: Sequence[str], profiles: Mapping[str, Mapping[str, Any]], now: datetime,
) -> list[str]:
    return [
        profile_id for profile_id in chain
        if _usable(profiles.get(profile_id), now)
        and VERIFY_CAPABILITY in ((profiles.get(profile_id) or {}).get("capabilities") or ())
    ]


def link_independence(
    profile_id: str,
    verifier_chains: Sequence[Sequence[str]],
    profiles: Mapping[str, Mapping[str, Any]],
    cooldowns: Mapping[str, Any],
    now: datetime,
) -> dict[str, Any]:
    """Whether a draft served by this profile can be verified by every chain given.

    ``independent``: each verifier chain has a capable live link of another
    family (cooling or not).  ``independent_now``: the same with cooling links
    left out -- what a call made this minute would see.
    """

    family = str((profiles.get(profile_id) or {}).get("family") or "")
    independent = independent_now = True
    for chain in verifier_chains:
        capable = _capable_verifiers(chain, profiles, now)
        others = [item for item in capable
                  if str((profiles.get(item) or {}).get("family") or "") != family]
        independent = independent and bool(others)
        independent_now = independent_now and any(item not in cooldowns for item in others)
    return {"family": family, "independent": independent,
            "independent_now": independent_now}


def evaluate_pair(
    *,
    draft_chain: Sequence[str],
    verifier_chain: Sequence[str],
    profiles: Mapping[str, Mapping[str, Any]],
    cooldowns: Mapping[str, Any],
    now: datetime,
) -> dict[str, Any]:
    """Is this pair independent today, and if not, which model is why."""

    capable = _capable_verifiers(verifier_chain, profiles, now)
    links: list[dict[str, Any]] = []
    for position, profile_id in enumerate(draft_chain, start=1):
        profile = profiles.get(profile_id)
        facts = link_independence(profile_id, [verifier_chain], profiles, cooldowns, now)
        links.append({
            "position": position,
            "profile_id": profile_id,
            "family": facts["family"] or None,
            "capabilities": list((profile or {}).get("capabilities") or ()),
            "usable": _usable(profile, now),
            "cooling": profile_id in cooldowns,
            "cooldown_until": (cooldowns.get(profile_id) or {}).get("until")
            if isinstance(cooldowns.get(profile_id), Mapping) else None,
            "conflict": not facts["independent"],
        })
    verifier_links = [{
        "position": position,
        "profile_id": profile_id,
        "family": (profiles.get(profile_id) or {}).get("family"),
        "capable": profile_id in capable,
        "usable": _usable(profiles.get(profile_id), now),
        "cooling": profile_id in cooldowns,
    } for position, profile_id in enumerate(verifier_chain, start=1)]
    usable = [link for link in links if link["usable"]]
    nominal = usable[0] if usable else None
    serving = next((link for link in usable if not link["cooling"]), None)
    later_conflicts = [link["profile_id"] for link in usable[1:] if link["conflict"]]
    result: dict[str, Any] = {
        "draft_links": links,
        "verifier_links": verifier_links,
        "verifier_families": sorted({str((profiles.get(item) or {}).get("family") or "")
                                     for item in capable}),
        "expected_producer": None if serving is None else serving["profile_id"],
        "nominal_producer": None if nominal is None else nominal["profile_id"],
        "fallback_conflicts": later_conflicts,
    }
    if not capable:
        status, culprit = STATUS_NO_VERIFIER, None
    elif serving is None:
        status = STATUS_COOLDOWN if any(link["cooling"] for link in usable) else STATUS_NO_DRAFT
        culprit = None if nominal is None else nominal["profile_id"]
    elif serving["conflict"]:
        culprit = serving["profile_id"]
        status = (STATUS_COOLDOWN if nominal is not serving and not nominal["conflict"]
                  else STATUS_CONFLICT)
    elif not link_independence(serving["profile_id"], [verifier_chain], profiles,
                               cooldowns, now)["independent_now"]:
        status, culprit = STATUS_COOLDOWN, serving["profile_id"]
    else:
        status, culprit = STATUS_INDEPENDENT, None
    result["status"] = status
    result["culprit"] = culprit
    result["culprit_family"] = (None if culprit is None
                                else (profiles.get(culprit) or {}).get("family"))
    return result


def status_label(evaluation: Mapping[str, Any], *,
                 display_name: Any = _short) -> str:
    """The owner's words for one evaluation: 独立 / 冲突：… / 冷却中：…"""

    status = evaluation.get("status")
    culprit = evaluation.get("culprit")
    name = display_name(culprit) if culprit else ""
    family = evaluation.get("culprit_family") or ""
    if status == STATUS_INDEPENDENT:
        return "独立"
    if status == STATUS_CONFLICT:
        return f"冲突：{name} 与核验同家族（{family}）"
    if status == STATUS_COOLDOWN:
        nominal = evaluation.get("nominal_producer")
        if culprit and nominal and nominal != culprit:
            return (f"冷却中：{display_name(nominal)} 正在冷却，起草会落到 {name}，"
                    f"与核验同家族（{family}）")
        if culprit:
            return f"冷却中：能独立核验 {name} 的模型正在冷却"
        return "冷却中：起草链上的模型都在冷却"
    if status == STATUS_NO_VERIFIER:
        return "冲突：核验链里没有能承担受控核验的可用模型"
    if status == STATUS_NO_DRAFT:
        return "起草链里没有可用模型"
    if status == STATUS_UNBOUND:
        return "本环境没有配置这个环节"
    return str(status or "未知")


# ---------------------------------------------------------------------------
# Recommendation


def per_call_cost_usd(profile: Mapping[str, Any] | None, *, input_tokens: int,
                      output_tokens: int) -> str | None:
    """List-price estimate of one call, or ``None`` for an unpriced model."""

    cost = (profile or {}).get("cost") if isinstance(profile, Mapping) else None
    if not isinstance(cost, Mapping):
        return None
    try:
        inbound = Decimal(str(cost["input_per_million_usd"]))
        outbound = Decimal(str(cost["output_per_million_usd"]))
    except (KeyError, InvalidOperation):
        return None
    value = (inbound * input_tokens + outbound * output_tokens) / Decimal(1_000_000)
    return str(value.quantize(Decimal("0.0001")))


def recommend_order(
    chain: Sequence[str],
    *,
    purpose: str,
    verifier_chains: Sequence[Sequence[str]],
    profiles: Mapping[str, Mapping[str, Any]],
    cooldowns: Mapping[str, Any],
    now: datetime,
    candidates: Sequence[str] = (),
    input_tokens: int = 8_000,
    output_tokens: int = 2_000,
) -> dict[str, Any]:
    """The smallest change that makes every verifier of this draft independent.

    The first link -- in the current chain's own order -- that is usable, not
    cooling and verifiable by every chain is moved to the front; the rest keep
    their order.  Only when no link of the chain qualifies is a model from
    ``candidates`` brought in, cheapest list price first, and put in front of
    the unchanged chain.
    """

    current = [str(item) for item in chain]

    def good(profile_id: str) -> bool:
        if not _usable(profiles.get(profile_id), now) or profile_id in cooldowns:
            return False
        return link_independence(profile_id, verifier_chains, profiles,
                                 cooldowns, now)["independent_now"]

    if required_transport(purpose) is not None:
        return {"status": "pinned", "chain": current,
                "note": "这个环节的链首由契约固定，不能调整顺序。"}
    if current and good(current[0]):
        return {"status": "unchanged", "chain": current, "moved": None,
                "note": "当前顺序已经独立，不需要调整。"}
    pick = next((item for item in current if good(item)), None)
    source = "chain"
    if pick is None:
        priced = []
        for item in candidates:
            if item in current or not good(item):
                continue
            estimate = per_call_cost_usd(profiles.get(item), input_tokens=input_tokens,
                                         output_tokens=output_tokens)
            priced.append((Decimal(estimate) if estimate is not None else Decimal("1e9"),
                           item))
        if priced:
            pick, source = sorted(priced)[0][1], "catalog"
    if pick is None:
        return {"status": "none", "chain": current, "moved": None,
                "note": "目录里没有既可用、又不在冷却、又与核验不同家族的模型。"}
    proposed = [pick, *[item for item in current if item != pick]]
    return {"status": "proposed", "chain": proposed, "moved": pick, "source": source,
            "note": (f"把 {_short(pick)} 提到第一位，其余顺序不变。" if source == "chain"
                     else f"链里没有合适的模型；把目录中的 {_short(pick)} 放在第一位，其余顺序不变。")}


# ---------------------------------------------------------------------------
# Reading one environment (read-only throughout)


def _ro(path: Path) -> sqlite3.Connection | None:
    if not path.is_file():
        return None
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None
    connection.row_factory = sqlite3.Row
    return connection


class RouterSnapshot:
    """The catalog, cooldowns and pinned policies of one router, read once."""

    def __init__(self, router_db: str | Path, *, now: datetime) -> None:
        from .readonly_sqlite import connect_read_only

        self.path = Path(router_db).expanduser()
        if not self.path.is_file():
            raise FileNotFoundError(f"这里没有模型路由数据库：{self.path}")
        # Same two-step as model_routing_sync._query_router: the strict WAL
        # reader on a running environment, plain mode=ro on a stopped one
        # (no sidecars), and never anything that can write.
        try:
            self._connection = connect_read_only(self.path)
        except (sqlite3.OperationalError, ValueError):
            self._connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=5)
        self._connection.row_factory = sqlite3.Row
        self._query = lambda sql, params=(): self._connection.execute(sql, tuple(params)).fetchall()
        self.profiles: dict[str, dict[str, Any]] = {
            json.loads(row["profile_json"])["id"]: json.loads(row["profile_json"])
            for row in self._query(
                "SELECT p.profile_json FROM model_endpoint_profile_versions p "
                "WHERE NOT EXISTS (SELECT 1 FROM model_endpoint_profile_versions n "
                "WHERE n.profile_id=p.profile_id AND n.version>p.version)")
        }
        self.cooldowns = self._read_cooldowns(now)
        self._policies: dict[str, dict[str, Any] | None] = {}

    def _read_cooldowns(self, now: datetime) -> dict[str, dict[str, Any]]:
        moment = now.astimezone(timezone.utc).isoformat(timespec="microseconds")
        try:
            rows = self._query(
                "SELECT c.profile_id AS profile_id, c.reason AS reason, "
                "c.failure_code AS failure_code, c.started_at AS started_at, "
                "c.until AS until FROM model_profile_cooldowns c "
                "WHERE c.cooldown_sequence=(SELECT MAX(d.cooldown_sequence) "
                "FROM model_profile_cooldowns d WHERE d.profile_id=c.profile_id) "
                "AND c.until>?", (moment,))
        except sqlite3.Error:
            return {}
        live: dict[str, dict[str, Any]] = {}
        for row in rows:
            try:
                served = self._query(
                    "SELECT 1 FROM model_profile_health_events WHERE profile_id=? "
                    "AND outcome='served' AND observed_at>=? LIMIT 1",
                    (row["profile_id"], row["started_at"]))
            except sqlite3.Error:
                served = []
            if not served:
                live[str(row["profile_id"])] = {key: row[key] for key in row.keys()}
        return live

    def close(self) -> None:
        self._connection.close()

    def policy(self, policy_version_ref: str) -> dict[str, Any] | None:
        if policy_version_ref not in self._policies:
            rows = self._query(
                "SELECT policy_json FROM model_routing_policy_versions "
                "WHERE policy_version_ref=?", (policy_version_ref,))
            self._policies[policy_version_ref] = (
                json.loads(rows[0]["policy_json"]) if rows else None)
        return self._policies[policy_version_ref]


def refusal_counts(state_dir: str | Path, *, now: datetime,
                   window: timedelta = REFUSAL_WINDOW) -> dict[str, dict[str, int]]:
    """Verifier refusals in the window, by verifier purpose.

    ``recorded``: envelopes carrying batch w's ``metadata.chain_refusal`` with
    reason ``verifier_not_independent``.  ``unrecorded``: the pre-batch-w
    shape of the same refusal -- a cockpit ``MODEL_ROUTE_REJECTED`` control
    failure with no route decision and no chain failures -- attributed by the
    cockpit work-order id, which names the purpose.
    """

    since = (now - window).astimezone(timezone.utc).isoformat(timespec="microseconds")
    counts: dict[str, dict[str, int]] = {}
    directory = Path(state_dir).expanduser()
    seen: set[str] = set()
    for name in ("scheduler.sqlite", "core.sqlite"):
        connection = _ro(directory / name)
        if connection is None:
            continue
        try:
            rows = connection.execute(
                "SELECT result_envelope_id, work_order_id, result_envelope_json "
                "FROM scheduler_result_envelopes WHERE created_at>=? "
                "AND outcome!='succeeded' AND (result_envelope_json LIKE '%chain_refusal%' "
                "OR result_envelope_json LIKE '%MODEL_ROUTE_REJECTED%')", (since,)
            ).fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            connection.close()
        for row in rows:
            if row["result_envelope_id"] in seen:
                continue
            seen.add(row["result_envelope_id"])
            try:
                envelope = json.loads(row["result_envelope_json"])
            except ValueError:
                continue
            metadata = envelope.get("metadata") or {}
            refusal = metadata.get("chain_refusal")
            if isinstance(refusal, Mapping):
                if refusal.get("reason") != "verifier_not_independent":
                    continue
                purpose, kind = str(refusal.get("purpose") or ""), "recorded"
            else:
                error = envelope.get("error") or {}
                match = _COCKPIT_WORK_RE.match(str(row["work_order_id"] or ""))
                if (error.get("code") != "MODEL_ROUTE_REJECTED" or match is None
                        or not metadata.get("control_plane_failure")
                        or metadata.get("route_decision_ref") or metadata.get("chain_failures")):
                    continue
                purpose, kind = match.group(1), "unrecorded"
            bucket = counts.setdefault(purpose, {"recorded": 0, "unrecorded": 0})
            bucket[kind] += 1
    return counts


def environment_report(
    state_dir: str | Path,
    *,
    now: datetime,
    pairs: Sequence[Mapping[str, Any]] | None = None,
    cockpit_model_config_path: str | Path | None = None,
) -> dict[str, Any]:
    """Every pair, evaluated against what this environment's lanes really pin."""

    from .model_selection import purpose_policy_bindings

    pairs = list(discover_pairs()["pairs"] if pairs is None else pairs)
    try:
        bindings = purpose_policy_bindings(
            state_dir, cockpit_model_config_path=cockpit_model_config_path)
    except Exception as exc:  # noqa: BLE001 - one unreadable environment is a row
        return {"available": False, "reason": f"模型配置读不出来：{exc}", "pairs": []}
    snapshots: dict[str, RouterSnapshot | None] = {}
    errors: list[str] = []

    def snapshot(router_db: Any) -> RouterSnapshot | None:
        if not isinstance(router_db, str):
            return None
        key = str(Path(router_db).expanduser())
        if key not in snapshots:
            try:
                snapshots[key] = RouterSnapshot(key, now=now)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{key}: {exc}")
                snapshots[key] = None
        return snapshots[key]

    def chain_for(purpose: str) -> tuple[dict[str, Any] | None, RouterSnapshot | None]:
        binding = bindings.get(purpose) or {}
        ref = binding.get("policy_version_ref")
        snap = snapshot(binding.get("model_router_db"))
        if not isinstance(ref, str) or snap is None:
            return None, snap
        policy = snap.policy(ref)
        if policy is None:
            return None, snap
        resolved = effective_chain(policy, purpose, profiles=snap.profiles)
        tier_links = declared_tier_chain(policy, resolved["tier"])
        return {**resolved, "policy_version_ref": ref,
                "tier_chain": list(tier_links or ()),
                "override": (policy.get("purpose_overrides") or {}).get(purpose)}, snap

    try:
        refusals = refusal_counts(state_dir, now=now)
        rows: list[dict[str, Any]] = []
        for pair in pairs:
            draft, draft_snap = chain_for(pair["draft_purpose"])
            verifier, verifier_snap = chain_for(pair["verifier_purpose"])
            counted = refusals.get(pair["verifier_purpose"]) or {}
            row: dict[str, Any] = {
                "draft_purpose": pair["draft_purpose"],
                "verifier_purpose": pair["verifier_purpose"],
                "refusals_48h": int(counted.get("recorded", 0)),
                "refusals_unrecorded_48h": int(counted.get("unrecorded", 0)),
            }
            if draft is None or verifier is None or draft_snap is None:
                row.update({"status": STATUS_UNBOUND, "draft_chain": [], "verifier_chain": [],
                            "draft_mode": None, "evaluation": None})
                rows.append(row)
                continue
            # The verifier's catalog is the draft router's unless they differ: a
            # family comparison only means something within one catalog.
            profiles = {**(verifier_snap.profiles if verifier_snap else {}), **draft_snap.profiles}
            cooldowns = {**(verifier_snap.cooldowns if verifier_snap else {}), **draft_snap.cooldowns}
            evaluation = evaluate_pair(draft_chain=draft["chain"], verifier_chain=verifier["chain"],
                                       profiles=profiles, cooldowns=cooldowns, now=now)
            row.update({
                "status": evaluation["status"],
                "draft_chain": list(draft["chain"]),
                "draft_mode": draft["mode"],
                "draft_tier": draft["tier"],
                "draft_policy_version_ref": draft["policy_version_ref"],
                "draft_tier_chain": draft["tier_chain"],
                "draft_override": draft["override"],
                "verifier_chain": list(verifier["chain"]),
                "verifier_mode": verifier["mode"],
                "evaluation": evaluation,
            })
            rows.append(row)
        catalog = next((value for value in snapshots.values() if value is not None), None)
        return {"available": True, "pairs": rows, "errors": errors,
                "profiles": dict(catalog.profiles) if catalog is not None else {},
                "cooldowns": dict(catalog.cooldowns) if catalog is not None else {}}
    finally:
        for value in snapshots.values():
            if value is not None:
                value.close()


def conflict_rows(report: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The pairs of one environment report that are in conflict right now."""

    return [row for row in report.get("pairs") or () if row.get("status") in CONFLICT_STATUSES]


__all__ = [
    "CONFLICT_STATUSES", "REFUSAL_WINDOW", "RouterSnapshot", "STATUS_CONFLICT",
    "STATUS_COOLDOWN", "STATUS_INDEPENDENT", "STATUS_NO_DRAFT", "STATUS_NO_VERIFIER",
    "STATUS_UNBOUND", "VERIFIER_EXTRA_PRODUCERS", "VERIFY_CAPABILITY", "conflict_rows",
    "discover_pairs", "environment_report", "evaluate_pair", "link_independence",
    "per_call_cost_usd", "recommend_order", "refusal_counts", "status_label",
]
