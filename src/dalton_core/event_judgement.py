"""P14a: the brain. One event, one bounded call, one decision, one record.

Everything else in this slice is plumbing that makes this call possible: the
event ledger gives it one thing with a name, the cadence policy decides how
often things arrive, the capability map tells it where evidence lives.  This
module is where the system finally says *what it thinks* about something that
happened -- and, just as importantly, records that it thought nothing needed
to change and why, so the weekly review can ask the question that matters more
than any revision: why did we not change our mind about this?

Four properties are the whole design.

**The decision is two closed words, not prose.**  The model returns one word
from the Playbook's frozen ``DECISION_VOCABULARY`` -- the first code that has
ever consumed it -- and one word from the action vocabulary below saying what
should happen.  A frozen table says which actions each decision permits, so
"NO_CHANGE, therefore revise the thesis" is a refusal rather than something a
downstream reader has to notice.

**Nothing is repaired.**  An output with an extra key, a citation to a ref
that was not in the prompt, a driver this company does not have, or a note
where no note was asked for is ``refused`` as a whole.  A judgement that has
to be cleaned up before it can be read is not a judgement (``thesis_impact``'s
finding, and ``research_quality_score`` reached it again independently).

**The verifier is a different model family and answers one question.**  Not a
second grader: a verdict on whether the decision follows from what was shown.
The family is resolved from the route the broker actually took, not asserted
by configuration, and an unresolvable family fails closed -- a verifier that
might be the same model is not an independent verifier.

**Only a revise-shaped decision touches an entry point.**  ADR-0008's split:
the authorities never publish because they noticed something; this lane calls
``revise_assumptions`` because it decided to, carrying the decision word into
the version's own ``change_reason``.  Where the mission does not grant the
write, the decision becomes a proposal for a person instead of being dropped.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable

from .cockpit_model import (
    CockpitModelError,
    lane_status_for,
    register_purpose,
    unwrap_json_object,
)
from .research_event import EVIDENCE_TIERS, worst_tier
from .research_playbook import DECISION_VOCABULARY
from .store import canonical_json, content_hash

SCHEMA_VERSION = "0.1"
_SCHEMA_PATH = Path(__file__).with_name("event_judgement_schema.sql")

# Registered at import: anything that can call ``judge()`` has already run this
# line, and a purpose registered after the call that uses it is not registered.
PURPOSE = register_purpose("event_judgement")
# Named separately from the judgement because it is a different question with a
# different output shape, and because the owner should be able to read what the
# reflection layer costs as its own line in the day ledger rather than as part
# of the judgement's.
REFLECTION_PURPOSE = register_purpose("thesis_reflection")

# When a reflection is owed. Two triggers and no third: we changed our mind, or
# the market kept disagreeing with us. A no_change on a divergence still owes
# one -- "why we are holding" is the answer the weekly review most needs and
# the one nothing in this system has ever written down.
REFLECTION_ACTIONS: frozenset[str] = frozenset({
    "revise_forecast", "revise_thesis", "revise_dossier",
})
REFLECTION_TRIGGER_KINDS: frozenset[str] = frozenset({"price_divergence"})
MAX_FOLLOWUPS = 4
MAX_MISSED_DEBATES = 4

# What should happen because of this event.  Closed, and deliberately smaller
# than the space of things that could happen: every one of these six has an
# implementation below, and an action with no implementation is a promise the
# cockpit would show and nothing would keep.
ACTION_VOCABULARY: tuple[str, ...] = (
    "no_change", "note", "research", "revise_forecast", "revise_thesis", "revise_dossier",
)

# Which actions each of the Playbook's five words permits.  The pairs that are
# missing are the ones that contradict themselves: NO_CHANGE cannot revise
# anything, and a thesis that broke cannot be answered with a note.
DECISION_ACTIONS: Mapping[str, frozenset[str]] = MappingProxyType({
    "NO_CHANGE": frozenset({"no_change", "note", "research"}),
    "THESIS_STRENGTHENED": frozenset({
        "note", "research", "revise_forecast", "revise_thesis", "revise_dossier",
    }),
    "THESIS_WEAKENED": frozenset({
        "note", "research", "revise_forecast", "revise_thesis", "revise_dossier",
    }),
    "THESIS_BROKEN": frozenset({"research", "revise_forecast", "revise_thesis"}),
    "NEW_THESIS": frozenset({"research", "revise_thesis"}),
})

VERIFIER_VERDICTS: tuple[str, ...] = ("pass", "reject")
VERIFIER_FINDING_CODES: tuple[str, ...] = (
    "decision_not_supported_by_the_event",
    "action_does_not_follow_from_the_decision",
    "citation_does_not_say_what_is_claimed",
    "evidence_tier_overweighted",
    "driver_or_thesis_link_is_asserted_not_shown",
)

# Budget.  ``event_response`` is C2's name for this pool; the share is
# deliberately small because a judgement call is cheap and a lane that could
# spend a quarter of the day's money on reading news would be competing with
# the extraction lane for the thing the mission is actually for.
POOL_NAME = "event_response"
POOL_SHARE = Decimal("0.15")

MAX_BECAUSE_CHARS = 1200
MAX_NOTE_CHARS = 1200
MAX_NOTE_SENTENCES = 4
MAX_REFS = 12
MAX_RECENT_JUDGEMENTS = 5
MAX_CLAIMS_IN_PROMPT = 12
MAX_PROMPT_CHARS = 24_000
# Payload fields that hold a ref, named rather than sniffed. Testing a string
# for a colon would let "3.0:1" or a title with a time in it become a citable
# ref, and the whole point of the permitted set is that a citation names
# something this system can resolve.
REF_PAYLOAD_FIELDS: frozenset[str] = frozenset({
    "document_ref", "discovery_ref", "claim_version_ref", "claim_ref",
    "reconciliation_ref", "forecast_line_version_ref", "price_version_ref",
    "invocation_ref", "calendar_version_ref", "thesis_ref", "source_ref",
})
_SENTENCE_RE = re.compile(r"[^.!?。！？]+[.!?。！？]?")


class EventJudgementError(RuntimeError):
    """Base error for the judgement lane."""


class EventJudgementValidationError(EventJudgementError, ValueError):
    """A model output or a request does not satisfy the closed contract."""


class EventJudgementConflict(EventJudgementError):
    """A request conflicts with the immutable ledger."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any, name: str, *, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EventJudgementValidationError(f"{name} must be non-empty text")
    value = value.strip()
    if len(value) > maximum:
        raise EventJudgementValidationError(f"{name} must be at most {maximum} characters")
    return value


# ---------------------------------------------------------------------------
# the prompt
# ---------------------------------------------------------------------------


def _payload_lines(event: Mapping[str, Any]) -> list[str]:
    return [
        f"  {field} = {value}"
        for field, value in sorted(event["payload"].items())
        if value is not None
    ]


def build_judge_prompt(context: Mapping[str, Any]) -> str:
    """The whole table the brain reads: event, theses, drivers, history, sources.

    A table rather than a narrative because every line of it has a ref the
    answer may cite, and a narrative would let the model cite the narrative.
    """

    event = context["event"]
    lines = [
        "You are the covering analyst for this company. One thing happened. Decide what,",
        "if anything, should change because of it. Deciding that nothing should change is a",
        "real answer and is often the right one -- but you must say why.",
        "",
        "Weigh the event by its evidence tier. A company-filed number and a crowd post are",
        "not the same kind of fact; a sales note is what the desk heard, not what happened.",
        "",
        "Standing instruction: agreeing with the market is worth nothing. In `because`,",
        "say whether our view differs from what the price and the street imply, and if it",
        "does, name the observable that would move the market toward our view. If the",
        "event is a price_divergence, the price has been running against our thesis: say",
        "what we may have missed rather than restating the thesis.",
        "",
        f"## The event ({event['kind']}, evidence tier: {event['evidence_tier']})",
        f"ref: {event['id']}",
        f"occurred_at: {event['occurred_at']}",
        *_payload_lines(event),
        f"source refs: {', '.join(event['source_refs'])}",
        "",
        f"## Company: {context.get('ticker') or context['company_ref']} ({context['company_ref']})",
    ]
    theses = context.get("theses") or ()
    lines.append("")
    lines.append("## Theses in force")
    if not theses:
        lines.append("(none admitted for this company)")
    for thesis in theses:
        lines.append(
            f"- {thesis['ref']} [confidence {thesis.get('confidence')}] {thesis['statement']}"
        )
        if thesis.get("mechanism"):
            lines.append(f"    mechanism: {thesis['mechanism']}")
    drivers = context.get("drivers") or ()
    lines.append("")
    lines.append("## Forecast drivers and what we currently assume")
    if not drivers:
        lines.append("(this company has no driver model yet)")
    for driver in drivers:
        lines.append(
            f"- {driver['ref']} ({driver.get('role') or 'unroled'}, {driver.get('status')})"
            f" {driver.get('label') or ''}".rstrip()
        )
        for assumption in driver.get("assumptions") or ():
            lines.append(
                f"    {assumption['period_end']}: {assumption['measure']} = "
                f"{assumption['value']} ({assumption['kind']})"
            )
    history = context.get("recent_judgements") or ()
    lines.append("")
    lines.append("## The last decisions taken on this company")
    if not history:
        lines.append("(none yet)")
    for row in history:
        lines.append(
            f"- {row.get('kind')} -> {row['decision']} / {row['action']}: {row['because']}"
        )
    claims = context.get("claims") or ()
    lines.append("")
    lines.append("## Canonical Claims for this aspect")
    if not claims:
        lines.append("(none)")
    for claim in claims:
        lines.append(f"- {claim['ref']}: {claim['statement']}")
    lines.append("")
    lines.append("## Where evidence can be got, if you decide research is needed")
    lines.append(context.get("source_table") or "(no source map)")
    lines.append("")
    lines.append("Return raw JSON only, nothing else, exactly these keys:")
    lines.append(
        '{"decision": "' + "|".join(DECISION_VOCABULARY) + '",'
        ' "action": "' + "|".join(ACTION_VOCABULARY) + '",'
        ' "driver_refs": [], "thesis_refs": [], "because": "<why, in a few sentences>",'
        ' "citations": ["<refs printed above, and only those>"]}'
    )
    lines.append(
        'Add "note": "<at most four sentences>" if and only if action is "note".'
    )
    lines.append(
        'Add "forecast_change": {"driver_ref": "...", "period_end": "YYYY-MM-DD",'
        ' "value": "<decimal>", "because": "..."} if and only if action is "revise_forecast".'
    )
    lines.append(
        'Add "research_question": "<one question>" if and only if action is "research".'
    )
    lines.append("Every citation must be a ref printed above. Do not invent a ref.")
    prompt = "\n".join(lines)
    return prompt[:MAX_PROMPT_CHARS]


def allowed_refs(context: Mapping[str, Any]) -> set[str]:
    """Every ref the prompt printed.  A citation outside this set is a refusal."""

    refs = {context["event"]["id"], *context["event"]["source_refs"]}
    for field in REF_PAYLOAD_FIELDS:
        value = context["event"]["payload"].get(field)
        if isinstance(value, str) and value:
            refs.add(value)
    refs.update(thesis["ref"] for thesis in context.get("theses") or ())
    refs.update(driver["ref"] for driver in context.get("drivers") or ())
    refs.update(claim["ref"] for claim in context.get("claims") or ())
    return refs


# ---------------------------------------------------------------------------
# output contract
# ---------------------------------------------------------------------------


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE_RE.findall(text) if part.strip()]


def validate_judge_output(value: Any, context: Mapping[str, Any]) -> dict[str, Any]:
    """The closed shape.  Anything else is refused whole, never repaired."""

    if not isinstance(value, Mapping):
        raise EventJudgementValidationError("the model did not return an object")
    decision = value.get("decision")
    if decision not in DECISION_VOCABULARY:
        raise EventJudgementValidationError(
            f"decision must be one of {list(DECISION_VOCABULARY)}, got {decision!r}"
        )
    action = value.get("action")
    if action not in ACTION_VOCABULARY:
        raise EventJudgementValidationError(
            f"action must be one of {list(ACTION_VOCABULARY)}, got {action!r}"
        )
    if action not in DECISION_ACTIONS[decision]:
        raise EventJudgementValidationError(
            f"{decision} does not permit {action}; permitted: "
            f"{sorted(DECISION_ACTIONS[decision])}"
        )
    required = {"decision", "action", "driver_refs", "thesis_refs", "because", "citations"}
    conditional = {
        "note": "note", "revise_forecast": "forecast_change", "research": "research_question",
    }
    expected = set(required)
    if action in conditional:
        expected.add(conditional[action])
    extra = sorted(set(value) - expected)
    if extra:
        raise EventJudgementValidationError(
            f"the model returned keys the contract does not have: {extra}"
        )
    missing = sorted(expected - set(value))
    if missing:
        raise EventJudgementValidationError(f"the model omitted required keys: {missing}")

    permitted = allowed_refs(context)
    driver_refs = _ref_list(value["driver_refs"], "driver_refs")
    thesis_refs = _ref_list(value["thesis_refs"], "thesis_refs")
    citations = _ref_list(value["citations"], "citations")
    known_drivers = {driver["ref"] for driver in context.get("drivers") or ()}
    known_theses = {thesis["ref"] for thesis in context.get("theses") or ()}
    stray = sorted(set(driver_refs) - known_drivers)
    if stray:
        raise EventJudgementValidationError(f"driver_refs names drivers this model has not: {stray}")
    stray = sorted(set(thesis_refs) - known_theses)
    if stray:
        raise EventJudgementValidationError(f"thesis_refs names theses not shown: {stray}")
    stray = sorted(set(citations) - permitted)
    if stray:
        raise EventJudgementValidationError(f"citations names refs that were not shown: {stray}")
    if action != "no_change" and not citations:
        raise EventJudgementValidationError(
            "an action other than no_change must cite what it rests on"
        )
    result: dict[str, Any] = {
        "decision": decision,
        "action": action,
        "driver_refs": driver_refs,
        "thesis_refs": thesis_refs,
        "because": _text(value["because"], "because", maximum=MAX_BECAUSE_CHARS),
        "citations": citations,
    }
    if action == "note":
        note = _text(value["note"], "note", maximum=MAX_NOTE_CHARS)
        if len(_sentences(note)) > MAX_NOTE_SENTENCES:
            raise EventJudgementValidationError(
                f"a note is at most {MAX_NOTE_SENTENCES} sentences"
            )
        result["note"] = note
    if action == "research":
        result["research_question"] = _text(
            value["research_question"], "research_question", maximum=500
        )
    if action == "revise_forecast":
        change = value["forecast_change"]
        if not isinstance(change, Mapping) or set(change) != {
            "driver_ref", "period_end", "value", "because"
        }:
            raise EventJudgementValidationError(
                "forecast_change must be exactly driver_ref, period_end, value and because"
            )
        driver_ref = _text(change["driver_ref"], "forecast_change.driver_ref")
        if driver_ref not in known_drivers:
            raise EventJudgementValidationError(
                "forecast_change names a driver this model has not"
            )
        result["forecast_change"] = {
            "driver_ref": driver_ref,
            "period_end": _text(change["period_end"], "forecast_change.period_end", maximum=32),
            "value": _text(str(change["value"]), "forecast_change.value", maximum=64),
            "because": _text(change["because"], "forecast_change.because", maximum=MAX_BECAUSE_CHARS),
        }
    return result


def _ref_list(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_REFS:
        raise EventJudgementValidationError(f"{name} must be a list of at most {MAX_REFS} refs")
    return list(dict.fromkeys(_text(item, f"{name}[]") for item in value))


# ---------------------------------------------------------------------------
# reflection: where the market disagrees with us, and why we might be wrong
# ---------------------------------------------------------------------------


def reflection_is_owed(event: Mapping[str, Any], judgement: Mapping[str, Any]) -> bool:
    """Two triggers: we changed our mind, or the market kept disagreeing."""

    if event.get("kind") in REFLECTION_TRIGGER_KINDS:
        return True
    return judgement.get("action") in REFLECTION_ACTIONS


def market_view_rows(
    events: Sequence[Mapping[str, Any]], *, limit: int = 8
) -> list[dict[str, Any]]:
    """What this Core actually holds about the market's view of the company.

    Rating changes, the desk's notes and the crowd, newest first.  There is no
    consensus authority yet (P11b is Wave 2), so the honest answer is often a
    short list and sometimes an empty one -- which the prompt says out loud
    rather than letting the model infer a street view from four news items.
    """

    wanted = ("rating_change", "sales_note", "crowd_post", "news")
    rows = [event for event in events if event.get("kind") in wanted]
    rows.sort(key=lambda event: str(event.get("occurred_at")), reverse=True)
    return [
        {"ref": event["id"], "kind": event["kind"], "tier": event["evidence_tier"],
         "payload": {key: value for key, value in event["payload"].items()
                     if value is not None}}
        for event in rows[:limit]
    ]


def build_reflection_prompt(
    context: Mapping[str, Any], judgement: Mapping[str, Any]
) -> str:
    """The self-reflection call: what we expected, what happened, what we missed."""

    event = context["event"]
    market = context.get("market_view") or ()
    lines = [
        "You are the covering analyst writing down what you got wrong, or what you are",
        "holding through and why. This is not a defence of the thesis and it changes",
        "nothing: a person reads it beside the revision candidate.",
        "",
        "The point of the exercise: if the market is bullish and we are bullish, our view",
        "is worth nothing. Say where the market's pricing is wrong and what would bring",
        "the market to our view -- or say that we now think the market was right.",
        "",
        f"## Trigger ({event['kind']}, ref {event['id']})",
        *_payload_lines(event),
        "",
        f"## The decision just taken: {judgement['decision']} / {judgement['action']}",
        judgement["because"],
        "",
        "## Theses in force",
    ]
    for thesis in context.get("theses") or ():
        lines.append(
            f"- {thesis['ref']} [confidence {thesis.get('confidence')}] {thesis['statement']}"
        )
    if not context.get("theses"):
        lines.append("(none admitted for this company)")
    lines.append("")
    lines.append("## What this system holds about the market's view")
    if market:
        for row in market:
            lines.append(f"- {row['ref']} ({row['kind']}, {row['tier']}): {row['payload']}")
    else:
        lines.append(
            "(nothing: no consensus authority exists yet and no rating change, sales note "
            "or crowd post has been recorded for this company. Say so; do not infer a "
            "street view from the absence of one.)"
        )
    lines.append("")
    lines.append("## The last decisions taken on this company")
    for row in context.get("recent_judgements") or ():
        lines.append(f"- {row.get('kind')} -> {row['decision']} / {row['action']}: {row['because']}")
    lines.append("")
    lines.append("## Where evidence can be got")
    lines.append(context.get("source_table") or "(no source map)")
    lines.append("")
    lines.append("Return raw JSON only, nothing else, exactly these keys:")
    lines.append(
        '{"thesis_refs": [], "what_we_expected": "...", "what_happened": "...",'
        ' "why": "...", "citations": ["<refs printed above, and only those>"],'
        ' "missed_debates": [{"question": "...", "refs": []}],'
        ' "followup_tracking": [{"source_key": "...", "interval_seconds": 0,'
        ' "because": "..."}],'
        ' "followup_research": [{"question": "...", "wants": "..."}],'
        ' "market_view_vs_ours": {"available": true, "our_direction": "...",'
        ' "summary": "...", "refs": []},'
        ' "convergence_pathway": "..."}'
    )
    lines.append(
        "market_view_vs_ours.available is false when this system holds nothing about the "
        "street's view; then summary says that, refs is empty, and you do not guess."
    )
    lines.append(
        "followup_tracking names a source_key from the table above and a proposed interval "
        "in seconds; followup_research names a question. Both are proposals a later step "
        "decides on -- nothing you write here changes a cadence or opens a task."
    )
    lines.append("Every citation must be a ref printed above. Do not invent a ref.")
    return "\n".join(lines)[:MAX_PROMPT_CHARS]


def validate_reflection_output(value: Any, context: Mapping[str, Any]) -> dict[str, Any]:
    """The closed shape.  Refused whole; a reflection is not repaired either."""

    if not isinstance(value, Mapping):
        raise EventJudgementValidationError("the model did not return an object")
    expected = {
        "thesis_refs", "what_we_expected", "what_happened", "why", "citations",
        "missed_debates", "followup_tracking", "followup_research",
        "market_view_vs_ours", "convergence_pathway",
    }
    extra = sorted(set(value) - expected)
    if extra:
        raise EventJudgementValidationError(
            f"the reflection returned keys the contract does not have: {extra}"
        )
    missing = sorted(expected - set(value))
    if missing:
        raise EventJudgementValidationError(f"the reflection omitted required keys: {missing}")
    permitted = allowed_refs(context) | {
        row["ref"] for row in (context.get("market_view") or ())
    }
    known_theses = {thesis["ref"] for thesis in context.get("theses") or ()}
    thesis_refs = _ref_list(value["thesis_refs"], "thesis_refs")
    stray = sorted(set(thesis_refs) - known_theses)
    if stray:
        raise EventJudgementValidationError(f"thesis_refs names theses not shown: {stray}")
    citations = _ref_list(value["citations"], "citations")
    stray = sorted(set(citations) - permitted)
    if stray:
        raise EventJudgementValidationError(f"citations names refs that were not shown: {stray}")
    if not citations:
        raise EventJudgementValidationError(
            "a reflection with no citation is an opinion about nothing"
        )

    debates = value["missed_debates"]
    if not isinstance(debates, list) or len(debates) > MAX_MISSED_DEBATES:
        raise EventJudgementValidationError(
            f"missed_debates must be a list of at most {MAX_MISSED_DEBATES}"
        )
    checked_debates = []
    for row in debates:
        if not isinstance(row, Mapping) or set(row) != {"question", "refs"}:
            raise EventJudgementValidationError(
                "each missed debate must be exactly question and refs"
            )
        refs = _ref_list(row["refs"], "missed_debates[].refs")
        stray = sorted(set(refs) - permitted)
        if stray:
            raise EventJudgementValidationError(
                f"a missed debate cites refs that were not shown: {stray}"
            )
        checked_debates.append({
            "question": _text(row["question"], "missed_debates[].question", maximum=500),
            "refs": refs,
        })

    tracking = value["followup_tracking"]
    if not isinstance(tracking, list) or len(tracking) > MAX_FOLLOWUPS:
        raise EventJudgementValidationError(
            f"followup_tracking must be a list of at most {MAX_FOLLOWUPS}"
        )
    known_sources = set(context.get("source_keys") or ())
    if tracking and not known_sources:
        raise EventJudgementValidationError(
            "a tracking follow-up cannot be checked without the cadence policy's "
            "source keys; refusing rather than accepting an unverifiable proposal"
        )
    checked_tracking = []
    for row in tracking:
        if not isinstance(row, Mapping) or set(row) != {
            "source_key", "interval_seconds", "because"
        }:
            raise EventJudgementValidationError(
                "each tracking follow-up must be exactly source_key, interval_seconds "
                "and because"
            )
        source_key = _text(row["source_key"], "followup_tracking[].source_key", maximum=64)
        if source_key not in known_sources:
            raise EventJudgementValidationError(
                f"followup_tracking names a source with no baseline cadence: {source_key!r}"
            )
        interval = row["interval_seconds"]
        if isinstance(interval, bool) or not isinstance(interval, int) or interval < 60:
            raise EventJudgementValidationError(
                "followup_tracking.interval_seconds must be an integer of at least 60"
            )
        checked_tracking.append({
            "source_key": source_key, "interval_seconds": interval,
            "because": _text(row["because"], "followup_tracking[].because",
                             maximum=MAX_BECAUSE_CHARS),
        })

    research = value["followup_research"]
    if not isinstance(research, list) or len(research) > MAX_FOLLOWUPS:
        raise EventJudgementValidationError(
            f"followup_research must be a list of at most {MAX_FOLLOWUPS}"
        )
    checked_research = []
    for row in research:
        if not isinstance(row, Mapping) or set(row) != {"question", "wants"}:
            raise EventJudgementValidationError(
                "each research follow-up must be exactly question and wants"
            )
        checked_research.append({
            "question": _text(row["question"], "followup_research[].question", maximum=500),
            "wants": _text(row["wants"], "followup_research[].wants", maximum=500),
        })

    market = value["market_view_vs_ours"]
    if not isinstance(market, Mapping) or set(market) != {
        "available", "our_direction", "summary", "refs"
    }:
        raise EventJudgementValidationError(
            "market_view_vs_ours must be exactly available, our_direction, summary and refs"
        )
    if not isinstance(market["available"], bool):
        raise EventJudgementValidationError("market_view_vs_ours.available must be a boolean")
    market_refs = _ref_list(market["refs"], "market_view_vs_ours.refs")
    stray = sorted(set(market_refs) - permitted)
    if stray:
        raise EventJudgementValidationError(
            f"market_view_vs_ours cites refs that were not shown: {stray}"
        )
    if market["available"] and not market_refs:
        raise EventJudgementValidationError(
            "a market view asserted as available must name what it rests on"
        )
    if not market["available"] and market_refs:
        raise EventJudgementValidationError(
            "a market view said to be unavailable cannot cite anything"
        )
    return {
        "thesis_refs": thesis_refs,
        "what_we_expected": _text(value["what_we_expected"], "what_we_expected",
                                  maximum=MAX_BECAUSE_CHARS),
        "what_happened": _text(value["what_happened"], "what_happened",
                               maximum=MAX_BECAUSE_CHARS),
        "why": _text(value["why"], "why", maximum=MAX_BECAUSE_CHARS),
        "citations": citations,
        "missed_debates": checked_debates,
        "followup_tracking": checked_tracking,
        "followup_research": checked_research,
        "market_view_vs_ours": {
            "available": market["available"],
            "our_direction": _text(market["our_direction"],
                                   "market_view_vs_ours.our_direction", maximum=64),
            "summary": _text(market["summary"], "market_view_vs_ours.summary",
                             maximum=MAX_BECAUSE_CHARS),
            "refs": market_refs,
        },
        "convergence_pathway": _text(value["convergence_pathway"],
                                     "convergence_pathway", maximum=MAX_BECAUSE_CHARS),
    }


def reflect(
    context: Mapping[str, Any],
    judgement: Mapping[str, Any],
    *,
    model: Any,
    mission: Mapping[str, Any],
    request_id: str,
) -> dict[str, Any]:
    """One bounded call, verified against the closed schema before it is believed."""

    prompt = build_reflection_prompt(context, judgement)
    try:
        call = model.call(
            purpose=REFLECTION_PURPOSE, request_id=request_id, prompt=prompt,
            mission=mission,
        )
    except CockpitModelError as exc:
        return {"status": "refused", "reason": f"the reflection call did not succeed: {exc}",
                "lane_status": lane_status_for(exc, "refused"), "model": None}
    provenance = _provenance(call)
    provenance["purpose"] = REFLECTION_PURPOSE
    try:
        validated = validate_reflection_output(unwrap_json_object(call["text"]), context)
    except EventJudgementValidationError as exc:
        return {"status": "refused", "reason": str(exc), "model": provenance}
    return {"status": "reflected", "model": provenance, **validated}


def build_reflection_verifier_prompt(
    context: Mapping[str, Any], reflection: Mapping[str, Any]
) -> str:
    lines = [
        "You are an independent verifier. Another model wrote down what this analyst",
        "expected, what happened and what may have been missed. You do not rewrite it.",
        "You answer one question: is what it says supported by what it was shown, at the",
        "weight that evidence deserves?",
        "",
        f"Trigger: {context['event']['kind']} ({context['event']['id']})",
        f"Expected: {reflection['what_we_expected']}",
        f"Happened: {reflection['what_happened']}",
        f"Why: {reflection['why']}",
        f"Market view available: {reflection['market_view_vs_ours']['available']} -- "
        f"{reflection['market_view_vs_ours']['summary']}",
        f"Convergence pathway: {reflection['convergence_pathway']}",
        f"Cited: {', '.join(reflection['citations']) or '(nothing)'}",
        "",
        "Return raw JSON only, nothing else:",
        '{"verdict": "pass|reject", "findings": [{"code": "'
        + "|".join(VERIFIER_FINDING_CODES) + '", "detail": "<one sentence>"}]}',
        "A pass verdict must have no findings; a reject verdict must have at least one.",
    ]
    return "\n".join(lines)[:MAX_PROMPT_CHARS]


def verify_reflection(
    context: Mapping[str, Any],
    reflection: Mapping[str, Any],
    *,
    model: Any,
    mission: Mapping[str, Any],
    request_id: str,
    family_resolver: Callable[[str | None], str | None],
) -> dict[str, Any]:
    """The same independence rule as a judgement: different family or nothing."""

    if reflection.get("status") != "reflected":
        return {"status": "skipped", "reason": "there is no reflection to verify"}
    producer_family = family_resolver((reflection.get("model") or {}).get("route_decision_ref"))
    if producer_family is None:
        return {"status": "refused", "model": None,
                "independence": {"producer_family": None, "verifier_family": None,
                                 "predicate": "model_family_ne"},
                "reason": "the model family behind the reflection could not be resolved; "
                          "an unverifiable independence claim is not independence"}
    prompt = build_reflection_verifier_prompt(context, reflection)
    try:
        call = model.call(
            purpose=REFLECTION_PURPOSE, request_id=request_id, prompt=prompt,
            mission=mission,
        )
    except CockpitModelError as exc:
        return {"status": "refused", "model": None,
                "lane_status": lane_status_for(exc, "refused"),
                "reason": f"the reflection verifier call did not succeed: {exc}"}
    provenance = _provenance(call)
    provenance["purpose"] = REFLECTION_PURPOSE
    verifier_family = family_resolver(provenance.get("route_decision_ref"))
    independence = {"producer_family": producer_family,
                    "verifier_family": verifier_family,
                    "predicate": "model_family_ne"}
    if verifier_family is None:
        return {"status": "refused", "model": provenance, "independence": independence,
                "reason": "the model family behind the reflection verifier could not be "
                          "resolved; an unverifiable independence claim is not independence"}
    if producer_family == verifier_family:
        return {"status": "refused", "model": provenance, "independence": independence,
                "reason": f"model_family_not_independent: both calls ran on {producer_family}"}
    try:
        validated = validate_verifier_output(unwrap_json_object(call["text"]))
    except EventJudgementValidationError as exc:
        return {"status": "refused", "reason": str(exc), "model": provenance,
                "independence": independence}
    return {"status": "verified", "independence": independence, "model": provenance,
            **validated}


def build_verifier_prompt(context: Mapping[str, Any], judgement: Mapping[str, Any]) -> str:
    event = context["event"]
    lines = [
        "You are an independent verifier. Another model read one event and decided what,",
        "if anything, should change. You do not re-decide and you do not improve it. You",
        "answer one question: does the decision follow from what the event actually says,",
        "at the weight its evidence tier deserves?",
        "",
        f"Event ({event['kind']}, evidence tier {event['evidence_tier']}, ref {event['id']}):",
        *_payload_lines(event),
        "",
        f"Decision: {judgement['decision']} / {judgement['action']}",
        f"Because: {judgement['because']}",
        f"Cited: {', '.join(judgement['citations']) or '(nothing)'}",
        f"Drivers named: {', '.join(judgement['driver_refs']) or '(none)'}",
        f"Theses named: {', '.join(judgement['thesis_refs']) or '(none)'}",
        "",
        "Return raw JSON only, nothing else:",
        '{"verdict": "pass|reject", "findings": [{"code": "'
        + "|".join(VERIFIER_FINDING_CODES) + '", "detail": "<one sentence>"}]}',
        "A pass verdict must have no findings; a reject verdict must have at least one.",
    ]
    return "\n".join(lines)[:MAX_PROMPT_CHARS]


def validate_verifier_output(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise EventJudgementValidationError("the verifier did not return an object")
    extra = sorted(set(value) - {"verdict", "findings"})
    if extra:
        raise EventJudgementValidationError(
            f"the verifier returned keys the contract does not have: {extra}"
        )
    verdict = value.get("verdict")
    if verdict not in VERIFIER_VERDICTS:
        raise EventJudgementValidationError(f"invalid verdict: {verdict!r}")
    rows = value.get("findings") or []
    if not isinstance(rows, list):
        raise EventJudgementValidationError("findings must be a list")
    findings = []
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != {"code", "detail"}:
            raise EventJudgementValidationError("each finding must be exactly code and detail")
        if row["code"] not in VERIFIER_FINDING_CODES:
            raise EventJudgementValidationError(f"unknown finding code: {row['code']!r}")
        findings.append({
            "code": str(row["code"]),
            "detail": _text(row["detail"], "finding.detail", maximum=500),
        })
    if verdict == "pass" and findings:
        raise EventJudgementValidationError("a pass verdict must have no findings")
    if verdict == "reject" and not findings:
        raise EventJudgementValidationError("a reject verdict must have at least one finding")
    return {"verdict": verdict, "findings": findings}


# ---------------------------------------------------------------------------
# the two calls
# ---------------------------------------------------------------------------


def _provenance(call: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "work_order_ref": call.get("work_order_ref"),
        "invocation_ref": call.get("invocation_ref"),
        "result_envelope_ref": call.get("result_envelope_ref"),
        "route_decision_ref": call.get("route_decision_ref"),
        "replayed": bool(call.get("replayed")),
        "cost_micros": int(call.get("cost_micros") or 0),
        "purpose": PURPOSE,
    }


def judge(
    context: Mapping[str, Any],
    *,
    model: Any,
    mission: Mapping[str, Any],
    request_id: str,
) -> dict[str, Any]:
    """One bounded call, verified against the closed schema before it is believed."""

    prompt = build_judge_prompt(context)
    try:
        call = model.call(
            purpose=PURPOSE, request_id=request_id, prompt=prompt, mission=mission
        )
    except CockpitModelError as exc:
        # C2: still a refusal -- the caller branches on this word -- but a
        # spent pool says which kind, so the run reports a budget decision
        # instead of eight identical refusals that look like an outage.
        return {"status": "refused", "reason": f"the model call did not succeed: {exc}",
                "lane_status": lane_status_for(exc, "refused"),
                "prompt_chars": len(prompt), "model": None}
    provenance = _provenance(call)
    try:
        validated = validate_judge_output(unwrap_json_object(call["text"]), context)
    except EventJudgementValidationError as exc:
        return {"status": "refused", "reason": str(exc),
                "prompt_chars": len(prompt), "model": provenance}
    return {"status": "judged", "prompt_chars": len(prompt), "model": provenance, **validated}


def verify(
    context: Mapping[str, Any],
    judgement: Mapping[str, Any],
    *,
    model: Any,
    mission: Mapping[str, Any],
    request_id: str,
    family_resolver: Callable[[str | None], str | None],
) -> dict[str, Any]:
    """A second, separate call that returns only a verdict on the first.

    Fails closed on independence.  If the two families cannot be resolved, or
    resolve to the same family, the verification is refused rather than
    recorded as a pass: a verifier that might be the producer is not one.
    """

    if judgement.get("status") != "judged":
        return {"status": "skipped", "reason": "there is no judgement to verify"}
    # Resolve what can be resolved before paying: a producer whose family is
    # unknown can never be verified independently, so there is nothing to buy.
    producer_family = family_resolver((judgement.get("model") or {}).get("route_decision_ref"))
    if producer_family is None:
        return {"status": "refused", "model": None,
                "independence": {"producer_family": None, "verifier_family": None,
                                 "predicate": "model_family_ne"},
                "reason": "the model family behind the judgement could not be resolved; "
                          "an unverifiable independence claim is not independence"}
    prompt = build_verifier_prompt(context, judgement)
    try:
        call = model.call(
            purpose=PURPOSE, request_id=request_id, prompt=prompt, mission=mission
        )
    except CockpitModelError as exc:
        return {"status": "refused", "reason": f"the verifier call did not succeed: {exc}",
                "lane_status": lane_status_for(exc, "refused"), "model": None}
    provenance = _provenance(call)
    verifier_family = family_resolver(provenance.get("route_decision_ref"))
    independence = {
        "producer_family": producer_family,
        "verifier_family": verifier_family,
        "predicate": "model_family_ne",
    }
    if verifier_family is None:
        return {"status": "refused", "model": provenance, "independence": independence,
                "reason": "the model family behind the verifier could not be resolved; "
                          "an unverifiable independence claim is not independence"}
    if producer_family == verifier_family:
        return {"status": "refused", "model": provenance, "independence": independence,
                "reason": f"model_family_not_independent: both calls ran on {producer_family}"}
    try:
        validated = validate_verifier_output(unwrap_json_object(call["text"]))
    except EventJudgementValidationError as exc:
        return {"status": "refused", "reason": str(exc), "model": provenance,
                "independence": independence}
    return {
        "status": "verified",
        "judged_decision_hash": content_hash({
            "decision": judgement["decision"], "action": judgement["action"],
            "because": judgement["because"], "citations": judgement["citations"],
        }),
        "independence": independence,
        "model": provenance,
        **validated,
    }


def route_family_resolver(model_router_db: str | Path | None) -> Callable[[str | None], str | None]:
    """Resolve a route decision to the family the broker actually used.

    Read from the router's own decision record rather than from a
    configuration file, because a configuration says which policy was asked
    and the decision says which model answered.
    """

    if not model_router_db:
        return lambda _ref: None

    def resolve(decision_ref: str | None) -> str | None:
        if not decision_ref:
            return None
        try:
            from .model_router import ModelRouter

            with ModelRouter(str(model_router_db)) as router:
                decision = router.get_decision(decision_ref)
        except Exception:  # noqa: BLE001 - an unreadable router is "unknown", not a crash
            return None
        endpoint = (decision or {}).get("selected_endpoint") or {}
        family = endpoint.get("family")
        return family if isinstance(family, str) and family else None

    return resolve


# ---------------------------------------------------------------------------
# the ledger
# ---------------------------------------------------------------------------


def _decode(row: sqlite3.Row | None, name: str) -> dict[str, Any]:
    if row is None:
        raise EventJudgementConflict(f"{name} is missing")
    try:
        wire = json.loads(row["record_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise EventJudgementConflict(f"{name} record_json is invalid") from exc
    if not isinstance(wire, dict) or canonical_json(wire) != row["record_json"]:
        raise EventJudgementConflict(f"{name} record_json is not canonical")
    body = dict(wire)
    asserted = body.pop("content_hash", None)
    if asserted != content_hash(body) or asserted != row["content_hash"]:
        raise EventJudgementConflict(f"{name} content hash drifted")
    return wire


class EventJudgementAuthority:
    """One append-only judgement per event, and the proposals it produced."""

    def __init__(self, store: Any) -> None:
        if not hasattr(store, "connection") or not hasattr(store, "_transaction"):
            raise TypeError("EventJudgementAuthority requires a DaltonStore")
        self.store = store
        self.connection: sqlite3.Connection = store.connection
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    # -- reading -----------------------------------------------------------

    def judgement_for(self, event_ref: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM event_judgements WHERE event_ref=?",
            (_text(event_ref, "event_ref"),),
        ).fetchone()
        return None if row is None else _decode(row, "EventJudgement")

    def judged_event_refs(self, company_ref: str | None = None) -> set[str]:
        query = "SELECT event_ref FROM event_judgements"
        params: list[Any] = []
        if company_ref is not None:
            query += " WHERE company_ref=?"
            params.append(company_ref)
        return {row["event_ref"] for row in self.connection.execute(query, params).fetchall()}

    def judged_count(self, company_ref: str) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) AS n FROM event_judgements WHERE company_ref=?",
            (_text(company_ref, "company_ref"),),
        ).fetchone()
        return 0 if row is None else int(row["n"])

    def recent(self, company_ref: str, limit: int = MAX_RECENT_JUDGEMENTS) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM event_judgements WHERE company_ref=? "
            "ORDER BY created_at DESC, judgement_id DESC LIMIT ?",
            (_text(company_ref, "company_ref"), max(1, int(limit))),
        ).fetchall()
        return [_decode(row, "EventJudgement") for row in rows]

    def day_cost_micros(self, day: str) -> int:
        """Everything this lane paid for today, refusals and reflections included.

        Read from the spend ledger rather than from the judgement rows: a
        refused judgement writes no judgement row and its two calls were still
        paid for, and a reflection is a second pair against the same
        judgement. Summing the judgements would under-report the day by
        exactly the spend a misbehaving model produces most of.
        """

        row = self.connection.execute(
            "SELECT COALESCE(SUM(cost_micros), 0) AS spent FROM event_response_spend "
            "WHERE day=?",
            (day,),
        ).fetchone()
        return 0 if row is None else int(row["spent"])

    def record_spend(
        self,
        *,
        call: Mapping[str, Any] | None,
        purpose: str,
        outcome: str,
        event_ref: str,
        mission: Mapping[str, Any],
        day: str | None = None,
    ) -> int:
        """Book one call against the day, or nothing when there was no call.

        Keyed by the WorkOrder, so a replayed call -- which costs nothing --
        is booked once and a retry of the same request does not double-count.
        """

        provenance = dict(call or {})
        work_order_ref = provenance.get("work_order_ref")
        if not work_order_ref:
            return 0
        cost = int(provenance.get("cost_micros") or 0)
        created_at = _now()
        record_day = day or created_at[:10]
        with self.store._transaction() as cur:
            existing = cur.execute(
                "SELECT cost_micros FROM event_response_spend WHERE work_order_ref=?",
                (work_order_ref,),
            ).fetchone()
            if existing is not None:
                return 0
            cur.execute(
                "INSERT INTO event_response_spend(spend_id,day,work_order_ref,purpose,"
                "outcome,event_ref,cost_micros,mission_version_ref,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    "event-response-spend:" + content_hash(
                        {"work_order_ref": work_order_ref})[:32],
                    record_day, work_order_ref, purpose, outcome, event_ref, cost,
                    mission["id"], created_at,
                ),
            )
        return cost

    def thesis_candidates(self, company_ref: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM thesis_revision_candidates"
        params: list[Any] = []
        if company_ref is not None:
            query += " WHERE company_ref=?"
            params.append(company_ref)
        query += " ORDER BY created_at, candidate_id"
        return [
            _decode(row, "ThesisRevisionCandidate")
            for row in self.connection.execute(query, params).fetchall()
        ]

    def reflection_for(self, judgement_ref: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM thesis_reflections WHERE judgement_ref=?",
            (_text(judgement_ref, "judgement_ref"),),
        ).fetchone()
        return None if row is None else _decode(row, "ThesisReflection")

    def reflections(self, company_ref: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM thesis_reflections"
        params: list[Any] = []
        if company_ref is not None:
            query += " WHERE company_ref=?"
            params.append(company_ref)
        query += " ORDER BY created_at, reflection_id"
        return [
            _decode(row, "ThesisReflection")
            for row in self.connection.execute(query, params).fetchall()
        ]

    def forecast_proposals(self, company_ref: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM forecast_revision_proposals"
        params: list[Any] = []
        if company_ref is not None:
            query += " WHERE company_ref=?"
            params.append(company_ref)
        query += " ORDER BY created_at, proposal_id"
        return [
            _decode(row, "ForecastRevisionProposal")
            for row in self.connection.execute(query, params).fetchall()
        ]

    # -- writing -----------------------------------------------------------

    def record(
        self,
        *,
        event: Mapping[str, Any],
        judgement: Mapping[str, Any],
        verification: Mapping[str, Any],
        effect: Mapping[str, Any],
        mission: Mapping[str, Any],
        actor_ref: str,
    ) -> dict[str, Any]:
        """Bind one decision to one event and one pair of model work orders."""

        actor_ref = _text(actor_ref, "actor_ref")
        if actor_ref.startswith("automation:") and (
            actor_ref != mission["autonomy"]["automation_principal"]
        ):
            raise EventJudgementConflict("automation actor is not the mission principal")
        record = {
            "schema_version": SCHEMA_VERSION,
            "id": "event-judgement:" + content_hash({
                "event_ref": event["id"],
                "event_hash": event["content_hash"],
                "work_order_ref": (judgement.get("model") or {}).get("work_order_ref"),
            })[:32],
            "created_at": _now(),
            "event_ref": event["id"],
            "event_hash": event["content_hash"],
            "event_kind": event["kind"],
            "evidence_tier": event["evidence_tier"],
            "company_ref": event["company_ref"],
            "decision": judgement["decision"],
            "action": judgement["action"],
            "driver_refs": list(judgement["driver_refs"]),
            "thesis_refs": list(judgement["thesis_refs"]),
            "because": judgement["because"],
            "citations": list(judgement["citations"]),
            "note": judgement.get("note"),
            "research_question": judgement.get("research_question"),
            "forecast_change": judgement.get("forecast_change"),
            "verifier": {
                "status": verification.get("status"),
                "verdict": verification.get("verdict"),
                "findings": list(verification.get("findings") or ()),
                "independence": verification.get("independence"),
                "reason": verification.get("reason"),
            },
            "model": judgement.get("model"),
            "verifier_model": verification.get("model"),
            "effect": dict(effect),
            "mission_version_ref": mission["id"],
            "mission_version_hash": mission["content_hash"],
            "actor_ref": actor_ref,
        }
        record["content_hash"] = content_hash(record)
        cost = int((judgement.get("model") or {}).get("cost_micros") or 0) + int(
            (verification.get("model") or {}).get("cost_micros") or 0
        )
        with self.store._transaction() as cur:
            existing = cur.execute(
                "SELECT * FROM event_judgements WHERE event_ref=?", (event["id"],)
            ).fetchone()
            if existing is not None:
                return {**_decode(existing, "EventJudgement"), "status": "duplicate"}
            cur.execute(
                "INSERT INTO event_judgements(judgement_id,event_ref,event_hash,company_ref,"
                "decision,action,verdict,cost_micros,mission_version_ref,record_json,"
                "content_hash,actor_ref,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record["id"], event["id"], event["content_hash"], event["company_ref"],
                    record["decision"], record["action"],
                    str(verification.get("verdict") or verification.get("status") or "none"),
                    cost, mission["id"], canonical_json(record), record["content_hash"],
                    actor_ref, record["created_at"],
                ),
            )
            written = _decode(
                cur.execute(
                    "SELECT * FROM event_judgements WHERE judgement_id=?", (record["id"],)
                ).fetchone(),
                "EventJudgement",
            )
        return {**written, "status": "fresh"}

    def record_reflection(
        self,
        *,
        judgement: Mapping[str, Any],
        event: Mapping[str, Any],
        reflection: Mapping[str, Any],
        verification: Mapping[str, Any],
        mission: Mapping[str, Any],
        actor_ref: str,
    ) -> dict[str, Any]:
        """Append one reflection, bound to the judgement that occasioned it.

        It never modifies a thesis and has no path to: it is a record of what
        we expected, what happened and what we may have missed, attached to
        the revision candidate so the person deciding sees both.
        """

        trigger = ("price_divergence" if event["kind"] == "price_divergence"
                   else "revision")
        record = {
            "schema_version": SCHEMA_VERSION,
            "id": "thesis-reflection:" + content_hash({
                "judgement_ref": judgement["id"],
                "work_order_ref": (reflection.get("model") or {}).get("work_order_ref"),
            })[:32],
            "created_at": _now(),
            "judgement_ref": judgement["id"],
            "trigger_event_ref": event["id"],
            "trigger_event_hash": event["content_hash"],
            "trigger_kind": trigger,
            "company_ref": event["company_ref"],
            "decision": judgement["decision"],
            "action": judgement["action"],
            "thesis_refs": list(reflection["thesis_refs"]),
            "what_we_expected": reflection["what_we_expected"],
            "what_happened": reflection["what_happened"],
            "why": reflection["why"],
            "citations": list(reflection["citations"]),
            "missed_debates": [dict(row) for row in reflection["missed_debates"]],
            "followup_tracking": [dict(row) for row in reflection["followup_tracking"]],
            "followup_research": [dict(row) for row in reflection["followup_research"]],
            "market_view_vs_ours": dict(reflection["market_view_vs_ours"]),
            "convergence_pathway": reflection["convergence_pathway"],
            "verifier": {
                "status": verification.get("status"),
                "verdict": verification.get("verdict"),
                "findings": list(verification.get("findings") or ()),
                "independence": verification.get("independence"),
                "reason": verification.get("reason"),
            },
            "model": reflection.get("model"),
            "verifier_model": verification.get("model"),
            "mission_version_ref": mission["id"],
            "mission_version_hash": mission["content_hash"],
            "actor_ref": _text(actor_ref, "actor_ref"),
        }
        record["content_hash"] = content_hash(record)
        cost = int((reflection.get("model") or {}).get("cost_micros") or 0) + int(
            (verification.get("model") or {}).get("cost_micros") or 0
        )
        with self.store._transaction() as cur:
            existing = cur.execute(
                "SELECT * FROM thesis_reflections WHERE judgement_ref=?",
                (judgement["id"],),
            ).fetchone()
            if existing is not None:
                return {**_decode(existing, "ThesisReflection"), "status": "duplicate"}
            cur.execute(
                "INSERT INTO thesis_reflections(reflection_id,judgement_ref,"
                "trigger_event_ref,trigger_event_hash,trigger_kind,company_ref,verdict,"
                "cost_micros,mission_version_ref,record_json,content_hash,actor_ref,"
                "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record["id"], judgement["id"], event["id"], event["content_hash"],
                    trigger, event["company_ref"],
                    str(verification.get("verdict") or verification.get("status") or "none"),
                    cost, mission["id"], canonical_json(record), record["content_hash"],
                    record["actor_ref"], record["created_at"],
                ),
            )
            written = _decode(
                cur.execute(
                    "SELECT * FROM thesis_reflections WHERE reflection_id=?",
                    (record["id"],),
                ).fetchone(),
                "ThesisReflection",
            )
        return {**written, "status": "fresh"}

    def record_thesis_candidate(
        self,
        *,
        judgement_ref: str,
        thesis: Mapping[str, Any],
        company_ref: str,
        decision: str,
        because: str,
        evidence_refs: Sequence[str],
        falsifier_ref: str | None,
        proposed_statement: str | None,
        proposed_confidence: str | None,
        mission: Mapping[str, Any],
        actor_ref: str,
        reflection_ref: str | None = None,
    ) -> dict[str, Any]:
        """ADR-0007's shape.  It carries no authority to change anything.

        ``reflection_ref`` attaches the self-reflection written for the same
        judgement, so the person deciding sees the proposal and the account of
        what we may have missed in one place rather than two.
        """

        if decision not in DECISION_VOCABULARY:
            raise EventJudgementValidationError(
                f"a candidate carries one word of {list(DECISION_VOCABULARY)}"
            )
        if proposed_confidence is not None and proposed_confidence not in ("low", "medium", "high"):
            raise EventJudgementValidationError(
                "confidence is ADR-0001's ordinal vocabulary, never a float"
            )
        refs = [_text(ref, "evidence_refs[]") for ref in (evidence_refs or ())]
        if not refs:
            raise EventJudgementValidationError(
                "a thesis revision candidate must name the evidence that occasioned it"
            )
        record = {
            "schema_version": SCHEMA_VERSION,
            "id": "thesis-revision-candidate:" + content_hash({
                "judgement_ref": judgement_ref, "thesis_version_ref": thesis["id"],
            })[:32],
            "created_at": _now(),
            "judgement_ref": judgement_ref,
            "thesis_version_ref": thesis["id"],
            "thesis_version_hash": thesis["content_hash"],
            "thesis_ref": thesis.get("thesis_ref"),
            "company_ref": company_ref,
            "decision": decision,
            "proposed_statement": proposed_statement,
            "proposed_confidence": proposed_confidence,
            "falsifier_ref": falsifier_ref,
            "reflection_ref": reflection_ref,
            "because": _text(because, "because", maximum=MAX_BECAUSE_CHARS),
            "evidence_refs": list(dict.fromkeys(refs)),
            "checkpoint_kind": "thesis_revision_candidate",
            "mission_version_ref": mission["id"],
            "mission_version_hash": mission["content_hash"],
            "actor_ref": actor_ref,
        }
        record["content_hash"] = content_hash(record)
        with self.store._transaction() as cur:
            existing = cur.execute(
                "SELECT * FROM thesis_revision_candidates WHERE candidate_id=?",
                (record["id"],),
            ).fetchone()
            if existing is not None:
                return {**_decode(existing, "ThesisRevisionCandidate"), "status": "duplicate"}
            cur.execute(
                "INSERT INTO thesis_revision_candidates(candidate_id,judgement_ref,"
                "thesis_version_ref,thesis_version_hash,company_ref,decision,checkpoint_kind,"
                "record_json,content_hash,actor_ref,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record["id"], judgement_ref, thesis["id"], thesis["content_hash"],
                    company_ref, decision, "thesis_revision_candidate",
                    canonical_json(record), record["content_hash"], actor_ref,
                    record["created_at"],
                ),
            )
        return {**record, "status": "fresh"}

    def record_forecast_proposal(
        self,
        *,
        judgement_ref: str,
        company_ref: str,
        model_version_ref: str | None,
        change: Mapping[str, Any],
        decision: str,
        because: str,
        evidence_refs: Sequence[str],
        mission: Mapping[str, Any],
        actor_ref: str,
        reason: str,
    ) -> dict[str, Any]:
        """What a revise_forecast becomes when the mission does not grant the write."""

        refs = [_text(ref, "evidence_refs[]") for ref in (evidence_refs or ())]
        if not refs:
            raise EventJudgementValidationError(
                "a forecast revision proposal must name its evidence"
            )
        record = {
            "schema_version": SCHEMA_VERSION,
            "id": "forecast-revision-proposal:" + content_hash({
                "judgement_ref": judgement_ref, "driver_ref": change["driver_ref"],
                "period_end": change["period_end"],
            })[:32],
            "created_at": _now(),
            "judgement_ref": judgement_ref,
            "company_ref": company_ref,
            "model_version_ref": model_version_ref,
            "driver_ref": change["driver_ref"],
            "period_end": change["period_end"],
            "proposed_value": change["value"],
            "decision": decision,
            "because": _text(because, "because", maximum=MAX_BECAUSE_CHARS),
            "evidence_refs": list(dict.fromkeys(refs)),
            "checkpoint_kind": "forecast_overturn",
            "reason": _text(reason, "reason", maximum=500),
            "mission_version_ref": mission["id"],
            "mission_version_hash": mission["content_hash"],
            "actor_ref": actor_ref,
        }
        record["content_hash"] = content_hash(record)
        with self.store._transaction() as cur:
            existing = cur.execute(
                "SELECT * FROM forecast_revision_proposals WHERE proposal_id=?",
                (record["id"],),
            ).fetchone()
            if existing is not None:
                return {**_decode(existing, "ForecastRevisionProposal"), "status": "duplicate"}
            cur.execute(
                "INSERT INTO forecast_revision_proposals(proposal_id,judgement_ref,company_ref,"
                "model_version_ref,driver_ref,period_end,record_json,content_hash,actor_ref,"
                "created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    record["id"], judgement_ref, company_ref, model_version_ref,
                    change["driver_ref"], change["period_end"], canonical_json(record),
                    record["content_hash"], actor_ref, record["created_at"],
                ),
            )
        return {**record, "status": "fresh"}


# ---------------------------------------------------------------------------
# the budget pool
# ---------------------------------------------------------------------------


def pool(mission: Mapping[str, Any]) -> dict[str, Any]:
    cap = (Decimal(str(mission["budget"]["max_daily_cost_usd"])) * POOL_SHARE)
    return {
        "pool": POOL_NAME,
        "share": str(POOL_SHARE),
        "cap_usd": str(cap.quantize(Decimal("0.000001"))),
        "cap_micros": int(cap * 1_000_000),
    }


def pool_state(
    authority: EventJudgementAuthority, mission: Mapping[str, Any], *, day: str
) -> dict[str, Any]:
    """The day's account, derived from the judgement ledger itself.

    No second book: the pool and the ledger cannot disagree because the pool
    *is* the ledger, summed.  (P14e reached the same conclusion for ``adhoc``
    and it is the shape C2 should generalise.)
    """

    state = pool(mission)
    spent = authority.day_cost_micros(day)
    return {**state, "day": day, "spent_micros": spent,
            "remaining_micros": max(0, state["cap_micros"] - spent)}


# ---------------------------------------------------------------------------
# what the brain is shown
# ---------------------------------------------------------------------------


def company_theses(connection: sqlite3.Connection, company_ref: str) -> list[dict[str, Any]]:
    """The current version of every thesis that binds this company or its industry.

    A ThesisVersion carries no subject of its own; the admission candidate it
    came from does.  Industry-level theses are included for every company in
    the industry on purpose -- "US IT services demand is bottoming" is the
    thing an ACN event most often bears on, and a judgement that could not
    name it would have to invent a company-level thesis to say so.
    """

    try:
        rows = connection.execute(
            "SELECT v.version_id AS version_id, v.thesis_id AS thesis_id, "
            "v.version_number AS version_number, v.created_at AS created_at, "
            "v.content_json AS content_json "
            "FROM thesis_versions v WHERE v.version_number = "
            "(SELECT MAX(version_number) FROM thesis_versions w WHERE w.thesis_id=v.thesis_id) "
            # Oldest first, so a caller that wants "the newest" takes the last
            # one rather than whichever version id happened to sort highest --
            # a version id is a hash and hash order is not time order.
            "ORDER BY v.created_at ASC, v.version_id ASC"
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        return []
    try:
        subjects = {
            row["thesis_ref"]: row["company_ref"]
            for row in connection.execute(
                "SELECT thesis_ref, company_ref FROM thesis_admission_candidates"
            ).fetchall()
        }
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        subjects = {}
    theses: list[dict[str, Any]] = []
    for row in rows:
        try:
            wire = json.loads(row["content_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        subject = subjects.get(wire.get("thesis_ref"))
        if subject is not None and subject != company_ref and subject.startswith("company:"):
            continue
        theses.append({
            "ref": row["version_id"],
            "thesis_ref": wire.get("thesis_ref"),
            "statement": str(wire.get("statement") or "")[:1200],
            "mechanism": str(wire.get("mechanism") or "")[:800],
            "confidence": wire.get("confidence"),
            "falsifier_refs": list(wire.get("falsifier_refs") or ()),
            "content_hash": wire.get("content_hash"),
            "subject_ref": subject,
            "created_at": row["created_at"],
        })
    return theses


MAX_ASSUMPTIONS_PER_DRIVER = 3


def model_drivers(model_version: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """The driver rows and the nearest assumptions, small enough for a prompt."""

    if not model_version:
        return []
    by_driver: dict[str, list[dict[str, Any]]] = {}
    for assumption in model_version.get("assumptions") or ():
        if assumption.get("superseded_by"):
            continue
        by_driver.setdefault(str(assumption["driver_ref"]), []).append({
            "period_end": str(assumption["period"]["end"]),
            "measure": str(assumption["measure"]),
            "value": str(assumption["value"]),
            "kind": str(assumption["kind"]),
        })
    drivers = []
    for driver in model_version.get("drivers") or ():
        rows = sorted(by_driver.get(str(driver["ref"]), ()), key=lambda item: item["period_end"])
        drivers.append({
            "ref": str(driver["ref"]),
            "label": driver.get("label"),
            "role": driver.get("role"),
            "status": driver.get("status"),
            "assumptions": rows[:MAX_ASSUMPTIONS_PER_DRIVER],
        })
    return drivers


def recent_claims(
    connection: sqlite3.Connection, company_ref: str, *, limit: int = MAX_CLAIMS_IN_PROMPT
) -> list[dict[str, Any]]:
    """The company's newest live Claims, as citable one-liners."""

    try:
        retired = {
            row["claim_version_ref"]
            for row in connection.execute(
                "SELECT claim_version_ref FROM claim_retirement_decisions WHERE decision='retired'"
            ).fetchall()
        }
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        retired = set()
    rows = connection.execute(
        "SELECT claim_version_id AS id, claim_json FROM claim_versions "
        "WHERE json_extract(claim_json,'$.subject_ref')=? "
        "ORDER BY created_at DESC, claim_version_id DESC LIMIT ?",
        (company_ref, int(limit) * 2),
    ).fetchall()
    claims = []
    for row in rows:
        if row["id"] in retired or len(claims) >= limit:
            continue
        try:
            claim = json.loads(row["claim_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        statement = claim.get("normalized_statement") or claim.get("statement") or ""
        claims.append({"ref": row["id"], "statement": str(statement)[:400],
                       "aspect": claim.get("aspect")})
    return claims


def build_context(
    *,
    event: Mapping[str, Any],
    mission: Mapping[str, Any],
    connection: sqlite3.Connection,
    judgements: EventJudgementAuthority,
    model_version: Mapping[str, Any] | None = None,
    source_table: str | None = None,
    recent_events: Sequence[Mapping[str, Any]] = (),
    source_keys: Sequence[str] = (),
) -> dict[str, Any]:
    """Everything the one bounded call is allowed to see, and nothing else."""

    company_ref = event["company_ref"]
    ticker = next(
        (member.get("ticker") for member in mission["universe"]
         if member["company_ref"] == company_ref),
        None,
    )
    history = [
        {"kind": row.get("event_kind"), "decision": row["decision"],
         "action": row["action"], "because": row["because"][:400]}
        for row in judgements.recent(company_ref)
    ]
    return {
        "event": dict(event),
        "company_ref": company_ref,
        "ticker": ticker,
        "theses": company_theses(connection, company_ref),
        "drivers": model_drivers(model_version),
        "model_version_ref": None if not model_version else model_version.get("id"),
        "recent_judgements": history,
        "claims": recent_claims(connection, company_ref),
        "source_table": source_table,
        "market_view": market_view_rows(recent_events),
        "source_keys": list(source_keys),
    }


# ---------------------------------------------------------------------------
# effects: the only place an entry point is called
# ---------------------------------------------------------------------------


def publish_event_note(
    deliverables: Any,
    *,
    event: Mapping[str, Any],
    judgement: Mapping[str, Any],
    mission: Mapping[str, Any],
    playbook: Mapping[str, Any],
    actor_ref: str,
) -> dict[str, Any]:
    """The short note a ``note`` decision produces.

    Published through the deliverable authority the mission already grants, so
    the note inherits the rule that matters: a figure with no live Claim
    behind it is refused, and the drafter is expected to write the gap marker
    rather than a number it cannot source.
    """

    title = f"{event['kind']} {event['occurred_at'][:10]}"
    return deliverables.publish(
        kind="event_note",
        subject_ref=event["company_ref"],
        mission=mission,
        playbook=playbook,
        template_ref="template:event-note:p14a:v1",
        sections=[{
            "title": title,
            "body": judgement["note"],
            "claim_refs": [
                ref for ref in judgement["citations"] if ref.startswith("claim-version:")
            ],
            "numbers": [],
            "gaps": [],
        }],
        summary=f"{event['id']} -> {judgement['decision']} / {judgement['action']}: "
                f"{judgement['because'][:400]}",
        gaps=[],
        model_invocation_refs=[
            ref for ref in [(judgement.get("model") or {}).get("invocation_ref")] if ref
        ],
        actor_ref=actor_ref,
        idempotency_key=f"event-note:{event['id']}",
    )


def revision_for_event(
    forecast_models: Any, model_version: Mapping[str, Any], event_ref: str
) -> dict[str, Any] | None:
    """The version this event already published against this model, if any."""

    try:
        chain = forecast_models.versions(model_version["company_ref"])
    except Exception:  # noqa: BLE001 - an unreadable chain is "not found"
        return None
    for version in chain:
        if version.get("change_reason") != "driver_event":
            continue
        for ref in version.get("evidence_refs") or ():
            if isinstance(ref, Mapping) and ref.get("ref") == event_ref:
                return version
    return None


def _event_evidence_refs(event: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [{"kind": "event", "ref": event["id"], "concept": None,
             "period_end": None, "accession": None}]


def apply_effect(
    *,
    event: Mapping[str, Any],
    judgement: Mapping[str, Any],
    context: Mapping[str, Any],
    mission: Mapping[str, Any],
    playbook: Mapping[str, Any] | None,
    deliverables: Any | None,
    forecast_models: Any | None,
    model_version: Mapping[str, Any] | None,
    research_admitter: Callable[[Mapping[str, Any], Mapping[str, Any]], dict[str, Any]] | None,
    actor_ref: str,
) -> dict[str, Any]:
    """Do what the decision said, or say precisely why it could not be done.

    Nothing here decides anything.  Every branch either calls one existing
    entry point with the decision and its evidence, or returns a queued
    outcome naming the thing that is missing -- which is the honest answer
    while half the layers this will call are still being built.
    """

    action = judgement["action"]
    if action == "no_change":
        return {"kind": "no_change", "status": "recorded", "reason": judgement["because"][:400]}

    if action == "note":
        if deliverables is None or playbook is None:
            return {"kind": "note", "status": "queued",
                    "reason": "no deliverable authority on this run"}
        try:
            published = publish_event_note(
                deliverables, event=event, judgement=judgement, mission=mission,
                playbook=playbook, actor_ref=actor_ref,
            )
        except Exception as exc:  # noqa: BLE001 - a refused note is an outcome, not a crash
            return {"kind": "note", "status": "refused", "reason": f"{type(exc).__name__}: {exc}"}
        return {"kind": "note", "status": published.get("status", "fresh"),
                "deliverable_ref": published.get("deliverable_ref"),
                "deliverable_version_ref": published.get("id")}

    if action == "research":
        if research_admitter is None:
            return {"kind": "research", "status": "queued",
                    "reason": "the research task admission entry point is not installed on "
                              "this Core (P14e); the question is recorded on the judgement"}
        try:
            admitted = research_admitter(event, judgement)
        except Exception as exc:  # noqa: BLE001
            return {"kind": "research", "status": "queued",
                    "reason": f"{type(exc).__name__}: {exc}"}
        return {"kind": "research", **admitted}

    if action == "revise_forecast":
        change = judgement["forecast_change"]
        if forecast_models is None or model_version is None:
            return {"kind": "revise_forecast", "status": "queued",
                    "reason": "this company has no driver model to revise"}
        if "forecast_line" not in mission["autonomy"]["may_write"]:
            return {"kind": "revise_forecast", "status": "proposed",
                    "reason": "the mission does not grant forecast_line writes; "
                              "the revision is a proposal for the human checkpoint"}
        from .model_forecast_driver import ForecastModelError, revise_assumptions

        # Keyed on the event, because the judgement row is written after this
        # and a crash in between would otherwise publish a second version of
        # the same revision on the retry. The effect is idempotent instead of
        # the ordering being made safe, which is the cheaper of the two: the
        # note, the candidate and the proposal are already keyed this way.
        already = revision_for_event(forecast_models, model_version, event["id"])
        if already is not None:
            return {"kind": "revise_forecast", "status": "duplicate",
                    "model_version_ref": already["id"],
                    "change_reason": already.get("change_reason"),
                    "decision": already.get("decision"),
                    "reason": "this event has already revised this model"}
        try:
            body = revise_assumptions(
                model_version,
                [{
                    "driver": change["driver_ref"],
                    "period": change["period_end"],
                    "value": change["value"],
                    "because": change["because"],
                    "refs": _event_evidence_refs(event),
                }],
                change_reason="driver_event",
                evidence_refs=_event_evidence_refs(event),
                actor_ref=actor_ref,
                decision=judgement["decision"],
                mission_version_ref=mission["id"],
            )
            published = forecast_models.publish(body)
        except ForecastModelError as exc:
            return {"kind": "revise_forecast", "status": "refused",
                    "reason": f"{type(exc).__name__}: {exc}"}
        return {"kind": "revise_forecast", "status": published.get("status", "published"),
                "model_version_ref": published.get("id"),
                "change_reason": "driver_event", "decision": judgement["decision"]}

    if action == "revise_thesis":
        if "thesis_revision_candidate" not in mission["autonomy"]["may_write"]:
            return {"kind": "revise_thesis", "status": "queued",
                    "reason": "the mission does not grant thesis_revision_candidate; "
                              "ADR-0007 needs the mission version that grants it with "
                              "its matching checkpoint"}
        if "thesis_revision_candidate" not in mission["autonomy"]["human_checkpoints"]:
            return {"kind": "revise_thesis", "status": "queued",
                    "reason": "a mission that grants the scope must carry the checkpoint"}
        named = set(judgement["thesis_refs"])
        theses = [t for t in (context.get("theses") or ()) if t["ref"] in named]
        if not theses:
            return {"kind": "revise_thesis", "status": "refused",
                    "reason": "a thesis revision that names no thesis is not a revision"}
        return {"kind": "revise_thesis", "status": "candidate", "theses": theses}

    return {"kind": "revise_dossier", "status": "queued",
            "reason": "the company dossier is Wave 2; the decision is recorded and the "
                      "dossier lane will read it when it exists"}


__all__ = [
    "ACTION_VOCABULARY",
    "DECISION_ACTIONS",
    "EventJudgementAuthority",
    "EventJudgementConflict",
    "EventJudgementError",
    "EventJudgementValidationError",
    "MAX_NOTE_SENTENCES",
    "MAX_RECENT_JUDGEMENTS",
    "POOL_NAME",
    "POOL_SHARE",
    "PURPOSE",
    "REFLECTION_ACTIONS",
    "REFLECTION_PURPOSE",
    "REFLECTION_TRIGGER_KINDS",
    "SCHEMA_VERSION",
    "VERIFIER_FINDING_CODES",
    "VERIFIER_VERDICTS",
    "allowed_refs",
    "apply_effect",
    "build_context",
    "build_judge_prompt",
    "build_reflection_prompt",
    "build_reflection_verifier_prompt",
    "build_verifier_prompt",
    "company_theses",
    "judge",
    "market_view_rows",
    "model_drivers",
    "pool",
    "pool_state",
    "publish_event_note",
    "recent_claims",
    "reflect",
    "revision_for_event",
    "reflection_is_owed",
    "route_family_resolver",
    "validate_judge_output",
    "validate_reflection_output",
    "validate_verifier_output",
    "verify",
    "verify_reflection",
]
