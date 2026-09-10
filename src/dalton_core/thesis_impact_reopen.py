"""Un-park thesis-impact: a live verifier pin, and an output that lands somewhere.

Two separate things stopped this lane, and they need separate fixes.

**The verifier pin died.**  ``VERIFIER_POLICY_REF`` is an immutable routing
policy that allows exactly one profile, ``profile:gemini-3-7-flash``, and the
broker has not offered that model for some time.  After the catalog sync the
profile is ``retired`` and verifier routing is *refused* with
``profile_retired`` -- which is the correct behaviour and is why the pin cannot
simply be edited: a phase pin is immutable on purpose, so repointing it means a
**new pinned version**, v2, chained to v1 by ``prior_version_ref``.  That is
what ``openclaw_verifier_policy_v2`` is, and it pins two profiles rather than
one: ``profile:zai-glm-5-3`` then ``profile:gemini-3-5-flash-lite``, which are
the second and third links of the ``verifier`` tier chain.

The chain's *first* link is ``profile:claude-fable-5-1``, and it is left out
deliberately.  The brain chain's second link is the same profile, so a producer
drawn from the brain tier and a verifier drawn from the first verifier link can
be the same family -- ``anthropic-claude-5`` -- and the router would refuse the
pair at route time with ``model_family_not_independent``.  A pin whose only
allowed profile is refused for a common producer is the same failure this
module exists to fix, one family along.  So the pin is the part of the chain
that is independent of *every* brain link by construction, and the router's
family filter still sits on top of it.

**The output had nowhere to go.**  ``eligible_assessment`` was the end of the
road: the producer said ``supports`` / ``weakens`` / ``no_change`` /
``insufficient`` about a Claim and a thesis, the verifier passed it, and
nothing downstream consumed the result -- because in August the only thing it
could have done was revise a thesis, and ADR-0001 forbade that.  ADR-0007
accepted on 2026-09-09 changed the destination, not the permission: the
producer's verdict becomes a **ThesisRevisionCandidate** in P14a's shape, which
is a proposal a person decides.  ``route_impact_to_candidate`` is that wiring,
and it goes through the public entry points of the two authorities it touches
so the candidate is indistinguishable from one the event-judgement lane wrote.

**The flag.**  ``service.json``'s ``thesis_impact.enabled`` was set to ``false``
on 2026-09-08 and became the only thing holding the lane still.  It should not
be a mood.  ``flag_state`` states the rule the owner should apply: *on iff the
pinned verifier policy version is live and the mission grants
``thesis_revision_candidate`` with its matching checkpoint*.  Either condition
missing and the lane produces judgements that cannot land, which is what parked
it in the first place.

Nothing here calls a model.  ``recheck_eligibility`` re-scores the frozen
30-example corpus from the recorded outputs already embedded in the fixture, so
it proves the release gate is wired and the thresholds have not drifted -- it
cannot prove the *new* verifier's detection rate, because those recorded
outputs came from a third family.  That is one live canary run, by the
integrator, and §"the one live run" of the report says exactly what it is.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from .model_deployment import ADAPTER_REF, VERIFIER_POLICY_REF, VERIFIER_PROFILE_ID
from .thesis_impact import IMPACTS

SCHEMA_VERSION = "0.1"

# The repointed pin.  A new version, never an edit: v1 is immutable and its
# hash is in every verification that ran under it.
REOPENED_VERIFIER_POLICY_REF = "model-routing-policy-version:dalton-openclaw-verifier:2"
REOPENED_VERIFIER_PROFILE_IDS: tuple[str, ...] = (
    "profile:zai-glm-5-3",
    "profile:gemini-3-5-flash-lite",
)
# Families of the brain chain, which is where a producer comes from.  Written
# down rather than derived so the independence assertion is a statement this
# module makes and a test can hold it to.
BRAIN_CHAIN_FAMILIES: tuple[str, ...] = ("openai-gpt-6", "anthropic-claude-5")
# What the routing report calls the producer for this work: the owner's model.
PRODUCER_PROFILE_ID = "profile:claude-fable-5-1"
PRODUCER_FAMILY = "anthropic-claude-5"

VERIFIER_CAPABILITY = "verify"

# The producer's four words, and what each one means as a proposal about a
# thesis.  ``insufficient`` is not a proposal -- it is the producer saying it
# could not tell, which the control plane already turns into one backlog
# question, and inventing a candidate from it would be putting words in its
# mouth.
IMPACT_DECISIONS: Mapping[str, str] = {
    "supports": "THESIS_STRENGTHENED",
    "weakens": "THESIS_WEAKENED",
    "no_change": "NO_CHANGE",
}
IMPACT_ACTIONS: Mapping[str, str] = {
    "supports": "revise_thesis",
    "weakens": "revise_thesis",
    "no_change": "no_change",
}
MISSION_WRITE_SCOPE = "thesis_revision_candidate"
MISSION_CHECKPOINT = "thesis_revision_candidate"

MAX_BECAUSE_CHARS = 1200


class ThesisImpactReopenError(RuntimeError):
    """Base error for reopening the thesis-impact lane."""


class ThesisImpactReopenValidationError(ThesisImpactReopenError, ValueError):
    """A request does not satisfy the closed contract."""


class ThesisImpactReopenConflict(ThesisImpactReopenError):
    """The reopen cannot proceed against what the ledger or the policy says."""


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ThesisImpactReopenValidationError("created_at must be timezone-aware")
    return value.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# the pin
# ---------------------------------------------------------------------------


def openclaw_verifier_policy_v2(*, created_at: datetime) -> dict[str, Any]:
    """Version 2 of the verifier phase pin, on families that are still served.

    Same shape as v1 in every other respect -- same adapter, same
    ``family_independence_capabilities``, same cost-then-version ordering --
    because the only thing being decided here is *which* profiles, and a pin
    that changed two things at once could not be reasoned about afterwards.
    """

    created = _utc(created_at).isoformat(timespec="microseconds")
    return {
        "schema_version": "0.1",
        "policy_version_ref": REOPENED_VERIFIER_POLICY_REF,
        "id": "model-routing-policy:dalton-openclaw-verifier",
        "version": 2,
        "created_at": created,
        "prior_version_ref": VERIFIER_POLICY_REF,
        "filters": {
            "allowed_profile_ids": list(REOPENED_VERIFIER_PROFILE_IDS),
            "allowed_providers": [],
            "allowed_families": [],
            "allowed_adapter_refs": [ADAPTER_REF],
            "required_modalities": ["text"],
            "family_independence_capabilities": ["verify", "adjudicate"],
        },
        "ordered_preferences": [
            {"field": "estimated_cost_usd", "direction": "asc"},
            {"field": "profile_version_ref", "direction": "asc"},
        ],
    }


def ensure_reopened_verifier_policy(
    router: Any, *, created_at: datetime
) -> dict[str, Any]:
    """Append v2 if the router has never seen it; say so either way.

    Registering a policy the router already holds is a duplicate rather than an
    error (``register_policy`` compares hashes), so this is safe to run on every
    install -- which is the property that lets the integrator run it once and
    not have to remember whether they already did.
    """

    from .model_deployment import openclaw_verifier_policy
    from .model_router import RoutingPolicyNotFound

    # A chain has to start where it started.  The router refuses a first
    # version that names a prior, so on a router that has never seen this
    # policy -- a fresh install, a test -- v1 is recreated from the same pure
    # function that wrote it originally.  On the live router v1 is already
    # there and this is one read: recreating it there would be a hash conflict,
    # which is the immutability working.
    seeded = False
    try:
        router.get_policy(VERIFIER_POLICY_REF)
    except RoutingPolicyNotFound:
        router.register_policy(openclaw_verifier_policy(created_at=created_at))
        seeded = True
    policy = openclaw_verifier_policy_v2(created_at=created_at)
    result = router.register_policy(policy)
    return {
        "policy_version_ref": REOPENED_VERIFIER_POLICY_REF,
        "prior_version_ref": VERIFIER_POLICY_REF,
        "status": result.get("status", "unknown"),
        "seeded_prior_version": seeded,
        "allowed_profile_ids": list(REOPENED_VERIFIER_PROFILE_IDS),
    }


def _latest_profiles(router: Any) -> dict[str, dict[str, Any]]:
    rows = router.connection.execute(
        "SELECT profile_id, profile_json FROM model_endpoint_profile_versions "
        "ORDER BY rowid DESC"
    ).fetchall()
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        latest.setdefault(row["profile_id"], json.loads(row["profile_json"]))
    return latest


def independence_report(
    router: Any,
    *,
    policy_version_ref: str = REOPENED_VERIFIER_POLICY_REF,
    producer_families: Sequence[str] = BRAIN_CHAIN_FAMILIES,
) -> dict[str, Any]:
    """Would every profile this pin allows survive the router's family filter?

    The predicate the router applies is one line -- ``profile["family"] ==
    producer_family`` -- and this asks it ahead of time, for every producer the
    brain chain can serve, so a pin that is independent of the producer we
    happen to have today and not of the one we fall back to tomorrow is caught
    at install rather than at 3am.
    """

    policy = router.get_policy(policy_version_ref)
    allowed = list(policy["filters"]["allowed_profile_ids"])
    profiles = _latest_profiles(router)
    families = list(dict.fromkeys(producer_families))
    entries: list[dict[str, Any]] = []
    for profile_id in allowed:
        profile = profiles.get(profile_id)
        collisions = [] if profile is None else [
            family for family in families if profile["family"] == family
        ]
        entries.append({
            "profile_id": profile_id,
            "registered": profile is not None,
            "family": None if profile is None else profile["family"],
            "status": None if profile is None else profile.get("status", "live"),
            "capability": (
                None if profile is None
                else VERIFIER_CAPABILITY in profile["capabilities"]
            ),
            "collides_with": collisions,
            "independent": profile is not None and not collisions,
        })
    usable = [
        entry for entry in entries
        if entry["independent"] and entry["status"] != "retired" and entry["capability"]
    ]
    reasons: list[str] = []
    if not entries:
        reasons.append("the pin allows no profile at all")
    for entry in entries:
        if not entry["registered"]:
            reasons.append(f"{entry['profile_id']} is not registered with the router")
        elif entry["status"] == "retired":
            reasons.append(f"{entry['profile_id']} is retired")
        elif entry["collides_with"]:
            reasons.append(
                f"{entry['profile_id']} is family {entry['family']}, which the "
                f"brain chain also serves"
            )
        elif not entry["capability"]:
            reasons.append(f"{entry['profile_id']} does not declare verify")
    return {
        "schema_version": SCHEMA_VERSION,
        "policy_version_ref": policy_version_ref,
        "producer_families": families,
        "profiles": entries,
        "usable_profile_ids": [entry["profile_id"] for entry in usable],
        "independent": bool(usable),
        "live": bool(usable),
        "reasons": sorted(set(reasons)),
    }


# ---------------------------------------------------------------------------
# the eligibility re-check, offline
# ---------------------------------------------------------------------------


def gold_output_map(corpus: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """What a perfect verifier would have said about all thirty cases.

    Not a claim about any model.  It is the input that makes the *scorer*
    exercise every branch it has -- one output per case, every seeded condition
    named with its own severity -- so "the release gate is wired and would
    unlock" is a thing this repository can assert without a broker.
    """

    from .thesis_impact import VERIFIER_FINDING_SEVERITIES

    outputs: dict[str, dict[str, Any]] = {}
    for case in corpus["cases"]:
        gold = case["gold"]
        findings = [
            {
                "code": code,
                "severity": VERIFIER_FINDING_SEVERITIES[code],
                "detail": "Seeded condition is present in the exact quoted input.",
                "expected_impact": (
                    gold["expected_impact"] if code == "impact_mismatch" else None
                ),
            }
            for code in gold["required_finding_codes"]
        ]
        outputs[case["id"]] = {
            "schema_version": "0.2",
            "assessment_ref": case["input"]["assessment"]["id"],
            "assessment_hash": case["input"]["assessment"]["content_hash"],
            "verdict": gold["verdict"],
            "findings": findings,
        }
    return outputs


def recheck_eligibility(*, source: str = "observed") -> dict[str, Any]:
    """Re-score the frozen 30-example corpus offline, two ways.

    No model call, no broker socket.

    ``source="observed"`` scores the outputs the fixture actually recorded, and
    the answer is the true state of the release gate today: **one** of the
    thirty cases has a recorded output -- the Gate-2 false positive from August
    -- so the gate is ineligible for *coverage*, not because a model failed.
    That is worth knowing before anyone reads a green light into a re-run.

    ``source="gold"`` scores a perfect verifier's answers, which proves the
    other half: the corpus, its thresholds (30 cases, 90% detection, zero
    high-severity misses) and the scorer are wired such that the gate *can*
    unlock.  Neither says anything about the newly pinned verifier -- the one
    recorded family is a third one -- and ``covers_the_new_pin`` is ``False``
    in both, because that is one live canary run.
    """

    from .thesis_impact_calibration import (
        load_frozen_calibration_corpus,
        observed_output_map,
        score_verifier_outputs,
    )

    if source not in ("observed", "gold"):
        raise ThesisImpactReopenValidationError("source must be observed or gold")
    corpus = load_frozen_calibration_corpus()
    outputs = (
        observed_output_map(corpus) if source == "observed" else gold_output_map(corpus)
    )
    score = score_verifier_outputs(outputs, corpus=corpus)
    families = sorted({
        str(record.get("model_family") or record.get("family") or "unknown")
        for case in corpus["cases"]
        for record in (case.get("observed_outputs") or ())
    })
    return {
        "schema_version": SCHEMA_VERSION,
        "source": source,
        "corpus_ref": corpus["id"],
        "frozen_at": corpus["frozen_at"],
        "case_count": len(corpus["cases"]),
        "scored_case_count": len(outputs),
        "observed_families": families,
        "score": score,
        "automation_eligible": bool(score["automation_eligible"]),
        "reasons": list(score["automation_ineligibility_reasons"]),
        "pinned_profile_ids": list(REOPENED_VERIFIER_PROFILE_IDS),
        "covers_the_new_pin": False,
        "note": (
            "离线重跑，没有模型调用：observed 路径用 fixture 里录下来的输出（家族："
            + "、".join(families)
            + "），gold 路径证明发布门本身能解锁。两条都不覆盖新钉的 verifier，"
            "那要集成时跑一次 live canary。"
        ),
    }


# ---------------------------------------------------------------------------
# the flag
# ---------------------------------------------------------------------------


def mission_grants_candidate(mission: Mapping[str, Any]) -> tuple[bool, list[str]]:
    """Whether a mission may write a candidate *and* carries its checkpoint."""

    reasons: list[str] = []
    autonomy = mission.get("autonomy") if isinstance(mission, Mapping) else None
    scopes = (autonomy or {}).get("may_write") or ()
    checkpoints = (autonomy or {}).get("human_checkpoints") or ()
    if MISSION_WRITE_SCOPE not in scopes:
        reasons.append(f"the mission does not grant {MISSION_WRITE_SCOPE} (ADR-0007)")
    if MISSION_CHECKPOINT not in checkpoints:
        reasons.append(
            f"the mission does not carry the {MISSION_CHECKPOINT} human checkpoint "
            "(ADR-0007)"
        )
    return (not reasons), reasons


def flag_state(
    *, independence: Mapping[str, Any], mission: Mapping[str, Any]
) -> dict[str, Any]:
    """What ``service.json``'s ``thesis_impact.enabled`` should be, and why.

    A rule rather than a switch.  On 2026-09-08 the flag was flipped off
    because a policy rollover made every result ineligible; the fix is not to
    flip it back but to make the flag a statement about two facts that can be
    checked, so the next rollover parks the lane by itself and the reason is
    already written down.
    """

    granted, grant_reasons = mission_grants_candidate(mission)
    reasons = list(independence.get("reasons") or ()) + grant_reasons
    enabled = bool(independence.get("live")) and granted
    return {
        "schema_version": SCHEMA_VERSION,
        "flag": "thesis_impact.enabled",
        "enabled": enabled,
        "policy_version_ref": independence.get("policy_version_ref"),
        "policy_live": bool(independence.get("live")),
        "mission_grants_candidate": granted,
        "mission_version_ref": mission.get("id"),
        "reasons": sorted(set(reasons)),
        "retired_pin": VERIFIER_PROFILE_ID,
    }


# ---------------------------------------------------------------------------
# the destination
# ---------------------------------------------------------------------------


def decision_for_impact(impact: str) -> str:
    if impact not in IMPACTS:
        raise ThesisImpactReopenValidationError(
            f"impact must be one of {sorted(IMPACTS)}"
        )
    if impact == "insufficient":
        raise ThesisImpactReopenConflict(
            "an insufficient assessment is not a proposal about the thesis; it "
            "stays on the control plane's backlog path"
        )
    return IMPACT_DECISIONS[impact]


def _because(assessment: Mapping[str, Any], verification: Mapping[str, Any]) -> str:
    """The producer's own words, plus the fact that somebody else checked them.

    Read off the assessment's top level, which is where ``record_assessment``
    spreads the model's closed output; the ``output`` fallback is for a caller
    that still has the raw envelope in hand.
    """

    output = assessment.get("output") or assessment
    parts = [
        str(output.get("driver_statement") or "").strip(),
        str(output.get("rationale") or "").strip(),
    ]
    verdict = verification.get("verdict") or verification.get("status")
    parts.append(f"独立核验：{verdict}")
    return "　".join(part for part in parts if part)[:MAX_BECAUSE_CHARS]


def route_impact_to_candidate(
    store: Any,
    *,
    assessment: Mapping[str, Any],
    verification: Mapping[str, Any],
    thesis: Mapping[str, Any],
    claim: Mapping[str, Any],
    company_ref: str,
    mission: Mapping[str, Any],
    actor_ref: str,
    occurred_at: str | None = None,
) -> dict[str, Any]:
    """Turn one verified assessment into one ThesisRevisionCandidate.

    Through the public entry points of both authorities, and in P14a's order:
    the Claim becomes a ``ResearchEvent`` of kind ``claim``, the producer's
    verdict and the verifier's pass become one ``EventJudgement`` bound to it,
    and only then does the candidate exist.  Doing it any other way would put a
    candidate in the ledger with no event behind it, and the cockpit's ledger
    of "what did we decide about what" would have a hole in it exactly where
    this lane's proposals are.

    Returns a status: ``candidate`` when one was written, ``no_change`` when the
    producer said nothing moved (recorded, because *why didn't we change our
    mind* is the question a weekly review needs), ``queued`` when the mission
    does not grant the scope, ``skipped`` for ``insufficient``.
    """

    from .event_judgement import EventJudgementAuthority
    from .research_event import ResearchEventAuthority, record_event

    impact = str(assessment.get("impact") or "")
    if impact == "insufficient":
        return {
            "status": "skipped",
            "reason": "insufficient assessments go to the question backlog, not to a candidate",
            "impact": impact,
        }
    decision = decision_for_impact(impact)
    verdict = verification.get("verdict") or verification.get("status")
    if verdict != "pass":
        raise ThesisImpactReopenConflict(
            "only an independently verified assessment may become a candidate"
        )

    events = ResearchEventAuthority(store)
    judgements = EventJudgementAuthority(store)
    claim_ref = claim["id"]
    event = record_event(
        events,
        company_ref=company_ref,
        kind="claim",
        occurred_at=occurred_at or claim.get("created_at") or assessment["created_at"],
        source_refs=[assessment["id"], claim_ref],
        payload={
            "claim_version_ref": claim_ref,
            "claim_ref": claim.get("claim_ref"),
            "metric_ref": claim.get("metric_or_aspect"),
            "period": claim.get("period"),
            "statement": (claim.get("normalized_statement") or "")[:600],
            "source_ref": assessment["id"],
        },
        mission=mission,
        actor_ref=actor_ref,
    )
    because = _because(assessment, verification)
    judgement = judgements.record(
        event=event,
        judgement={
            "decision": decision,
            "action": IMPACT_ACTIONS[impact],
            "driver_refs": [],
            "thesis_refs": [thesis["id"]],
            "because": because,
            "citations": [claim_ref],
            "note": None,
            "research_question": None,
            "forecast_change": None,
            # The judgement id hashes on this, so binding it to the
            # producer's result envelope makes one assessment produce one
            # judgement however many times this runs. The cost is zero here
            # and that is not a rounding: the producer call was paid for and
            # booked in the thesis-impact budget ledger, and booking it a
            # second time against the event-response pool would double-count
            # it against a cap it never spent.
            "model": {
                "work_order_ref": assessment["producer_result_envelope_ref"],
                "cost_micros": 0,
            },
        },
        verification={
            "status": "verified",
            "verdict": verdict,
            "findings": list(verification.get("findings") or ()),
            "independence": verification.get("independence"),
            "reason": None,
            "model": {
                "work_order_ref": verification["id"], "cost_micros": 0,
            },
        },
        effect={
            "kind": "thesis_impact_reopen",
            "assessment_ref": assessment["id"],
            "verification_ref": verification["id"],
            "impact": impact,
        },
        mission=mission,
        actor_ref=actor_ref,
    )
    if decision == "NO_CHANGE":
        return {
            "status": "no_change",
            "impact": impact,
            "event": event,
            "judgement": judgement,
            "candidate": None,
        }
    granted, reasons = mission_grants_candidate(mission)
    if not granted:
        return {
            "status": "queued",
            "impact": impact,
            "reason": "；".join(reasons),
            "event": event,
            "judgement": judgement,
            "candidate": None,
        }
    candidate = judgements.record_thesis_candidate(
        judgement_ref=judgement["id"],
        thesis=thesis,
        company_ref=company_ref,
        decision=decision,
        because=because,
        evidence_refs=[claim_ref, assessment["id"], verification["id"]],
        falsifier_ref=None,
        proposed_statement=None,
        proposed_confidence=None,
        mission=mission,
        actor_ref=actor_ref,
        reflection_ref=None,
    )
    return {
        "status": "candidate",
        "impact": impact,
        "decision": decision,
        "event": event,
        "judgement": judgement,
        "candidate": candidate,
    }


__all__ = [
    "BRAIN_CHAIN_FAMILIES",
    "IMPACT_ACTIONS",
    "IMPACT_DECISIONS",
    "MISSION_CHECKPOINT",
    "MISSION_WRITE_SCOPE",
    "PRODUCER_FAMILY",
    "PRODUCER_PROFILE_ID",
    "REOPENED_VERIFIER_POLICY_REF",
    "REOPENED_VERIFIER_PROFILE_IDS",
    "SCHEMA_VERSION",
    "ThesisImpactReopenConflict",
    "ThesisImpactReopenError",
    "ThesisImpactReopenValidationError",
    "decision_for_impact",
    "ensure_reopened_verifier_policy",
    "flag_state",
    "gold_output_map",
    "independence_report",
    "mission_grants_candidate",
    "openclaw_verifier_policy_v2",
    "recheck_eligibility",
    "route_impact_to_candidate",
]
